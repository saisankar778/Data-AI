from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

HOST = "127.0.0.1"  # the local UI never listens on other interfaces


def default_data_dir() -> Path:
    """Per-user data folder: the CLI, UI and MCP server share one database wherever they start."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Datawright"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "datawright"


class Settings(BaseSettings):
    """Read from `DATAWRIGHT_*` environment variables. API credentials are never stored here."""

    model_config = SettingsConfigDict(env_prefix="DATAWRIGHT_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    fallbacks: bool = True
    max_turns: int = 30
    max_tokens: int = 16000
    port: int = 8765

    @property
    def db_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'datawright.db').resolve().as_posix()}"


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path: Path) -> list[str]:
    """Load KEY=VALUE lines into the environment without overriding what's already set.
    Returns the names it set (never the values, which may be secrets)."""
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_env() -> list[str]:
    """`.env` in the current folder, then `.env` in the data folder."""
    loaded = load_env_file(Path.cwd() / ".env")
    loaded += load_env_file(Settings().data_dir / ".env")
    return loaded


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
EVALS_DIR = REPO_ROOT / "evals"
