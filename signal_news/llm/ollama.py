"""Ollama provider using the native ``/api/chat`` and ``/api/embed`` endpoints.

The native API lets us set ``options.num_ctx``. Ollama's default context window is
small and silently truncates long prompts, so it is always set explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from signal_news.llm.base import LLMError, LLMUnavailable, strip_thinking


class OllamaProvider:
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        num_ctx: int = 16384,
        keep_alive: str | None = "10m",
        think: bool | None = None,
        timeout: float = 300.0,
        name: str = "ollama",
        transport: httpx.BaseTransport | None = None,
    ):
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.context_tokens = num_ctx
        self.keep_alive = keep_alive
        self.think = think
        self.timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=5.0), transport=self._transport
        )

    def _payload(
        self, system: str, user: str, max_tokens: int, temperature: float, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": stream,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        if self.think is not None:
            payload["think"] = self.think
        return payload

    def _post(self, client: httpx.Client, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = client.post(f"{self.base_url}{path}", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable(
                f"Ollama not reachable at {self.base_url} ({exc}). Is it running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"Ollama timed out after {self.timeout:.0f}s") from exc
        if resp.status_code == 404 and "not found" in resp.text.lower():
            raise LLMError(
                f"model {self.model!r} not found in Ollama. Run: ollama pull {self.model}"
            )
        if resp.status_code >= 400:
            raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        return resp

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
    ) -> str:
        payload = self._payload(system, user, max_tokens, temperature, stream=False)
        if json_mode:
            payload["format"] = "json"
        with self._client() as client:
            data = self._post(client, "/api/chat", payload).json()
        content = (data.get("message") or {}).get("content", "")
        if not content.strip():
            raise LLMError("Ollama returned an empty response")
        return strip_thinking(content)

    def stream(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Iterator[str]:
        payload = self._payload(system, user, max_tokens, temperature, stream=True)
        in_think = False
        try:
            with (
                self._client() as client,
                client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp,
            ):
                if resp.status_code >= 400:
                    resp.read()
                    raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise LLMError(f"Ollama: {chunk['error']}")
                    piece = (chunk.get("message") or {}).get("content", "")
                    if "<think>" in piece:
                        in_think = True
                    if in_think:
                        if "</think>" in piece:
                            in_think = False
                            piece = piece.split("</think>", 1)[1]
                        else:
                            continue
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable(
                f"Ollama not reachable at {self.base_url} ({exc}). Is it running?"
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self.model, "input": texts, "truncate": True}
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        with self._client() as client:
            data = self._post(client, "/api/embed", payload).json()
        vecs = data.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise LLMError("Ollama returned an unexpected embedding response")
        return vecs

    def check(self) -> tuple[bool, str]:
        """Is Ollama running, and is the model pulled?"""
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/api/tags")
        except httpx.HTTPError as exc:
            return False, f"Ollama not reachable at {self.base_url}: {exc}"
        if resp.status_code >= 400:
            return False, f"Ollama error {resp.status_code}"
        names = {m.get("name") for m in resp.json().get("models", [])}
        names |= {n.removesuffix(":latest") for n in names if n}
        if self.model in names or f"{self.model}:latest" in names:
            return True, f"Ollama OK, model {self.model} is available"
        return (
            False,
            f"Ollama is running but {self.model!r} isn't pulled. Run: ollama pull {self.model}",
        )
