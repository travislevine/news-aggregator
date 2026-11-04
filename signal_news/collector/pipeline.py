"""The collection run: fetch every source, dedupe, extract, tag, cluster, store."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from signal_news.collector import special
from signal_news.collector.dedupe import (
    ClusterCandidate,
    article_id,
    assign_cluster,
    canonicalize_url,
    new_cluster_id,
    normalise_title,
)
from signal_news.collector.extract import RobotsCache, fetch_full_text, html_to_markdown, plain_text
from signal_news.collector.feeds import (
    FetchResult,
    PoliteClient,
    RawItem,
    discover_feeds,
    looks_like_html,
    newest_date,
    parse_feed,
)
from signal_news.collector.tagger import Tagger, build_tags, matches_keyword_filter
from signal_news.config import Config, Source, load_sources, update_source_entry
from signal_news.storage.db import Database
from signal_news.storage.vault import Vault
from signal_news.timeutil import to_local_iso, to_utc_iso

log = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


@dataclass
class SourceOutcome:
    source: str
    ok: bool
    status: int | None = None
    items: int = 0
    new: int = 0
    error: str | None = None
    newest: datetime | None = None
    feed_url: str | None = None
    not_modified: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectResult:
    started: datetime
    finished: datetime | None = None
    outcomes: list[SourceOutcome] = field(default_factory=list)
    new_articles: list[dict[str, Any]] = field(default_factory=list)
    new_kev: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    auto_disabled: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "sources": len(self.outcomes),
            "sources_ok": sum(1 for o in self.outcomes if o.ok),
            "sources_failed": sum(1 for o in self.outcomes if not o.ok),
            "items_seen": sum(o.items for o in self.outcomes),
            "new_articles": len(self.new_articles),
            "new_kev": len(self.new_kev),
            "affects_me": sum(1 for a in self.new_articles if a.get("affects_me")),
        }


@dataclass
class _Candidate:
    src: Source
    item: RawItem
    url: str
    canonical: str
    id: str
    feed_md: str
    resolved: bool = True
    body_md: str = ""
    full_text: bool = False


def all_sources(cfg: Config) -> list[Source]:
    """Sources from sources.yaml plus generated watchlist feeds."""
    return load_sources(cfg) + special.watchlist_sources(cfg)


async def _fetch_source(
    client: PoliteClient, db: Database, cfg: Config, src: Source
) -> tuple[SourceOutcome, list[RawItem], list[dict[str, Any]] | None]:
    """Fetch one source. Returns (outcome, items, kev_entries)."""
    url = special.source_url(src, cfg)
    state = db.get_feed_state(src.name) or {}
    headers: dict[str, str] = {}
    if state.get("url") == url and src.type != "json":
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
    res: FetchResult = await client.get(url, headers=headers or None)
    out = SourceOutcome(source=src.name, ok=False, status=res.status, feed_url=url)
    if res.not_modified:
        out.ok, out.not_modified = True, True
        return out, [], None
    if not res.ok:
        out.error = res.error or f"HTTP {res.status}"
        return out, [], None

    if src.type == "json":
        try:
            entries = special.parse_kev(res.content)
        except Exception as exc:  # noqa: BLE001
            out.error = f"invalid KEV JSON: {exc}"
            return out, [], None
        out.ok, out.items = True, len(entries)
        return out, [], entries

    content = res.content
    if looks_like_html(content, res.headers.get("content-type", "")):
        feeds = discover_feeds(content, res.final_url or url)
        if not feeds:
            out.error = "page is HTML and has no feed link"
            return out, [], None
        res2 = await client.get(feeds[0])
        if not res2.ok:
            out.error = f"autodiscovered feed {feeds[0]} failed: {res2.error}"
            return out, [], None
        content, out.feed_url = res2.content, feeds[0]
        res = res2

    items, err = parse_feed(content, src.name, res.final_url or url)
    if err:
        out.error = err
        return out, [], None
    out.ok, out.items, out.newest = True, len(items), newest_date(items)
    db_etag = res.headers.get("etag")
    db_lm = res.headers.get("last-modified")
    out.extra = {"etag": db_etag, "last_modified": db_lm}
    return out, items, None


def _filter_items(cfg: Config, src: Source, items: list[RawItem], now: datetime) -> list[RawItem]:
    cutoff = now - timedelta(days=cfg.collection.max_age_days)
    kept: list[RawItem] = []
    for it in items:
        if it.published and it.published < cutoff:
            continue
        if src.type == "hnrss":
            special.adapt_hnrss(it)
        if src.type == "google_news":
            it.title = special.strip_publisher_suffix(it.title, it.publisher)
        text = plain_text(it.best_html)
        if not matches_keyword_filter(src.keyword_filter, it.title, text):
            continue
        kept.append(it)
    return kept[: cfg.collection.max_items_per_feed]


async def _resolve(
    client: PoliteClient, db: Database, cfg: Config, it: RawItem
) -> tuple[str, bool]:
    """Return (url, resolved). Google News links are resolved to the publisher URL when possible."""
    if not special.is_google_news_link(it.url):
        return it.url, True
    if not cfg.collection.resolve_google_news:
        return it.url, False
    cached = db.cached_resolution(it.url)
    if cached is not None:
        return (cached, True) if cached else (it.url, False)
    try:
        real = await special.resolve_google_news(client, it.url)
    except Exception as exc:  # noqa: BLE001
        log.debug("Google News resolve failed: %s", exc)
        real = None
    db.cache_resolution(it.url, real or "")
    return (real, True) if real else (it.url, False)


def _auto_disable(cfg: Config, db: Database, src: Source, failures: int, error: str) -> bool:
    limit = cfg.collection.auto_disable_after_failures
    if src.generated or failures < limit:
        return False
    note = f"Auto-disabled {datetime.now(cfg.tz):%Y-%m-%d} after {failures} consecutive failures: {error[:120]}"
    try:
        update_source_entry(cfg, src.name, {"enabled": False, "notes": note})
    except Exception as exc:  # noqa: BLE001
        log.error("Could not update sources.yaml for %s: %s", src.name, exc)
    db.mark_auto_disabled(src.name)
    log.warning("Source %s auto-disabled after %d failures", src.name, failures)
    return True


async def collect_async(
    cfg: Config,
    db: Database,
    vault: Vault,
    *,
    only: list[str] | None = None,
    progress: ProgressFn | None = None,
    transport: Any = None,
) -> CollectResult:
    """Run one collection over all enabled sources (or just ``only``)."""
    say = progress or (lambda msg: None)
    now = datetime.now(UTC)
    result = CollectResult(started=now)
    sources = [s for s in all_sources(cfg) if s.enabled]
    if only:
        wanted = {o.lower() for o in only}
        sources = [s for s in sources if s.name.lower() in wanted]
    say(f"Fetching {len(sources)} sources")
    tagger = Tagger(cfg)

    async with PoliteClient(cfg, transport=transport) as client:
        fetched = await asyncio.gather(
            *(_fetch_source(client, db, cfg, s) for s in sources), return_exceptions=True
        )

        # -- source health + raw items -------------------------------------
        per_source_items: list[tuple[Source, list[RawItem]]] = []
        kev_entries: list[dict[str, Any]] | None = None
        outcome_by_name: dict[str, SourceOutcome] = {}
        for src, res in zip(sources, fetched, strict=True):
            if isinstance(res, BaseException):
                outcome = SourceOutcome(
                    source=src.name, ok=False, error=f"{type(res).__name__}: {res}"
                )
                items, kev = [], None
            else:
                outcome, items, kev = res
            outcome_by_name[src.name] = outcome
            result.outcomes.append(outcome)
            if kev is not None:
                kev_entries = kev
            if outcome.ok:
                per_source_items.append((src, _filter_items(cfg, src, items, now)))
            else:
                result.errors.append(f"{src.name}: {outcome.error}")

        # -- CISA KEV diff ---------------------------------------------------
        known_kev = db.kev_ids()
        kev_set = set(known_kev)
        if kev_entries is not None:
            new_entries = special.kev_diff(kev_entries, known_kev)
            first_load = not known_kev
            db.store_kev(kev_entries)
            kev_set |= {e["cveID"] for e in kev_entries}
            if first_load:
                # First run: store the whole catalog, but only surface recent additions.
                cutoff = (now - timedelta(days=cfg.collection.max_age_days)).date().isoformat()
                new_entries = [e for e in new_entries if (e.get("dateAdded") or "") >= cutoff]
            kev_src = next((s for s in sources if s.type == "json"), None)
            if kev_src is not None and new_entries:
                per_source_items.append((kev_src, [special.kev_to_item(e) for e in new_entries]))
                result.new_kev = new_entries

        # -- resolve + dedupe ------------------------------------------------
        say("Resolving links and removing duplicates")
        candidates: list[_Candidate] = []
        seen_canonical: set[str] = set()
        resolve_jobs = []
        flat: list[tuple[Source, RawItem]] = [
            (s, i) for s, items in per_source_items for i in items
        ]
        resolve_jobs = [_resolve(client, db, cfg, it) for _, it in flat]
        resolved = await asyncio.gather(*resolve_jobs, return_exceptions=True)
        for (src, it), res in zip(flat, resolved, strict=True):
            url, ok = (it.url, False) if isinstance(res, BaseException) else res
            canonical = canonicalize_url(url)
            if canonical in seen_canonical or db.has_canonical_url(canonical):
                continue
            seen_canonical.add(canonical)
            feed_md = html_to_markdown(it.best_html)
            candidates.append(
                _Candidate(
                    src=src,
                    item=it,
                    url=url,
                    canonical=canonical,
                    id=article_id(canonical),
                    feed_md=feed_md,
                    resolved=ok,
                    body_md=feed_md,
                )
            )

        # -- full text ---------------------------------------------------------
        robots = (
            RobotsCache(client, cfg.collection.user_agent)
            if cfg.collection.respect_robots_txt
            else None
        )
        need_full = [
            c
            for c in candidates
            if c.src.fetch_full_text
            and not c.src.paywalled
            and c.resolved
            and len(plain_text(c.feed_md)) < cfg.collection.full_text_min_chars
        ]
        for c in candidates:
            if (
                not c.src.paywalled
                and len(plain_text(c.feed_md)) >= cfg.collection.full_text_min_chars
            ):
                c.full_text = True
        if need_full:
            say(f"Fetching full text for {len(need_full)} articles")

        async def _full(c: _Candidate) -> None:
            md, err = await fetch_full_text(client, robots, c.url)
            if md:
                c.body_md, c.full_text = md, True
            elif err:
                log.debug("Full text unavailable for %s: %s", c.url, err)

        await asyncio.gather(*(_full(c) for c in need_full), return_exceptions=True)

    # -- tag, cluster, write ---------------------------------------------------
    say(f"Storing {len(candidates)} new articles")
    window_start = to_utc_iso(now - timedelta(hours=cfg.dedupe.window_hours))
    recent = [
        ClusterCandidate(id=r["id"], title_norm=r["title_norm"], cluster_id=r["cluster_id"])
        for r in db.recent_for_clustering(window_start)
    ]
    vectors = _embed_candidates(cfg, db, candidates, recent) if cfg.qa.embeddings_enabled else {}
    new_counts: dict[str, int] = {}
    for c in candidates:
        try:
            art = _store_candidate(cfg, db, vault, tagger, c, recent, kev_set, vectors.get(c.id))
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to store %s", c.url)
            result.errors.append(f"{c.src.name}: store failed for {c.url}: {exc}")
            continue
        result.new_articles.append(art)
        new_counts[c.src.name] = new_counts.get(c.src.name, 0) + 1
        recent.append(
            ClusterCandidate(
                id=art["id"],
                title_norm=art["title_norm"],
                cluster_id=art["cluster_id"],
                vector=vectors.get(c.id),
            )
        )

    # -- flag older articles that mention a newly added KEV CVE --------------------
    for entry in result.new_kev:
        _mark_existing_kev(db, vault, entry["cveID"])

    # -- record health -----------------------------------------------------------
    src_by_name = {s.name: s for s in sources}
    for o in result.outcomes:
        o.new = new_counts.get(o.source, 0)
        src = src_by_name[o.source]
        if o.ok:
            extra = o.extra
            db.record_feed_success(
                o.source,
                o.feed_url or src.url,
                status=o.status or 0,
                etag=extra.get("etag"),
                last_modified=extra.get("last_modified"),
                items=o.items,
                new=o.new,
                newest=to_utc_iso(o.newest) if o.newest else None,
            )
        else:
            n = db.record_feed_failure(
                o.source, o.feed_url or src.url, o.error or "error", o.status
            )
            if _auto_disable(cfg, db, src, n, o.error or "error"):
                result.auto_disabled.append(o.source)

    result.finished = datetime.now(UTC)
    return result


def _embed_candidates(
    cfg: Config, db: Database, candidates: list[_Candidate], recent: list[ClusterCandidate]
) -> dict[str, list[float]]:
    """Best-effort embeddings for same-story clustering. Returns {} if the model is unavailable."""
    import numpy as np

    from signal_news.llm.base import LLMError, get_provider

    if not candidates:
        return {}
    try:
        provider = get_provider(cfg, "embeddings")
        texts = [f"{c.item.title}\n{plain_text(c.feed_md)[:500]}" for c in candidates]
        vecs = provider.embed(texts)
    except (LLMError, ValueError) as exc:
        log.info("Embeddings skipped during collection: %s", exc)
        return {}
    out: dict[str, list[float]] = {}
    for c, v in zip(candidates, vecs, strict=False):
        out[c.id] = v
        arr = np.asarray(v, dtype=np.float32)
        db.store_embedding(c.id, provider.model, arr.tobytes(), int(arr.shape[0]))
    stored = {
        aid: np.frombuffer(b, dtype=np.float32).tolist()
        for aid, _, b in db.embeddings_for([r.id for r in recent])
    }
    for r in recent:
        r.vector = stored.get(r.id)
    return out


def _store_candidate(
    cfg: Config,
    db: Database,
    vault: Vault,
    tagger: Tagger,
    c: _Candidate,
    recent: list[ClusterCandidate],
    kev_set: set[str],
    vector: list[float] | None = None,
) -> dict[str, Any]:
    it, src = c.item, c.src
    collected = datetime.now(cfg.tz).replace(microsecond=0)
    text = plain_text(c.body_md)
    tags = tagger.tag(src.topic, it.title, text, src.extra_topics)
    kev_entry = it.extra.get("kev_entry")
    if kev_entry:
        for v in (kev_entry.get("vendorProject"),):
            if v and v not in tags.vendors:
                tags.vendors.append(v)
        if "exploited" not in tags.subtopics:
            tags.subtopics.insert(0, "exploited")
        if kev_entry["cveID"] not in tags.cves:
            tags.cves.insert(0, kev_entry["cveID"])
    is_kev = bool(kev_entry) or any(cve in kev_set for cve in tags.cves)

    title_norm = normalise_title(it.title)
    cluster, matched = assign_cluster(
        title_norm,
        recent,
        threshold=cfg.dedupe.title_similarity,
        min_shared=cfg.dedupe.min_shared_words,
        vector=vector,
        embedding_threshold=cfg.dedupe.embedding_similarity,
    )
    if matched and not cluster:
        cluster = new_cluster_id(matched)
        db.update_article(matched, cluster_id=cluster)
    cluster = cluster or new_cluster_id(c.id)

    source_name = src.name
    if src.type == "google_news" and it.publisher:
        source_name = it.publisher
    meta: dict[str, Any] = {
        "id": c.id,
        "title": it.title,
        "url": c.url,
        "canonical_url": c.canonical,
        "source": source_name,
        "topic": src.topic,
        "extra_topics": list(src.extra_topics),
        "subtopics": tags.subtopics,
        "published": to_local_iso(it.published, cfg.tz),
        "collected": collected.isoformat(),
        "author": it.author,
        "tickers": tags.tickers,
        "cves": tags.cves,
        "kev": is_kev,
        "vendors": tags.vendors,
        "affects_me": tags.affects_me,
        "cluster_id": cluster,
        "full_text": c.full_text and not src.paywalled,
        "paywalled": src.paywalled,
        "hn_url": it.extra.get("hn_url"),
        "summary": "",
        "tags": build_tags(src.topic, list(src.extra_topics), tags, is_kev),
    }
    if src.type == "google_news":
        meta["via"] = src.name
    if not meta["extra_topics"]:
        meta.pop("extra_topics")
    if not meta["hn_url"]:
        meta.pop("hn_url")
    if not meta["author"]:
        meta.pop("author")

    body = c.body_md if not src.paywalled else c.feed_md
    if not body.strip():
        body = "_No article text was available in the feed._"
    path = vault.article_path(c.id, it.title, src.topic, collected)
    vault.write_article(meta, body, path)

    row = {
        **meta,
        "path": vault.rel(path),
        "title_norm": title_norm,
        "collected": to_utc_iso(collected),
        "published": to_utc_iso(it.published) if it.published else None,
        "extra_topics": list(src.extra_topics),
        "kev_new": bool(kev_entry),
        "priority": src.priority,
        "summary": None,
    }
    db.upsert_article(row, body=plain_text(body)[:20000])
    return row


def _mark_existing_kev(db: Database, vault: Vault, cve: str) -> None:
    from signal_news.storage.vault import update_front_matter

    for art in db.query_articles(cve=cve, kev=False, limit=200):
        db.update_article(art["id"], kev=True)
        path = vault.abs(art["path"])
        if path.exists():
            tags = list(dict.fromkeys([*(_front_tags(path)), "kev"]))
            update_front_matter(path, {"kev": True, "tags": tags})


def _front_tags(path) -> list[str]:
    from signal_news.storage.vault import read_document

    meta, _ = read_document(path)
    return list(meta.get("tags") or [])


def collect(cfg: Config, db: Database, vault: Vault, **kwargs: Any) -> CollectResult:
    """Synchronous wrapper around :func:`collect_async` that records a run."""
    run_id = db.start_run("collect")
    t0 = time.monotonic()
    try:
        result = asyncio.run(collect_async(cfg, db, vault, **kwargs))
    except Exception as exc:
        db.finish_run(run_id, status="failed", errors=[f"{type(exc).__name__}: {exc}"])
        raise
    status = "ok" if not result.errors else "partial"
    db.finish_run(
        run_id,
        status=status,
        counts=result.counts,
        errors=result.errors,
        message=f"{len(result.new_articles)} new articles in {time.monotonic() - t0:.0f}s",
    )
    return result
