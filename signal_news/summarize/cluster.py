"""Group same-story articles and place clusters into digest sections."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from signal_news.config import TopicConfig


@dataclass
class Cluster:
    id: str
    articles: list[dict[str, Any]]
    rep: dict[str, Any] = field(default_factory=dict)

    @property
    def topic(self) -> str:
        return self.rep["topic"]

    @property
    def others(self) -> list[dict[str, Any]]:
        return [a for a in self.articles if a["id"] != self.rep["id"]]

    @property
    def also_covered_by(self) -> list[str]:
        seen: list[str] = []
        for a in self.others:
            if a["source"] != self.rep["source"] and a["source"] not in seen:
                seen.append(a["source"])
        return seen

    @property
    def importance(self) -> int:
        vals = [a.get("importance") or 0 for a in self.articles]
        return max(vals) if any(vals) else 2

    @property
    def subtopics(self) -> Counter[str]:
        c: Counter[str] = Counter()
        for a in self.articles:
            c.update(a.get("subtopics") or [])
        return c

    @property
    def tickers(self) -> list[str]:
        out: list[str] = []
        for a in self.articles:
            for t in a.get("tickers") or []:
                if t not in out:
                    out.append(t)
        return out

    @property
    def cves(self) -> list[str]:
        out: list[str] = []
        for a in self.articles:
            for c in a.get("cves") or []:
                if c not in out:
                    out.append(c)
        return out

    @property
    def kev(self) -> bool:
        return any(a.get("kev") for a in self.articles)

    @property
    def affects_me(self) -> bool:
        return any(a.get("affects_me") for a in self.articles)

    @property
    def summary(self) -> str:
        return self.rep.get("summary") or next(
            (a["summary"] for a in self.articles if a.get("summary")), ""
        )

    @property
    def score(self) -> float:
        """Ranking score: model importance, boosted for KEV/gear and broad coverage."""
        s = float(self.importance)
        if self.kev:
            s += 1.0
        if self.affects_me:
            s += 0.5
        s += 0.4 * math.log(len({a["source"] for a in self.articles}))
        s -= 0.15 * (self.rep.get("priority", 2) - 1)
        return s


def _rep_key(a: dict[str, Any]) -> tuple:
    return (
        a.get("priority", 2),
        0 if a.get("full_text") else 1,
        0 if a.get("summary") else 1,
        -(a.get("importance") or 0),
        1 if str(a.get("url", "")).startswith("https://news.google.com") else 0,
        a.get("collected", ""),
    )


def build_clusters(articles: list[dict[str, Any]]) -> list[Cluster]:
    """Group articles by ``cluster_id`` and pick the best-sourced representative of each."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for a in articles:
        groups.setdefault(a.get("cluster_id") or f"solo-{a['id']}", []).append(a)
    clusters = []
    for cid, arts in groups.items():
        rep = sorted(arts, key=_rep_key)[0]
        clusters.append(Cluster(id=cid, articles=arts, rep=rep))
    clusters.sort(key=lambda c: c.score, reverse=True)
    return clusters


def assign_sections(
    clusters: list[Cluster], topics: dict[str, TopicConfig]
) -> dict[str, dict[str, list[Cluster]]]:
    """Map ``topic -> section title -> clusters`` using each topic's configured sections."""
    out: dict[str, dict[str, list[Cluster]]] = {
        t: {s.title: [] for s in cfg.sections} for t, cfg in topics.items()
    }
    for c in clusters:
        tcfg = topics.get(c.topic)
        if tcfg is None or not tcfg.sections:
            out.setdefault(c.topic, {}).setdefault("News", []).append(c)
            continue
        chosen = None
        watch = next((s for s in tcfg.sections if s.watchlist), None)
        if watch and c.tickers:
            chosen = watch.title
        else:
            subs = c.subtopics
            best_score = 0
            for s in tcfg.sections:
                if s.watchlist or not s.subtopics:
                    continue
                score = sum(subs.get(x, 0) for x in s.subtopics)
                if score > best_score:
                    best_score, chosen = score, s.title
            if chosen is None:
                catch_all = next(
                    (s for s in tcfg.sections if not s.subtopics and not s.watchlist), None
                )
                first = next((s for s in tcfg.sections if not s.watchlist), tcfg.sections[0])
                chosen = (catch_all or first).title
        out[c.topic][chosen].append(c)
    return out
