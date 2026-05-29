"""Watchlist premium TEMPORÁRIA: GRU→Londres/Paris business round-trip em
datas específicas de setembro/2026 (PR #67).

A Olivia quer prioridade alta para confirmar executiva nessas combinações
exatas. O pass Duffel consulta estas entradas ANTES da rota genérica
(GRU-MIA one_way), respeitando um cap dedicado e conservador.

Read-only: estas entradas só geram Offer Requests no Duffel. NUNCA criam
order/payment. As datas são fixas e públicas (sem dado sensível)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from typing import Sequence

from .regions import Cabin, Route, TripType


# Origem São Paulo = GRU (premissa do goal). Destinos premium: Londres/Paris.
_GRU_LHR = Route(
    origin="GRU", destination="LHR", region="Europa",
    trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS,
)
_GRU_CDG = Route(
    origin="GRU", destination="CDG", region="Europa",
    trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS,
)

# Combinações de datas pedidas: ida 02/03 set, volta 12/13 set.
_OUTBOUND_DATES = ("2026-09-02", "2026-09-03")
_RETURN_DATES = ("2026-09-12", "2026-09-13")


@dataclass(frozen=True)
class DuffelWatchEntry:
    """Uma combinação rota+datas+cabine da watchlist. Round-trip.

    Carrega só dados públicos (rota IATA + datas + cabine). NUNCA
    token/offer_id. `cabin` ∈ {"business","economy"} (PR #68)."""

    route: Route
    outbound_date: str  # YYYY-MM-DD
    return_date: str    # YYYY-MM-DD
    cabin: str = "business"

    @property
    def cabin_enum(self) -> Cabin:
        return Cabin.ECONOMY if self.cabin == "economy" else Cabin.BUSINESS

    @property
    def threshold_key(self) -> str:
        """Chave de teto/score por cabine. Business preserva `route.key`
        (`GRU-LHR-business`); economy usa namespace `-economy` separado
        (`GRU-LHR-economy` / `GRU-MIA-one_way-economy`)."""
        if self.cabin != "economy":
            return self.route.key
        base = f"{self.route.origin}-{self.route.destination}"
        if self.route.trip_type == TripType.ONE_WAY:
            return f"{base}-one_way-economy"
        return f"{base}-economy"

    @property
    def history_key(self) -> str:
        """Chave de histórico/dedup ISOLADA por cabine + combinação de datas:
        `GRU-LHR-business::duffel::2026-09-02_2026-09-12` (business) ou
        `GRU-LHR-economy::duffel::...` (economy)."""
        return (
            f"{self.threshold_key}::duffel::"
            f"{self.outbound_date}_{self.return_date}"
        )


def build_september_watchlist(
    cabins: Sequence[str] = ("business",),
) -> list[DuffelWatchEntry]:
    """Constrói as combinações pedidas por cabine. Para cada cabine:
    LHR (1-4) antes de CDG (5-8), ida 02 antes de 03, volta 12 antes de 13.

    Default `("business",)` → 8 combinações (compat PR #67). Com
    `("business","economy")` → 16 (business primeiro, depois economy).
    """
    entries: list[DuffelWatchEntry] = []
    for cabin in cabins:
        for route in (_GRU_LHR, _GRU_CDG):
            for outbound in _OUTBOUND_DATES:
                for ret in _RETURN_DATES:
                    entries.append(
                        DuffelWatchEntry(
                            route=route, outbound_date=outbound,
                            return_date=ret, cabin=cabin,
                        )
                    )
    return entries


@dataclass
class DuffelWatchlistState:
    """Offset rotativo persistido p/ cobrir todas as combinações ao longo
    dos ciclos respeitando o cap por ciclo. Mesmo padrão de `CycleState`.

    Persiste só `{"offset": N}` — runtime state, sem dado sensível."""

    path: Path | None
    offset: int = 0

    @classmethod
    def load(cls, path: Path | None) -> "DuffelWatchlistState":
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                return cls(path=path, offset=int(data.get("offset", 0)))
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                return cls(path=path, offset=0)
        return cls(path=path, offset=0)

    def window(self, total: int, cap: int) -> list[int]:
        """Índices da janela atual (com wraparound). `[]` se total/cap <= 0."""
        if total <= 0 or cap <= 0:
            return []
        base = self.offset % total
        return [(base + i) % total for i in range(min(cap, total))]

    def advance(self, total: int, cap: int) -> None:
        if total <= 0 or cap <= 0:
            self.offset = 0
            return
        self.offset = (self.offset + cap) % total

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"offset": self.offset}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # Persistência é best-effort — falha de I/O não derruba o ciclo.
            pass
