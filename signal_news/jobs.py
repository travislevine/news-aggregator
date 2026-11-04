"""Job runners shared by the CLI, the scheduler and the web UI.

Each job takes a named lock so overlapping runs (for example the scheduler and a
CLI command at the same time) can't happen, records progress for the UI, and
never raises: failures are logged and stored in the run history.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from signal_news.context import AppContext
from signal_news.storage.db import LockError

log = logging.getLogger(__name__)


@dataclass
class JobState:
    """Live progress of the current/last job, shown in the UI."""

    name: str = ""
    running: bool = False
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    ok: bool | None = None
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self.running,
            "message": self.message,
            "ok": self.ok,
            "elapsed": round((self.finished or time.time()) - self.started) if self.started else 0,
            "log": self.log[-8:],
        }


STATE = JobState()
_state_lock = threading.Lock()


def _begin(name: str) -> Callable[[str], None]:
    with _state_lock:
        STATE.name, STATE.running, STATE.message = name, True, "starting"
        STATE.started, STATE.finished, STATE.ok = time.time(), 0.0, None
        STATE.log = []

    def progress(msg: str) -> None:
        log.info("[%s] %s", name, msg)
        with _state_lock:
            STATE.message = msg
            STATE.log.append(msg)

    return progress


def _end(ok: bool, msg: str) -> None:
    with _state_lock:
        STATE.running, STATE.ok, STATE.message, STATE.finished = False, ok, msg, time.time()
        STATE.log.append(msg)


def _chain(*fns: Callable[[str], None] | None) -> Callable[[str], None]:
    def call(msg: str) -> None:
        for f in fns:
            if f:
                f(msg)

    return call


# ---------------------------------------------------------------------------- collect


def run_collect(
    ctx: AppContext, *, only: list[str] | None = None, progress: Callable[[str], None] | None = None
):
    """Collect all sources. Returns a CollectResult, or None if locked/failed."""
    from signal_news.collector.pipeline import collect

    say = _chain(_begin("collect"), progress)
    try:
        with ctx.db.lock("collect", ttl_minutes=30):
            res = collect(ctx.cfg, ctx.db, ctx.vault, only=only, progress=say)
    except LockError as exc:
        _end(False, str(exc))
        log.warning("Collect skipped: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("Collect failed")
        _end(False, f"Collect failed: {exc}")
        return None
    _end(True, f"Collected {len(res.new_articles)} new articles")
    _instant_alerts(ctx, res)
    return res


def _instant_alerts(ctx: AppContext, res: Any) -> None:
    if not (ctx.cfg.notifications.enabled and ctx.cfg.notifications.instant_alerts):
        return
    try:
        from signal_news.notify import send_instant_alerts

        send_instant_alerts(
            ctx.cfg, res.new_kev, [a for a in res.new_articles if a.get("affects_me")]
        )
    except Exception:  # noqa: BLE001
        log.exception("Instant alerts failed")


# -------------------------------------------------------------------------- summarize


def run_summarize(
    ctx: AppContext, *, limit: int | None = None, progress: Callable[[str], None] | None = None
):
    """Summarise pending articles. Returns a SummarizeResult, or None if locked/failed."""
    from signal_news.summarize.article import summarize_pending

    say = _chain(_begin("summarize"), progress)
    run_id = ctx.db.start_run("summarize")
    try:
        with ctx.db.lock("summarize", ttl_minutes=120):
            res = summarize_pending(ctx, limit=limit, progress=say)
    except LockError as exc:
        ctx.db.finish_run(run_id, status="skipped", message=str(exc))
        _end(False, str(exc))
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("Summarize failed")
        ctx.db.finish_run(run_id, status="failed", errors=[str(exc)])
        _end(False, f"Summarize failed: {exc}")
        return None
    status = "ok" if not res.failed and not res.unreachable else "partial"
    ctx.db.finish_run(
        run_id,
        status=status,
        counts={"summarised": res.done, "failed": res.failed, "remaining": res.remaining},
        errors=res.errors,
        message="model unreachable" if res.unreachable else None,
    )
    _end(
        not res.unreachable,
        f"Summarised {res.done}, failed {res.failed}, remaining {res.remaining}",
    )
    if ctx.cfg.qa.embeddings_enabled:
        try:
            from signal_news.summarize.ask import embed_pending

            embed_pending(ctx, limit=200)
        except Exception:  # noqa: BLE001
            log.exception("Embedding failed")
    return res


# ----------------------------------------------------------------------------- digest


def digest_due(ctx: AppContext, now: datetime | None = None) -> bool:
    """True if it's past today's digest time and today's digest doesn't exist yet."""
    now = now or ctx.cfg.now()
    hh, mm = ctx.cfg.schedule.digest_hour_minute
    if (now.hour, now.minute) < (hh, mm):
        return False
    return ctx.db.get_digest(now.strftime("%Y-%m-%d")) is None


def run_digest(
    ctx: AppContext,
    *,
    force: bool = False,
    use_llm: bool = True,
    notify: bool = True,
    progress: Callable[[str], None] | None = None,
):
    """Generate today's digest. Returns a DigestResult, or None if skipped/failed."""
    from signal_news.summarize.digest import LLMUnavailable, generate_digest

    say = _chain(_begin("digest"), progress)
    run_id = ctx.db.start_run("digest")
    today = ctx.cfg.now().strftime("%Y-%m-%d")
    if not force and ctx.db.get_digest(today):
        msg = f"Digest for {today} already exists (use force to regenerate)"
        ctx.db.finish_run(run_id, status="skipped", message=msg)
        _end(True, msg)
        return None
    try:
        with ctx.db.lock("digest", ttl_minutes=90):
            # Catch up on summaries first so the digest has them.
            if use_llm:
                from signal_news.summarize.article import summarize_pending

                say("Summarising pending articles first")
                summarize_pending(ctx, progress=say)
            res = generate_digest(ctx, use_llm=use_llm, progress=say)
    except LockError as exc:
        ctx.db.finish_run(run_id, status="skipped", message=str(exc))
        _end(False, str(exc))
        return None
    except LLMUnavailable as exc:
        log.warning("Digest postponed: %s", exc)
        ctx.db.finish_run(
            run_id, status="postponed", errors=[str(exc)], message="model unreachable; will retry"
        )
        _end(False, f"Model unreachable, digest postponed: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("Digest failed")
        ctx.db.finish_run(run_id, status="failed", errors=[str(exc)])
        _end(False, f"Digest failed: {exc}")
        return None
    ctx.db.finish_run(
        run_id,
        status="ok",
        counts={"articles": res.article_count, "clusters": res.cluster_count},
        errors=res.errors,
        message=f"{res.path.name} in {res.seconds:.0f}s",
    )
    _end(True, f"Digest ready: {res.path.name}")
    if notify and ctx.cfg.notifications.enabled and ctx.cfg.notifications.send_digest:
        try:
            from signal_news.notify import send_digest

            send_digest(ctx.cfg, res)
        except Exception:  # noqa: BLE001
            log.exception("Digest notification failed")
    return res


# ---------------------------------------------------------------------------- cleanup


def run_cleanup(ctx: AppContext) -> int:
    """Delete articles older than ``retention.cleanup_after_days`` that never appeared in a digest."""
    days = ctx.cfg.retention.cleanup_after_days
    if not days:
        return 0
    from signal_news.timeutil import to_utc_iso

    cutoff = to_utc_iso(ctx.cfg.now() - timedelta(days=days))
    removed = 0
    with ctx.db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM articles WHERE collected < ? AND in_digest IS NULL", (cutoff,)
        ).fetchall()
    for r in rows:
        path = ctx.vault.abs(r["path"])
        try:
            if path.exists():
                path.unlink()
            ctx.db.delete_article(r["id"])
            removed += 1
        except OSError as exc:
            log.error("Could not delete %s: %s", path, exc)
    if removed:
        log.info("Retention cleanup removed %d articles", removed)
    return removed
