"""Configuration loader — combines .env + config.yaml into one settings object."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"


class EnvSettings(BaseSettings):
    """Secrets and environment-specific values from .env."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'liquidity_hunter.db'}",
        alias="DATABASE_URL",
    )
    env: str = Field(default="development", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    binance_futures_base_url: str = Field(
        default="https://fapi.binance.com", alias="BINANCE_FUTURES_BASE_URL"
    )


def _load_yaml() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_FILE}")
    with open(CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)


class Config:
    """Singleton-like config holder. Access via `from src.core.config import settings`."""

    def __init__(self) -> None:
        self.env = EnvSettings()
        self._yaml = _load_yaml()

    def __getattr__(self, name: str) -> Any:
        # Allow dot-access to top-level yaml sections: settings.scanner, settings.decision_engine
        if name in self._yaml:
            return self._yaml[name]
        raise AttributeError(f"Config has no section '{name}'")

    def section(self, name: str) -> dict:
        return self._yaml.get(name, {})


settings = Config()
