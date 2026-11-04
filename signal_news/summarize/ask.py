"""Q&A over the collected news: date-phrase parsing, hybrid retrieval and cited answers."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from signal_news.collector.extract import plain_text
from signal_news.context import AppContext
from signal_news.llm.base import (
    LLMError,
    estimate_tokens,
    get_provider,
    job_settings,
    load_prompt,
    render_prompt,
    split_prompt,
)
from signal_news.storage.vault import render_document, write_if_changed
from signal_news.timeutil import local, to_utc_iso

log = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUM_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fourteen": 14,
    "thirty": 30,
}
REF_RE = re.compile(r"\[(\d{1,3})\]")


@dataclass
class DateFilter:
    start: datetime | None = None
    end: datetime | None = None
    label: str = ""


def _midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_date_phrase(text: str, now: datetime) -> DateFilter:
    """Turn phrases like "this week", "since Monday", "last 3 days" into a date range.

    ``now`` must be timezone-aware (the user's local time). Weeks start on Monday.
    """
    t = text.lower()
    today = _midnight(now)

    m = re.search(r"\b(?:last|past|previous)\s+(\d+|[a-z]+)\s+(hour|day|week|month)s?\b", t)
    if m:
        n_raw, unit = m.group(1), m.group(2)
        n = int(n_raw) if n_raw.isdigit() else NUM_WORDS.get(n_raw)
        if n:
            delta = {
                "hour": timedelta(hours=n),
                "day": timedelta(days=n),
                "week": timedelta(weeks=n),
                "month": timedelta(days=30 * n),
            }[unit]
            return DateFilter(now - delta, None, f"last {n} {unit}{'s' if n != 1 else ''}")
    m = re.search(r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        wd = WEEKDAYS.index(m.group(1))
        days_back = (today.weekday() - wd) % 7
        return DateFilter(
            today - timedelta(days=days_back), None, f"since {m.group(1).capitalize()}"
        )
    m = re.search(r"\bon\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        wd = WEEKDAYS.index(m.group(1))
        start = today - timedelta(days=(today.weekday() - wd) % 7)
        return DateFilter(start, start + timedelta(days=1), m.group(1).capitalize())
    if re.search(r"\b(?:past|last)\s+24\s*h", t):
        return DateFilter(now - timedelta(hours=24), None, "last 24 hours")
    if re.search(r"\bsince yesterday\b", t):
        return DateFilter(today - timedelta(days=1), None, "since yesterday")
    if re.search(r"\btoday\b", t):
        return DateFilter(today, None, "today")
    if re.search(r"\byesterday\b", t):
        return DateFilter(today - timedelta(days=1), today, "yesterday")
    if re.search(r"\bthis week\b", t):
        return DateFilter(today - timedelta(days=today.weekday()), None, "this week")
    if re.search(r"\blast week\b", t):
        start = today - timedelta(days=today.weekday() + 7)
        return DateFilter(start, start + timedelta(days=7), "last week")
    if re.search(r"\b(?:past|last)\s+week\b", t):
        return DateFilter(now - timedelta(days=7), None, "past week")
    if re.search(r"\bthis month\b", t):
        return DateFilter(today.replace(day=1), None, "this month")
    if re.search(r"\blast month\b", t):
        first = today.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        return DateFilter(prev, first, "last month")
    if re.search(r"\bthis year\b", t):
        return DateFilter(today.replace(month=1, day=1), None, "this year")
    return DateFilter()


# ------------------------------------------------------------------------- retrieval


@dataclass
class Retrieved:
    articles: list[dict[str, Any]] = field(default_factory=list)
    dates: DateFilter = field(default_factory=DateFilter)
    ticker: str | None = None
    used_embeddings: bool = False


def _detect_ticker(ctx: AppContext, question: str) -> str | None:
    for w in ctx.cfg.watchlist:
        pats = [rf"\b\$?{re.escape(w.base_symbol)}\b"] + [rf"\b{re.escape(n)}\b" for n in w.names]
        for p in pats:
            flags = 0 if p.startswith(r"\b\$?") else re.IGNORECASE
            if re.search(p, question, flags):
                return w.ticker.upper()
    return None


def _vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _embedding_search(
    ctx: AppContext, question: str, candidates: list[dict[str, Any]], k: int
) -> list[str]:
    provider = get_provider(ctx.cfg, "embeddings")
    q = np.asarray(provider.embed([question])[0], dtype=np.float32)
    ids = [a["id"] for a in candidates]
    rows = ctx.db.embeddings_for(ids)
    if not rows:
        return []
    mat = np.stack([_vec(b) for _, _, b in rows])
    sims = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
    order = np.argsort(-sims)[:k]
    return [rows[i][0] for i in order if sims[i] > 0.3]


def retrieve(
    ctx: AppContext, question: str, *, topic: str | None = None, now: datetime | None = None
) -> Retrieved:
    """FTS5/BM25 retrieval with date/topic/ticker filters, fused with embeddings when enabled."""
    cfg = ctx.cfg
    now = now or cfg.now()
    dates = parse_date_phrase(question, now)
    ticker = _detect_ticker(ctx, question)
    k = cfg.qa.max_results
    date_from = to_utc_iso(dates.start) if dates.start else None
    date_to = to_utc_iso(dates.end) if dates.end else None

    fts = ctx.db.search_fts(
        question, date_from=date_from, date_to=date_to, topic=topic, ticker=ticker, limit=k * 3
    )
    if ticker and len(fts) < 3:
        # Ticker-tagged articles only: if the question names the company, all of its news is relevant.
        tagged = ctx.db.query_articles(
            ticker=ticker, date_from=date_from, date_to=date_to, topic=topic, limit=k * 2
        )
        fts += [(a, 0.0) for a in tagged if a["id"] not in {f[0]["id"] for f in fts}]
    ranked: dict[str, float] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for rank, (art, _score) in enumerate(fts):
        by_id[art["id"]] = art
        ranked[art["id"]] = ranked.get(art["id"], 0) + 1 / (60 + rank)

    used_emb = False
    if cfg.qa.embeddings_enabled:
        try:
            pool = ctx.db.query_articles(
                date_from=date_from, date_to=date_to, topic=topic, ticker=ticker, limit=3000
            )
            hits = _embedding_search(ctx, question, pool, k * 3)
            pool_by_id = {a["id"]: a for a in pool}
            for rank, aid in enumerate(hits):
                by_id.setdefault(aid, pool_by_id[aid])
                ranked[aid] = ranked.get(aid, 0) + 1 / (60 + rank)
            used_emb = bool(hits)
        except (LLMError, ValueError) as exc:
            log.warning("Embedding search unavailable: %s", exc)

    order = sorted(ranked, key=lambda i: ranked[i], reverse=True)[:k]
    arts = [by_id[i] for i in order]
    return Retrieved(articles=arts, dates=dates, ticker=ticker, used_embeddings=used_emb)


# --------------------------------------------------------------------------- answering


def _context_block(ctx: AppContext, arts: list[dict[str, Any]], budget_tokens: int) -> str:
    blocks, used = [], 0
    for n, a in enumerate(arts, 1):
        when = local(a.get("published") or a.get("collected"), ctx.cfg.tz)
        excerpt = plain_text(ctx.vault.article_body(a["path"]))[:900]
        dated = f", {when:%a %d %b %Y}" if when else ""
        block = (
            f"[{n}] {a['title']} ({a['source']}{dated})\n"
            f"Summary: {a.get('summary') or '-'}\nExcerpt: {excerpt}"
        )
        t = estimate_tokens(block)
        if used + t > budget_tokens:
            break
        blocks.append(block)
        used += t
    return "\n\n".join(blocks)


def _link(a: dict[str, Any]) -> str:
    label = str(a.get("source", "source")).replace("|", "-").replace("]", ")")
    return f"[[{a['path']}|{label}]] [↗]({a['url']})"


def cite(answer: str, arts: list[dict[str, Any]]) -> str:
    """Replace ``[n]`` with links and append a source list."""
    used: list[int] = []

    def repl(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if 1 <= n <= len(arts):
            if n not in used:
                used.append(n)
            return f" ({_link(arts[n - 1])})"
        return ""

    body = REF_RE.sub(repl, answer).replace("  (", " (")
    if used:
        lines = [
            f"{i}. [[{arts[n - 1]['path']}|{arts[n - 1]['title']}]] ({arts[n - 1]['source']})"
            for i, n in enumerate(used, 1)
        ]
        body += "\n\n**Sources**\n" + "\n".join(lines)
    return body.strip()


def answer_events(
    ctx: AppContext, question: str, *, topic: str | None = None, provider: Any = None
) -> Iterator[tuple[str, str]]:
    """Yield ``(event, data)``: ``meta`` (what was searched), ``token`` chunks, then ``done`` (Markdown)."""
    cfg = ctx.cfg
    found = retrieve(ctx, question, topic=topic)
    scope = []
    if found.dates.label:
        scope.append(found.dates.label)
    if topic:
        scope.append(cfg.topic_label(topic))
    if found.ticker:
        scope.append(found.ticker)
    scope_txt = f" ({', '.join(scope)})" if scope else ""
    if not found.articles:
        msg = f"I couldn't find any collected articles about that{scope_txt}. Try different words or a wider date range."
        yield "meta", f"No matching articles{scope_txt}."
        yield "token", msg
        yield "done", msg
        return
    yield (
        "meta",
        (
            f"Found {len(found.articles)} relevant articles{scope_txt}"
            + (" · hybrid search" if found.used_embeddings else "")
        ),
    )

    provider = provider or get_provider(cfg, "ask")
    js = job_settings(cfg, "ask")
    max_tokens = js.max_tokens or 1500
    system_t, user_t = split_prompt(load_prompt(cfg, "ask"))
    budget = (
        provider.context_tokens - max_tokens - estimate_tokens(system_t + user_t + question) - 300
    )
    filters = f"Date range: {found.dates.label}" if found.dates.label else ""
    values = {
        "question": question,
        "filters": filters,
        "articles": _context_block(ctx, found.articles, max(1000, budget)),
    }
    parts: list[str] = []
    try:
        for chunk in provider.stream(
            render_prompt(system_t, **values),
            render_prompt(user_t, **values),
            max_tokens=max_tokens,
            temperature=js.temperature or 0.2,
        ):
            parts.append(chunk)
            yield "token", chunk
    except LLMError as exc:
        listing = "\n".join(
            f"{n}. [[{a['path']}|{a['title']}]] ({a['source']}){': ' + a['summary'] if a.get('summary') else ''}"
            for n, a in enumerate(found.articles, 1)
        )
        yield (
            "done",
            f"⚠️ The model isn't available ({exc}).\n\nThese collected articles match your question:\n\n{listing}",
        )
        return
    answer = "".join(parts).strip()
    final = cite(answer, found.articles)
    if cfg.qa.save_sessions:
        _save_session(ctx, question, final, found)
    yield "done", final


def answer_stream(ctx: AppContext, question: str, topic: str | None = None) -> Iterator[str]:
    """CLI helper: stream the answer text, then the source list."""
    final, streamed = "", False
    for event, data in answer_events(ctx, question, topic=topic):
        if event == "meta":
            yield f"({data})\n\n"
        elif event == "token":
            streamed = True
            yield data
        elif event == "error":
            yield f"\n{data}"
        elif event == "done":
            final = data
    from signal_news.notify import to_message_markdown

    final = to_message_markdown(final)  # wikilinks -> plain titles/links for the terminal
    if not streamed:
        yield final  # model unavailable or nothing found: show the full message
    elif "**Sources**" in final:
        yield "\n\n" + final[final.index("**Sources**") :].replace("**", "")


def _save_session(ctx: AppContext, question: str, answer: str, found: Retrieved) -> None:
    now = ctx.cfg.now().replace(microsecond=0)
    path = ctx.vault.qa_path(now, question)
    meta = {
        "asked": now.isoformat(),
        "question": question,
        "articles": len(found.articles),
        "tags": ["qa"],
    }
    if found.dates.label:
        meta["date_range"] = found.dates.label
    try:
        write_if_changed(path, render_document(meta, f"# {question}\n\n{answer}\n"))
    except OSError as exc:
        log.warning("Could not save Q&A session: %s", exc)


# --------------------------------------------------------------------------- embeddings


def embed_pending(ctx: AppContext, *, limit: int = 500, batch: int = 32) -> int:
    """Embed articles that don't have a vector yet. Returns how many were embedded."""
    if not ctx.cfg.qa.embeddings_enabled:
        return 0
    provider = get_provider(ctx.cfg, "embeddings")
    todo = ctx.db.articles_without_embedding(limit)
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        texts = [
            f"{a['title']}\n{a.get('summary') or ''}\n{plain_text(ctx.vault.article_body(a['path']))[:1500]}"
            for a in chunk
        ]
        try:
            vecs = provider.embed(texts)
        except LLMError as exc:
            log.warning("Embedding failed: %s", exc)
            break
        for a, v in zip(chunk, vecs, strict=False):
            arr = np.asarray(v, dtype=np.float32)
            ctx.db.store_embedding(a["id"], provider.model, arr.tobytes(), int(arr.shape[0]))
            done += 1
    return done
