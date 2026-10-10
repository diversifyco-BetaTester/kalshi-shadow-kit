"""
config.py — settings from .env (python-dotenv). Only what a read-only client needs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

HOSTS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


@dataclass(frozen=True)
class Settings:
    env: str
    api_key_id: str
    private_key_path: Path
    rest_base: str
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @property
    def rest_path_prefix(self) -> str:
        """Path part of the base URL ("/trade-api/v2") — it is part of what Kalshi signs."""
        return urlparse(self.rest_base).path.rstrip("/")


def load_settings(env_file: str | os.PathLike | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)
    env = os.getenv("KALSHI_ENV", "demo").strip().lower()
    if env not in HOSTS:
        raise ValueError(f"KALSHI_ENV must be one of {sorted(HOSTS)}, got {env!r}")
    return Settings(
        env=env,
        api_key_id=os.getenv("KALSHI_API_KEY_ID", "").strip(),
        private_key_path=Path(os.getenv("KALSHI_PRIVATE_KEY_PATH", "")).expanduser(),
        rest_base=HOSTS[env],
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    )
