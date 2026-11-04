"""Configuration loading and validation.

``config.yaml`` holds settings, ``sources.yaml`` holds feeds and ``.env`` holds
secrets. Relative paths in the config are resolved against the folder that
contains ``config.yaml`` (the project root).
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML

log = logging.getLogger(__name__)

TopicName = str
SourceType = Literal["rss", "json", "google_news", "hnrss"]


# --------------------------------------------------------------------------- models


class ScheduleConfig(BaseModel):
    collect_interval_minutes: int = 60
    digest_time: str = "11:00"
    catch_up_on_start: bool = True
    max_digest_gap_hours: int = 72
    summarize_after_collect: bool = True

    @field_validator("digest_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        datetime.strptime(v, "%H:%M")
        return v

    @property
    def digest_hour_minute(self) -> tuple[int, int]:
        hh, mm = self.digest_time.split(":")
        return int(hh), int(mm)


class GoogleNewsLocale(BaseModel):
    hl: str = "en-US"
    gl: str = "US"
    ceid: str = "US:en"


class CollectionConfig(BaseModel):
    user_agent: str = "SignalNewsBot/0.1 (personal use)"
    timeout_seconds: float = 20.0
    max_retries: int = 3
    per_domain_delay_seconds: float = 1.0
    max_concurrency: int = 10
    full_text_min_chars: int = 800
    respect_robots_txt: bool = True
    resolve_google_news: bool = True
    google_news: GoogleNewsLocale = Field(default_factory=GoogleNewsLocale)
    auto_disable_after_failures: int = 10
    max_items_per_feed: int = 60
    max_age_days: int = 7


class DedupeConfig(BaseModel):
    title_similarity: int = 85
    min_shared_words: int = 3
    window_hours: int = 48
    embedding_similarity: float = 0.88


class DigestSection(BaseModel):
    title: str
    subtopics: list[str] = Field(default_factory=list)
    watchlist: bool = False  # finance: articles that mention a watchlist ticker


class TopicConfig(BaseModel):
    label: str
    emoji: str = ""
    subtopics: dict[str, list[str]] = Field(default_factory=dict)
    sections: list[DigestSection] = Field(default_factory=list)


class WatchItem(BaseModel):
    ticker: str
    names: list[str] = Field(default_factory=list)

    @property
    def base_symbol(self) -> str:
        """Ticker without an exchange suffix, e.g. ``BHP`` for ``BHP.AX``."""
        return self.ticker.split(".")[0].upper()


class WatchlistFeeds(BaseModel):
    yahoo: bool = True
    google_news: bool = True


class LLMJob(BaseModel):
    provider: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class LLMConfig(BaseModel):
    default: str = "ollama"
    jobs: dict[str, LLMJob] = Field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fallback_provider: str | None = None
    fallback_model: str | None = None
    request_timeout_seconds: float = 300.0


class NotificationConfig(BaseModel):
    enabled: bool = True
    urls: list[str] = Field(default_factory=list)
    urls_env: str = "APPRISE_URLS"
    send_digest: bool = True
    instant_alerts: bool = True
    ui_base_url: str = "http://127.0.0.1:8765"
    max_message_chars: int = 1900


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class RetentionConfig(BaseModel):
    cleanup_after_days: int | None = None


class SummarizeConfig(BaseModel):
    batch_size: int = 25
    max_attempts: int = 3
    body_chars: int = 6000
    max_clusters_per_section: int = 25
    clusters_per_chunk: int = 40


class QAConfig(BaseModel):
    save_sessions: bool = True
    max_results: int = 12
    embeddings_enabled: bool = False


class Source(BaseModel):
    """One feed from ``sources.yaml``."""

    name: str
    url: str = ""
    topic: TopicName
    type: SourceType = "rss"
    enabled: bool = True
    fetch_full_text: bool = False
    paywalled: bool = False
    priority: int = 2
    notes: str = ""
    query: str | None = None  # google_news only
    locale: GoogleNewsLocale | None = None  # google_news only
    keyword_filter: list[str] = Field(default_factory=list)
    fallback_urls: list[str] = Field(default_factory=list)
    extra_topics: list[TopicName] = Field(default_factory=list)
    generated: bool = False  # built from the watchlist, not stored in sources.yaml

    @field_validator("priority")
    @classmethod
    def _priority_range(cls, v: int) -> int:
        return max(1, min(3, v))


class Config(BaseModel):
    vault_path: Path = Path("./vault")
    data_dir: Path = Path("./data")
    logs_dir: Path = Path("./logs")
    sources_file: Path = Path("./sources.yaml")
    prompts_dir: Path | None = None
    timezone: str = "Australia/Brisbane"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    topics: dict[TopicName, TopicConfig] = Field(default_factory=dict)
    watchlist: list[WatchItem] = Field(default_factory=list)
    watchlist_feeds: WatchlistFeeds = Field(default_factory=WatchlistFeeds)
    gear: list[str] = Field(default_factory=list)
    vendors: list[str] = Field(default_factory=list)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    qa: QAConfig = Field(default_factory=QAConfig)

    # Set by the loader, not by the YAML file.
    base_dir: Path = Path(".")
    config_file: Path | None = None

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    # -- helpers ---------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """Current time in the configured time zone."""
        return datetime.now(self.tz)

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    @property
    def vault(self) -> Path:
        return self.resolve(self.vault_path)

    @property
    def data(self) -> Path:
        return self.resolve(self.data_dir)

    @property
    def logs(self) -> Path:
        return self.resolve(self.logs_dir)

    @property
    def sources_path(self) -> Path:
        return self.resolve(self.sources_file)

    @property
    def db_path(self) -> Path:
        return self.data / "signal.db"

    def topic_label(self, topic: str) -> str:
        t = self.topics.get(topic)
        return t.label if t else topic


# --------------------------------------------------------------------------- loading

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)


def _to_plain(obj: Any) -> Any:
    """Convert ruamel round-trip containers into plain dicts/lists."""
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def read_yaml(path: Path) -> Any:
    """Read a YAML file with comments preserved (ruamel round-trip)."""
    with path.open("r", encoding="utf-8") as fh:
        return _yaml.load(fh)


def write_yaml(path: Path, data: Any) -> None:
    """Write YAML atomically, preserving comments where ``data`` came from ``read_yaml``."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        _yaml.dump(data, fh)
    os.replace(tmp, path)


def find_config_file(explicit: str | Path | None = None) -> Path:
    """Locate ``config.yaml``: explicit path, ``$SIGNAL_CONFIG``, then the current folder."""
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("SIGNAL_CONFIG")
    if env:
        return Path(env).resolve()
    return (Path.cwd() / "config.yaml").resolve()


def load_config(path: str | Path | None = None, *, create: bool = True) -> Config:
    """Load and validate the config.

    If ``config.yaml`` does not exist yet and ``config.example.yaml`` sits next to
    it, the example is copied first so a fresh checkout works out of the box.
    """
    cfg_path = find_config_file(path)
    base = cfg_path.parent
    load_dotenv(base / ".env", override=False)

    example = base / "config.example.yaml"
    if not cfg_path.exists() and create and example.exists():
        shutil.copyfile(example, cfg_path)
        log.info("Created %s from config.example.yaml", cfg_path)

    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = _to_plain(read_yaml(cfg_path)) or {}
    elif example.exists():
        raw = _to_plain(read_yaml(example)) or {}

    cfg = Config.model_validate(raw)
    cfg.base_dir = base
    cfg.config_file = cfg_path if cfg_path.exists() else None
    return cfg


def update_config_file(cfg: Config, updates: dict[str, Any]) -> None:
    """Write ``updates`` (dotted keys, e.g. ``schedule.digest_time``) into config.yaml.

    Comments and layout in the file are preserved.
    """
    path = cfg.config_file or (cfg.base_dir / "config.yaml")
    data = read_yaml(path) if path.exists() else {}
    for dotted, value in updates.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            if part not in node or node[part] is None:
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
    write_yaml(path, data)


# --------------------------------------------------------------------------- sources


def load_sources(cfg: Config) -> list[Source]:
    """Load ``sources.yaml``. Invalid entries are logged and skipped."""
    path = cfg.sources_path
    if not path.exists():
        log.warning("Sources file %s not found", path)
        return []
    data = _to_plain(read_yaml(path)) or {}
    items = data.get("sources", []) if isinstance(data, dict) else data
    out: list[Source] = []
    for raw in items or []:
        try:
            out.append(Source.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            log.error("Invalid source entry %r: %s", raw, exc)
    return out


def update_source_entry(cfg: Config, name: str, updates: dict[str, Any]) -> bool:
    """Change fields of one source in ``sources.yaml`` (comments preserved)."""
    path = cfg.sources_path
    data = read_yaml(path)
    items = data["sources"] if isinstance(data, dict) else data
    for entry in items:
        if entry.get("name") == name:
            for k, v in updates.items():
                entry[k] = v
            write_yaml(path, data)
            return True
    return False


def add_source_entry(cfg: Config, source: Source) -> None:
    """Append a new source to ``sources.yaml``."""
    path = cfg.sources_path
    data = read_yaml(path) if path.exists() else {"sources": []}
    items = data["sources"] if isinstance(data, dict) else data
    if any(e.get("name") == source.name for e in items):
        raise ValueError(f"A source named {source.name!r} already exists")
    entry = source.model_dump(exclude_defaults=True, exclude={"generated"})
    entry.setdefault("topic", source.topic)
    entry.setdefault("enabled", source.enabled)
    items.append(entry)
    write_yaml(path, data)


def env_secret(name: str | None) -> str | None:
    """Read a secret from the environment (``.env`` is loaded by ``load_config``)."""
    if not name:
        return None
    val = os.environ.get(name, "").strip()
    return val or None
