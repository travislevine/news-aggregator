"""SQLite index: articles + FTS5 search, run history, source health, KEV copy and locks.

The vault is the source of truth for content; everything here can be rebuilt with
``signal reindex`` (except run history and feed ETags, which are just caches).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    url           TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    title_norm    TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL,
    topic         TEXT NOT NULL,
    extra_topics  TEXT NOT NULL DEFAULT '[]',
    subtopics     TEXT NOT NULL DEFAULT '[]',
    published     TEXT,
    collected     TEXT NOT NULL,
    author        TEXT,
    tickers       TEXT NOT NULL DEFAULT '[]',
    cves          TEXT NOT NULL DEFAULT '[]',
    kev           INTEGER NOT NULL DEFAULT 0,
    kev_new       INTEGER NOT NULL DEFAULT 0,
    vendors       TEXT NOT NULL DEFAULT '[]',
    affects_me    INTEGER NOT NULL DEFAULT 0,
    cluster_id    TEXT,
    full_text     INTEGER NOT NULL DEFAULT 0,
    paywalled     INTEGER NOT NULL DEFAULT 0,
    priority      INTEGER NOT NULL DEFAULT 2,
    hn_url        TEXT,
    summary       TEXT,
    importance    INTEGER,
    sentiment     TEXT,
    entities      TEXT NOT NULL DEFAULT '[]',
    summary_attempts INTEGER NOT NULL DEFAULT 0,
    summary_error TEXT,
    in_digest     TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_collected ON articles(collected);
CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    id UNINDEXED, title, summary, body, tags,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS feed_state (
    source               TEXT PRIMARY KEY,
    url                  TEXT,
    etag                 TEXT,
    last_modified        TEXT,
    last_checked         TEXT,
    last_success         TEXT,
    last_error           TEXT,
    last_error_at        TEXT,
    last_status          INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    items_last_run       INTEGER NOT NULL DEFAULT 0,
    new_last_run         INTEGER NOT NULL DEFAULT 0,
    total_new            INTEGER NOT NULL DEFAULT 0,
    newest_item          TEXT,
    auto_disabled_at     TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    started   TEXT NOT NULL,
    finished  TEXT,
    status    TEXT NOT NULL DEFAULT 'running',
    counts    TEXT NOT NULL DEFAULT '{}',
    errors    TEXT NOT NULL DEFAULT '[]',
    message   TEXT
);

CREATE TABLE IF NOT EXISTS digests (
    date          TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    articles      INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    generated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kev (
    cve_id     TEXT PRIMARY KEY,
    date_added TEXT,
    data       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    name        TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    article_id TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS url_cache (
    url      TEXT PRIMARY KEY,
    resolved TEXT,
    fetched  TEXT NOT NULL
);
"""

JSON_FIELDS = ("extra_topics", "subtopics", "tickers", "cves", "vendors", "entities")
BOOL_FIELDS = ("kev", "kev_new", "affects_me", "full_text", "paywalled")


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def row_to_article(row: sqlite3.Row) -> dict[str, Any]:
    """Convert an ``articles`` row into a plain dict with JSON and bool fields decoded."""
    d = dict(row)
    for f in JSON_FIELDS:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except json.JSONDecodeError:
                d[f] = []
    for f in BOOL_FIELDS:
        if f in d:
            d[f] = bool(d[f])
    return d


class LockError(RuntimeError):
    """Raised when a named lock is already held."""


class Database:
    """Thin wrapper around a SQLite file. Opens a short-lived connection per operation."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ articles

    def has_canonical_url(self, url: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute("SELECT 1 FROM articles WHERE canonical_url = ?", (url,)).fetchone()
                is not None
            )

    def has_article(self, article_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone()
                is not None
            )

    def upsert_article(self, art: dict[str, Any], body: str = "") -> None:
        """Insert or replace an article row and its FTS entry."""
        cols = [
            "id",
            "path",
            "url",
            "canonical_url",
            "title",
            "title_norm",
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
            "kev_new",
            "vendors",
            "affects_me",
            "cluster_id",
            "full_text",
            "paywalled",
            "priority",
            "hn_url",
            "summary",
            "importance",
            "sentiment",
            "entities",
            "summary_attempts",
            "summary_error",
            "in_digest",
        ]
        values = []
        for c in cols:
            v = art.get(c)
            if c in JSON_FIELDS:
                v = json.dumps(v or [])
            elif c in BOOL_FIELDS:
                v = int(bool(v))
            elif c == "summary_attempts":
                v = int(v or 0)
            elif c == "priority":
                v = int(v or 2)
            elif c == "title_norm":
                v = v or ""
            values.append(v)
        placeholders = ",".join("?" for _ in cols)
        with self.transaction() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO articles ({','.join(cols)}) VALUES ({placeholders})",
                values,
            )
            conn.execute("DELETE FROM articles_fts WHERE id = ?", (art["id"],))
            conn.execute(
                "INSERT INTO articles_fts (id, title, summary, body, tags) VALUES (?,?,?,?,?)",
                (art["id"], art.get("title", ""), art.get("summary") or "", body, _fts_tags(art)),
            )

    def update_article(self, article_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if k in JSON_FIELDS:
                v = json.dumps(v or [])
            elif k in BOOL_FIELDS:
                v = int(bool(v))
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(article_id)
        with self.transaction() as conn:
            conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", vals)
            if "summary" in fields:
                conn.execute(
                    "UPDATE articles_fts SET summary = ? WHERE id = ?",
                    (fields["summary"] or "", article_id),
                )

    def get_article(self, article_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        return row_to_article(row) if row else None

    def get_article_by_path(self, rel_path: str) -> dict[str, Any] | None:
        rel = rel_path.removesuffix(".md")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM articles WHERE path = ? OR path = ?", (rel, rel + ".md")
            ).fetchone()
        return row_to_article(row) if row else None

    def articles_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Articles whose ``collected`` time is in ``[start, end)`` (ISO strings, UTC)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE collected >= ? AND collected < ? ORDER BY collected",
                (start, end),
            ).fetchall()
        return [row_to_article(r) for r in rows]

    def recent_for_clustering(self, since: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, title_norm, cluster_id, collected, topic FROM articles "
                "WHERE collected >= ?",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def articles_needing_summary(self, limit: int, max_attempts: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE (summary IS NULL OR summary = '') "
                "AND summary_attempts < ? ORDER BY kev_new DESC, priority ASC, collected DESC LIMIT ?",
                (max_attempts, limit),
            ).fetchall()
        return [row_to_article(r) for r in rows]

    def count_needing_summary(self, max_attempts: int) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM articles WHERE (summary IS NULL OR summary = '') "
                "AND summary_attempts < ?",
                (max_attempts,),
            ).fetchone()[0]

    def query_articles(
        self,
        *,
        topic: str | None = None,
        source: str | None = None,
        ticker: str | None = None,
        cve: str | None = None,
        kev: bool | None = None,
        affects_me: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filterable article list used by the web UI. ``search`` uses FTS5."""
        where, params = [], []
        join = ""
        if search:
            match = fts_query(search)
            if match:
                join = "JOIN articles_fts f ON f.id = a.id"
                where.append("articles_fts MATCH ?")
                params.append(match)
        if topic:
            where.append("(a.topic = ? OR a.extra_topics LIKE ?)")
            params += [topic, f'%"{topic}"%']
        if source:
            where.append("a.source = ?")
            params.append(source)
        if ticker:
            where.append("a.tickers LIKE ?")
            params.append(f'%"{ticker.upper()}"%')
        if cve:
            where.append("a.cves LIKE ?")
            params.append(f'%"{cve.upper()}"%')
        if kev is not None:
            where.append("a.kev = ?")
            params.append(int(kev))
        if affects_me is not None:
            where.append("a.affects_me = ?")
            params.append(int(affects_me))
        if date_from:
            where.append("a.collected >= ?")
            params.append(date_from)
        if date_to:
            where.append("a.collected < ?")
            params.append(date_to)
        sql = f"SELECT a.* FROM articles a {join}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY bm25(articles_fts)" if join else " ORDER BY a.collected DESC"
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_article(r) for r in rows]

    def search_fts(
        self,
        query: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        topic: str | None = None,
        ticker: str | None = None,
        limit: int = 20,
    ) -> list[tuple[dict[str, Any], float]]:
        """BM25-ranked full-text search returning ``(article, score)`` pairs (lower = better)."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT a.*, bm25(articles_fts, 0, 5.0, 3.0, 1.0, 2.0) AS score FROM articles_fts "
            "JOIN articles a ON a.id = articles_fts.id WHERE articles_fts MATCH ?"
        )
        params: list[Any] = [match]
        if date_from:
            sql += " AND a.collected >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND a.collected < ?"
            params.append(date_to)
        if topic:
            sql += " AND (a.topic = ? OR a.extra_topics LIKE ?)"
            params += [topic, f'%"{topic}"%']
        if ticker:
            sql += " AND a.tickers LIKE ?"
            params.append(f'%"{ticker.upper()}"%')
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(row_to_article(r), r["score"]) for r in rows]

    def distinct_sources(self) -> list[str]:
        with self.connect() as conn:
            return [
                r[0] for r in conn.execute("SELECT DISTINCT source FROM articles ORDER BY source")
            ]

    def article_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    def delete_article(self, article_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            conn.execute("DELETE FROM articles_fts WHERE id = ?", (article_id,))
            conn.execute("DELETE FROM embeddings WHERE article_id = ?", (article_id,))

    def clear_articles(self) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM articles")
            conn.execute("DELETE FROM articles_fts")
            conn.execute("DELETE FROM digests")

    # ---------------------------------------------------------------- feed state

    def get_feed_state(self, source: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM feed_state WHERE source = ?", (source,)).fetchone()
        return dict(row) if row else None

    def all_feed_states(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            return {r["source"]: dict(r) for r in conn.execute("SELECT * FROM feed_state")}

    def record_feed_success(
        self,
        source: str,
        url: str,
        *,
        status: int,
        etag: str | None,
        last_modified: str | None,
        items: int,
        new: int,
        newest: str | None,
    ) -> None:
        now = utcnow_iso()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO feed_state (source, url, etag, last_modified, last_checked, last_success,
                       last_status, consecutive_failures, items_last_run, new_last_run, total_new, newest_item)
                   VALUES (?,?,?,?,?,?,?,0,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET
                       url = excluded.url,
                       etag = COALESCE(excluded.etag, feed_state.etag),
                       last_modified = COALESCE(excluded.last_modified, feed_state.last_modified),
                       last_checked = excluded.last_checked,
                       last_success = excluded.last_success,
                       last_status = excluded.last_status,
                       consecutive_failures = 0,
                       items_last_run = excluded.items_last_run,
                       new_last_run = excluded.new_last_run,
                       total_new = feed_state.total_new + excluded.new_last_run,
                       newest_item = COALESCE(excluded.newest_item, feed_state.newest_item)""",
                (source, url, etag, last_modified, now, now, status, items, new, new, newest),
            )

    def record_feed_failure(self, source: str, url: str, error: str, status: int | None) -> int:
        """Record a failure and return the new consecutive-failure count."""
        now = utcnow_iso()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO feed_state (source, url, last_checked, last_error, last_error_at,
                       last_status, consecutive_failures)
                   VALUES (?,?,?,?,?,?,1)
                   ON CONFLICT(source) DO UPDATE SET
                       url = excluded.url,
                       last_checked = excluded.last_checked,
                       last_error = excluded.last_error,
                       last_error_at = excluded.last_error_at,
                       last_status = excluded.last_status,
                       consecutive_failures = feed_state.consecutive_failures + 1""",
                (source, url, now, error[:500], now, status),
            )
            return conn.execute(
                "SELECT consecutive_failures FROM feed_state WHERE source = ?", (source,)
            ).fetchone()[0]

    def mark_auto_disabled(self, source: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE feed_state SET auto_disabled_at = ? WHERE source = ?",
                (utcnow_iso(), source),
            )

    def clear_auto_disabled(self, source: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE feed_state SET auto_disabled_at = NULL, consecutive_failures = 0 WHERE source = ?",
                (source,),
            )

    def auto_disabled_since(self, since: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feed_state WHERE auto_disabled_at IS NOT NULL AND auto_disabled_at >= ?",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------------- runs

    def start_run(self, kind: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO runs (kind, started) VALUES (?, ?)", (kind, utcnow_iso())
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        counts: dict[str, Any] | None = None,
        errors: list[str] | None = None,
        message: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET finished = ?, status = ?, counts = ?, errors = ?, message = ? WHERE id = ?",
                (
                    utcnow_iso(),
                    status,
                    json.dumps(counts or {}),
                    json.dumps((errors or [])[:200]),
                    message,
                    run_id,
                ),
            )

    def recent_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["counts"] = json.loads(d["counts"] or "{}")
            d["errors"] = json.loads(d["errors"] or "[]")
            out.append(d)
        return out

    def fail_stale_runs(self) -> None:
        """Mark runs left 'running' by a crashed process as failed."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished = ? WHERE status = 'running'",
                (utcnow_iso(),),
            )

    # ------------------------------------------------------------------- digests

    def record_digest(
        self, date: str, path: str, window_start: str, window_end: str, articles: int, model: str
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO digests (date, path, window_start, window_end, articles, model, generated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (date, path, window_start, window_end, articles, model, utcnow_iso()),
            )

    def last_digest(self, before_date: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM digests"
        params: tuple[Any, ...] = ()
        if before_date:
            sql += " WHERE date < ?"
            params = (before_date,)
        sql += " ORDER BY date DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_digest(self, date: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM digests WHERE date = ?", (date,)).fetchone()
        return dict(row) if row else None

    def list_digests(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM digests ORDER BY date DESC")]

    def mark_in_digest(self, article_ids: list[str], date: str) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE articles SET in_digest = ? WHERE id = ? AND in_digest IS NULL",
                [(date, i) for i in article_ids],
            )

    # ----------------------------------------------------------------------- KEV

    def kev_ids(self) -> set[str]:
        with self.connect() as conn:
            return {r[0] for r in conn.execute("SELECT cve_id FROM kev")}

    def kev_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM kev").fetchone()[0]

    def store_kev(self, entries: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO kev (cve_id, date_added, data) VALUES (?,?,?)",
                [
                    (e["cveID"], e.get("dateAdded"), json.dumps(e))
                    for e in entries
                    if e.get("cveID")
                ],
            )

    def get_kev(self, cve_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM kev WHERE cve_id = ?", (cve_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---------------------------------------------------------------------- meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ----------------------------------------------------------------- url cache

    def cached_resolution(self, url: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT resolved FROM url_cache WHERE url = ?", (url,)).fetchone()
        return row[0] if row else None

    def cache_resolution(self, url: str, resolved: str | None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO url_cache (url, resolved, fetched) VALUES (?,?,?)",
                (url, resolved, utcnow_iso()),
            )

    # ---------------------------------------------------------------- embeddings

    def store_embedding(self, article_id: str, model: str, vector: bytes, dim: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (article_id, model, dim, vector) VALUES (?,?,?,?)",
                (article_id, model, dim, vector),
            )

    def embeddings_for(self, ids: list[str] | None = None) -> list[tuple[str, int, bytes]]:
        with self.connect() as conn:
            if ids is None:
                rows = conn.execute("SELECT article_id, dim, vector FROM embeddings").fetchall()
            else:
                q = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT article_id, dim, vector FROM embeddings WHERE article_id IN ({q})", ids
                ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def articles_without_embedding(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT a.* FROM articles a LEFT JOIN embeddings e ON e.article_id = a.id "
                "WHERE e.article_id IS NULL ORDER BY a.collected DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_article(r) for r in rows]

    # --------------------------------------------------------------------- locks

    @contextmanager
    def lock(self, name: str, *, ttl_minutes: int = 120) -> Iterator[str]:
        """Hold a named lock. Raises :class:`LockError` if another live holder has it.

        Locks expire after ``ttl_minutes`` so a crashed process cannot block forever.
        """
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        expires = (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT owner, expires_at FROM locks WHERE name = ?", (name,)
            ).fetchone()
            if row and row["expires_at"] > now.isoformat(timespec="seconds"):
                raise LockError(f"'{name}' is already running ({row['owner']})")
            conn.execute(
                "INSERT OR REPLACE INTO locks (name, owner, acquired_at, expires_at) VALUES (?,?,?,?)",
                (name, owner, now.isoformat(timespec="seconds"), expires),
            )
        try:
            yield owner
        finally:
            with self.transaction() as conn:
                conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))

    def lock_holder(self, name: str) -> str | None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT owner FROM locks WHERE name = ? AND expires_at > ?", (name, now)
            ).fetchone()
        return row[0] if row else None


def _fts_tags(art: dict[str, Any]) -> str:
    parts: list[str] = [art.get("topic", ""), art.get("source", "")]
    for f in ("subtopics", "tickers", "cves", "vendors", "extra_topics"):
        parts += [str(x) for x in (art.get(f) or [])]
    if art.get("kev"):
        parts.append("kev")
    return " ".join(p for p in parts if p)


_FTS_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "about",
    "what",
    "which",
    "who",
    "whom",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "any",
    "all",
    "news",
    "happened",
    "happen",
    "did",
    "does",
    "do",
    "this",
    "that",
    "these",
    "those",
    "there",
    "me",
    "my",
    "i",
    "tell",
    "give",
    "show",
    "summarise",
    "summarize",
    "summary",
    "since",
    "week",
    "month",
    "today",
    "yesterday",
    "last",
    "past",
    "days",
    "day",
    "weeks",
    "months",
    "how",
    "why",
    "when",
    "where",
    "has",
    "have",
    "had",
    "it",
    "its",
    "from",
    "by",
    "at",
    "as",
    "new",
    "latest",
    "recent",
    "recently",
    "please",
    "can",
    "you",
    "us",
    "we",
    "our",
    "so",
    "far",
}


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (terms OR-ed, prefix-matched)."""
    import re

    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.]*", text)
    terms: list[str] = []
    for t in tokens:
        t = t.strip(".-")
        if not t or t.lower() in _FTS_STOP:
            continue
        if re.fullmatch(r"CVE-\d{4}-\d{4,7}", t, re.I):
            terms.append(f'"{t.upper()}"')
        elif len(t) >= 3:
            safe = t.replace('"', "")
            terms.append(f'"{safe}"*')
        elif t.isupper():
            terms.append(f'"{t}"')
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return " OR ".join(seen[:20])
