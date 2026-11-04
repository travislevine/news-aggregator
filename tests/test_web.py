import asyncio
from datetime import timedelta

import pytest
from conftest import mock_transport
from fastapi.testclient import TestClient
from test_digest import _citing_reply
from test_llm import FakeProvider

from signal_news.collector.pipeline import collect_async
from signal_news.config import load_config, load_sources
from signal_news.summarize.article import summarize_pending
from signal_news.summarize.digest import generate_digest
from signal_news.web.app import create_app
from signal_news.web.render import render_markdown

HX = {"HX-Request": "true"}


@pytest.fixture
def loaded(ctx, feed_routes):
    asyncio.run(collect_async(ctx.cfg, ctx.db, ctx.vault, transport=mock_transport(feed_routes)))
    summarize_pending(ctx, provider=FakeProvider())
    generate_digest(
        ctx, provider=FakeProvider(reply=_citing_reply), now=ctx.cfg.now() + timedelta(minutes=1)
    )
    return ctx


@pytest.fixture
def client(loaded):
    app = create_app(loaded, start_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_pages_render(client, loaded):
    r = client.get("/")
    assert r.status_code == 200
    assert "top 5" in r.text and "🔥" in r.text
    assert "Flags" in r.text and "Collect now" in r.text
    assert "/articles/" in r.text  # wikilinks became app links
    for path in ("/digests", "/articles", "/sources", "/settings", "/ask"):
        assert client.get(path).status_code == 200, path
    art = loaded.db.query_articles(search="Ivanti")[0]
    r = client.get(f"/articles/{art['id']}")
    assert r.status_code == 200 and "Connect Secure" in r.text and "Open original" in r.text
    day = loaded.db.list_digests()[0]["date"]
    assert client.get(f"/digests/{day}").status_code == 200


def test_articles_filters_and_search(client):
    r = client.get("/articles", params={"q": "ransomware"}, headers=HX)
    assert r.status_code == 200 and "hospital" in r.text.lower() and "<html" not in r.text
    r = client.get("/articles", params={"kev": "1"})
    assert "Ivanti" in r.text and "Hospital" not in r.text
    r = client.get("/articles", params={"ticker": "NVDA"})
    assert "Nvidia" in r.text


def test_post_requires_htmx_header(client):
    assert client.post("/actions/collect").status_code == 403
    assert (
        client.post(
            "/settings/schedule", data={"collect_interval_minutes": 30, "digest_time": "11:00"}
        ).status_code
        == 403
    )


def test_rejects_foreign_host_header(client):
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 400


def test_password_required_when_exposed(loaded, monkeypatch):
    monkeypatch.delenv("SIGNAL_WEB_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        create_app(loaded, start_scheduler=False, host="0.0.0.0")
    monkeypatch.setenv("SIGNAL_WEB_PASSWORD", "s3cret")
    app = create_app(loaded, start_scheduler=False, host="0.0.0.0")
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("signal", "wrong")).status_code == 401
        assert c.get("/", auth=("signal", "s3cret")).status_code == 200


def test_settings_save_writes_config(client, loaded):
    r = client.post(
        "/settings/watchlist",
        headers=HX,
        data={"watchlist": "NVDA: Nvidia\nBHP.AX: BHP, BHP Group", "gear": "Proxmox\nOpenWrt"},
    )
    assert r.status_code == 200 and "Saved 2 tickers" in r.text
    cfg = load_config(loaded.cfg.config_file)
    assert [w.ticker for w in cfg.watchlist] == ["NVDA", "BHP.AX"] and cfg.gear == [
        "Proxmox",
        "OpenWrt",
    ]
    text = loaded.cfg.config_file.read_text()
    assert "# Signal configuration" in text  # comments preserved
    r = client.post(
        "/settings/schedule",
        headers=HX,
        data={"collect_interval_minutes": 30, "digest_time": "10:30"},
    )
    assert "digest at 10:30" in r.text
    assert load_config(loaded.cfg.config_file).schedule.digest_time == "10:30"
    r = client.post(
        "/settings/llm",
        headers=HX,
        data={"digest_provider": "anthropic", "digest_model": "some-model"},
    )
    cfg = load_config(loaded.cfg.config_file)
    assert (
        cfg.llm.jobs["digest"].provider == "anthropic"
        and cfg.llm.jobs["digest"].model == "some-model"
    )


def test_source_toggle(client, loaded):
    r = client.post("/sources/toggle", headers=HX, data={"name": "HA Blog", "enabled": "0"})
    assert r.status_code == 200 and "Enable" in r.text
    assert not next(s for s in load_sources(loaded.cfg) if s.name == "HA Blog").enabled


def test_job_status_partial(client):
    r = client.get("/actions/status", headers=HX)
    assert r.status_code == 200 and 'id="job-status"' in r.text


def test_ask_stream(client, monkeypatch):
    import signal_news.summarize.ask as ask

    monkeypatch.setattr(
        ask, "get_provider", lambda cfg, job: FakeProvider(reply="Ivanti's flaw is exploited [1].")
    )
    with client.stream(
        "GET", "/ask/stream", params={"q": "What happened with Ivanti this week?"}
    ) as r:
        body = "".join(r.iter_text())
    assert "event: meta" in body and "event: token" in body and "event: done" in body
    assert "/articles/" in body  # citation rendered as a link


def test_markdown_is_sanitised():
    html = str(
        render_markdown(
            'Hi <script>alert(1)</script> <a href="javascript:x">x</a> [ok](https://e.com)'
        )
    )
    assert "<script>" not in html and "javascript:" not in html
    assert 'target="_blank"' in html and "https://e.com" in html
