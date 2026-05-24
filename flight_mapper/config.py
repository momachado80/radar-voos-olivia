"""Carrega configuração via variáveis de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .currency import get_usd_brl_rate


@dataclass
class Config:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    travelpayouts_token: str | None
    kiwi_api_key: str | None
    data_dir: Path
    status_throttle_hours: int = 24
    # Câmbio USD→BRL obrigatório p/ converter preços Travelpayouts (USD).
    # None ⇒ alertas com preço USD são bloqueados (ver Monitor).
    usd_brl_rate: float | None = None

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "Config":
        root = repo_root or Path(__file__).resolve().parent.parent
        raw_throttle = os.environ.get("STATUS_REPORT_HOURS")
        try:
            throttle = int(raw_throttle) if raw_throttle else 24
        except ValueError:
            throttle = 24
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            travelpayouts_token=os.environ.get("TRAVELPAYOUTS_TOKEN"),
            kiwi_api_key=os.environ.get("KIWI_API_KEY"),
            data_dir=root / "data",
            status_throttle_hours=throttle,
            usd_brl_rate=get_usd_brl_rate(),
        )

    @property
    def history_path(self) -> Path:
        return self.data_dir / "price_history.json"

    @property
    def cycle_path(self) -> Path:
        return self.data_dir / "cycle.json"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def serpapi_validation_budget_path(self) -> Path:
        """Arquivo mínimo p/ orçamento diário da validação SerpApi.
        Armazena APENAS data UTC + contador. NUNCA token / URL /
        post_data / qualquer payload sensível."""
        return self.data_dir / "serpapi_validation_budget.json"
