"""Per-article summaries (the "map" step). Runs incrementally after each collection."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from signal_news.collector.extract import plain_text
from signal_news.context import AppContext
from signal_news.llm.base import (
    LLMError,
    LLMProvider,
    LLMUnavailable,
    estimate_tokens,
    get_provider,
    job_settings,
    load_prompt,
    parse_json_response,
    render_prompt,
    split_prompt,
    truncate_to_tokens,
)
from signal_news.storage.vault import update_front_matter
from signal_news.timeutil import local

log = logging.getLogger(__name__)

SENTIMENTS = {"bullish", "bearish", "neutral"}


@dataclass
class SummarizeResult:
    done: int = 0
    failed: int = 0
    remaining: int = 0
    unreachable: bool = False
    errors: list[str] = field(default_factory=list)


def _clean(data: dict[str, Any], topic: str) -> dict[str, Any]:
    """Validate the model's JSON into the fields we store."""
    summary = str(data.get("summary") or "").strip().replace("\n", " ")
    if not summary:
        raise LLMError("empty summary")
    try:
        importance = int(data.get("importance") or 3)
    except (TypeError, ValueError):
        importance = 3
    importance = max(1, min(5, importance))
    sentiment = data.get("sentiment")
    sentiment = str(sentiment).lower().strip() if sentiment else None
    if topic != "finance" or sentiment not in SENTIMENTS:
        sentiment = None
    ents = data.get("entities") or []
    if not isinstance(ents, list):
        ents = [ents]
    entities = [str(e).strip() for e in ents if str(e).strip()][:8]
    return {
        "summary": summary[:600],
        "importance": importance,
        "sentiment": sentiment,
        "entities": entities,
    }


def summarize_article(
    ctx: AppContext, provider: LLMProvider, art: dict[str, Any]
) -> dict[str, Any]:
    """Summarise one article and return the cleaned fields. Raises LLMError on failure."""
    cfg = ctx.cfg
    js = job_settings(cfg, "article_summary")
    system_t, user_t = split_prompt(load_prompt(cfg, "article_summary"))
    body = plain_text(ctx.vault.article_body(art["path"]))[: cfg.summarize.body_chars]
    max_tokens = js.max_tokens or 400
    budget = provider.context_tokens - max_tokens - estimate_tokens(system_t + user_t) - 200
    body = truncate_to_tokens(body, max(300, budget))
    published = local(art.get("published") or art.get("collected"), cfg.tz)
    values = {
        "topic_label": cfg.topic_label(art["topic"]),
        "source": art.get("source", ""),
        "title": art.get("title", ""),
        "published": published.strftime("%Y-%m-%d %H:%M") if published else "unknown",
        "body": body or "(no text available; summarise the title only)",
    }
    system = render_prompt(system_t, **values)
    user = render_prompt(user_t, **values)
    raw = provider.complete(
        system, user, max_tokens=max_tokens, temperature=js.temperature or 0.2, json_mode=True
    )
    return _clean(parse_json_response(raw), art["topic"])


def summarize_pending(
    ctx: AppContext,
    *,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
    provider: LLMProvider | None = None,
) -> SummarizeResult:
    """Summarise articles without a summary, in batches. Never raises for per-article failures."""
    say = progress or (lambda m: None)
    cfg = ctx.cfg
    res = SummarizeResult()
    max_attempts = cfg.summarize.max_attempts
    pending_total = ctx.db.count_needing_summary(max_attempts)
    if pending_total == 0:
        return res
    target = min(pending_total, limit) if limit else pending_total
    try:
        provider = provider or get_provider(cfg, "article_summary")
    except LLMError as exc:
        res.unreachable, res.remaining = True, pending_total
        res.errors.append(str(exc))
        return res

    say(f"Summarising {target} articles with {provider.model}")
    processed = 0
    tried: set[str] = set()
    while processed < target:
        size = min(cfg.summarize.batch_size, target - processed)
        batch = [
            a
            for a in ctx.db.articles_needing_summary(size + len(tried), max_attempts)
            if a["id"] not in tried
        ][:size]
        if not batch:
            break
        for art in batch:
            tried.add(art["id"])
            processed += 1
            try:
                fields = summarize_article(ctx, provider, art)
            except LLMUnavailable as exc:
                log.warning("Model unreachable, stopping summaries: %s", exc)
                res.unreachable = True
                res.errors.append(str(exc))
                res.remaining = ctx.db.count_needing_summary(max_attempts)
                return res
            except Exception as exc:  # noqa: BLE001
                res.failed += 1
                msg = f"{art['id']} ({art['title'][:60]}): {exc}"
                res.errors.append(msg)
                log.warning("Summary failed for %s", msg)
                ctx.db.update_article(
                    art["id"],
                    summary_attempts=(art.get("summary_attempts") or 0) + 1,
                    summary_error=str(exc)[:300],
                )
                continue
            path = ctx.vault.abs(art["path"])
            if path.exists():
                update_front_matter(path, fields)
            ctx.db.update_article(art["id"], **fields, summary_error=None)
            res.done += 1
            if res.done % 10 == 0:
                say(f"Summarised {res.done}/{target}")
    res.remaining = ctx.db.count_needing_summary(max_attempts)
    return res
