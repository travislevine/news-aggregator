from datetime import datetime
from zoneinfo import ZoneInfo

from signal_news.storage.vault import (
    ARTICLE_KEYS,
    Vault,
    read_document,
    render_document,
    slugify,
    split_front_matter,
    update_front_matter,
)

BNE = ZoneInfo("Australia/Brisbane")


def test_slugify_is_windows_safe_and_short():
    s = slugify(
        'Ivanti: zero-day "exploited" in the wild? <yes> * | / \\ — with ünïcödé and a very long tail of words'
    )
    assert len(s) <= 60
    assert not any(ch in s for ch in '<>:"/\\|?*')
    assert s.startswith("ivanti-zero-day-exploited-in-the-wild-yes")
    assert slugify("!!!") == "untitled"


def test_front_matter_round_trip(tmp_path):
    meta = {
        "id": "a1b2c3d4",
        "title": 'Ivanti: "zero-day" exploited',
        "published": "2026-11-04T02:15:00+10:00",
        "subtopics": ["vulnerability", "exploited"],
        "tickers": [],
        "kev": True,
        "summary": "",
        "url": "https://example.com/a?b=1&c=2",
    }
    doc = render_document(meta, "# Title\n\nBody text with --- dashes\n", ARTICLE_KEYS)
    assert "published: 2026-11-04T02:15:00+10:00" in doc
    assert "subtopics: [vulnerability, exploited]" in doc
    back, body = split_front_matter(doc)
    assert back["title"] == meta["title"]
    assert back["published"] == "2026-11-04T02:15:00+10:00"
    assert back["kev"] is True and back["tickers"] == []
    assert body.strip().startswith("# Title")
    assert "--- dashes" in body
    # keys follow ARTICLE_KEYS order
    assert doc.index("id:") < doc.index("title:") < doc.index("url:") < doc.index("published:")


def test_update_front_matter_keeps_body(tmp_path):
    p = tmp_path / "a.md"
    body = "\n# Hello\n\nLine one\n\n  indented  \n"
    p.write_text("---\nid: x\nsummary: ''\n---\n" + body, encoding="utf-8")
    assert update_front_matter(p, {"summary": "Short summary.", "importance": 4})
    text = p.read_text(encoding="utf-8")
    assert text.endswith(body)
    meta, _ = read_document(p)
    assert meta["summary"] == "Short summary." and meta["importance"] == 4
    # no change -> no write
    mtime = p.stat().st_mtime_ns
    assert not update_front_matter(p, {"summary": "Short summary."})
    assert p.stat().st_mtime_ns == mtime


def test_article_path_layout(tmp_path):
    v = Vault(tmp_path)
    p = v.article_path(
        "a1b2c3d4",
        "Ivanti zero-day exploited in the wild",
        "cyber",
        datetime(2026, 11, 4, 10, 2, tzinfo=BNE),
    )
    assert (
        v.rel(p)
        == "articles/2026/11/04/cyber/2026-11-04-ivanti-zero-day-exploited-in-the-wild-a1b2c3"
    )


def test_write_article_is_idempotent(tmp_path):
    v = Vault(tmp_path)
    p = v.article_path("abcdef12", "Hello", "iot", datetime(2026, 11, 4, tzinfo=BNE))
    meta = {
        "id": "abcdef12",
        "title": "Hello",
        "url": "https://x.y/",
        "source": "X",
        "collected": "2026-11-04T10:00:00+10:00",
    }
    assert v.write_article(meta, "Body", p)
    assert not v.write_article(meta, "Body", p)
