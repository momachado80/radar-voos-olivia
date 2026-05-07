"""Carrega configuração via variáveis de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    travelpayouts_token: str | None
    kiwi_api_key: str | None
    data_dir: Path

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "Config":
        root = repo_root or Path(__file__).resolve().parent.parent
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            travelpayouts_token=os.environ.get("TRAVELPAYOUTS_TOKEN"),
            kiwi_api_key=os.environ.get("KIWI_API_KEY"),
            data_dir=root / "data",
        )

    @property
    def history_path(self) -> Path:
        return self.data_dir / "price_history.json"

    @property
    def cycle_path(self) -> Path:
        return self.data_dir / "cycle.json"
