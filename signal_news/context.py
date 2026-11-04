"""Shared application context: config, database, vault and logging."""

from __future__ import annotations

import logging
import logging.handlers
from dataclasses import dataclass
from pathlib import Path

from signal_news.config import Config, load_config
from signal_news.storage.db import Database
from signal_news.storage.vault import Vault

_LOGGING_READY = False


def setup_logging(logs_dir: Path, level: int = logging.INFO, console: bool = True) -> None:
    """Rotating file log in ``logs_dir`` plus optional console output. Safe to call twice."""
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(
        logs_dir / "signal.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        ch.setLevel(level)
        root.addHandler(ch)
    for noisy in ("httpx", "httpcore", "trafilatura", "readability", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _LOGGING_READY = True


@dataclass
class AppContext:
    cfg: Config
    db: Database
    vault: Vault

    @classmethod
    def load(cls, config_path: str | Path | None = None, *, console_log: bool = True) -> AppContext:
        cfg = load_config(config_path)
        setup_logging(cfg.logs, console=console_log)
        return cls(cfg=cfg, db=Database(cfg.db_path), vault=Vault(cfg.vault))

    def reload_config(self) -> None:
        path = self.cfg.config_file
        self.cfg = load_config(path)
