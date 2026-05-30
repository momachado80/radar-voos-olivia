"""Cooldown/dedup das ofertas Duffel order_flow agrupadas (PR #71).

Evita repetir o MESMO combo Duffel "compra pendente" dentro de 6h, a menos
que o preço melhore ≥5%. Persiste só identidade da rota + timestamp + preço
arredondado/moeda — NUNCA offer_id/token/payload/passageiro."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


COOLDOWN_HOURS = 6
IMPROVEMENT_PCT = 0.05  # ≥5% de melhora de preço fura o cooldown
_PRUNE_DAYS = 7         # descarta entradas antigas p/ não crescer sem limite


def cooldown_key(quote) -> str:
    """Identidade do combo p/ cooldown: provider|rota|cabine|trip|datas.

    O PREÇO arredondado/moeda NÃO entra na chave de casamento — a regra dos
    5% é aplicada explicitamente sobre `last_price_brl`. (O preço/moeda
    arredondados ficam guardados no registro do cooldown.)"""
    dep = getattr(quote, "departure_date", "") or ""
    ret = getattr(quote, "return_date", "") or ""
    return "|".join([
        "duffel",
        f"{quote.route.origin}-{quote.route.destination}",
        quote.cabin.value,
        quote.trip_type.value,
        dep,
        ret,
    ])


@dataclass
class DuffelAlertCooldownState:
    """Estado persistido do cooldown de alertas Duffel agrupados."""

    path: Path | None
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> "DuffelAlertCooldownState":
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8") or "{}")
                ent = raw.get("entries") if isinstance(raw, dict) else {}
                return cls(path=path, entries=ent if isinstance(ent, dict) else {})
            except (OSError, json.JSONDecodeError):
                return cls(path=path, entries={})
        return cls(path=path, entries={})

    def is_suppressed(self, key: str, price_brl: float, now: datetime) -> bool:
        """True se o combo deve ser suprimido: alertado < 6h atrás E sem
        melhora de preço ≥5%."""
        e = self.entries.get(key)
        if not isinstance(e, dict):
            return False
        raw = e.get("last_alert_at")
        try:
            last = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last >= timedelta(hours=COOLDOWN_HOURS):
            return False  # janela expirou → pode alertar
        last_price = e.get("last_price_brl")
        if not isinstance(last_price, (int, float)) or last_price <= 0:
            return True  # dentro da janela, sem preço comparável → suprime
        if price_brl <= last_price * (1 - IMPROVEMENT_PCT):
            return False  # melhorou ≥5% → fura o cooldown
        return True

    def record(self, key: str, price_brl: float, currency: str | None,
               now: datetime) -> None:
        self.entries[key] = {
            "last_alert_at": now.isoformat(),
            "last_price_brl": round(float(price_brl), 2),
            "last_currency": (currency or "").upper() or None,
        }

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=_PRUNE_DAYS)
        keep: dict[str, dict] = {}
        for k, e in self.entries.items():
            try:
                last = datetime.fromisoformat(str(e.get("last_alert_at")))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if last >= cutoff:
                keep[k] = e
        self.entries = keep

    def save(self, now: datetime | None = None) -> None:
        if self.path is None:
            return
        self._prune(now or datetime.now(timezone.utc))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"entries": self.entries}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
