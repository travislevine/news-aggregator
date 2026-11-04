"""URL canonicalisation and near-duplicate (same story) clustering."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz import fuzz

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "yclid",
    "_hsenc",
    "_hsmi",
    "mkt_tok",
    "ref",
    "ref_src",
    "ref_url",
    "cmpid",
    "cmp",
    "ncid",
    "sr_share",
    "spm",
    "share",
    "smid",
    "taid",
    "guccounter",
    "guce_referrer",
    "guce_referrer_sig",
    "ito",
    "rss",
    "src",
    "via",
    "feature",
    "soc_src",
    "soc_trk",
    "outputtype",
    "amp",
    "__twitter_impression",
    "s_cid",
    "intcmp",
    "partner",
    "ns_mchannel",
    "ns_source",
    "ns_campaign",
    "ns_linkname",
    "ns_fee",
    "at_medium",
    "at_campaign",
    "trk",
    "trkcampaign",
}


def canonicalize_url(url: str) -> str:
    """Normalise a URL so the same article from different links compares equal.

    Lowercases scheme/host, drops ``www.``/``m.``/``amp.`` prefixes, tracking params
    (``utm_*``, ``fbclid`` ...), fragments, AMP path variants and trailing slashes.
    """
    url = url.strip()
    if not url:
        return url
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "amp.", "mobile."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix) :]
    port = parts.port
    netloc = host if not port or port in (80, 443) else f"{host}:{port}"

    path = parts.path or "/"
    path = re.sub(r"/+", "/", path)
    path = re.sub(r"(/amp)+/?$", "", path)  # .../story/amp
    path = re.sub(r"^/amp/", "/", path)  # /amp/story
    path = re.sub(r"/amp/(?=[^/])", "/", path)  # /news/amp/story
    path = re.sub(r"\.amp(\.html?)?$", r"\1", path)  # story.amp.html -> story.html
    if len(path) > 1:
        path = path.rstrip("/")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def article_id(canonical_url: str) -> str:
    """Stable 8-char id derived from the canonical URL."""
    return hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------- titles

_STOP = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "at",
    "by",
    "from",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "its",
    "it",
    "this",
    "that",
    "after",
    "over",
    "into",
    "new",
    "says",
    "say",
    "said",
    "report",
    "reports",
    "amid",
    "how",
    "why",
    "what",
    "will",
    "could",
    "may",
    "has",
    "have",
    "more",
    "than",
    "up",
    "out",
    "about",
    "you",
    "your",
}


def normalise_title(title: str) -> str:
    """Lowercase ASCII title with punctuation and outlet suffixes (" - Reuters") removed."""
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", t)  # trailing " - Outlet Name"
    t = re.sub(r"[^a-z0-9$ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_words(norm_title: str) -> set[str]:
    return {w for w in norm_title.split() if w not in _STOP and len(w) > 1}


def same_story(a_norm: str, b_norm: str, *, threshold: int = 85, min_shared: int = 3) -> bool:
    """Title-similarity test for "same story, different outlet"."""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    shared = title_words(a_norm) & title_words(b_norm)
    if len(shared) < min_shared:
        return False
    return (
        fuzz.token_set_ratio(a_norm, b_norm) >= threshold
        and fuzz.token_sort_ratio(a_norm, b_norm) >= threshold - 25
    )


def new_cluster_id(seed: str) -> str:
    return "c-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]


@dataclass
class ClusterCandidate:
    id: str
    title_norm: str
    cluster_id: str | None
    vector: list[float] | None = None


def cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def assign_cluster(
    title_norm: str,
    candidates: Iterable[ClusterCandidate],
    *,
    threshold: int = 85,
    min_shared: int = 3,
    vector: list[float] | None = None,
    embedding_threshold: float = 0.88,
) -> tuple[str | None, str | None]:
    """Find a matching recent article.

    Returns ``(cluster_id, matched_article_id)``. ``cluster_id`` is the match's
    existing cluster (may be None if the match had none yet). Both are None if
    nothing matched.
    """
    best: tuple[float, ClusterCandidate] | None = None
    for c in candidates:
        score = 0.0
        if same_story(title_norm, c.title_norm, threshold=threshold, min_shared=min_shared):
            score = fuzz.token_set_ratio(title_norm, c.title_norm) / 100
        elif vector is not None and c.vector is not None:
            sim = cosine(vector, c.vector)
            if sim >= embedding_threshold:
                score = sim
        if score and (best is None or score > best[0]):
            best = (score, c)
    if best is None:
        return None, None
    return best[1].cluster_id, best[1].id
