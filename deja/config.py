"""Paths and user configuration (TOML, all optional)."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env) or Path.home() / fallback)


def data_dir() -> Path:
    d = Path(os.environ.get("DEJA_DATA_DIR") or _xdg("XDG_DATA_HOME", ".local/share") / "deja")
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    d = Path(os.environ.get("DEJA_STATE_DIR") or _xdg("XDG_STATE_HOME", ".local/state") / "deja")
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return Path(os.environ.get("DEJA_DB") or data_dir() / "deja.db")


def config_path() -> Path:
    return Path(os.environ.get("DEJA_CONFIG")
                or _xdg("XDG_CONFIG_HOME", ".config") / "deja" / "config.toml")


def paused_path() -> Path:
    return state_dir() / "paused"


@dataclass
class Config:
    max_entries: int = 2000        # oldest unpinned entries pruned past this
    max_bytes: int = 256 * 1024    # skip huge clipboard payloads
    min_chars: int = 2             # skip single characters
    ignore_patterns: list[str] = field(default_factory=list)  # regexes to skip

    _compiled: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._compiled = []
        for pat in self.ignore_patterns:
            try:
                self._compiled.append(re.compile(pat))
            except re.error:
                pass  # a bad regex shouldn't take the daemon down

    def ignores(self, text: str) -> bool:
        return any(rx.search(text) for rx in self._compiled)


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return Config()
    known = {k: raw[k] for k in ("max_entries", "max_bytes", "min_chars",
                                 "ignore_patterns") if k in raw}
    return Config(**known)
