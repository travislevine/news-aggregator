"""Daily digest (the "reduce" step): flags, per-topic sections and today's top 5."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from signal_news.context import AppContext
from signal_news.llm.base import (
    LLMError,
    LLMProvider,
    LLMUnavailable,
    describe,
    estimate_tokens,
    get_provider,
    job_settings,
    load_prompt,
    render_prompt,
    split_prompt,
)
from signal_news.storage.vault import render_document, write_if_changed
from signal_news.summarize.cluster import Cluster, assign_sections, build_clusters
from signal_news.timeutil import human_duration, local, parse_iso, to_utc_iso

__all__ = ["LLMUnavailable", "DigestResult", "compute_window", "generate_digest"]

log = logging.getLogger(__name__)

REF_RE = re.compile(r"\[(\d{1,4})\]")


@dataclass
class Window:
    start: datetime
    end: datetime
    capped: bool = False
    gap_hours: float = 0.0


@dataclass
class DigestResult:
    date: str
    path: Path
    window: Window
    article_count: int
    cluster_count: int
    seconds: float
    model: str
    markdown: str
    top5: str = ""
    flags: str = ""
    errors: list[str] = field(default_factory=list)


def compute_window(ctx: AppContext, now: datetime) -> Window:
    """Window = since the previous digest (not a fixed 24 h), capped at ``max_digest_gap_hours``.

    Regenerating today's digest reuses the previous day's end, so the window is stable.
    """
    today = now.strftime("%Y-%m-%d")
    prev = ctx.db.last_digest(before_date=today)
    if prev:
        start = parse_iso(prev["window_end"]) or now - timedelta(hours=24)
    else:
        start = now - timedelta(hours=24)
    start = start.astimezone(now.tzinfo)
    max_gap = timedelta(hours=ctx.cfg.schedule.max_digest_gap_hours)
    gap = now - start
    # Count the gap in calendar days so a normal weekend (Fri 11:00 -> Mon 13:00) isn't cut short.
    calendar_days = (now.date() - start.date()).days
    capped = gap > max_gap and calendar_days > max_gap.days
    if capped:
        start = now - max_gap
    return Window(start=start, end=now, capped=capped, gap_hours=gap.total_seconds() / 3600)


# --------------------------------------------------------------------------- references


class RefBook:
    """Numbers clusters for the prompts and turns ``[n]`` citations into links."""

    def __init__(self) -> None:
        self.by_num: dict[int, Cluster] = {}
        self.num_of: dict[str, int] = {}
        self.cited: set[int] = set()
        self.linked_ids: set[str] = set()

    def number(self, c: Cluster) -> int:
        if c.id not in self.num_of:
            n = len(self.by_num) + 1
            self.by_num[n] = c
            self.num_of[c.id] = n
        return self.num_of[c.id]

    def link(self, a: dict[str, Any]) -> str:
        self.linked_ids.add(a["id"])
        label = str(a.get("source", "source")).replace("|", "-").replace("]", ")")
        return f"[[{a['path']}|{label}]] [↗]({a['url']})"

    def links_for(self, n: int) -> str:
        c = self.by_num[n]
        self.cited.add(n)
        return self.link(c.rep)

    def render(self, text: str, allowed: set[int] | None = None) -> str:
        """Replace ``[n]`` with links; drop unknown numbers and bullets left without a citation."""
        out_lines = []
        for line in text.splitlines():
            nums = [int(m) for m in REF_RE.findall(line)]
            valid = [n for n in nums if n in self.by_num and (allowed is None or n in allowed)]
            stripped = line.strip()
            is_item = stripped.startswith(("- ", "* ")) or bool(re.match(r"^\d+\.\s", stripped))
            if is_item and not valid:
                continue  # uncited claim: drop it
            body = REF_RE.sub("", line).rstrip(" ,;")
            body = re.sub(r"\s+([.,;:])", r"\1", body).rstrip()
            if valid:
                uniq = list(dict.fromkeys(valid))
                body += " (" + " · ".join(self.links_for(n) for n in uniq) + ")"
            out_lines.append(body)
        return "\n".join(out_lines).strip()


def story_line(n: int, c: Cluster, tz) -> str:
    rep = c.rep
    when = local(rep.get("published") or rep.get("collected"), tz)
    bits = [f"[{n}] {rep['title']}", f"source: {rep['source']}"]
    if c.also_covered_by:
        bits.append("also: " + ", ".join(c.also_covered_by[:4]))
    if when:
        bits.append(when.strftime("%a %d %b %H:%M"))
    meta = []
    if c.tickers:
        meta.append("tickers " + ", ".join(c.tickers))
    if c.cves:
        meta.append(", ".join(c.cves[:4]))
    if c.kev:
        meta.append("actively exploited (CISA KEV)")
    if rep.get("sentiment"):
        meta.append(rep["sentiment"])
    meta.append(f"importance {c.importance}")
    line = " | ".join(bits) + "\n    " + (c.summary or "(no summary; headline only)")
    return line + "\n    (" + "; ".join(meta) + ")"


# ----------------------------------------------------------------------------- sections


def _fallback_bullets(clusters: list[Cluster], book: RefBook) -> str:
    lines = []
    for c in clusters:
        n = book.number(c)
        text = c.summary or ""
        lines.append(f"- **{c.rep['title']}**" + (f": {text}" if text else "") + f" [{n}]")
    return "\n".join(lines)


def _chunks(
    clusters: list[Cluster], size: int, provider: LLMProvider | None, tz, overhead: int
) -> list[list[Cluster]]:
    """Split clusters into chunks that fit the model's context window."""
    limit_tokens = (provider.context_tokens if provider else 16384) - overhead
    chunks: list[list[Cluster]] = []
    cur: list[Cluster] = []
    cur_tokens = 0
    for c in clusters:
        t = estimate_tokens(story_line(0, c, tz))
        if cur and (len(cur) >= size or cur_tokens + t > limit_tokens):
            chunks.append(cur)
            cur, cur_tokens = [], 0
        cur.append(c)
        cur_tokens += t
    if cur:
        chunks.append(cur)
    return chunks


class _Writer:
    """Calls the model for sections/top 5 with graceful per-section fallback."""

    def __init__(
        self, ctx: AppContext, provider: LLMProvider | None, book: RefBook, date_label: str
    ):
        self.ctx = ctx
        self.provider = provider
        self.book = book
        self.date_label = date_label
        self.errors: list[str] = []
        self.any_success = False
        js = job_settings(ctx.cfg, "digest")
        self.max_tokens = js.max_tokens or 2500
        self.temperature = js.temperature if js.temperature is not None else 0.3

    def _call(self, prompt_name: str, **values: Any) -> str:
        assert self.provider is not None
        system_t, user_t = split_prompt(load_prompt(self.ctx.cfg, prompt_name))
        return self.provider.complete(
            render_prompt(system_t, **values),
            render_prompt(user_t, **values),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

    def _guard(self, fn: Callable[[], str], fallback: Callable[[], str], what: str) -> str:
        if self.provider is None:
            return fallback()
        try:
            out = fn()
            self.any_success = True
            return out
        except LLMUnavailable:
            if not self.any_success:
                raise  # model down from the start: postpone the whole digest
            self.errors.append(f"{what}: model became unreachable")
        except LLMError as exc:
            self.errors.append(f"{what}: {exc}")
            log.warning("Digest %s failed, using fallback: %s", what, exc)
        return fallback()

    def section(self, title: str, topic_label: str, clusters: list[Cluster]) -> str:
        cfg = self.ctx.cfg
        clusters = clusters[: cfg.summarize.max_clusters_per_section]
        nums = {self.book.number(c) for c in clusters}

        def via_model() -> str:
            overhead = self.max_tokens + 800
            parts = []
            for chunk in _chunks(
                clusters, cfg.summarize.clusters_per_chunk, self.provider, cfg.tz, overhead
            ):
                stories = "\n".join(story_line(self.book.number(c), c, cfg.tz) for c in chunk)
                raw = self._call(
                    "digest_section",
                    section_title=title,
                    topic_label=topic_label,
                    date=self.date_label,
                    stories=stories,
                    max_bullets=max(3, min(10, len(chunk))),
                )
                parts.append(raw.strip())
            rendered = self.book.render("\n".join(parts), allowed=nums)
            if not rendered.strip():
                raise LLMError("no cited bullets in model output")
            return rendered

        def fallback() -> str:
            return self.book.render(_fallback_bullets(clusters[:12], self.book), allowed=nums)

        return self._guard(via_model, fallback, f"section {title}")

    def top5(self, clusters: list[Cluster]) -> str:
        pool = clusters[:30]
        nums = {self.book.number(c) for c in pool}

        def via_model() -> str:
            stories = "\n".join(story_line(self.book.number(c), c, self.ctx.cfg.tz) for c in pool)
            raw = self._call("top5", date=self.date_label, stories=stories)
            lines = [ln for ln in raw.splitlines() if re.match(r"^\s*\d+\.\s", ln)][:5]
            rendered = self.book.render("\n".join(lines), allowed=nums)
            if not rendered.strip():
                raise LLMError("no cited items in top-5 output")
            return rendered

        def fallback() -> str:
            lines = []
            for i, c in enumerate(pool[:5], 1):
                text = f": {c.summary}" if c.summary else ""
                lines.append(f"{i}. **{c.rep['title']}**{text} [{self.book.number(c)}]")
            return self.book.render("\n".join(lines), allowed=nums)

        return self._guard(via_model, fallback, "top 5")


# -------------------------------------------------------------------------------- flags


def build_flags(ctx: AppContext, clusters: list[Cluster], book: RefBook) -> str:
    """Rule-based flags: new KEV entries, articles about your gear, watchlist movers."""
    lines: list[str] = []
    all_articles = [a for c in clusters for a in c.articles]

    # -- actively exploited (KEV)
    kev_lines: list[str] = []
    seen_cves: set[str] = set()
    for a in sorted(all_articles, key=lambda a: a.get("collected", "")):
        if not a.get("kev_new"):
            continue
        for cve in a.get("cves") or []:
            if cve in seen_cves:
                continue
            seen_cves.add(cve)
            entry = ctx.db.get_kev(cve) or {}
            vendor = (
                f"{entry.get('vendorProject', '')} {entry.get('product', '')}".strip()
                or ", ".join(a.get("vendors") or [])
            )
            action = entry.get("requiredAction", "").strip()
            due = entry.get("dueDate")
            text = f"**{cve}**, {vendor}"
            if entry.get("vulnerabilityName"):
                text += f": {entry['vulnerabilityName']}"
            if action:
                text += f". What to do: {action}"
            if due:
                text += f" (US federal deadline {due})"
            kev_lines.append(f"    - {text} {book.link(a)}")
            break
    mentioned: list[str] = []
    for c in clusters:
        if c.kev and not any(a.get("kev_new") for a in c.articles):
            cves = [x for x in c.cves if x not in seen_cves and ctx.db.get_kev(x)]
            if cves:
                seen_cves.update(cves)
                mentioned.append(
                    f"    - {', '.join(cves)} in the news: {c.rep['title']} {book.link(c.rep)}"
                )
    if kev_lines or mentioned:
        lines.append("- **Actively exploited (KEV):**")
        lines += kev_lines + mentioned
    else:
        lines.append("- **Actively exploited (KEV):** nothing new in this window.")

    # -- affects your gear
    if ctx.cfg.gear:
        gear_lines = []
        for c in clusters:
            if c.affects_me:
                vendors = (
                    [v for v in (c.rep.get("vendors") or []) if v in ctx.cfg.gear]
                    or c.rep.get("vendors")
                    or []
                )
                label = ", ".join(vendors[:3]) or "Your gear"
                gear_lines.append(f"    - **{label}**: {c.rep['title']} {book.link(c.rep)}")
        if gear_lines:
            lines.append("- **Affects your gear:**")
            lines += gear_lines[:10]
        else:
            lines.append("- **Affects your gear:** nothing mentioned your gear.")

    # -- watchlist movers
    movers = []
    for w in ctx.cfg.watchlist:
        tick = w.ticker.upper()
        related = [c for c in clusters if tick in c.tickers]
        if not related:
            continue
        best = max(related, key=lambda c: c.score)
        reason = best.summary or best.rep["title"]
        senti = f" ({best.rep['sentiment']})" if best.rep.get("sentiment") else ""
        movers.append(f"    - **{tick}**{senti}: {reason} {book.link(best.rep)}")
    if movers:
        lines.append("- **Watchlist movers:**")
        lines += movers
    elif ctx.cfg.watchlist:
        lines.append("- **Watchlist movers:** no watchlist news in this window.")
    return "\n".join(lines)


def watchlist_bullets(ctx: AppContext, clusters: list[Cluster], book: RefBook) -> str:
    """Finance > Watchlist: up to three stories per ticker, rule-based (no invented numbers)."""
    lines = []
    for w in ctx.cfg.watchlist:
        tick = w.ticker.upper()
        related = [c for c in clusters if tick in c.tickers][:3]
        for c in related:
            text = c.summary or c.rep["title"]
            lines.append(f"- **{tick}**: {text} {book.link(c.rep)}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------- digest


def _sources_footer(ctx: AppContext, since_iso: str) -> str:
    from signal_news.collector.pipeline import all_sources

    states = ctx.db.all_feed_states()
    enabled = [s for s in all_sources(ctx.cfg) if s.enabled]
    failing = [
        s.name for s in enabled if (states.get(s.name) or {}).get("consecutive_failures", 0) > 0
    ]
    active = len(enabled) - len(failing)
    text = f"Sources: {active} active · {len(failing)} failing (see Sources page)"
    disabled = ctx.db.auto_disabled_since(since_iso)
    if disabled:
        text += " · **auto-disabled:** " + ", ".join(d["source"] for d in disabled)
    return text


def generate_digest(
    ctx: AppContext,
    *,
    use_llm: bool = True,
    progress: Callable[[str], None] | None = None,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
) -> DigestResult:
    """Build and write today's digest. Raises :class:`LLMUnavailable` if the model is down."""
    say = progress or (lambda m: None)
    t0 = time.monotonic()
    cfg = ctx.cfg
    now = (now or cfg.now()).replace(microsecond=0)
    day = now.strftime("%Y-%m-%d")
    win = compute_window(ctx, now)
    articles = ctx.db.articles_between(to_utc_iso(win.start), to_utc_iso(win.end))
    say(
        f"Building digest from {len(articles)} articles ({win.start:%a %d %b %H:%M} to {win.end:%a %d %b %H:%M})"
    )

    if use_llm and provider is None:
        provider = get_provider(cfg, "digest")
    if not use_llm:
        provider = None
    clusters = build_clusters(articles)
    book = RefBook()
    date_label = now.strftime("%A %d %B %Y")
    writer = _Writer(ctx, provider, book, date_label)

    # Top 5 first (it decides whether the model is reachable at all).
    say("Writing today's top 5")
    top5 = writer.top5(clusters) if clusters else "_No articles were collected in this window._"
    flags = build_flags(ctx, clusters, book)

    by_section = assign_sections(clusters, cfg.topics)
    body_parts: list[str] = []
    for topic, tcfg in cfg.topics.items():
        sections = by_section.get(topic, {})
        if not any(sections.values()):
            continue
        body_parts.append(f"## {tcfg.emoji + ' ' if tcfg.emoji else ''}{tcfg.label}")
        for sec in tcfg.sections:
            items = sections.get(sec.title) or []
            if not items:
                continue
            say(f"Writing {tcfg.label}: {sec.title} ({len(items)} stories)")
            if sec.watchlist:
                text = watchlist_bullets(ctx, items, book)
            else:
                text = writer.section(sec.title, tcfg.label, items)
            if text.strip():
                body_parts.append(f"### {sec.title}\n{text}")
        for (
            extra_title,
            items,
        ) in sections.items():  # clusters in topics without configured sections
            if items and extra_title not in {s.title for s in tcfg.sections}:
                body_parts.append(
                    f"### {extra_title}\n{writer.section(extra_title, tcfg.label, items)}"
                )

    seconds = time.monotonic() - t0
    model = describe(provider) if provider else "none (headlines only)"
    notes = []
    if win.capped:
        notes.append(
            f"The last digest was {win.gap_hours / 24:.1f} days ago, so this digest only covers the last "
            f"{cfg.schedule.max_digest_gap_hours} hours."
        )
    if writer.errors:
        notes.append(
            f"{len(writer.errors)} section(s) used plain headlines because the model output failed."
        )
    footer = f"*{_sources_footer(ctx, to_utc_iso(win.start))} · {len(articles)} articles · generated in {human_duration(seconds)}*"

    title = f"# Daily Digest — {now:%a} {now.day} {now:%b %Y}"
    parts = [title, ""]
    if notes:
        parts += [f"> {n}" for n in notes] + [""]
    parts += ["## 🔥 Today's top 5", top5, "", "## 🚨 Flags", flags, ""]
    parts += ["\n\n".join(body_parts), "", "---", footer]
    body = "\n".join(parts)

    meta: dict[str, Any] = {
        "date": day,
        "window_start": win.start.isoformat(),
        "window_end": win.end.isoformat(),
        "articles": len(articles),
        "clusters": len(clusters),
        "model": model,
        "tags": ["digest"],
    }
    if win.capped:
        meta["capped"] = True
    path = ctx.vault.digest_path(day)
    markdown = render_document(
        meta, body, ["date", "window_start", "window_end", "articles", "clusters", "model", "tags"]
    )
    write_if_changed(path, markdown)
    ctx.db.record_digest(
        day, ctx.vault.rel(path), to_utc_iso(win.start), to_utc_iso(win.end), len(articles), model
    )
    in_digest = set(book.linked_ids)
    for n in book.cited:
        in_digest.update(a["id"] for a in book.by_num[n].articles)
    ctx.db.mark_in_digest(sorted(in_digest), day)
    return DigestResult(
        date=day,
        path=path,
        window=win,
        article_count=len(articles),
        cluster_count=len(clusters),
        seconds=seconds,
        model=model,
        markdown=markdown,
        top5=top5,
        flags=flags,
        errors=writer.errors,
    )
