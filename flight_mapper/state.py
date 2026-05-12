"""Histórico rolante de preços por rota, persistido em JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


HISTORY_WINDOW = 50


@dataclass
class RouteHistory:
    prices: list[float] = field(default_factory=list)
    last_alert_at: str | None = None
    last_alert_price: float | None = None
    last_quote: dict | None = None

    def push(self, price: float) -> None:
        self.prices.append(round(float(price), 2))
        if len(self.prices) > HISTORY_WINDOW:
            self.prices = self.prices[-HISTORY_WINDOW:]

    @property
    def average(self) -> float | None:
        if not self.prices:
            return None
        return sum(self.prices) / len(self.prices)


class PriceStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, RouteHistory] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        for key, value in raw.items():
            self._data[key] = RouteHistory(
                prices=list(value.get("prices", [])),
                last_alert_at=value.get("last_alert_at"),
                last_alert_price=value.get("last_alert_price"),
                last_quote=value.get("last_quote"),
            )

    def get(self, key: str) -> RouteHistory:
        if key not in self._data:
            self._data[key] = RouteHistory()
        return self._data[key]

    def keys(self) -> Iterable[str]:
        return self._data.keys()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialised = {key: asdict(value) for key, value in sorted(self._data.items())}
        self.path.write_text(
            json.dumps(serialised, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
