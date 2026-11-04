"""Turn HTML into clean Markdown: feed content directly, full pages via trafilatura."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from markdownify import markdownify

from signal_news.collector.feeds import PoliteClient

log = logging.getLogger(__name__)

_BLANKS = re.compile(r"\n{3,}")


def html_to_markdown(html: str) -> str:
    """Convert a feed's HTML snippet to tidy Markdown (scripts/styles/images dropped)."""
    if not html:
        return ""
    if "<" not in html:
        return html.strip()
    html = re.sub(r"(?is)<(script|style|iframe|noscript)\b.*?</\1>", "", html)
    md = markdownify(
        html, heading_style="ATX", strip=["img", "figure", "svg", "picture", "source", "video"]
    )
    md = "\n".join(line.rstrip() for line in md.splitlines())
    return _BLANKS.sub("\n\n", md).strip()


def plain_text(md_or_html: str) -> str:
    """Rough plain text (for length checks, tagging and search)."""
    t = re.sub(r"<[^>]+>", " ", md_or_html or "")
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[#*_`>|]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_article(html: str, url: str) -> str | None:
    """Extract the main article text from a full page as Markdown. None if nothing usable."""
    try:
        import trafilatura

        md = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=True,
            favor_precision=True,
        )
        if md and len(md) > 200:
            return _BLANKS.sub("\n\n", md).strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("trafilatura failed for %s: %s", url, exc)
    try:
        from readability import Document

        doc = Document(html)
        md = html_to_markdown(doc.summary(html_partial=True))
        if md and len(md) > 200:
            return md
    except Exception as exc:  # noqa: BLE001
        log.debug("readability failed for %s: %s", url, exc)
    return None


class RobotsCache:
    """Caches robots.txt per host and answers ``can_fetch``."""

    def __init__(self, client: PoliteClient, user_agent: str):
        self.client = client
        self.user_agent = user_agent
        self.cache: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self.cache:
            res = await self.client.get(f"{host}/robots.txt")
            if res.status and res.status < 400 and res.content:
                rp = RobotFileParser()
                rp.parse(res.content.decode("utf-8", "ignore").splitlines())
                self.cache[host] = rp
            elif res.status in (401, 403):
                # Treat "robots.txt forbidden" as disallow-all, per the convention.
                rp = RobotFileParser()
                rp.parse(["User-agent: *", "Disallow: /"])
                self.cache[host] = rp
            else:
                self.cache[host] = None  # missing robots.txt = allowed
        rp = self.cache[host]
        return True if rp is None else rp.can_fetch(self.user_agent, url)


async def fetch_full_text(
    client: PoliteClient, robots: RobotsCache | None, url: str
) -> tuple[str | None, str | None]:
    """Fetch a page and extract it. Returns ``(markdown, error)``."""
    if robots is not None and not await robots.allowed(url):
        return None, "blocked by robots.txt"
    res = await client.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    if not res.ok:
        return None, res.error or f"HTTP {res.status}"
    ctype = res.headers.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype and ctype:
        return None, f"not HTML ({ctype})"
    html = res.content.decode(_charset(ctype), "ignore")
    md = extract_article(html, res.final_url or url)
    return (md, None) if md else (None, "extraction found no article text")


def _charset(ctype: str) -> str:
    import codecs

    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    if m:
        try:
            codecs.lookup(m.group(1))
            return m.group(1)
        except LookupError:
            pass
    return "utf-8"
