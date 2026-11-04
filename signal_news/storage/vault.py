"""Markdown vault: articles and digests as files with YAML front matter.

Layout (Obsidian-friendly)::

    vault/articles/YYYY/MM/DD/<topic>/YYYY-MM-DD-<slug>-<shortid>.md
    vault/digests/YYYY-MM-DD.md
    vault/qa/YYYY-MM-DD-HHMMSS-<slug>.md
    vault/_index/<topic>.md
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

# Front-matter key order for articles. Unknown keys are kept after these.
ARTICLE_KEYS = [
    "id",
    "title",
    "url",
    "canonical_url",
    "source",
    "topic",
    "extra_topics",
    "subtopics",
    "published",
    "collected",
    "author",
    "tickers",
    "cves",
    "kev",
    "vendors",
    "affects_me",
    "cluster_id",
    "full_text",
    "paywalled",
    "hn_url",
    "summary",
    "importance",
    "sentiment",
    "entities",
    "tags",
]
DATETIME_KEYS = {"published", "collected", "window_start", "window_end", "asked"}
FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ----------------------------------------------------------------------------- YAML


class _Dumper(yaml.SafeDumper):
    """SafeDumper that writes datetimes as ISO 8601 with a ``T`` and never wraps lines."""


def _repr_datetime(dumper: yaml.SafeDumper, data: datetime) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", data.isoformat())


def _repr_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(datetime, _repr_datetime)
_Dumper.add_representer(str, _repr_str)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def dump_front_matter(meta: dict[str, Any], key_order: list[str] | None = None) -> str:
    """Serialise ``meta`` as YAML front matter (without the ``---`` fences)."""
    ordered: dict[str, Any] = {}
    for k in key_order or []:
        if k in meta:
            ordered[k] = meta[k]
    for k, v in meta.items():
        if k not in ordered:
            ordered[k] = v
    for k in DATETIME_KEYS & ordered.keys():
        dt = _parse_dt(ordered[k])
        if dt is not None:
            ordered[k] = dt
    if isinstance(ordered.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ordered["date"]):
        ordered["date"] = date.fromisoformat(ordered["date"])
    text = yaml.dump(
        ordered,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=None,
        width=100000,
    )
    return text.rstrip("\n")


def normalise_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Convert YAML-native dates back into ISO strings so values compare predictably."""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(meta, body)``. Files without front matter give ``({}, text)``."""
    m = FM_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return normalise_meta(meta), text[m.end() :]


def render_document(meta: dict[str, Any], body: str, key_order: list[str] | None = None) -> str:
    return (
        f"---\n{dump_front_matter(meta, key_order)}\n---\n\n{body.lstrip(chr(10))}".rstrip() + "\n"
    )


def read_document(path: Path) -> tuple[dict[str, Any], str]:
    return split_front_matter(path.read_text(encoding="utf-8"))


def write_if_changed(path: Path, content: str) -> bool:
    """Atomically write ``content`` unless the file already holds exactly that. Returns True if written."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return True


def update_front_matter(
    path: Path, updates: dict[str, Any], key_order: list[str] | None = None
) -> bool:
    """Merge ``updates`` into a file's front matter, leaving the body byte-for-byte unchanged."""
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if m:
        meta = normalise_meta(yaml.safe_load(m.group(1)) or {})
        body = text[m.end() :]
    else:
        meta, body = {}, "\n" + text
    new_meta = {**meta, **updates}
    if new_meta == meta:
        return False
    content = f"---\n{dump_front_matter(new_meta, key_order or ARTICLE_KEYS)}\n---\n{body}"
    return write_if_changed(path, content)


# ------------------------------------------------------------------------ filenames


def slugify(text: str, max_len: int = 60) -> str:
    """ASCII, lowercase, hyphenated slug that is safe on Windows. At most ``max_len`` chars."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _WIN_BAD.sub(" ", text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] if "-" in text[:max_len] else text[:max_len]
    return text.strip("-") or "untitled"


class Vault:
    """Read/write access to the vault folder."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.articles_dir = self.root / "articles"
        self.digests_dir = self.root / "digests"
        self.qa_dir = self.root / "qa"
        self.index_dir = self.root / "_index"
        for d in (self.articles_dir, self.digests_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- articles -------------------------------------------------------------

    def article_path(self, article_id: str, title: str, topic: str, collected: datetime) -> Path:
        day = collected.strftime("%Y-%m-%d")
        name = f"{day}-{slugify(title)}-{article_id[:6]}.md"
        return (
            self.articles_dir
            / collected.strftime("%Y")
            / collected.strftime("%m")
            / collected.strftime("%d")
            / topic
            / name
        )

    def rel(self, path: Path) -> str:
        """Vault-relative POSIX path without ``.md`` (the form used in ``[[wikilinks]]``)."""
        return path.relative_to(self.root).as_posix().removesuffix(".md")

    def abs(self, rel: str) -> Path:
        rel = rel if rel.endswith(".md") else rel + ".md"
        return self.root / Path(rel)

    def write_article(self, meta: dict[str, Any], body: str, path: Path) -> bool:
        """Write an article file. Existing files are only rewritten if their content changed."""
        title = meta.get("title", "Untitled")
        source = meta.get("source", "")
        url = meta.get("url", "")
        pub = _parse_dt(meta.get("published")) or _parse_dt(meta.get("collected"))
        day = pub.strftime("%Y-%m-%d") if pub else ""
        header = f"# {title}\n\n**Source:** [{source}]({url})" + (f" · {day}" if day else "")
        if meta.get("hn_url"):
            header += f" · [HN discussion]({meta['hn_url']})"
        if meta.get("paywalled"):
            header += "\n\n> Paywalled source: only the headline and feed summary are stored."
        doc = render_document(meta, f"{header}\n\n{body.strip()}\n", ARTICLE_KEYS)
        return write_if_changed(path, doc)

    def iter_article_files(self):
        yield from sorted(self.articles_dir.rglob("*.md"))

    def article_body(self, rel: str) -> str:
        path = self.abs(rel)
        if not path.exists():
            return ""
        _, body = read_document(path)
        return body

    # -- digests ---------------------------------------------------------------

    def digest_path(self, day: str) -> Path:
        return self.digests_dir / f"{day}.md"

    def iter_digest_files(self):
        yield from sorted(self.digests_dir.glob("*.md"), reverse=True)

    # -- Q&A -------------------------------------------------------------------

    def qa_path(self, when: datetime, question: str) -> Path:
        return self.qa_dir / f"{when.strftime('%Y-%m-%d-%H%M%S')}-{slugify(question, 40)}.md"
