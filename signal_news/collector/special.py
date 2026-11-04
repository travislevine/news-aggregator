"""Special sources: Google News queries and link resolution, hnrss, CISA KEV, watchlist feeds."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

from signal_news.collector.feeds import PoliteClient, RawItem
from signal_news.config import Config, GoogleNewsLocale, Source

log = logging.getLogger(__name__)

GOOGLE_NEWS_HOST = "news.google.com"


# ------------------------------------------------------------------ Google News


def google_news_url(query: str, locale: GoogleNewsLocale) -> str:
    """Build a Google News RSS search URL."""
    return (
        f"https://news.google.com/rss/search?q={quote_plus(query)}"
        f"&hl={locale.hl}&gl={locale.gl}&ceid={quote(locale.ceid, safe='')}"
    )


def source_url(src: Source, cfg: Config) -> str:
    """The URL to fetch for a source (Google News sources are built from their query)."""
    if src.type == "google_news" and src.query:
        return google_news_url(src.query, src.locale or cfg.collection.google_news)
    return src.url


def is_google_news_link(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith(GOOGLE_NEWS_HOST)


def strip_publisher_suffix(title: str, publisher: str | None) -> str:
    """Google News titles end with " - Publisher"; remove it."""
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(publisher) - 3].strip()
    return title


def _gnews_article_id(url: str) -> str | None:
    m = re.search(r"/(?:rss/)?articles/([^/?#]+)", urlsplit(url).path)
    return m.group(1) if m else None


def decode_google_news_offline(url: str) -> str | None:
    """Older Google News ids embed the target URL in base64-encoded protobuf. Try that first."""
    art_id = _gnews_article_id(url)
    if not art_id:
        return None
    try:
        raw = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
    except (ValueError, TypeError):
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    candidate = m.group(0).decode("ascii", "ignore")
    # Newer ids start with "AU_yqL" and hold an opaque token, not a URL.
    if (
        candidate.startswith("http")
        and "." in candidate
        and not candidate.startswith("https://news.google")
    ):
        return re.split(r"[\x00-\x20\xd2]", candidate)[0].rstrip("\x01\x02")
    return None


async def resolve_google_news(client: PoliteClient, url: str) -> str | None:
    """Turn a news.google.com article link into the publisher's URL, or None if that fails.

    Google News no longer issues plain HTTP redirects, so this (1) tries an offline
    decode, then (2) reads the signature/timestamp from the article page and asks
    Google's batchexecute endpoint for the real URL.
    """
    offline = decode_google_news_offline(url)
    if offline:
        return offline
    art_id = _gnews_article_id(url)
    if not art_id:
        return None
    page = await client.get(f"https://news.google.com/rss/articles/{art_id}")
    if not page.ok:
        return None
    html = page.content.decode("utf-8", "ignore")
    sig = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not sig or not ts:
        return None
    payload = [
        "Fbv4je",
        f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
        f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{art_id}",{ts.group(1)},"{sig.group(1)}"]',
    ]
    body = "f.req=" + quote(json.dumps([[payload]]))
    resp = await client.get(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    if not resp.ok:
        return None
    try:
        text = resp.content.decode("utf-8", "ignore")
        chunk = text.split("\n\n", 1)[1]
        outer = json.loads(chunk)[:-2]
        decoded = json.loads(outer[0][2])[1]
        return decoded if isinstance(decoded, str) and decoded.startswith("http") else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------------ hnrss


def adapt_hnrss(item: RawItem) -> RawItem:
    """hnrss items: link = the story URL, comments = HN discussion. Ask HN posts link to HN."""
    hn = item.comments_url
    if not hn:
        m = re.search(r"https://news\.ycombinator\.com/item\?id=\d+", item.summary_html or "")
        hn = m.group(0) if m else None
    item.comments_url = hn
    item.extra["hn_url"] = hn
    # hnrss summaries are just "Article URL / Comments URL / Points" boilerplate.
    if "Article URL:" in (item.summary_html or ""):
        item.summary_html = ""
    return item


# -------------------------------------------------------------------- CISA KEV


def parse_kev(content: bytes) -> list[dict[str, Any]]:
    """Parse the CISA KEV JSON catalog into a list of vulnerability dicts."""
    data = json.loads(content)
    vulns = data.get("vulnerabilities", []) if isinstance(data, dict) else []
    return [v for v in vulns if isinstance(v, dict) and v.get("cveID")]


def kev_diff(entries: list[dict[str, Any]], known_ids: set[str]) -> list[dict[str, Any]]:
    """Entries in the catalog that are not in ``known_ids`` (newly added), oldest first."""
    new = [e for e in entries if e["cveID"] not in known_ids]
    new.sort(key=lambda e: (e.get("dateAdded") or "", e["cveID"]))
    return new


def kev_to_item(entry: dict[str, Any]) -> RawItem:
    """Turn one new KEV entry into a short article."""
    cve = entry["cveID"]
    vendor = entry.get("vendorProject", "").strip()
    product = entry.get("product", "").strip()
    name = entry.get("vulnerabilityName", "").strip() or f"{vendor} {product} vulnerability"
    title = f"CISA adds {cve} ({vendor} {product}) to Known Exploited Vulnerabilities".replace(
        "  ", " "
    )
    lines = [
        f"<p><strong>{name}</strong></p>",
        f"<p>{entry.get('shortDescription', '').strip()}</p>",
        "<ul>",
        f"<li><strong>CVE:</strong> {cve}</li>",
        f"<li><strong>Vendor / product:</strong> {vendor} / {product}</li>",
        f"<li><strong>Date added:</strong> {entry.get('dateAdded', '')}</li>",
        f"<li><strong>Due date (US federal agencies):</strong> {entry.get('dueDate', '')}</li>",
        f"<li><strong>Known ransomware use:</strong> {entry.get('knownRansomwareCampaignUse', 'Unknown')}</li>",
        f"<li><strong>Required action:</strong> {entry.get('requiredAction', '').strip()}</li>",
        "</ul>",
    ]
    if entry.get("notes"):
        lines.append(f"<p><strong>Notes:</strong> {entry['notes']}</p>")
    published = None
    if entry.get("dateAdded"):
        try:
            published = datetime.fromisoformat(entry["dateAdded"]).replace(tzinfo=UTC)
        except ValueError:
            published = None
    return RawItem(
        title=title,
        url=f"https://nvd.nist.gov/vuln/detail/{cve}",
        source="CISA KEV",
        content_html="\n".join(lines),
        published=published,
        extra={"kev_entry": entry, "vendor": vendor, "product": product},
    )


# ------------------------------------------------------------- watchlist feeds


def watchlist_sources(cfg: Config) -> list[Source]:
    """Generate per-ticker Yahoo Finance and Google News sources from the watchlist."""
    out: list[Source] = []
    for w in cfg.watchlist:
        if cfg.watchlist_feeds.yahoo:
            region = "AU" if w.ticker.upper().endswith(".AX") else "US"
            out.append(
                Source(
                    name=f"Yahoo Finance: {w.ticker}",
                    url=(
                        "https://feeds.finance.yahoo.com/rss/2.0/headline?"
                        f"s={quote(w.ticker)}&region={region}&lang=en-{region}"
                    ),
                    topic="finance",
                    type="rss",
                    fetch_full_text=False,
                    priority=2,
                    notes="Generated from the watchlist",
                    generated=True,
                )
            )
        if cfg.watchlist_feeds.google_news:
            name = w.names[0] if w.names else w.base_symbol
            query = f'"{name}" OR {w.base_symbol} stock when:1d'
            locale = (
                GoogleNewsLocale(hl="en-AU", gl="AU", ceid="AU:en")
                if w.ticker.upper().endswith(".AX")
                else None
            )
            out.append(
                Source(
                    name=f"Google News: {w.ticker}",
                    topic="finance",
                    type="google_news",
                    query=query,
                    locale=locale,
                    fetch_full_text=True,
                    priority=3,
                    notes="Generated from the watchlist",
                    generated=True,
                )
            )
    return out
