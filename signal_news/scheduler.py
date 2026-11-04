"""In-process scheduler (APScheduler).

Designed for a PC that is only on part of the day (e.g. 10:00-18:00):

* On start: catch-up collection immediately; if today's digest is overdue, build it
  as soon as collection (and summaries) finish.
* While running: collect every ``collect_interval_minutes``, summarising new
  articles after each run, and re-check whether the digest is due.
* At ``digest_time``: build the digest (window = since the previous digest).
* If the model is down, the digest is retried after each collection; after
  ``digest_fallback_after_hours`` a headline-only digest is sent instead.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from signal_news.context import AppContext
from signal_news.jobs import STATE, digest_due, run_cleanup, run_collect, run_digest, run_summarize

log = logging.getLogger(__name__)


class SignalScheduler:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._chain_lock = threading.Lock()
        self.scheduler = BackgroundScheduler(
            timezone=ctx.cfg.tz,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        cfg = self.ctx.cfg
        self.ctx.db.fail_stale_runs()
        now = cfg.now()
        if cfg.schedule.catch_up_on_start:
            self.scheduler.add_job(
                self.pipeline,
                "date",
                run_date=now + timedelta(seconds=3),
                args=["startup catch-up"],
                id="catch_up",
                replace_existing=True,
            )
        self.scheduler.add_job(
            self.pipeline,
            IntervalTrigger(minutes=cfg.schedule.collect_interval_minutes, timezone=cfg.tz),
            args=["scheduled collection"],
            id="collect",
            replace_existing=True,
        )
        hh, mm = cfg.schedule.digest_hour_minute
        self.scheduler.add_job(
            self.digest_if_due,
            CronTrigger(hour=hh, minute=mm, timezone=cfg.tz),
            args=["daily digest time"],
            id="digest",
            replace_existing=True,
        )
        self.scheduler.start()
        log.info(
            "Scheduler started: collect every %d min, digest at %s (%s)",
            cfg.schedule.collect_interval_minutes,
            cfg.schedule.digest_time,
            cfg.timezone,
        )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reschedule(self) -> None:
        """Apply schedule changes from the Settings page."""
        cfg = self.ctx.cfg
        self.scheduler.reschedule_job(
            "collect",
            trigger=IntervalTrigger(minutes=cfg.schedule.collect_interval_minutes, timezone=cfg.tz),
        )
        hh, mm = cfg.schedule.digest_hour_minute
        self.scheduler.reschedule_job(
            "digest", trigger=CronTrigger(hour=hh, minute=mm, timezone=cfg.tz)
        )

    # ---------------------------------------------------------------------- jobs

    def pipeline(self, reason: str = "manual") -> None:
        """Collect, summarise new articles, then build the digest if it's due."""
        if not self._chain_lock.acquire(blocking=False):
            log.info("Pipeline already running; skipping (%s)", reason)
            return
        try:
            log.info("Pipeline start: %s", reason)
            run_collect(self.ctx)
            if self.ctx.cfg.schedule.summarize_after_collect:
                run_summarize(self.ctx)
            self._digest_if_due_locked(reason)
        except Exception:  # noqa: BLE001
            log.exception("Pipeline failed")
        finally:
            self._chain_lock.release()

    def digest_if_due(self, reason: str = "check") -> None:
        if not self._chain_lock.acquire(blocking=False):
            # The collection chain is running and will check the digest when it finishes.
            log.info("Digest check deferred: pipeline busy")
            return
        try:
            self._digest_if_due_locked(reason)
        finally:
            self._chain_lock.release()

    def _digest_if_due_locked(self, reason: str) -> None:
        if not digest_due(self.ctx):
            return
        log.info("Digest is due (%s)", reason)
        res = run_digest(self.ctx)
        if res is None and digest_due(self.ctx) and self._past_fallback_time():
            log.warning("Model still unavailable; sending a headline-only digest")
            res = run_digest(self.ctx, use_llm=False)
        if res is not None and self.ctx.cfg.retention.cleanup_after_days:
            run_cleanup(self.ctx)

    def _past_fallback_time(self, now: datetime | None = None) -> bool:
        cfg = self.ctx.cfg
        now = now or cfg.now()
        hh, mm = cfg.schedule.digest_hour_minute
        due_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return now >= due_at + timedelta(hours=cfg.schedule.digest_fallback_after_hours)

    # ------------------------------------------------------------------- manual

    def trigger(self, kind: str, **kwargs: Any) -> bool:
        """Start a job in the background from the UI. Returns False if one is already running."""
        if STATE.running:
            return False
        targets = {
            "collect": lambda: run_collect(self.ctx),
            "summarize": lambda: run_summarize(self.ctx),
            "digest": lambda: run_digest(self.ctx, force=True, **kwargs),
            "pipeline": lambda: self.pipeline("manual"),
        }
        fn = targets.get(kind)
        if fn is None:
            return False
        threading.Thread(target=fn, name=f"signal-{kind}", daemon=True).start()
        return True

    def next_runs(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for job in self.scheduler.get_jobs():
            nrt = job.next_run_time
            out[job.id] = (
                nrt.astimezone(self.ctx.cfg.tz).strftime("%a %d %b %H:%M") if nrt else None
            )
        return out


def run_background_job(ctx: AppContext, kind: str, **kwargs: Any) -> bool:
    """Start a job thread without a scheduler (used when the UI runs with --no-scheduler)."""
    if STATE.running:
        return False
    targets = {
        "collect": lambda: run_collect(ctx),
        "summarize": lambda: run_summarize(ctx),
        "digest": lambda: run_digest(ctx, force=True, **kwargs),
    }
    fn = targets.get(kind)
    if fn is None:
        return False
    threading.Thread(target=fn, name=f"signal-{kind}", daemon=True).start()
    return True
