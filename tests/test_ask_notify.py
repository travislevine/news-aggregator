import asyncio
from datetime import datetime

from conftest import mock_transport
from test_llm import FakeProvider

from signal_news import notify
from signal_news.collector.pipeline import collect_async
from signal_news.config import WatchItem
from signal_news.summarize.article import summarize_pending
from signal_news.summarize.ask import answer_events, cite, parse_date_phrase, retrieve


def _now(cfg):
    return datetime(2026, 11, 4, 14, 30, tzinfo=cfg.tz)  # a Wednesday


def test_parse_date_phrases(cfg):
    now = _now(cfg)
    d = parse_date_phrase("What happened with Ivanti this week?", now)
    assert d.start == datetime(2026, 11, 2, tzinfo=cfg.tz) and d.label == "this week"
    d = parse_date_phrase("Summarise all NVDA news since Monday", now)
    assert d.start == datetime(2026, 11, 2, tzinfo=cfg.tz)
    d = parse_date_phrase("since wednesday", now)
    assert d.start == datetime(2026, 11, 4, tzinfo=cfg.tz)
    d = parse_date_phrase("anything in the last 3 days", now)
    assert d.start == datetime(2026, 11, 1, 14, 30, tzinfo=cfg.tz)
    d = parse_date_phrase("past two weeks", now)
    assert (now - d.start).days == 14
    d = parse_date_phrase("Any vulnerabilities in Synology gear this month?", now)
    assert d.start == datetime(2026, 11, 1, tzinfo=cfg.tz)
    d = parse_date_phrase("yesterday", now)
    assert d.start == datetime(2026, 11, 3, tzinfo=cfg.tz) and d.end == datetime(
        2026, 11, 4, tzinfo=cfg.tz
    )
    d = parse_date_phrase("last week", now)
    assert d.start == datetime(2026, 10, 26, tzinfo=cfg.tz) and d.end == datetime(
        2026, 11, 2, tzinfo=cfg.tz
    )
    assert parse_date_phrase("what is ransomware", now).start is None


def _load(ctx, feed_routes):
    ctx.cfg.watchlist = [WatchItem(ticker="NVDA", names=["Nvidia"])]
    asyncio.run(collect_async(ctx.cfg, ctx.db, ctx.vault, transport=mock_transport(feed_routes)))
    summarize_pending(ctx, provider=FakeProvider())


def test_retrieve_with_filters(ctx, feed_routes):
    _load(ctx, feed_routes)
    found = retrieve(ctx, "What happened with Ivanti this week?")
    assert found.dates.label == "this week"
    assert "Ivanti" in found.articles[0]["title"]
    nv = retrieve(ctx, "Summarise all Nvidia news today")
    assert nv.ticker == "NVDA" and any("Nvidia" in a["title"] for a in nv.articles)
    none = retrieve(ctx, "zebra migration patterns")
    assert none.articles == []


def test_answer_events_cites_and_saves(ctx, feed_routes):
    _load(ctx, feed_routes)
    events = list(
        answer_events(
            ctx, "Ivanti zero-day?", provider=FakeProvider(reply="It is exploited [1][9].")
        )
    )
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and "token" in kinds and kinds[-1] == "done"
    final = events[-1][1]
    assert "[[articles/" in final and "**Sources**" in final and "[9]" not in final
    assert list(ctx.vault.qa_dir.glob("*.md"))


def test_answer_when_nothing_found(ctx):
    events = list(answer_events(ctx, "anything about zebras?", provider=FakeProvider()))
    assert events[-1][0] == "done" and "couldn't find" in events[-1][1]


def test_cite_ignores_out_of_range():
    arts = [{"path": "articles/a", "url": "https://e/a", "source": "S", "title": "T"}]
    out = cite("Claim [1]. Other [5].", arts)
    assert "[[articles/a|S]]" in out and "[5]" not in out


def test_notify_message_formatting():
    md = "1. **Lead** fact ([[articles/x/y|BleepingComputer]] [↗](https://bc.com/a))"
    assert (
        notify.to_message_markdown(md) == "1. **Lead** fact ([BleepingComputer](https://bc.com/a))"
    )
    long = "\n".join(f"- line {i}" for i in range(500))
    out = notify.truncate(long, 200, "\n\nlink")
    assert len(out) <= 200 and out.endswith("link") and "…" in out


def test_notify_urls_from_env(cfg, monkeypatch):
    cfg.notifications.urls = ["ntfy://topic-a"]
    monkeypatch.setenv("APPRISE_URLS", "ntfy://topic-a, discord://id/token")
    assert notify.notification_urls(cfg) == ["ntfy://topic-a", "discord://id/token"]


def test_send_test_without_targets(cfg, monkeypatch):
    monkeypatch.delenv("APPRISE_URLS", raising=False)
    cfg.notifications.urls = []
    ok, msg = notify.send_test(cfg)
    assert not ok and "No notification URLs" in msg


def test_send_digest_splits_chat_and_email(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(
        notify,
        "_send",
        lambda urls, title, body, fmt="markdown": sent.append((urls, fmt, body)) or (True, "ok"),
    )
    monkeypatch.delenv("APPRISE_URLS", raising=False)
    cfg.notifications.urls = ["ntfy://t", "mailtos://user:pw@example.com"]

    class R:
        date = "2026-11-04"
        top5 = "1. **A** x ([[articles/a|S]] [↗](https://e/a))"
        flags = "- **Actively exploited (KEV):** nothing new"
        markdown = "---\ndate: 2026-11-04\n---\n# Daily Digest\n\n" + top5

    ok, _ = notify.send_digest(cfg, R())
    assert ok and len(sent) == 2
    chat, email = sent
    assert (
        chat[0] == ["ntfy://t"] and "Today's top 5" in chat[2] and "/digests/2026-11-04" in chat[2]
    )
    assert len(chat[2]) <= cfg.notifications.max_message_chars
    assert email[1] == "html" and "<h1>" in email[2]


def test_instant_alerts(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(
        notify,
        "_send",
        lambda urls, title, body, fmt="markdown": sent.append((title, body)) or (True, "ok"),
    )
    cfg.notifications.urls = ["ntfy://t"]
    notify.send_instant_alerts(
        cfg,
        [
            {
                "cveID": "CVE-2026-1",
                "vendorProject": "Ivanti",
                "product": "X",
                "vulnerabilityName": "Bug",
                "requiredAction": "Patch",
            }
        ],
        [],
    )
    assert sent and "CVE-2026-1" in sent[0][1] and "actively exploited" in sent[0][0]
    sent.clear()
    notify.send_instant_alerts(cfg, [], [])
    assert not sent


def test_answer_lists_articles_when_model_down(ctx, feed_routes):
    _load(ctx, feed_routes)
    events = list(answer_events(ctx, "Ivanti zero-day?", provider=FakeProvider(unavailable=True)))
    assert events[-1][0] == "done"
    assert "isn't available" in events[-1][1] and "[[articles/" in events[-1][1]
