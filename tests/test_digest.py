import asyncio
import re
from datetime import datetime, timedelta

import pytest
from conftest import mock_transport
from test_llm import FakeProvider

from signal_news.collector.pipeline import collect_async
from signal_news.config import WatchItem
from signal_news.llm.base import LLMUnavailable
from signal_news.storage.vault import read_document
from signal_news.summarize.article import summarize_pending
from signal_news.summarize.digest import RefBook, compute_window, generate_digest
from signal_news.timeutil import to_utc_iso


def _at(cfg, y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=cfg.tz)


def test_window_without_previous_digest_is_24h(ctx):
    now = _at(ctx.cfg, 2026, 11, 4)
    w = compute_window(ctx, now)
    assert w.end - w.start == timedelta(hours=24) and not w.capped


def test_window_since_previous_digest_and_weekend_gap(ctx):
    cfg = ctx.cfg
    fri = _at(cfg, 2026, 10, 30)  # Friday 11:00
    ctx.db.record_digest(
        "2026-10-30",
        "digests/2026-10-30",
        to_utc_iso(fri - timedelta(days=1)),
        to_utc_iso(fri),
        10,
        "m",
    )
    mon = _at(cfg, 2026, 11, 2)  # Monday 11:00 -> exactly 72 h, not capped
    w = compute_window(ctx, mon)
    assert w.start == fri and w.end == mon and not w.capped
    # Regenerating Monday's digest keeps the same window
    ctx.db.record_digest(
        "2026-11-02", "digests/2026-11-02", to_utc_iso(fri), to_utc_iso(mon), 5, "m"
    )
    w2 = compute_window(ctx, mon + timedelta(hours=2))
    assert w2.start == fri and not w2.capped  # 74 h but still a normal weekend


def test_window_is_capped_after_long_gap(ctx):
    cfg = ctx.cfg
    thu = _at(cfg, 2026, 10, 29)
    ctx.db.record_digest(
        "2026-10-29",
        "digests/2026-10-29",
        to_utc_iso(thu - timedelta(days=1)),
        to_utc_iso(thu),
        1,
        "m",
    )
    mon = _at(cfg, 2026, 11, 2)
    w = compute_window(ctx, mon)
    assert w.capped and w.end - w.start == timedelta(hours=72) and round(w.gap_hours) == 96


def test_refbook_drops_uncited_and_invalid_refs():
    from signal_news.summarize.cluster import Cluster

    book = RefBook()
    art = {
        "id": "a1",
        "path": "articles/x/a1",
        "url": "https://e.com/a",
        "source": "Src",
        "topic": "cyber",
    }
    n = book.number(Cluster(id="c1", articles=[art], rep=art))
    text = f"- **Good** fact [{n}]\n- **Uncited** claim\n- **Bad ref** [99]\n- **Mixed** [99][{n}]"
    out = book.render(text)
    assert "Uncited" not in out and "Bad ref" not in out
    assert out.count("[[articles/x/a1|Src]] [↗](https://e.com/a)") == 2
    assert "[99]" not in out


def _collect_and_summarize(ctx, feed_routes):
    ctx.cfg.watchlist = [WatchItem(ticker="NVDA", names=["Nvidia"])]
    asyncio.run(collect_async(ctx.cfg, ctx.db, ctx.vault, transport=mock_transport(feed_routes)))
    summarize_pending(ctx, provider=FakeProvider())


def _citing_reply(system, user):
    nums = re.findall(r"^\[(\d+)\]", user, re.M)
    if "five most important" in system:
        return "\n".join(f"{i}. **Lead {i}** Fact. [{n}]" for i, n in enumerate(nums[:5], 1))
    return (
        "\n".join(f"- **Story {n}** Something happened. [{n}]" for n in nums)
        + "\n- **Made up** no citation"
    )


def test_generate_digest_end_to_end(ctx, feed_routes):
    _collect_and_summarize(ctx, feed_routes)
    now = ctx.cfg.now() + timedelta(minutes=1)
    res = generate_digest(
        ctx, provider=FakeProvider(reply=_citing_reply, model="qwen3:14b", name="ollama"), now=now
    )
    meta, body = read_document(res.path)
    assert meta["date"] == now.strftime("%Y-%m-%d") and meta["articles"] == res.article_count > 0
    assert meta["model"] == "qwen3:14b (ollama)"
    assert "## 🔥 Today's top 5" in body and "## 🚨 Flags" in body
    assert "## 📈 Finance & Markets" in body and "### Watchlist" in body and "**NVDA**" in body
    assert "## 🛡️ Cybersecurity" in body and "### Vulnerabilities & Exploits" in body
    assert "## 🏠 Embedded, IoT & Homelab" in body
    assert "CVE-2026-12345" in body and "Actively exploited (KEV)" in body
    assert "Affects your gear" in body  # gear configured in tests
    assert "Made up" not in body  # uncited bullets are removed
    assert "[[articles/" in body and "[↗](https://" in body
    assert "Sources:" in body and "generated in" in body
    assert ctx.db.get_digest(meta["date"]) is not None


def test_digest_without_model_uses_headlines(ctx, feed_routes):
    _collect_and_summarize(ctx, feed_routes)
    res = generate_digest(ctx, use_llm=False, now=ctx.cfg.now() + timedelta(minutes=1))
    assert res.model.startswith("none")
    assert "A short factual summary." in res.markdown


def test_digest_postponed_when_model_down(ctx, feed_routes):
    _collect_and_summarize(ctx, feed_routes)
    with pytest.raises(LLMUnavailable):
        generate_digest(
            ctx, provider=FakeProvider(unavailable=True), now=ctx.cfg.now() + timedelta(minutes=1)
        )
    assert not list(ctx.vault.digests_dir.glob("*.md"))


def test_digest_section_falls_back_on_bad_output(ctx, feed_routes):
    _collect_and_summarize(ctx, feed_routes)
    res = generate_digest(
        ctx,
        provider=FakeProvider(reply="I cannot help with that."),
        now=ctx.cfg.now() + timedelta(minutes=1),
    )
    assert res.errors  # recorded
    assert "A short factual summary." in res.markdown  # fallback bullets present
