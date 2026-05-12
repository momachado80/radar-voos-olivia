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


# Níveis de alerta por rota. `excellent_brl` ≤ `good_brl`.
# Preço <= excellent_brl: alerta 🚨 Excelente.
# excellent_brl < Preço <= good_brl: alerta 🎯 Bom.
# Acima de good_brl: ceiling não dispara (detector legado de queda pode disparar).
ROUTE_THRESHOLDS: dict[str, dict[str, float]] = {
    "GRU-CDG-business": {"excellent_brl": 2400, "good_brl": 2800},
    "GRU-LHR-business": {"excellent_brl": 1700, "good_brl": 2000},
    "GRU-JFK-business": {"excellent_brl": 1800, "good_brl": 2100},
    "GRU-MIA-business": {"excellent_brl": 1100, "good_brl": 1300},
    "GRU-SFO-business": {"excellent_brl": 1800, "good_brl": 2100},
    "GRU-LAX-business": {"excellent_brl": 1700, "good_brl": 2000},
    "GRU-LIS-business": {"excellent_brl": 1800, "good_brl": 2100},
    "GRU-MAD-business": {"excellent_brl": 1900, "good_brl": 2200},
    "GRU-FCO-business": {"excellent_brl": 2000, "good_brl": 2300},
    "GRU-AMS-business": {"excellent_brl": 2200, "good_brl": 2500},
}


# Rotas escaneadas pelo `hot-scan` — varredura focada em oportunidade
# perecível. Inicialmente igual ao conjunto de chaves com teto, mas
# pode divergir no futuro (ex.: hot scanner mais frequente cobrindo
# subconjunto menor).
HOT_ROUTE_KEYS: frozenset[str] = frozenset(ABSOLUTE_CEILING_BRL.keys())


def ceiling_for(route_key: str) -> float | None:
    """Compat com camada antiga: usa good_brl do ROUTE_THRESHOLDS se houver,
    senão cai no ABSOLUTE_CEILING_BRL."""
    if route_key in ROUTE_THRESHOLDS:
        return ROUTE_THRESHOLDS[route_key].get("good_brl")
    return ABSOLUTE_CEILING_BRL.get(route_key)


def levels_for(route_key: str) -> dict | None:
    """Retorna dict {'excellent_brl': X, 'good_brl': Y} ou None.

    Quando a rota está apenas em ABSOLUTE_CEILING_BRL (camada legada),
    devolve {'excellent_brl': None, 'good_brl': ceiling}.
    """
    if route_key in ROUTE_THRESHOLDS:
        return dict(ROUTE_THRESHOLDS[route_key])
    if route_key in ABSOLUTE_CEILING_BRL:
        return {"excellent_brl": None, "good_brl": ABSOLUTE_CEILING_BRL[route_key]}
    return None


def hot_routes() -> list[Route]:
    """Filtra `all_routes()` para apenas as rotas em `HOT_ROUTE_KEYS`."""
    return [r for r in all_routes() if r.key in HOT_ROUTE_KEYS]

