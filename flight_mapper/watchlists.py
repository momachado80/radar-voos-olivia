"""Watchlists agrupam rotas monitoradas por contexto humano.

Iniciais espelham `REGIONS` mas com label próprio e mais expressivo
("Europa Executiva", "EUA Executiva", "Ásia/Oriente Médio Executiva").
A separação permite divergir no futuro — uma watchlist "Lua de Mel"
ou "Família" pode misturar regiões.
"""

from __future__ import annotations

from dataclasses import dataclass

from .regions import REGIONS
from .state import PriceStore


@dataclass(frozen=True)
class Watchlist:
    name: str           # identificador estável (snake_case)
    label: str          # texto humano no relatório
    route_keys: frozenset[str]
    priority: int = 0   # menor = mais prioritário no relatório


def _make_keys(destinations: list[str], origin: str = "GRU", cabin: str = "business") -> frozenset[str]:
    return frozenset(f"{origin}-{dest}-{cabin}" for dest in destinations)


WATCHLISTS: list[Watchlist] = [
    Watchlist(
        name="europa_executiva",
        label="Europa Executiva",
        route_keys=_make_keys(REGIONS["Europa"]),
        priority=0,
    ),
    Watchlist(
        name="eua_executiva",
        label="EUA Executiva",
        route_keys=_make_keys(REGIONS["EUA"]),
        priority=1,
    ),
    Watchlist(
        name="asia_oriente_medio_executiva",
        label="Ásia/Oriente Médio Executiva",
        route_keys=_make_keys(REGIONS["Ásia"]),
        priority=2,
    ),
]


def best_per_watchlist(store: PriceStore) -> list[tuple[Watchlist, str, float]]:
    """Para cada watchlist, devolve (watchlist, route_key, latest_price) do
    menor preço. Ordem por watchlist.priority. Watchlists sem nenhuma rota
    no store são omitidas.
    """
    out: list[tuple[Watchlist, str, float]] = []
    for wl in sorted(WATCHLISTS, key=lambda w: w.priority):
        best_key: str | None = None
        best_price: float | None = None
        for key in wl.route_keys:
            history = store.get(key)
            if not history.prices:
                continue
            price = history.prices[-1]
            if best_price is None or price < best_price:
                best_key = key
                best_price = price
        if best_key is not None and best_price is not None:
            out.append((wl, best_key, best_price))
    return out
