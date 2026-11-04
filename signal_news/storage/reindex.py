"""Rebuild the SQLite index from the Markdown vault (``signal reindex``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from signal_news.collector.dedupe import canonicalize_url, normalise_title
from signal_news.collector.extract import plain_text
from signal_news.storage.db import Database
from signal_news.storage.vault import Vault, read_document
from signal_news.timeutil import parse_iso, to_utc_iso

log = logging.getLogger(__name__)


@dataclass
class ReindexResult:
    articles: int = 0
    digests: int = 0
    errors: int = 0


def reindex(db: Database, vault: Vault, *, clear: bool = True) -> ReindexResult:
    """Scan ``vault/articles`` and ``vault/digests`` and re-create the index rows."""
    res = ReindexResult()
    if clear:
        db.clear_articles()
    for path in vault.iter_article_files():
        try:
            meta, body = read_document(path)
            if not meta.get("id") or not meta.get("url"):
                continue
            collected = parse_iso(meta.get("collected"))
            published = parse_iso(meta.get("published"))
            row = {
                **meta,
                "path": vault.rel(path),
                "canonical_url": meta.get("canonical_url") or canonicalize_url(meta["url"]),
                "title": meta.get("title") or path.stem,
                "title_norm": normalise_title(meta.get("title") or ""),
                "collected": to_utc_iso(collected)
                if collected
                else to_utc_iso(parse_iso("1970-01-01")),
                "published": to_utc_iso(published) if published else None,
                "summary": meta.get("summary") or None,
                "kev_new": meta.get("source") == "CISA KEV",
            }
            db.upsert_article(row, body=plain_text(body)[:20000])
            res.articles += 1
        except Exception as exc:  # noqa: BLE001
            res.errors += 1
            log.error("Could not index %s: %s", path, exc)
    for path in vault.iter_digest_files():
        try:
            meta, _ = read_document(path)
            day = str(meta.get("date") or path.stem)
            ws, we = parse_iso(meta.get("window_start")), parse_iso(meta.get("window_end"))
            if not ws or not we:
                continue
            db.record_digest(
                day,
                vault.rel(path),
                to_utc_iso(ws),
                to_utc_iso(we),
                int(meta.get("articles") or 0),
                str(meta.get("model") or ""),
            )
            res.digests += 1
        except Exception as exc:  # noqa: BLE001
            res.errors += 1
            log.error("Could not index digest %s: %s", path, exc)
    return res
