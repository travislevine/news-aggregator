"""Provider interface, errors, prompt loading and the per-job provider factory."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from string import Template
from typing import Any, Protocol, runtime_checkable

from signal_news.config import Config, LLMJob, env_secret

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

JOB_DEFAULTS: dict[str, dict[str, Any]] = {
    "article_summary": {"max_tokens": 400, "temperature": 0.2},
    "digest": {"max_tokens": 2500, "temperature": 0.3},
    "ask": {"max_tokens": 1500, "temperature": 0.2},
    "embeddings": {},
}


class LLMError(RuntimeError):
    """A model call failed (bad response, refused request...)."""


class LLMUnavailable(LLMError):
    """The provider can't be reached at all (not running, network down, no API key)."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    context_tokens: int

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
    ) -> str: ...

    def stream(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Iterator[str]: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def check(self) -> tuple[bool, str]: ...


# --------------------------------------------------------------------------- helpers


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove ``<think>...</think>`` blocks some reasoning models emit."""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # unterminated opening tag earlier in the stream
        text = text.split("</think>", 1)[1]
    return text.strip()


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate (~3.5 chars per token for English)."""
    return int(len(text) / 3.5) + 1


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = int(max_tokens * 3.5)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    return cut.rsplit(" ", 1)[0] + " …"


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model reply, tolerating code fences and chatter around it."""
    text = strip_thinking(text)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            val = json.loads(text[start : end + 1])
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not return valid JSON: {text[:200]!r}")


def load_prompt(cfg: Config, name: str) -> str:
    """Load ``<name>.md`` from the user's prompts_dir (if set) or the built-in prompts."""
    if cfg.prompts_dir:
        custom = cfg.resolve(cfg.prompts_dir) / f"{name}.md"
        if custom.exists():
            return custom.read_text(encoding="utf-8")
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render_prompt(template: str, **values: Any) -> str:
    """Fill ``$placeholders`` in a prompt (JSON braces in prompts stay untouched)."""
    return Template(template).safe_substitute({k: str(v) for k, v in values.items()})


def split_prompt(text: str) -> tuple[str, str]:
    """Prompt files hold a system part and a user part separated by a line ``---user---``."""
    if "---user---" in text:
        system, user = text.split("---user---", 1)
        return system.strip(), user.strip()
    return text.strip(), ""


# ---------------------------------------------------------------------------- factory


def _provider_type(name: str, conf: dict[str, Any]) -> str:
    t = conf.get("type") or name
    if t not in ("ollama", "openai_compat", "anthropic"):
        raise LLMError(f"unknown provider type {t!r} for provider {name!r}")
    return t


def build_provider(cfg: Config, provider_name: str, model: str) -> LLMProvider:
    """Instantiate a provider by its name in ``llm.providers``."""
    conf = dict(cfg.llm.providers.get(provider_name) or {})
    ptype = _provider_type(provider_name, conf)
    timeout = float(conf.get("timeout_seconds", cfg.llm.request_timeout_seconds))
    if ptype == "ollama":
        from signal_news.llm.ollama import OllamaProvider

        return OllamaProvider(
            model=model,
            base_url=conf.get("base_url", "http://localhost:11434"),
            num_ctx=int(conf.get("num_ctx", 16384)),
            keep_alive=conf.get("keep_alive", "10m"),
            think=conf.get("think"),
            timeout=timeout,
            name=provider_name,
        )
    api_key = env_secret(conf.get("api_key_env")) or conf.get("api_key")
    if ptype == "openai_compat":
        from signal_news.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            model=model,
            base_url=conf.get("base_url", "https://api.openai.com/v1"),
            api_key=api_key,
            context_tokens=int(conf.get("context_tokens", 128000)),
            timeout=timeout,
            name=provider_name,
            extra_headers=conf.get("headers") or {},
        )
    from signal_news.llm.anthropic import AnthropicProvider

    return AnthropicProvider(
        model=model,
        api_key=api_key,
        base_url=conf.get("base_url", "https://api.anthropic.com"),
        context_tokens=int(conf.get("context_tokens", 200000)),
        timeout=timeout,
        name=provider_name,
    )


class FallbackProvider:
    """Uses ``primary``; if it's unreachable, switches to ``fallback`` for the rest of the run."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider):
        self.primary, self.fallback = primary, fallback
        self._use_fallback = False

    @property
    def active(self) -> LLMProvider:
        return self.fallback if self._use_fallback else self.primary

    @property
    def name(self) -> str:
        return self.active.name

    @property
    def model(self) -> str:
        return self.active.model

    @property
    def context_tokens(self) -> int:
        return self.active.context_tokens

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self._use_fallback:
            try:
                return getattr(self.primary, method)(*args, **kwargs)
            except LLMUnavailable as exc:
                log.warning(
                    "%s unreachable (%s); falling back to %s",
                    self.primary.name,
                    exc,
                    self.fallback.name,
                )
                self._use_fallback = True
        return getattr(self.fallback, method)(*args, **kwargs)

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return self._call("complete", *args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        if not self._use_fallback:
            try:
                yield from self.primary.stream(*args, **kwargs)
                return
            except LLMUnavailable:
                self._use_fallback = True
        yield from self.fallback.stream(*args, **kwargs)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._call("embed", texts)

    def check(self) -> tuple[bool, str]:
        return self.primary.check()


def job_settings(cfg: Config, job: str) -> LLMJob:
    """Merged settings for a job: config values over built-in defaults."""
    conf = cfg.llm.jobs.get(job) or LLMJob()
    d = JOB_DEFAULTS.get(job, {})
    return LLMJob(
        provider=conf.provider or cfg.llm.default,
        model=conf.model,
        max_tokens=conf.max_tokens or d.get("max_tokens"),
        temperature=conf.temperature if conf.temperature is not None else d.get("temperature", 0.2),
    )


def get_provider(cfg: Config, job: str) -> LLMProvider:
    """Provider for a job (``article_summary``, ``digest``, ``ask``, ``embeddings``)."""
    js = job_settings(cfg, job)
    if not js.model:
        raise LLMError(f"no model configured for llm.jobs.{job}")
    primary = build_provider(cfg, js.provider or cfg.llm.default, js.model)
    if cfg.llm.fallback_provider and cfg.llm.fallback_provider != js.provider:
        fb_model = cfg.llm.fallback_model or js.model
        try:
            fallback = build_provider(cfg, cfg.llm.fallback_provider, fb_model)
            return FallbackProvider(primary, fallback)  # type: ignore[return-value]
        except LLMError as exc:
            log.warning("Fallback provider not usable: %s", exc)
    return primary


def describe(provider: LLMProvider) -> str:
    """Human label, e.g. ``qwen3:14b (ollama)``."""
    return f"{provider.model} ({provider.name})"
