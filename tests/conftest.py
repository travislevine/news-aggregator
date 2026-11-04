"""Shared test fixtures. Tests never touch the network."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from signal_news.config import load_config
from signal_news.context import AppContext
from signal_news.storage.db import Database
from signal_news.storage.vault import Vault

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temporary project folder with the example config and a small sources.yaml."""
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.example.yaml")
    (tmp_path / "sources.yaml").write_text(
        """sources:
  - name: Example Security
    url: https://www.example-security.com/feed/
    topic: cyber
    type: rss
    enabled: true
    fetch_full_text: true
    priority: 1
  - name: HA Blog
    url: https://www.home-assistant.io/atom.xml
    topic: iot
    type: rss
    enabled: true
    priority: 1
  - name: CISA KEV
    url: https://www.cisa.gov/kev.json
    topic: cyber
    type: json
    enabled: true
    priority: 1
  - name: HN Security
    url: https://hnrss.org/newest?q=security
    topic: cyber
    type: hnrss
    enabled: true
    priority: 2
  - name: Reuters (via Google News)
    topic: finance
    type: google_news
    query: "site:reuters.com markets when:1d"
    enabled: true
    priority: 1
  - name: Dead Feed
    url: https://dead.example.com/feed
    topic: iot
    type: rss
    enabled: true
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def cfg(project: Path):
    c = load_config(project / "config.yaml")
    c.collection.max_age_days = 36500
    c.collection.max_retries = 0
    c.collection.per_domain_delay_seconds = 0
    c.collection.respect_robots_txt = True
    c.watchlist_feeds.yahoo = False
    c.watchlist_feeds.google_news = False
    c.gear = ["Synology", "Home Assistant"]
    return c


@pytest.fixture
def ctx(cfg) -> AppContext:
    return AppContext(cfg=cfg, db=Database(cfg.db_path), vault=Vault(cfg.vault))


def mock_transport(
    routes: dict[str, tuple[int, bytes, dict[str, str]] | int],
) -> httpx.MockTransport:
    """Return an httpx transport that serves fixed responses by URL prefix."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix in sorted(routes, key=len, reverse=True):
            spec = routes[prefix]
            if url.startswith(prefix):
                if isinstance(spec, int):
                    return httpx.Response(spec)
                status, body, headers = spec
                return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def feed_routes() -> dict:
    return make_feed_routes()


def make_feed_routes() -> dict:
    """Fake responses for every URL the test sources use."""
    xml = {"content-type": "application/rss+xml"}
    return {
        "https://www.example-security.com/feed/": (
            200,
            fixture_bytes("rss_sample.xml"),
            {**xml, "etag": '"abc"'},
        ),
        "https://www.example-security.com/robots.txt": (
            200,
            b"User-agent: *\nDisallow: /private/\n",
            {},
        ),
        "https://www.example-security.com/news/hospital-ransomware": (
            200,
            fixture_bytes("article.html"),
            {"content-type": "text/html; charset=utf-8"},
        ),
        "https://www.home-assistant.io/atom.xml": (
            200,
            fixture_bytes("atom_sample.xml"),
            {"content-type": "application/atom+xml"},
        ),
        "https://www.cisa.gov/kev.json": (
            200,
            fixture_bytes("kev_sample.json"),
            {"content-type": "application/json"},
        ),
        "https://hnrss.org/": (200, fixture_bytes("hnrss.xml"), xml),
        "https://blog.example.org/robots.txt": 404,
        "https://blog.example.org/openssh-vuln": (
            200,
            fixture_bytes("article.html"),
            {"content-type": "text/html"},
        ),
        "https://news.google.com/rss/search": (200, fixture_bytes("google_news.xml"), xml),
        "https://news.google.com/rss/articles/AU_yqL": 404,
        "https://dead.example.com/": 500,
    }
