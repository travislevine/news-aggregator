from datetime import datetime, timedelta

from signal_news import jobs, scheduler
from signal_news.storage.db import LockError
from signal_news.timeutil import to_utc_iso


def _at(cfg, hh, mm=0):
    return datetime(2026, 11, 4, hh, mm, tzinfo=cfg.tz)


def test_digest_due_logic(ctx):
    cfg = ctx.cfg
    assert not jobs.digest_due(ctx, _at(cfg, 10, 30))  # before 11:00
    assert jobs.digest_due(ctx, _at(cfg, 11, 0))
    assert jobs.digest_due(ctx, _at(cfg, 16, 45))  # PC switched on late: still due
    ctx.db.record_digest(
        "2026-11-04",
        "digests/2026-11-04",
        to_utc_iso(_at(cfg, 0)),
        to_utc_iso(_at(cfg, 11)),
        1,
        "m",
    )
    assert not jobs.digest_due(ctx, _at(cfg, 16, 45))


def test_run_lock_prevents_overlap(ctx):
    with ctx.db.lock("collect"):
        try:
            with ctx.db.lock("collect"):
                raise AssertionError("second lock should fail")
        except LockError:
            pass
    with ctx.db.lock("collect"):  # released afterwards
        pass


def test_stale_lock_expires(ctx):
    with ctx.db.connect() as conn:
        conn.execute(
            "INSERT INTO locks (name, owner, acquired_at, expires_at) VALUES ('collect','dead','2020-01-01','2020-01-01')"
        )
    with ctx.db.lock("collect"):
        pass


def test_pipeline_runs_collect_summarize_then_digest(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "run_collect", lambda c: calls.append("collect"))
    monkeypatch.setattr(scheduler, "run_summarize", lambda c: calls.append("summarize"))
    monkeypatch.setattr(scheduler, "digest_due", lambda c: "digest" not in calls)
    monkeypatch.setattr(scheduler, "run_digest", lambda c, **kw: calls.append("digest") or object())
    s = scheduler.SignalScheduler(ctx)
    s.pipeline("test")
    assert calls == ["collect", "summarize", "digest"]


def test_digest_falls_back_to_headlines_when_model_down_late(ctx, monkeypatch):
    calls = []

    def fake_digest(c, **kw):
        calls.append(kw.get("use_llm", True))
        return None if kw.get("use_llm", True) else object()

    monkeypatch.setattr(scheduler, "digest_due", lambda c: len(calls) < 2)
    monkeypatch.setattr(scheduler, "run_digest", fake_digest)
    s = scheduler.SignalScheduler(ctx)
    monkeypatch.setattr(s, "_past_fallback_time", lambda now=None: True)
    s.digest_if_due("test")
    assert calls == [True, False]

    calls.clear()
    monkeypatch.setattr(s, "_past_fallback_time", lambda now=None: False)
    s.digest_if_due("test")
    assert calls == [True]  # early: just postpone, retry after the next collection


def test_past_fallback_time(ctx):
    s = scheduler.SignalScheduler(ctx)
    cfg = ctx.cfg
    assert not s._past_fallback_time(_at(cfg, 14, 59))
    assert s._past_fallback_time(_at(cfg, 15, 0))


def test_scheduler_starts_with_expected_jobs(ctx):
    s = scheduler.SignalScheduler(ctx)
    s.start()
    try:
        s.scheduler.pause()
        ids = {j.id for j in s.scheduler.get_jobs()}
        assert {"catch_up", "collect", "digest"} <= ids
        digest_job = s.scheduler.get_job("digest")
        nrt = digest_job.next_run_time.astimezone(ctx.cfg.tz)
        assert (nrt.hour, nrt.minute) == (11, 0)
        collect_job = s.scheduler.get_job("collect")
        assert collect_job.trigger.interval == timedelta(minutes=60)
    finally:
        s.shutdown()


def test_scheduled_digest_skips_empty_window(ctx):
    res = jobs.run_digest(ctx, use_llm=False, notify=False)
    assert res is None and "No new articles" in jobs.STATE.message
    assert not list(ctx.vault.digests_dir.glob("*.md"))
    forced = jobs.run_digest(ctx, use_llm=False, notify=False, force=True)
    assert forced is not None and forced.path.exists()
