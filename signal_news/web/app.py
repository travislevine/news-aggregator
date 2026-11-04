"""FastAPI + Jinja2 + HTMX web UI."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from signal_news.config import Source, add_source_entry, update_config_file, update_source_entry
from signal_news.context import AppContext
from signal_news.jobs import STATE
from signal_news.timeutil import local, parse_iso, to_utc_iso
from signal_news.web.render import render_markdown, split_sections

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
JOBS = ("article_summary", "digest", "ask", "embeddings")


# ----------------------------------------------------------------------------- security


class GuardMiddleware(BaseHTTPMiddleware):
    """Password (when exposed beyond localhost), Host check and CSRF protection."""

    def __init__(self, app: Any, *, password: str | None, loopback_only: bool):
        super().__init__(app)
        self.password = password
        self.loopback_only = loopback_only

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if self.loopback_only and host not in LOOPBACK and host != "testserver":
            return PlainTextResponse("Invalid host", status_code=400)  # DNS-rebinding guard
        if self.password and not self._authorised(request):
            return Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Signal"'},
            )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Cross-site form posts can't set custom headers; HTMX always sends HX-Request.
            origin = request.headers.get("origin")
            same_origin = origin is None or origin.split("://", 1)[-1] == request.headers.get(
                "host"
            )
            if not request.headers.get("hx-request") or not same_origin:
                return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)

    def _authorised(self, request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(pw.encode(), (self.password or "").encode())


# ------------------------------------------------------------------------------ helpers


def _fmt_dt(value: Any, tz: Any, fmt: str = "%a %d %b %H:%M") -> str:
    d = local(value, tz) if value else None
    return d.strftime(fmt) if d else "-"


def _ago(value: Any) -> str:
    d = parse_iso(value)
    if not d:
        return "never"
    secs = (datetime.now(d.tzinfo) - d).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400 * 2:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


def create_app(
    ctx: AppContext, *, start_scheduler: bool = True, host: str = "127.0.0.1"
) -> FastAPI:
    """Build the FastAPI app. Starts the scheduler on startup unless disabled."""
    loopback = host in LOOPBACK
    password = os.environ.get("SIGNAL_WEB_PASSWORD", "").strip() or None
    if not loopback and not password:
        raise RuntimeError(
            f"Refusing to listen on {host} without a password. Set SIGNAL_WEB_PASSWORD in .env, "
            "or keep web.host at 127.0.0.1."
        )
    sched_holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            from signal_news.scheduler import SignalScheduler

            sched = SignalScheduler(ctx)
            sched.start()
            sched_holder["s"] = sched
        yield
        if "s" in sched_holder:
            sched_holder["s"].shutdown()

    app = FastAPI(
        title="Signal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(GuardMiddleware, password=password, loopback_only=loopback)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters["dt"] = lambda v, fmt="%a %d %b %H:%M": _fmt_dt(v, ctx.cfg.tz, fmt)
    env.filters["ago"] = _ago
    env.filters["md"] = lambda text: render_markdown(text or "", ctx.db)
    env.globals["topic_label"] = lambda t: ctx.cfg.topic_label(t)

    def page(request: Request, name: str, **data: Any) -> HTMLResponse:
        data.setdefault("cfg", ctx.cfg)
        data.setdefault("job", STATE.as_dict())
        return templates.TemplateResponse(request, name, data)

    def trigger(kind: str, **kwargs: Any) -> bool:
        s = sched_holder.get("s")
        if s is not None:
            return s.trigger(kind, **kwargs)
        from signal_news.scheduler import run_background_job

        return run_background_job(ctx, kind, **kwargs)

    # ---------------------------------------------------------------- today

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        digests = ctx.db.list_digests()
        latest = digests[0] if digests else None
        sections, flags = [], None
        if latest:
            path = ctx.vault.abs(latest["path"])
            if path.exists():
                for heading, body in split_sections(path.read_text(encoding="utf-8")):
                    if "Flags" in heading:
                        flags = (heading, body)
                    else:
                        sections.append((heading, body))
        since = to_utc_iso(ctx.cfg.now() - timedelta(hours=24))
        stats = {
            "articles_24h": len(
                ctx.db.articles_between(since, to_utc_iso(ctx.cfg.now() + timedelta(minutes=1)))
            ),
            "total": ctx.db.article_count(),
            "pending": ctx.db.count_needing_summary(ctx.cfg.summarize.max_attempts),
            "last_collect": next(
                (r for r in ctx.db.recent_runs(20) if r["kind"] == "collect"), None
            ),
        }
        nxt = sched_holder["s"].next_runs() if "s" in sched_holder else {}
        is_today = bool(latest and latest["date"] == ctx.cfg.now().strftime("%Y-%m-%d"))
        return page(
            request,
            "today.html",
            active="today",
            latest=latest,
            sections=sections,
            flags=flags,
            stats=stats,
            next_runs=nxt,
            is_today=is_today,
        )

    @app.post("/actions/{kind}", response_class=HTMLResponse)
    def action(request: Request, kind: str) -> HTMLResponse:
        if kind not in ("collect", "summarize", "digest"):
            raise HTTPException(404)
        started = trigger(kind)
        job = STATE.as_dict()
        if not started and not job["running"]:
            job["message"] = "Could not start the job"
        return page(request, "partials/job_status.html", job=job)

    @app.get("/actions/status", response_class=HTMLResponse)
    def action_status(request: Request) -> HTMLResponse:
        resp = page(request, "partials/job_status.html", job=STATE.as_dict())
        if not STATE.running and STATE.finished and request.headers.get("hx-request"):
            resp.headers["HX-Trigger"] = "job-finished"
        return resp

    # -------------------------------------------------------------- digests

    @app.get("/digests", response_class=HTMLResponse)
    def digests(request: Request) -> HTMLResponse:
        return page(request, "digests.html", active="digests", digests=ctx.db.list_digests())

    @app.get("/digests/{day}", response_class=HTMLResponse)
    def digest_view(request: Request, day: str) -> HTMLResponse:
        d = ctx.db.get_digest(day)
        path = ctx.vault.abs(d["path"]) if d else ctx.vault.digest_path(day)
        if not path.exists():
            raise HTTPException(404, "Digest not found")
        return page(
            request,
            "digest.html",
            active="digests",
            day=day,
            digest=d,
            content=path.read_text(encoding="utf-8"),
        )

    # ------------------------------------------------------------- articles

    def _article_filters(request: Request) -> dict[str, Any]:
        q = request.query_params
        f: dict[str, Any] = {}
        for k in ("topic", "source", "ticker", "cve"):
            if q.get(k):
                f[k] = q[k].strip()
        if q.get("kev"):
            f["kev"] = True
        if q.get("affects_me"):
            f["affects_me"] = True
        tz = ctx.cfg.tz
        if q.get("date_from"):
            f["date_from"] = to_utc_iso(datetime.fromisoformat(q["date_from"]).replace(tzinfo=tz))
        if q.get("date_to"):
            f["date_to"] = to_utc_iso(
                datetime.fromisoformat(q["date_to"]).replace(tzinfo=tz) + timedelta(days=1)
            )
        if q.get("q"):
            f["search"] = q["q"]
        return f

    @app.get("/articles", response_class=HTMLResponse)
    def articles(request: Request) -> HTMLResponse:
        f = _article_filters(request)
        offset = int(request.query_params.get("offset", 0) or 0)
        rows = ctx.db.query_articles(**f, limit=50, offset=offset)
        tmpl = (
            "partials/articles_table.html" if request.headers.get("hx-request") else "articles.html"
        )

        def page_url(new_offset: int) -> str:
            params = {k: v for k, v in request.query_params.items() if k != "offset" and v}
            params["offset"] = str(max(0, new_offset))
            return "/articles?" + urlencode(params)

        return page(
            request,
            tmpl,
            active="articles",
            rows=rows,
            q=request.query_params,
            offset=offset,
            sources=ctx.db.distinct_sources(),
            topics=list(ctx.cfg.topics),
            page_url=page_url,
        )

    @app.get("/articles/{article_id}", response_class=HTMLResponse)
    def article_view(request: Request, article_id: str) -> HTMLResponse:
        art = ctx.db.get_article(article_id)
        if not art:
            raise HTTPException(404, "Article not found")
        body = ctx.vault.article_body(art["path"])
        related = (
            ctx.db.articles_in_cluster(art["cluster_id"], exclude=art["id"])
            if art.get("cluster_id")
            else []
        )
        return page(request, "article.html", active="articles", art=art, body=body, related=related)

    @app.get("/vault/{rel:path}")
    def vault_link(rel: str) -> Response:
        art = ctx.db.get_article_by_path(rel)
        if art:
            return RedirectResponse(f"/articles/{art['id']}")
        raise HTTPException(404)

    # ------------------------------------------------------------------ ask

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request) -> HTMLResponse:
        return page(request, "ask.html", active="ask", topics=list(ctx.cfg.topics))

    @app.get("/ask/stream")
    async def ask_stream(request: Request) -> StreamingResponse:
        from signal_news.summarize.ask import answer_events

        question = (request.query_params.get("q") or "").strip()[:500]
        topic = request.query_params.get("topic") or None
        if not question:
            raise HTTPException(400, "Empty question")

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                for event, data in answer_events(ctx, question, topic=topic):
                    if event == "done":
                        data = json.dumps({"html": str(render_markdown(data, ctx.db))})
                    loop.call_soon_threadsafe(queue.put_nowait, (event, data))
            except Exception as exc:  # noqa: BLE001
                log.exception("Ask failed")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, produce)

        async def gen():
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                payload = "\n".join(f"data: {line}" for line in (data.split("\n") or [""]))
                yield f"event: {event}\n{payload}\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    # -------------------------------------------------------------- sources

    def _source_rows() -> list[dict[str, Any]]:
        from signal_news.collector.pipeline import all_sources

        states = ctx.db.all_feed_states()
        rows = []
        for s in all_sources(ctx.cfg):
            st = states.get(s.name) or {}
            failures = st.get("consecutive_failures") or 0
            health = "unknown"
            if st.get("last_success") and not failures:
                health = "ok"
            elif failures:
                health = "failing"
            if not s.enabled:
                health = "disabled"
            rows.append({"src": s, "state": st, "health": health})
        return rows

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request) -> HTMLResponse:
        return page(
            request,
            "sources.html",
            active="sources",
            rows=_source_rows(),
            runs=ctx.db.recent_runs(25),
            topics=list(ctx.cfg.topics),
        )

    @app.post("/sources/toggle", response_class=HTMLResponse)
    def source_toggle(
        request: Request, name: str = Form(...), enabled: str = Form("")
    ) -> HTMLResponse:
        on = enabled in ("1", "true", "on")
        if not update_source_entry(ctx.cfg, name, {"enabled": on}):
            raise HTTPException(
                404, "Source not found (generated watchlist sources can't be toggled)"
            )
        if on:
            ctx.db.clear_auto_disabled(name)
        row = next(r for r in _source_rows() if r["src"].name == name)
        return page(request, "partials/source_row.html", r=row)

    @app.post("/sources/test", response_class=HTMLResponse)
    def source_test(request: Request, name: str = Form(...)) -> HTMLResponse:
        from signal_news.collector.pipeline import all_sources
        from signal_news.collector.validate import validate_async

        src = next((s for s in all_sources(ctx.cfg) if s.name == name), None)
        if not src:
            raise HTTPException(404)
        result = asyncio.run(validate_async(ctx.cfg, [name]))[0]
        return page(request, "partials/source_test.html", r=result)

    @app.post("/sources/add", response_class=HTMLResponse)
    def source_add(
        request: Request,
        name: str = Form(...),
        url: str = Form(""),
        query: str = Form(""),
        topic: str = Form(...),
        type: str = Form("rss"),
        fetch_full_text: str = Form(""),
        paywalled: str = Form(""),
        priority: int = Form(2),
    ) -> HTMLResponse:
        from signal_news.collector.feeds import (
            PoliteClient,
            discover_feeds,
            looks_like_html,
            parse_feed,
        )

        name = name.strip()
        msg, ok = "", False
        try:
            if type == "google_news":
                if not query.strip():
                    raise ValueError("A Google News source needs a search query")
                src = Source(
                    name=name,
                    topic=topic,
                    type="google_news",
                    query=query.strip(),
                    fetch_full_text=bool(fetch_full_text),
                    paywalled=bool(paywalled),
                    priority=priority,
                )
            else:
                feed_url = url.strip()

                async def probe() -> tuple[str, int]:
                    async with PoliteClient(ctx.cfg) as client:
                        res = await client.get(feed_url)
                        if not res.ok:
                            raise ValueError(f"Could not fetch {feed_url}: {res.error}")
                        target, content = feed_url, res.content
                        if looks_like_html(content, res.headers.get("content-type", "")):
                            feeds = discover_feeds(content, res.final_url or feed_url)
                            if not feeds:
                                raise ValueError("That page has no RSS/Atom feed link")
                            target = feeds[0]
                            res2 = await client.get(target)
                            if not res2.ok:
                                raise ValueError(f"Found feed {target} but it failed: {res2.error}")
                            content = res2.content
                        items, err = parse_feed(content, name, target)
                        if err:
                            raise ValueError(f"Not a valid feed: {err}")
                        return target, len(items)

                feed_url, n = asyncio.run(probe())
                src = Source(
                    name=name,
                    url=feed_url,
                    topic=topic,
                    type="rss",
                    fetch_full_text=bool(fetch_full_text),
                    paywalled=bool(paywalled),
                    priority=priority,
                    notes=f"Added from the UI ({n} items when added)",
                )
            add_source_entry(ctx.cfg, src)
            ok, msg = True, f"Added {name}" + (f" ({src.url})" if src.url else "")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
        resp = page(request, "partials/flash.html", ok=ok, msg=msg)
        if ok:
            resp.headers["HX-Refresh"] = "true"
        return resp

    # ------------------------------------------------------------- settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        from signal_news.llm.base import job_settings

        jobs = {j: job_settings(ctx.cfg, j) for j in JOBS}
        watch_text = "\n".join(
            f"{w.ticker}: {', '.join(w.names)}" if w.names else w.ticker for w in ctx.cfg.watchlist
        )
        env_urls = bool(os.environ.get(ctx.cfg.notifications.urls_env, "").strip())
        return page(
            request,
            "settings.html",
            active="settings",
            jobs=jobs,
            providers=list(ctx.cfg.llm.providers),
            watch_text=watch_text,
            gear_text="\n".join(ctx.cfg.gear),
            env_urls=env_urls,
        )

    def _saved(request: Request, msg: str) -> HTMLResponse:
        ctx.reload_config()
        if "s" in sched_holder:
            try:
                sched_holder["s"].reschedule()
            except Exception:  # noqa: BLE001
                log.exception("Reschedule failed")
        return page(request, "partials/flash.html", ok=True, msg=msg)

    @app.post("/settings/watchlist", response_class=HTMLResponse)
    def save_watchlist(
        request: Request, watchlist: str = Form(""), gear: str = Form("")
    ) -> HTMLResponse:
        items = []
        for line in watchlist.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ticker, _, names = line.partition(":")
            entry: dict[str, Any] = {"ticker": ticker.strip().upper()}
            names_list = [n.strip() for n in names.split(",") if n.strip()]
            entry["names"] = names_list
            items.append(entry)
        gear_list = [g.strip() for g in gear.splitlines() if g.strip()]
        update_config_file(ctx.cfg, {"watchlist": items, "gear": gear_list})
        return _saved(request, f"Saved {len(items)} tickers and {len(gear_list)} gear items")

    @app.post("/settings/schedule", response_class=HTMLResponse)
    def save_schedule(
        request: Request, collect_interval_minutes: int = Form(...), digest_time: str = Form(...)
    ) -> HTMLResponse:
        try:
            datetime.strptime(digest_time, "%H:%M")
        except ValueError:
            return page(request, "partials/flash.html", ok=False, msg="Digest time must be HH:MM")
        interval = max(10, min(24 * 60, collect_interval_minutes))
        update_config_file(
            ctx.cfg,
            {
                "schedule.collect_interval_minutes": interval,
                "schedule.digest_time": digest_time,
            },
        )
        return _saved(
            request, f"Schedule saved: collect every {interval} min, digest at {digest_time}"
        )

    @app.post("/settings/llm", response_class=HTMLResponse)
    async def save_llm(request: Request) -> HTMLResponse:
        form = await request.form()
        updates: dict[str, Any] = {}
        for job in JOBS:
            prov = str(form.get(f"{job}_provider") or "").strip()
            model = str(form.get(f"{job}_model") or "").strip()
            if prov:
                updates[f"llm.jobs.{job}.provider"] = prov
            if model:
                updates[f"llm.jobs.{job}.model"] = model
        updates["qa.embeddings_enabled"] = bool(form.get("embeddings_enabled"))
        update_config_file(ctx.cfg, updates)
        return _saved(request, "Model settings saved")

    @app.post("/settings/test-llm", response_class=HTMLResponse)
    def test_llm(request: Request, job: str = Form(...)) -> HTMLResponse:
        from signal_news.llm.base import LLMError, get_provider

        try:
            ok, msg = get_provider(ctx.cfg, job).check()
        except LLMError as exc:
            ok, msg = False, str(exc)
        return page(request, "partials/flash.html", ok=ok, msg=msg)

    @app.post("/settings/notifications", response_class=HTMLResponse)
    def save_notifications(
        request: Request,
        urls: str = Form(""),
        instant_alerts: str = Form(""),
        send_digest: str = Form(""),
        ui_base_url: str = Form(""),
    ) -> HTMLResponse:
        url_list = [u.strip() for u in urls.splitlines() if u.strip()]
        updates: dict[str, Any] = {
            "notifications.urls": url_list,
            "notifications.instant_alerts": bool(instant_alerts),
            "notifications.send_digest": bool(send_digest),
        }
        if ui_base_url.strip():
            updates["notifications.ui_base_url"] = ui_base_url.strip()
        update_config_file(ctx.cfg, updates)
        return _saved(request, "Notification settings saved")

    @app.post("/settings/test-notify", response_class=HTMLResponse)
    def test_notify(request: Request) -> HTMLResponse:
        from signal_news.notify import send_test

        ok, msg = send_test(ctx.cfg)
        return page(request, "partials/flash.html", ok=ok, msg=msg)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "articles": ctx.db.article_count(), "job": STATE.as_dict()}

    return app
