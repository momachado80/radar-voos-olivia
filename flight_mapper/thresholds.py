"""Tetos de preço absoluto por rota.

Valores iniciais calibrados ~5-10% abaixo dos preços observados em
`data/price_history.json` no momento da configuração. Não disparam
alerta nos preços atuais — só em queda real.

Ajuste manualmente conforme acompanhar o histórico real das rotas.
"""

from __future__ import annotations

from .regions import Route, all_routes


ABSOLUTE_CEILING_BRL: dict[str, float] = {
    "GRU-CDG-business": 2400,
    "GRU-LHR-business": 1700,
    "GRU-JFK-business": 1800,
    "GRU-MIA-business": 1100,
    "GRU-SFO-business": 1800,
    "GRU-LAX-business": 1700,
    "GRU-LIS-business": 1800,
    "GRU-MAD-business": 1900,
    "GRU-FCO-business": 2000,
    "GRU-AMS-business": 2200,
}


# Rotas escaneadas pelo `hot-scan` — varredura focada em oportunidade
# perecível. Inicialmente igual ao conjunto de chaves com teto, mas
# pode divergir no futuro (ex.: hot scanner mais frequente cobrindo
# subconjunto menor).
HOT_ROUTE_KEYS: frozenset[str] = frozenset(ABSOLUTE_CEILING_BRL.keys())


def ceiling_for(route_key: str) -> float | None:
    return ABSOLUTE_CEILING_BRL.get(route_key)


def hot_routes() -> list[Route]:
    """Filtra `all_routes()` para apenas as rotas em `HOT_ROUTE_KEYS`."""
    return [r for r in all_routes() if r.key in HOT_ROUTE_KEYS]

