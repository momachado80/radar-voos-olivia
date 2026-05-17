"""Histórico rolante de preços por rota, persistido em JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    # Apenas para type-hint. Em runtime só acessamos os atributos
    # `.canonical_key` / `.legacy_key` (duck typing), sem importar
    # `regions` — evita qualquer acoplamento/ciclo de import.
    from .regions import Route


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

    def clone(self) -> "RouteHistory":
        """Cópia independente (sem aliasing de listas/dicts). Usada no
        seed legacy→canonical para não duplicar nem compartilhar estado."""
        return RouteHistory(
            prices=list(self.prices),
            last_alert_at=self.last_alert_at,
            last_alert_price=self.last_alert_price,
            last_quote=dict(self.last_quote) if self.last_quote is not None else None,
        )


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
        """Acesso por string — caminho de produção (Route.key ainda legado).
        Comportamento inalterado: cria histórico vazio sob demanda."""
        if key not in self._data:
            self._data[key] = RouteHistory()
        return self._data[key]

    def resolve_history_key(self, route: "Route") -> str:
        """Decide qual chave usar para o histórico desta rota.

        Prioridade:
        1. canonical_key se já existir no store;
        2. senão legacy_key se já existir no store;
        3. senão canonical_key (lar futuro) — nada é criado aqui.

        Pura: não cria entrada nem escreve em disco. Enquanto
        `Route.key` for legado e o pipeline usar `get(route.key)`, este
        método não participa do fluxo de produção.
        """
        canonical = route.canonical_key
        if canonical in self._data:
            return canonical
        legacy = route.legacy_key
        if legacy in self._data:
            return legacy
        return canonical

    def get_history(self, route: "Route") -> RouteHistory:
        """Histórico da rota pela chave resolvida (canonical→legacy→canonical).

        Mantém a semântica de `get`: cria histórico VAZIO sob demanda na
        chave resolvida. Não faz seed legacy→canonical (leitura não migra
        dado); para isso use `ensure_canonical_seed` explicitamente.
        """
        return self.get(self.resolve_history_key(route))

    def ensure_canonical_seed(self, route: "Route") -> bool:
        """Semeia o histórico canonical a partir do legacy, se aplicável.

        Opt-in e idempotente. NÃO é chamado por leitura. NÃO escreve em
        disco (só muda estado em memória; persistência continua sendo
        responsabilidade exclusiva de `save()`, chamado pelo Monitor).
        NÃO apaga o legacy. NÃO duplica amostras (cópia, não merge; se o
        canonical já existe, não faz nada).

        Retorna True se semeou; False caso contrário.
        """
        canonical = route.canonical_key
        if canonical in self._data:
            return False
        legacy = route.legacy_key
        if legacy not in self._data:
            return False
        self._data[canonical] = self._data[legacy].clone()
        return True

    def keys(self) -> Iterable[str]:
        return self._data.keys()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialised = {key: asdict(value) for key, value in sorted(self._data.items())}
        self.path.write_text(
            json.dumps(serialised, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
