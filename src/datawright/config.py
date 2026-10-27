from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

HOST = "127.0.0.1"  # the local UI never listens on other interfaces


class Settings(BaseSettings):
    """Read from `DATAWRIGHT_*` environment variables. API credentials are never stored here."""

    model_config = SettingsConfigDict(env_prefix="DATAWRIGHT_", extra="ignore")

    data_dir: Path = Path(".data")
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    fallbacks: bool = True
    max_turns: int = 30
    max_tokens: int = 16000
    port: int = 8765

    @property
    def db_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'datawright.db').resolve().as_posix()}"


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
EVALS_DIR = REPO_ROOT / "evals"
