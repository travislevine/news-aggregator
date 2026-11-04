"""Rule-based tagging that runs before any model: subtopics, tickers, CVEs, vendors, gear."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from signal_news.config import Config, WatchItem

CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)

# Tickers that are ordinary English words (or very common acronyms). These only match
# in explicit forms such as $ALL or (NYSE: ALL), never as a bare word.
COMMON_WORD_TICKERS = {
    "A",
    "AI",
    "ALL",
    "AN",
    "ARE",
    "AS",
    "AT",
    "BE",
    "BIG",
    "BUY",
    "CAN",
    "CAR",
    "CAT",
    "CEO",
    "DO",
    "EAT",
    "EV",
    "FOR",
    "FUN",
    "GO",
    "GOOD",
    "HAS",
    "HE",
    "HUGE",
    "IT",
    "KEY",
    "LIVE",
    "LOVE",
    "LOW",
    "MAN",
    "ME",
    "NEW",
    "NOW",
    "NICE",
    "ON",
    "ONE",
    "OPEN",
    "OR",
    "OUT",
    "PAY",
    "PLAY",
    "REAL",
    "RUN",
    "SAFE",
    "SEE",
    "SO",
    "TECH",
    "TRUE",
    "TWO",
    "UP",
    "USA",
    "WELL",
    "WIN",
    "WORK",
    "YOU",
    "ZONE",
    "CASH",
    "BEST",
    "FAST",
    "GAIN",
    "HOPE",
    "LIFE",
    "MAIN",
    "MOVE",
    "NEXT",
    "POST",
    "RACE",
    "RENT",
    "SAVE",
    "SITE",
    "STAR",
    "TEAM",
    "TIME",
    "WAVE",
}
EXCHANGES = r"(?:NASDAQ|NYSE|NYSE\s?American|NYSEARCA|AMEX|ASX|TSX|LSE|OTC|OTCMKTS|CBOE|BATS)"


@dataclass
class Tags:
    subtopics: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    cves: list[str] = field(default_factory=list)
    vendors: list[str] = field(default_factory=list)
    gear_hits: list[str] = field(default_factory=list)

    @property
    def affects_me(self) -> bool:
        return bool(self.gear_hits)


def _phrase_re(phrase: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive pattern for a keyword or multi-word phrase."""
    esc = re.escape(phrase.strip()).replace(r"\ ", r"[\s\-]+")
    return re.compile(rf"(?<![\w-]){esc}(?![\w-])", re.IGNORECASE)


class Tagger:
    """Compiled tagging rules built from the config."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.subtopic_rules: dict[str, list[tuple[str, list[re.Pattern[str]]]]] = {}
        for topic, tcfg in cfg.topics.items():
            rules = []
            for sub, words in tcfg.subtopics.items():
                rules.append((sub, [_phrase_re(w) for w in words if w.strip()]))
            self.subtopic_rules[topic] = rules
        self.vendor_rules = [(v, _phrase_re(v)) for v in cfg.vendors if v.strip()]
        self.gear_rules = [(g, _phrase_re(g)) for g in cfg.gear if g.strip()]
        self.ticker_rules = [(w.ticker.upper(), self._ticker_patterns(w)) for w in cfg.watchlist]

    @staticmethod
    def _ticker_patterns(item: WatchItem) -> list[re.Pattern[str]]:
        sym = re.escape(item.base_symbol)
        full = re.escape(item.ticker.upper())
        pats = [
            re.compile(rf"\${sym}\b"),  # $NVDA
            re.compile(rf"\(\s*{EXCHANGES}\s*:\s*{sym}\s*\)", re.I),  # (NASDAQ: NVDA)
            re.compile(rf"\b{EXCHANGES}\s*:\s*{sym}\b", re.I),  # NASDAQ:NVDA
        ]
        if full != sym:
            pats.append(re.compile(rf"(?<![\w.]){full}(?![\w])"))  # BHP.AX
        base = item.base_symbol
        if len(base) >= 3 and base not in COMMON_WORD_TICKERS:
            pats.append(re.compile(rf"(?<![\w$.]){sym}(?![\w])"))  # bare NVDA (case-sensitive)
        for name in item.names:
            if name.strip():
                pats.append(_phrase_re(name))
        return pats

    def tag(self, topic: str, title: str, text: str, extra_topics: list[str] | None = None) -> Tags:
        """Tag an article. ``text`` should be the summary/body (title is searched too)."""
        haystack = f"{title}\n{text}"
        tags = Tags()

        for t in [topic, *(extra_topics or [])]:
            for sub, pats in self.subtopic_rules.get(t, []):
                if sub not in tags.subtopics and any(p.search(haystack) for p in pats):
                    tags.subtopics.append(sub)

        seen: set[str] = set()
        for m in CVE_RE.finditer(haystack):
            cve = m.group(0).upper()
            if cve not in seen:
                seen.add(cve)
                tags.cves.append(cve)

        for ticker, pats in self.ticker_rules:
            if any(p.search(haystack) for p in pats):
                tags.tickers.append(ticker)

        for vendor, pat in self.vendor_rules:
            if pat.search(haystack):
                tags.vendors.append(vendor)

        for gear, pat in self.gear_rules:
            if pat.search(haystack):
                tags.gear_hits.append(gear)
                if gear not in tags.vendors:
                    tags.vendors.append(gear)
        return tags


def matches_keyword_filter(keywords: list[str], title: str, text: str) -> bool:
    """True if any keyword appears (whole word, case-insensitive). An empty filter keeps everything."""
    if not keywords:
        return True
    hay = f"{title}\n{text}"
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        # Allow simple stems: "hack" matches "hacked", "hackers".
        pat = re.compile(rf"(?<![\w]){re.escape(k)}", re.IGNORECASE)
        if pat.search(hay):
            return True
    return False


def build_tags(topic: str, extra_topics: list[str], tags: Tags, kev: bool) -> list[str]:
    """Obsidian tags for the front matter."""
    out = [f"news/{topic}", *[f"news/{t}" for t in extra_topics]]
    out += [f"{topic}/{s}" for s in tags.subtopics]
    if tags.cves:
        out.append("cve")
    if kev:
        out.append("kev")
    out += [f"ticker/{t.replace('.', '-')}" for t in tags.tickers]
    if tags.affects_me:
        out.append("affects-me")
    return out
