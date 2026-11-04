from conftest import fixture_bytes

from signal_news.collector.extract import extract_article, html_to_markdown
from signal_news.collector.feeds import discover_feeds, looks_like_html, parse_feed
from signal_news.collector.special import (
    adapt_hnrss,
    decode_google_news_offline,
    google_news_url,
    kev_diff,
    kev_to_item,
    parse_kev,
    strip_publisher_suffix,
    watchlist_sources,
)
from signal_news.config import GoogleNewsLocale, WatchItem


def test_parse_rss():
    items, err = parse_feed(fixture_bytes("rss_sample.xml"), "Example")
    assert err is None and len(items) == 3
    first = items[0]
    assert first.title.startswith("Ivanti zero-day")
    assert first.author == "Jane Doe"
    assert first.published.isoformat() == "2026-11-03T16:15:00+00:00"
    assert "Connect Secure" in first.content_html


def test_parse_atom():
    items, err = parse_feed(fixture_bytes("atom_sample.xml"), "HA")
    assert err is None and len(items) == 2
    assert items[0].url == "https://www.home-assistant.io/blog/2026/11/03/release-202611/"
    assert "Matter" in items[0].best_html


def test_parse_rejects_html():
    assert looks_like_html(fixture_bytes("article.html"))
    items, err = parse_feed(fixture_bytes("article.html"), "x")
    assert items == [] and err


def test_discover_feeds_prefers_non_comment_feed():
    feeds = discover_feeds(fixture_bytes("article.html"), "https://www.example-security.com/news/x")
    assert feeds[0] == "https://www.example-security.com/feed/"
    assert discover_feeds(fixture_bytes("page_with_feed.html"), "https://rekt.example/") == [
        "https://rekt.example/rss/feed.xml"
    ]


def test_kev_parse_and_diff():
    entries = parse_kev(fixture_bytes("kev_sample.json"))
    assert len(entries) == 3
    new = kev_diff(entries, {"CVE-2021-44228"})
    assert [e["cveID"] for e in new] == ["CVE-2026-99999", "CVE-2026-12345"]  # oldest first
    item = kev_to_item(new[1])
    assert "CVE-2026-12345" in item.title and "Ivanti" in item.title
    assert item.url == "https://nvd.nist.gov/vuln/detail/CVE-2026-12345"
    assert "Required action" in item.content_html


def test_google_news_url_and_decode():
    url = google_news_url("crypto (hack OR exploit) when:1d", GoogleNewsLocale())
    assert url.startswith(
        "https://news.google.com/rss/search?q=crypto+%28hack+OR+exploit%29+when%3A1d"
    )
    assert "ceid=US%3Aen" in url
    items, _ = parse_feed(fixture_bytes("google_news.xml"), "GN")
    assert items[0].publisher == "Reuters"
    assert (
        strip_publisher_suffix(items[0].title, items[0].publisher)
        == "Wall Street ends higher as Nvidia rallies"
    )
    assert (
        decode_google_news_offline(items[0].url)
        == "https://www.reuters.com/markets/us/wall-street-ends-higher-nvidia-2026-11-03/"
    )
    assert decode_google_news_offline(items[1].url) is None


def test_hnrss_adapter():
    items, _ = parse_feed(fixture_bytes("hnrss.xml"), "HN")
    it = adapt_hnrss(items[0])
    assert it.url == "https://blog.example.org/openssh-vuln"
    assert it.extra["hn_url"] == "https://news.ycombinator.com/item?id=41000001"
    assert it.summary_html == ""


def test_watchlist_sources(cfg):
    cfg.watchlist = [
        WatchItem(ticker="NVDA", names=["Nvidia"]),
        WatchItem(ticker="BHP.AX", names=["BHP"]),
    ]
    cfg.watchlist_feeds.yahoo = True
    cfg.watchlist_feeds.google_news = True
    srcs = watchlist_sources(cfg)
    names = [s.name for s in srcs]
    assert names == [
        "Yahoo Finance: NVDA",
        "Google News: NVDA",
        "Yahoo Finance: BHP.AX",
        "Google News: BHP.AX",
    ]
    assert "s=NVDA" in srcs[0].url and srcs[1].query == '"Nvidia" OR NVDA stock when:1d'
    assert srcs[3].locale.gl == "AU"


def test_extraction_and_markdown():
    md = extract_article(fixture_bytes("article.html").decode(), "https://example.com/a")
    assert md and "Akira ransomware group" in md
    assert "Copyright Example" not in md
    assert html_to_markdown("<p>Hi <b>there</b></p><script>x()</script>") == "Hi **there**"
