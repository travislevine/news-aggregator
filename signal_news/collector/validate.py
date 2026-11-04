"""``signal validate-sources``: check every feed, repair or disable dead ones, report."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from signal_news.collector import special
from signal_news.collector.feeds import (
    PoliteClient,
    discover_feeds,
    looks_like_html,
    newest_date,
    parse_feed,
)
from signal_news.collector.pipeline import all_sources
from signal_news.config import Config, Source, update_source_entry

log = logging.getLogger(__name__)


@dataclass
class Validation:
    source: Source
    url: str
    status: int | None
    ok: bool
    items: int = 0
    newest: datetime | None = None
    error: str | None = None
    action: str = ""  # what was done: "", "url updated", "switched to Google News", "disabled"
    new_url: str | None = None
    new_query: str | None = None


async def _probe(
    client: PoliteClient, url: str, src_type: str
) -> tuple[int | None, int, datetime | None, str | None, bytes]:
    """Fetch and parse a feed URL. Returns (status, items, newest, error, raw_content)."""
    res = await client.get(url)
    if not res.ok:
        return res.status, 0, None, res.error or f"HTTP {res.status}", res.content
    if src_type == "json":
        try:
            entries = special.parse_kev(res.content)
        except Exception as exc:  # noqa: BLE001
            return res.status, 0, None, f"invalid JSON: {exc}", res.content
        dates = sorted(e.get("dateAdded", "") for e in entries)
        newest = (
            datetime.fromisoformat(dates[-1]).replace(tzinfo=UTC) if dates and dates[-1] else None
        )
        return res.status, len(entries), newest, None if entries else "no entries", res.content
    if looks_like_html(res.content, res.headers.get("content-type", "")):
        return res.status, 0, None, "HTML page, not a feed", res.content
    items, err = parse_feed(res.content, "probe", res.final_url or url)
    if err:
        return res.status, 0, None, err, res.content
    if not items:
        return res.status, 0, None, "feed has no items", res.content
    return res.status, len(items), newest_date(items), None, res.content


async def _validate_one(client: PoliteClient, cfg: Config, src: Source) -> Validation:
    url = special.source_url(src, cfg)
    status, n, newest, err, content = await _probe(client, url, src.type)
    v = Validation(
        source=src, url=url, status=status, ok=err is None, items=n, newest=newest, error=err
    )
    if v.ok or src.type in ("json", "google_news"):
        return v

    # 1) explicit fallbacks, 2) autodiscovery on the page itself and on the site root.
    tried: list[str] = list(src.fallback_urls)
    if content and looks_like_html(content):
        tried += discover_feeds(content, url)
    parts = urlsplit(src.url)
    root = f"{parts.scheme}://{parts.netloc}/"
    if root != src.url:
        res = await client.get(root)
        if res.ok:
            tried += discover_feeds(res.content, res.final_url or root)
    seen: set[str] = {url}
    for cand in tried:
        if cand in seen:
            continue
        seen.add(cand)
        s2, n2, newest2, err2, _ = await _probe(client, cand, src.type)
        if err2 is None:
            v.ok, v.items, v.newest, v.status, v.new_url = True, n2, newest2, s2, cand
            v.action = "url updated"
            return v

    # 3) Google News site: query.
    domain = (parts.hostname or "").removeprefix("www.")
    if domain:
        query = f"site:{domain} when:7d"
        gurl = special.google_news_url(query, cfg.collection.google_news)
        s3, n3, newest3, err3, _ = await _probe(client, gurl, "rss")
        if err3 is None and n3 > 0:
            v.ok, v.items, v.newest, v.status, v.new_query = True, n3, newest3, s3, query
            v.action = "switched to Google News"
            return v

    v.action = "disabled"
    return v


async def validate_async(
    cfg: Config, names: list[str] | None = None, transport: Any = None
) -> list[Validation]:
    sources = all_sources(cfg)
    if names:
        wanted = {n.lower() for n in names}
        sources = [s for s in sources if s.name.lower() in wanted]
    async with PoliteClient(cfg, transport=transport) as client:
        return list(await asyncio.gather(*(_validate_one(client, cfg, s) for s in sources)))


def apply_results(cfg: Config, results: list[Validation]) -> None:
    """Write repairs/disables back into sources.yaml. Generated (watchlist) sources are not stored."""
    today = datetime.now(cfg.tz).strftime("%Y-%m-%d")
    for v in results:
        src = v.source
        if src.generated or not v.action:
            continue
        if v.action == "url updated" and v.new_url:
            note = f"{src.notes} [validate {today}: feed moved from {src.url}]".strip()
            update_source_entry(cfg, src.name, {"url": v.new_url, "notes": note})
        elif v.action == "switched to Google News" and v.new_query:
            note = f"{src.notes} [validate {today}: direct feed dead ({v.error}); using Google News site: query]".strip()
            update_source_entry(
                cfg,
                src.name,
                {
                    "type": "google_news",
                    "query": v.new_query,
                    "notes": note,
                    "fetch_full_text": True,
                },
            )
        elif v.action == "disabled" and src.enabled:
            note = f"{src.notes} [validate {today}: disabled, {v.error}; no working alternative found]".strip()
            update_source_entry(cfg, src.name, {"enabled": False, "notes": note})


def report_markdown(cfg: Config, results: list[Validation]) -> str:
    """A Markdown table of the validation results."""
    now = datetime.now(cfg.tz)
    lines = [
        f"# Source validation, {now:%Y-%m-%d %H:%M}",
        "",
        f"{sum(r.ok for r in results)} of {len(results)} sources working.",
        "",
        "| Source | Topic | Status | Items | Newest item | Result |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r.ok, r.source.topic, r.source.name)):
        newest = r.newest.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M") if r.newest else "-"
        result = (
            "OK"
            if r.ok and not r.action
            else (r.action or "") + (f" ({r.error})" if r.error else "")
        )
        if r.new_url:
            result += f" -> {r.new_url}"
        if r.new_query:
            result += f" -> `{r.new_query}`"
        lines.append(
            f"| {r.source.name} | {r.source.topic} | {r.status or '-'} | {r.items} | {newest} | {result.strip()} |"
        )
    return "\n".join(lines) + "\n"
