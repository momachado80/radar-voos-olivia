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
    """Uma combinação rota+datas da watchlist. Round-trip business.

    Carrega só dados públicos (rota IATA + datas). NUNCA token/offer_id."""

    route: Route
    outbound_date: str  # YYYY-MM-DD
    return_date: str    # YYYY-MM-DD

    @property
    def history_key(self) -> str:
        """Chave de histórico/dedup ISOLADA por combinação de datas, dentro
        do namespace Duffel: `GRU-LHR-business::duffel::2026-09-02_2026-09-12`."""
        return (
            f"{self.route.key}::duffel::"
            f"{self.outbound_date}_{self.return_date}"
        )


def build_september_watchlist() -> list[DuffelWatchEntry]:
    """Constrói exatamente as 8 combinações pedidas, ordenadas
    LHR (1-4) antes de CDG (5-8), ida 02 antes de 03, volta 12 antes de 13.
    """
    entries: list[DuffelWatchEntry] = []
    for route in (_GRU_LHR, _GRU_CDG):
        for outbound in _OUTBOUND_DATES:
            for ret in _RETURN_DATES:
                entries.append(
                    DuffelWatchEntry(
                        route=route, outbound_date=outbound, return_date=ret,
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
