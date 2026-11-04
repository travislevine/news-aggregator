"""Anthropic Messages API provider (plain HTTP, no SDK dependency)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from signal_news.llm.base import LLMError, LLMUnavailable

API_VERSION = "2023-06-01"


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str = "https://api.anthropic.com",
        context_tokens: int = 200000,
        timeout: float = 300.0,
        name: str = "anthropic",
        transport: httpx.BaseTransport | None = None,
    ):
        self.name = name
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.context_tokens = context_tokens
        self.timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.Client:
        if not self.api_key:
            raise LLMUnavailable("no Anthropic API key set (ANTHROPIC_API_KEY in .env)")
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            transport=self._transport,
        )

    def _payload(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

    def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise LLMUnavailable(f"Anthropic rejected the API key ({resp.status_code})")
        if resp.status_code in (429, 529) or resp.status_code >= 500:
            raise LLMUnavailable(f"Anthropic unavailable ({resp.status_code})")
        if resp.status_code >= 400:
            raise LLMError(f"Anthropic error {resp.status_code}: {resp.text[:300]}")

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
    ) -> str:
        if json_mode:
            system += "\n\nRespond with a single JSON object and nothing else."
        payload = self._payload(system, user, max_tokens, temperature)
        try:
            with self._client() as client:
                resp = client.post(f"{self.base_url}/v1/messages", json=payload)
                if resp.status_code == 400 and "temperature" in resp.text.lower():
                    payload.pop("temperature", None)  # some models only accept the default
                    resp = client.post(f"{self.base_url}/v1/messages", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable(f"Anthropic not reachable: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("Anthropic timed out") from exc
        self._check_status(resp)
        data = resp.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        if not text.strip():
            raise LLMError("Anthropic returned an empty response")
        return text.strip()

    def stream(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Iterator[str]:
        payload = {**self._payload(system, user, max_tokens, temperature), "stream": True}
        try:
            with (
                self._client() as client,
                client.stream("POST", f"{self.base_url}/v1/messages", json=payload) as resp,
            ):
                if resp.status_code >= 400:
                    resp.read()
                    self._check_status(resp)
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        text = (event.get("delta") or {}).get("text")
                        if text:
                            yield text
                    elif event.get("type") == "message_stop":
                        break
                    elif event.get("type") == "error":
                        raise LLMError(f"Anthropic stream error: {event.get('error')}")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable(f"Anthropic not reachable: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise LLMError(
            "Anthropic has no embeddings endpoint; use Ollama (nomic-embed-text) for embeddings"
        )

    def check(self) -> tuple[bool, str]:
        try:
            out = self.complete(
                "You are a test.", "Reply with the single word OK.", max_tokens=5, temperature=0
            )
        except LLMError as exc:
            return False, str(exc)
        return True, f"Anthropic OK ({self.model}): {out[:20]!r}"
