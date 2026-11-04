"""Fetch and parse RSS/Atom/JSON feeds politely (async httpx)."""

from __future__ import annotations

import asyncio
import calendar
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from signal_news.config import Config

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status: int | None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    final_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass
class RawItem:
    """One entry from a feed, before dedupe/extraction."""

    title: str
    url: str
    source: str
    content_html: str = ""
    summary_html: str = ""
    published: datetime | None = None
    author: str | None = None
    comments_url: str | None = None
    publisher: str | None = None  # Google News: the original outlet name
    publisher_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def best_html(self) -> str:
        return (
            self.content_html
            if len(self.content_html) >= len(self.summary_html)
            else self.summary_html
        )


class PoliteClient:
    """httpx.AsyncClient wrapper: per-domain rate limit, global concurrency cap, retries."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        c = cfg.collection
        self.client = httpx.AsyncClient(
            transport=transport,
            headers={
                "User-Agent": c.user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                "text/xml;q=0.9, application/json;q=0.8, text/html;q=0.7, */*;q=0.5",
            },
            timeout=httpx.Timeout(c.timeout_seconds),
            follow_redirects=True,
            http2=False,
        )
        self.global_sem = asyncio.Semaphore(c.max_concurrency)
        self.domain_locks: dict[str, asyncio.Lock] = {}
        self.domain_last: dict[str, float] = {}
        self.delay = c.per_domain_delay_seconds
        self.max_retries = c.max_retries

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def _wait_for_domain(self, host: str) -> None:
        last = self.domain_last.get(host, 0.0)
        wait = self.delay - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self.domain_last[host] = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        data: Any = None,
        max_bytes: int = 15_000_000,
    ) -> FetchResult:
        """GET (or POST) with retries/backoff. Never raises; errors are in the result."""
        host = (urlsplit(url).hostname or "").lower()
        lock = self.domain_locks.setdefault(host, asyncio.Lock())
        attempt = 0
        last_error = None
        status: int | None = None
        while attempt <= self.max_retries:
            attempt += 1
            try:
                async with lock, self.global_sem:
                    await self._wait_for_domain(host)
                    resp = await self.client.request(method, url, headers=headers, data=data)
                status = resp.status_code
                if status in RETRY_STATUS and attempt <= self.max_retries:
                    delay = _retry_after(resp) or (2**attempt + random.random())
                    last_error = f"HTTP {status}"
                    log.debug("%s -> %s, retrying in %.1fs", url, status, delay)
                    await asyncio.sleep(min(delay, 60))
                    continue
                content = resp.content[:max_bytes]
                err = None if status < 400 or status == 304 else f"HTTP {status}"
                return FetchResult(
                    url=url,
                    status=status,
                    content=content,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    error=err,
                    final_url=str(resp.url),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                if attempt <= self.max_retries:
                    await asyncio.sleep(2**attempt + random.random())
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                break
        return FetchResult(url=url, status=status, error=last_error or "request failed")


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


# --------------------------------------------------------------------- parsing


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def looks_like_html(content: bytes, content_type: str = "") -> bool:
    head = content[:2048].lower()
    if "html" in content_type and b"<rss" not in head and b"<feed" not in head:
        return True
    return b"<!doctype html" in head or (b"<html" in head and b"<rss" not in head)


def parse_feed(content: bytes, source: str, base_url: str = "") -> tuple[list[RawItem], str | None]:
    """Parse RSS/Atom bytes. Returns ``(items, error)``; error is set if nothing usable was found."""
    parsed = feedparser.parse(content)
    items: list[RawItem] = []
    for e in parsed.entries:
        title = unescape(re.sub(r"\s+", " ", (e.get("title") or "").strip()))
        link = e.get("link") or ""
        if not link:
            for lk in e.get("links", []):
                if lk.get("href"):
                    link = lk["href"]
                    break
        if not link and str(e.get("id", "")).startswith("http"):
            link = e["id"]
        if not title or not link:
            continue
        if base_url and not link.startswith("http"):
            link = urljoin(base_url, link)
        content_html = ""
        if e.get("content"):
            content_html = max((c.get("value", "") for c in e["content"]), key=len, default="")
        summary_html = e.get("summary", "") or e.get("description", "")
        src = e.get("source") or {}
        items.append(
            RawItem(
                title=title,
                url=link.strip(),
                source=source,
                content_html=content_html,
                summary_html=summary_html,
                published=_entry_datetime(e),
                author=(e.get("author") or None),
                comments_url=e.get("comments"),
                publisher=src.get("title") if isinstance(src, dict) else None,
                publisher_url=src.get("href") if isinstance(src, dict) else None,
            )
        )
    if not items:
        if parsed.bozo and not parsed.entries:
            exc = parsed.get("bozo_exception")
            return [], f"not a valid feed ({type(exc).__name__ if exc else 'parse error'})"
        if not parsed.get("version"):
            return [], "not a feed"
    return items, None


def newest_date(items: list[RawItem]) -> datetime | None:
    dates = [i.published for i in items if i.published]
    return max(dates) if dates else None


_ALT_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z\-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')


def discover_feeds(html: bytes | str, base_url: str) -> list[str]:
    """Find ``<link rel="alternate" type="application/rss+xml|atom+xml">`` feed URLs in a page."""
    text = html.decode("utf-8", "ignore") if isinstance(html, bytes) else html
    found: list[str] = []
    for tag in _ALT_RE.findall(text):
        attrs = {
            m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "")
            for m in _ATTR_RE.finditer(tag)
        }
        rel = attrs.get("rel", "").lower()
        typ = attrs.get("type", "").lower()
        if "alternate" in rel and typ in (
            "application/rss+xml",
            "application/atom+xml",
            "application/feed+json",
        ):
            href = attrs.get("href")
            if href:
                full = urljoin(base_url, unescape(href))
                if full not in found:
                    found.append(full)
    # Prefer feeds that are not comment feeds.
    found.sort(key=lambda u: ("comment" in u.lower(), len(u)))
    return found
