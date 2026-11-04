import json

import httpx
import pytest

from signal_news.llm.anthropic import AnthropicProvider
from signal_news.llm.base import (
    FallbackProvider,
    LLMError,
    LLMUnavailable,
    get_provider,
    parse_json_response,
    strip_thinking,
)
from signal_news.llm.ollama import OllamaProvider
from signal_news.llm.openai_compat import OpenAICompatProvider


class FakeProvider:
    """Deterministic provider for pipeline tests."""

    def __init__(self, reply=None, *, fail_ids=(), unavailable=False, name="fake", model="fake-1"):
        self.name, self.model, self.context_tokens = name, model, 16384
        self.reply = reply
        self.fail_ids = set(fail_ids)
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *, max_tokens, temperature, json_mode=False):
        self.calls.append((system, user))
        if self.unavailable:
            raise LLMUnavailable("down")
        if any(f in user for f in self.fail_ids):
            raise LLMError("boom")
        if callable(self.reply):
            return self.reply(system, user)
        return self.reply or json.dumps(
            {
                "summary": "A short factual summary.",
                "importance": 4,
                "sentiment": "bullish",
                "entities": ["Ivanti"],
            }
        )

    def stream(self, system, user, *, max_tokens, temperature):
        text = self.complete(system, user, max_tokens=max_tokens, temperature=temperature)
        for i in range(0, len(text), 7):
            yield text[i : i + 7]

    def embed(self, texts):
        return [[float(len(t) % 7), 1.0, 0.5] for t in texts]

    def check(self):
        return True, "ok"


def test_strip_thinking_and_parse_json():
    assert strip_thinking("<think>hmm</think>\nAnswer") == "Answer"
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('Sure! {"summary": "x"} hope that helps') == {"summary": "x"}
    with pytest.raises(LLMError):
        parse_json_response("no json here")


def test_ollama_sets_num_ctx_and_json_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"content": '<think>x</think>{"summary": "ok"}'}}
        )

    p = OllamaProvider(
        model="qwen3:14b", num_ctx=16384, think=False, transport=httpx.MockTransport(handler)
    )
    out = p.complete("sys", "user", max_tokens=100, temperature=0.1, json_mode=True)
    assert out == '{"summary": "ok"}'
    assert seen["path"] == "/api/chat"
    body = seen["body"]
    assert body["options"]["num_ctx"] == 16384 and body["options"]["num_predict"] == 100
    assert body["format"] == "json" and body["think"] is False and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_ollama_stream_and_embed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            n = len(json.loads(request.content)["input"])
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * n})
        lines = [
            {"message": {"content": "<think>"}, "done": False},
            {"message": {"content": "secret"}, "done": False},
            {"message": {"content": "</think>Hel"}, "done": False},
            {"message": {"content": "lo"}, "done": True},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    p = OllamaProvider(model="m", transport=httpx.MockTransport(handler))
    assert "".join(p.stream("s", "u", max_tokens=10, temperature=0)) == "Hello"
    assert p.embed(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]


def test_ollama_unreachable_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    p = OllamaProvider(model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMUnavailable):
        p.complete("s", "u", max_tokens=5, temperature=0)


def test_ollama_check_reports_missing_model():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})

    ok, msg = OllamaProvider(model="qwen3:14b", transport=httpx.MockTransport(handler)).check()
    assert not ok and "ollama pull qwen3:14b" in msg
    ok, _ = OllamaProvider(model="gemma3:12b", transport=httpx.MockTransport(handler)).check()
    assert ok


def test_openai_compat_and_temperature_retry():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["authorization"] == "Bearer k"
        if "temperature" in body:
            return httpx.Response(400, text="Unsupported value: 'temperature'")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    p = OpenAICompatProvider(
        model="m",
        base_url="https://api.example/v1",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    assert p.complete("s", "u", max_tokens=5, temperature=0.3) == "hi"
    assert len(calls) == 2 and "temperature" not in calls[1]


def test_openai_compat_without_key_is_unavailable():
    p = OpenAICompatProvider(model="m", base_url="https://api.example/v1", api_key=None)
    with pytest.raises(LLMUnavailable):
        p.complete("s", "u", max_tokens=5, temperature=0)


def test_anthropic_messages_api():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "k" and request.headers["anthropic-version"]
        assert body["system"] == "s" and body["messages"] == [{"role": "user", "content": "u"}]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "answer"}]})

    p = AnthropicProvider(model="m", api_key="k", transport=httpx.MockTransport(handler))
    assert p.complete("s", "u", max_tokens=5, temperature=0) == "answer"


def test_fallback_provider_switches_when_unreachable():
    primary = FakeProvider(unavailable=True, name="ollama")
    backup = FakeProvider(reply="from backup", name="api")
    fb = FallbackProvider(primary, backup)
    assert fb.complete("s", "u", max_tokens=5, temperature=0) == "from backup"
    assert fb.name == "api"


def test_get_provider_uses_job_config(cfg):
    p = get_provider(cfg, "article_summary")
    assert isinstance(p, OllamaProvider)
    assert p.model == "qwen3:14b" and p.num_ctx == 16384


def test_summarize_pipeline_with_mock(ctx, feed_routes):
    import asyncio

    from conftest import mock_transport

    from signal_news.collector.pipeline import collect_async
    from signal_news.storage.vault import read_document
    from signal_news.summarize.article import summarize_pending

    asyncio.run(collect_async(ctx.cfg, ctx.db, ctx.vault, transport=mock_transport(feed_routes)))
    total = ctx.db.count_needing_summary(3)
    assert total > 5
    fake = FakeProvider(fail_ids=["Ransomware gang claims"])
    res = summarize_pending(ctx, provider=fake)
    assert res.done == total - 1 and res.failed == 1 and not res.unreachable
    # front matter updated, body untouched
    art = ctx.db.query_articles(search="Ivanti")[0]
    meta, body = read_document(ctx.vault.abs(art["path"]))
    assert meta["summary"] == "A short factual summary." and meta["importance"] == 4
    assert meta.get("sentiment") is None  # cyber article: sentiment dropped
    assert "Connect Secure" in body
    # idempotent: second run only retries the failed one
    fake2 = FakeProvider()
    res2 = summarize_pending(ctx, provider=fake2)
    assert res2.done == 1 and len(fake2.calls) == 1


def test_summarize_stops_when_model_unreachable(ctx, feed_routes):
    import asyncio

    from conftest import mock_transport

    from signal_news.collector.pipeline import collect_async
    from signal_news.summarize.article import summarize_pending

    asyncio.run(collect_async(ctx.cfg, ctx.db, ctx.vault, transport=mock_transport(feed_routes)))
    down = FakeProvider(unavailable=True)
    res = summarize_pending(ctx, provider=down)
    assert res.unreachable and res.done == 0 and len(down.calls) == 1
    assert res.remaining == ctx.db.count_needing_summary(3) > 0
