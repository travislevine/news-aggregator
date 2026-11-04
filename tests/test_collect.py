import asyncio

from conftest import mock_transport

from signal_news.collector.pipeline import collect_async
from signal_news.collector.validate import apply_results, validate_async
from signal_news.config import load_sources
from signal_news.storage.reindex import reindex
from signal_news.storage.vault import read_document


def run(ctx, routes, **kw):
    return asyncio.run(
        collect_async(ctx.cfg, ctx.db, ctx.vault, transport=mock_transport(routes), **kw)
    )


def test_collect_end_to_end(ctx, feed_routes):
    res = run(ctx, feed_routes)
    c = res.counts
    assert c["sources"] == 6 and c["sources_failed"] == 1  # Dead Feed
    titles = {a["title"] for a in res.new_articles}
    assert "Ransomware gang claims attack on hospital chain" in titles
    assert "Wall Street ends higher as Nvidia rallies" in titles
    # KEV: first load stores catalog, surfaces only recent entries (max_age_days is huge in tests)
    assert {e["cveID"] for e in res.new_kev} >= {"CVE-2026-12345", "CVE-2026-99999"}
    assert ctx.db.kev_count() == 3

    by_title = {a["title"]: a for a in res.new_articles}
    ivanti = by_title["Ivanti zero-day exploited in the wild (CVE-2026-12345)"]
    assert ivanti["kev"] is True and ivanti["cves"] == ["CVE-2026-12345"]
    assert ivanti["canonical_url"] == "https://example-security.com/news/ivanti-zero-day"
    assert ivanti["affects_me"] is True  # mentions Synology (gear)

    nvda = by_title["Nvidia shares climb after record data center revenue"]
    assert nvda["tickers"] == ["NVDA"]

    # Google News: decoded offline -> real Reuters URL; opaque link kept + snippet
    wall = by_title["Wall Street ends higher as Nvidia rallies"]
    assert wall["url"].startswith("https://www.reuters.com/") and wall["source"] == "Reuters"
    rba = by_title["RBA holds cash rate steady at 3.6%"]
    assert rba["url"].startswith("https://news.google.com/")

    # Full text fetched for the short ransomware item (feed text < 800 chars)
    hosp = by_title["Ransomware gang claims attack on hospital chain"]
    assert hosp["full_text"] is True
    body = ctx.vault.article_body(hosp["path"])
    assert "elective procedures" in body

    # HN item keeps discussion URL
    hn = by_title["Critical OpenSSH vulnerability allows remote root"]
    assert hn["hn_url"] == "https://news.ycombinator.com/item?id=41000001"

    # Vault file has the expected front matter
    meta, body = read_document(ctx.vault.abs(ivanti["path"]))
    assert meta["topic"] == "cyber" and meta["kev"] is True and meta["source"] == "Example Security"
    assert "<script>" not in body and "Connect Secure" in body

    # Source health recorded
    states = ctx.db.all_feed_states()
    assert states["Example Security"]["etag"] == '"abc"'
    assert states["Dead Feed"]["consecutive_failures"] == 1


def test_collect_is_idempotent(ctx, feed_routes):
    first = run(ctx, feed_routes)
    n = ctx.db.article_count()
    second = run(ctx, feed_routes)
    assert len(first.new_articles) > 0
    assert second.new_articles == [] and second.new_kev == []
    assert ctx.db.article_count() == n


def test_conditional_get_304(ctx, feed_routes):
    run(ctx, feed_routes)
    routes = dict(feed_routes)
    routes["https://www.example-security.com/feed/"] = 304
    res = run(ctx, routes, only=["Example Security"])
    assert res.outcomes[0].ok and res.outcomes[0].not_modified


def test_auto_disable_after_failures(ctx, feed_routes):
    ctx.cfg.collection.auto_disable_after_failures = 2
    run(ctx, feed_routes, only=["Dead Feed"])
    res = run(ctx, feed_routes, only=["Dead Feed"])
    assert res.auto_disabled == ["Dead Feed"]
    dead = [s for s in load_sources(ctx.cfg) if s.name == "Dead Feed"][0]
    assert dead.enabled is False and "Auto-disabled" in dead.notes


def test_reindex_rebuilds_from_vault(ctx, feed_routes):
    run(ctx, feed_routes)
    n = ctx.db.article_count()
    ctx.db.clear_articles()
    assert ctx.db.article_count() == 0
    res = reindex(ctx.db, ctx.vault)
    assert res.articles == n and res.errors == 0
    assert ctx.db.query_articles(search="ransomware")


def test_validate_sources_repairs_and_disables(ctx, feed_routes):
    routes = dict(feed_routes)
    routes["https://dead.example.com/feed"] = (
        200,
        b"<html><head></head><body>moved</body></html>",
        {"content-type": "text/html"},
    )
    routes["https://dead.example.com/"] = (
        200,
        b'<html><head><link rel="alternate" type="application/rss+xml" href="/new-feed.xml"></head></html>',
        {"content-type": "text/html"},
    )
    routes["https://dead.example.com/new-feed.xml"] = (
        200,
        open_fixture("atom_sample.xml"),
        {"content-type": "application/atom+xml"},
    )
    results = asyncio.run(validate_async(ctx.cfg, transport=mock_transport(routes)))
    by = {r.source.name: r for r in results}
    assert by["Example Security"].ok and by["Example Security"].items == 3
    assert (
        by["Dead Feed"].action == "url updated"
        and by["Dead Feed"].new_url == "https://dead.example.com/new-feed.xml"
    )
    apply_results(ctx.cfg, results)
    dead = [s for s in load_sources(ctx.cfg) if s.name == "Dead Feed"][0]
    assert dead.url == "https://dead.example.com/new-feed.xml"


def test_validate_disables_when_nothing_works(ctx, feed_routes):
    routes = dict(feed_routes)
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    routes["https://news.google.com/rss/search"] = (
        200,
        empty,
        {"content-type": "application/rss+xml"},
    )
    results = asyncio.run(validate_async(ctx.cfg, ["Dead Feed"], transport=mock_transport(routes)))
    assert results[0].action == "disabled"
    apply_results(ctx.cfg, results)
    dead = [s for s in load_sources(ctx.cfg) if s.name == "Dead Feed"][0]
    assert dead.enabled is False and "no working alternative" in dead.notes


def open_fixture(name):
    from conftest import fixture_bytes

    return fixture_bytes(name)
