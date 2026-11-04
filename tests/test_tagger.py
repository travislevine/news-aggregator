from signal_news.collector.tagger import Tagger, build_tags, matches_keyword_filter
from signal_news.config import WatchItem


def make_tagger(cfg, watchlist=None, gear=None):
    cfg.watchlist = watchlist or [
        WatchItem(ticker="NVDA", names=["Nvidia"]),
        WatchItem(ticker="AAPL", names=["Apple"]),
        WatchItem(ticker="ON", names=["onsemi", "ON Semiconductor"]),
        WatchItem(ticker="ALL", names=["Allstate"]),
        WatchItem(ticker="BHP.AX", names=["BHP Group"]),
    ]
    cfg.gear = gear if gear is not None else ["Synology", "Home Assistant"]
    return Tagger(cfg)


def test_ticker_forms(cfg):
    t = make_tagger(cfg)
    assert t.tag("finance", "NVDA jumps", "").tickers == ["NVDA"]
    assert t.tag("finance", "Chip stocks", "Shares of $NVDA rose").tickers == ["NVDA"]
    assert t.tag("finance", "x", "Nvidia (NASDAQ: NVDA) said").tickers == ["NVDA"]
    assert t.tag("finance", "Nvidia beats estimates", "").tickers == ["NVDA"]
    assert "BHP.AX" in t.tag("finance", "BHP.AX falls", "").tickers
    assert "BHP.AX" in t.tag("finance", "Miner slides", "BHP Group said iron ore").tickers


def test_short_and_common_word_tickers_no_false_positives(cfg):
    t = make_tagger(cfg)
    text = "Turn ON the lights. IT teams agree ALL systems are go. A new day."
    assert t.tag("finance", "Markets ON edge", text).tickers == []
    # ...but explicit forms still match.
    assert t.tag("finance", "x", "Shares of $ON rallied").tickers == ["ON"]
    assert t.tag("finance", "x", "Allstate (NYSE: ALL) raised guidance").tickers == ["ALL"]
    assert t.tag("finance", "x", "onsemi reported").tickers == ["ON"]


def test_lowercase_ticker_word_does_not_match(cfg):
    t = make_tagger(cfg)
    assert (
        t.tag("finance", "nvda is lowercase", "").tickers == []
    )  # bare tickers are case-sensitive
    assert t.tag("finance", "Mapple pie recipe", "").tickers == []


def test_cves_and_vendors(cfg):
    t = make_tagger(cfg)
    tags = t.tag(
        "cyber",
        "Ivanti flaw CVE-2026-12345 exploited",
        "Also cve-2025-0001 and CVE-2026-12345 again.",
    )
    assert tags.cves == ["CVE-2026-12345", "CVE-2025-0001"]
    assert "Ivanti" in tags.vendors
    assert "exploited" in tags.subtopics and "vulnerability" in tags.subtopics
    assert not t.tag("cyber", "CVE-26-1", "").cves


def test_gear_sets_affects_me(cfg):
    t = make_tagger(cfg)
    assert t.tag("iot", "Synology NAS flaw", "").affects_me
    assert t.tag("iot", "New home assistant release", "").affects_me
    assert not t.tag("iot", "Raspberry Pi 6 announced", "").affects_me
    t2 = make_tagger(cfg, gear=[])
    assert not t2.tag("iot", "Synology NAS flaw", "").affects_me


def test_keyword_filter():
    kws = ["hack", "exploit", "drained"]
    assert matches_keyword_filter(kws, "DeFi protocol hacked for $10M", "")
    assert matches_keyword_filter(kws, "Bridge", "Funds were drained overnight")
    assert not matches_keyword_filter(kws, "Bitcoin price rises", "ETF inflows")
    assert matches_keyword_filter([], "anything", "")


def test_build_tags(cfg):
    t = make_tagger(cfg)
    tags = t.tag("cyber", "Synology CVE-2026-1111 exploited", "")
    out = build_tags("cyber", [], tags, kev=True)
    assert "news/cyber" in out and "kev" in out and "cve" in out and "affects-me" in out
