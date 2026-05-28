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
    # Duffel (read-only confirmed business offer source). Desligado por
    # default: só roda se DUFFEL_PROVIDER_ENABLED=true E token presente.
    # Nunca cria order/payment — apenas Offer Requests (order_flow).
    duffel_provider_enabled: bool = False
    duffel_access_token: str | None = None
    duffel_max_requests_per_cycle: int = 1

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> "Config":
        root = repo_root or Path(__file__).resolve().parent.parent
        raw_throttle = os.environ.get("STATUS_REPORT_HOURS")
        try:
            throttle = int(raw_throttle) if raw_throttle else 24
        except ValueError:
            throttle = 24
        raw_duffel_cap = os.environ.get("DUFFEL_MAX_REQUESTS_PER_CYCLE")
        try:
            duffel_cap = int(raw_duffel_cap) if raw_duffel_cap else 1
        except ValueError:
            duffel_cap = 1
        duffel_cap = max(0, duffel_cap)
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            travelpayouts_token=os.environ.get("TRAVELPAYOUTS_TOKEN"),
            kiwi_api_key=os.environ.get("KIWI_API_KEY"),
            data_dir=root / "data",
            status_throttle_hours=throttle,
            usd_brl_rate=get_usd_brl_rate(),
            duffel_provider_enabled=(
                os.environ.get("DUFFEL_PROVIDER_ENABLED", "false").strip().lower()
                == "true"
            ),
            duffel_access_token=os.environ.get("DUFFEL_ACCESS_TOKEN"),
            duffel_max_requests_per_cycle=duffel_cap,
        )

    @property
    def history_path(self) -> Path:
        return self.data_dir / "price_history.json"

    @property
    def duffel_history_path(self) -> Path:
        """Histórico/dedup ISOLADO do provider Duffel. Mantido fora de
        `price_history.json` para NUNCA poluir os painéis de status/ciclo
        (que iteram o store principal). Armazena só preços BRL + timestamp
        de alerta. NUNCA token / offer_id / payload / passageiro."""
        return self.data_dir / "duffel_history.json"

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

    @property
    def cycle_snapshot_path(self) -> Path:
        """Snapshot mínimo do ciclo anterior p/ detecção de mudança.
        Armazena APENAS preços + chaves de rotas em 🟡 + contadores
        SerpApi. NUNCA token / URL / payload / carriers / post_data."""
        return self.data_dir / "cycle_snapshot.json"
