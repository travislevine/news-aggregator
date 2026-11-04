"""OpenAI-compatible provider: OpenAI, OpenRouter, LM Studio, vLLM, llama.cpp server..."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from signal_news.llm.base import LLMError, LLMUnavailable, strip_thinking


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_tokens: int = 128000,
        timeout: float = 300.0,
        name: str = "openai_compat",
        extra_headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.context_tokens = context_tokens
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if "openrouter.ai" in self.base_url:
            h.setdefault("X-Title", "Signal news digest")
        return h

    def _client(self) -> httpx.Client:
        is_local = any(h in self.base_url for h in ("localhost", "127.0.0.1"))
        if not self.api_key and not is_local:
            raise LLMUnavailable(
                f"no API key set for provider {self.name!r} (check api_key_env and .env)"
            )
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            headers=self._headers(),
            transport=self._transport,
        )

    def _post(self, client: httpx.Client, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = client.post(f"{self.base_url}{path}", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable(f"{self.name} not reachable at {self.base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"{self.name} timed out") from exc
        if resp.status_code in (401, 403):
            raise LLMUnavailable(f"{self.name} rejected the API key ({resp.status_code})")
        if resp.status_code >= 500:
            raise LLMUnavailable(f"{self.name} server error {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMError(f"{self.name} error {resp.status_code}: {resp.text[:300]}")
        return resp

    def _post_adaptive(self, client: httpx.Client, payload: dict[str, Any]) -> httpx.Response:
        """POST, dropping options the endpoint rejects (JSON mode, custom temperature)."""
        for _ in range(3):
            try:
                return self._post(client, "/chat/completions", payload)
            except LLMError as exc:
                msg = str(exc).lower()
                if isinstance(exc, LLMUnavailable) or " 400" not in msg:
                    raise
                if "response_format" in payload and ("response_format" in msg or "json" in msg):
                    payload.pop("response_format")
                elif "temperature" in payload and "temperature" in msg:
                    payload.pop("temperature")
                elif "max_tokens" in payload and "max_completion_tokens" in msg:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                else:
                    raise
        return self._post(client, "/chat/completions", payload)

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        with self._client() as client:
            resp = self._post_adaptive(client, payload)
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response: {str(data)[:200]}") from exc
        if not content.strip():
            raise LLMError(f"{self.name} returned an empty response")
        return strip_thinking(content)

    def stream(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Iterator[str]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        try:
            with (
                self._client() as client,
                client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as resp,
            ):
                if resp.status_code >= 400:
                    resp.read()
                    raise LLMError(f"{self.name} error {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable(f"{self.name} not reachable: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._client() as client:
            data = self._post(client, "/embeddings", {"model": self.model, "input": texts}).json()
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in items]

    def check(self) -> tuple[bool, str]:
        try:
            out = self.complete(
                "You are a test.", "Reply with the single word OK.", max_tokens=5, temperature=0
            )
        except LLMError as exc:
            return False, str(exc)
        return True, f"{self.name} OK ({self.model}): {out[:20]!r}"
