"""Markdown -> safe HTML for the web UI (wikilinks become app links)."""

from __future__ import annotations

import re
from urllib.parse import quote

import markdown as md
import nh3
from markupsafe import Markup

from signal_news.storage.db import Database
from signal_news.storage.vault import split_front_matter

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

ALLOWED_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
ALLOWED_ATTRS = {
    "a": {"href", "title", "target"},
    "td": {"align"},
    "th": {"align"},
    "span": {"class"},
    "div": {"class"},
}


def _wikilinks_to_md(text: str, db: Database | None) -> str:
    cache: dict[str, str] = {}

    def repl(m: re.Match[str]) -> str:
        target, label = m.group(1).strip(), (m.group(2) or m.group(1)).strip()
        if target not in cache:
            href = f"/vault/{quote(target)}"
            if target.startswith("articles/") and db is not None:
                art = db.get_article_by_path(target)
                if art:
                    href = f"/articles/{art['id']}"
            elif target.startswith("digests/"):
                href = f"/digests/{target.split('/', 1)[1]}"
            cache[target] = href
        return f"[{label}]({cache[target]})"

    return WIKILINK_RE.sub(repl, text)


def render_markdown(text: str, db: Database | None = None) -> Markup:
    """Render Markdown (front matter stripped) to sanitised HTML."""
    if text.startswith("---"):
        _, text = split_front_matter(text)
    text = _wikilinks_to_md(text, db)
    html = md.markdown(text, extensions=["extra", "sane_lists"], output_format="html")
    clean = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )
    clean = re.sub(r'<a href="(https?://[^"]+)"', r'<a target="_blank" href="\1"', clean)
    return Markup(clean)


def split_sections(markdown_text: str) -> list[tuple[str, str]]:
    """Split a digest body into ``(heading, content)`` pairs on ``## `` headings."""
    if markdown_text.startswith("---"):
        _, markdown_text = split_front_matter(markdown_text)
    parts: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in markdown_text.splitlines():
        if line.startswith("## "):
            parts.append((heading, "\n".join(buf)))
            heading, buf = line[3:].strip(), []
        else:
            buf.append(line)
    parts.append((heading, "\n".join(buf)))
    return [(h, b) for h, b in parts if h or b.strip()]
