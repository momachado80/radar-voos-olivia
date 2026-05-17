"""Tetos de preço por rota.

ATENÇÃO — moeda: estes valores foram calibrados contra
`data/price_history.json`, que continha preços do Travelpayouts em
**USD** (o endpoint ignora `currency=brl`). Portanto os números abaixo
estão em **USD**, não em BRL, apesar do sufixo `_brl` mantido por
compatibilidade de schema.

A correção de moeda normaliza o preço da cotação para BRL e escala
estes tetos por `USD_BRL_RATE` (ver `scaled_levels`), preservando
exatamente o comportamento de disparo original — só tornando a moeda
honesta. Não disparam alerta nos preços atuais — só em queda real.
"""

from __future__ import annotations

from .regions import REGIONS, Route, TripType, all_routes


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


# Thresholds one-way business (PR F1). Mesma convenção do ROUTE_THRESHOLDS:
# valores armazenados em **USD** (apesar do sufixo `_brl`, mantido por
# compat de schema) e escalados USD→BRL em runtime via `scaled_levels`.
# Chaves no namespace one-way (`GRU-XX-one_way-business`), isolado do
# round_trip — sem mistura de histórico nem de teto.
ONE_WAY_ROUTE_THRESHOLDS: dict[str, dict[str, float]] = {
    "GRU-MIA-one_way-business": {"excellent_brl": 700, "good_brl": 1000},
    "GRU-JFK-one_way-business": {"excellent_brl": 900, "good_brl": 1300},
    "GRU-LAX-one_way-business": {"excellent_brl": 1100, "good_brl": 1600},
    "GRU-SFO-one_way-business": {"excellent_brl": 1100, "good_brl": 1600},
    "GRU-LHR-one_way-business": {"excellent_brl": 1100, "good_brl": 1500},
    "GRU-CDG-one_way-business": {"excellent_brl": 1100, "good_brl": 1500},
    "GRU-LIS-one_way-business": {"excellent_brl": 1100, "good_brl": 1500},
    "GRU-MAD-one_way-business": {"excellent_brl": 1100, "good_brl": 1500},
    "GRU-AMS-one_way-business": {"excellent_brl": 1100, "good_brl": 1500},
    "GRU-FCO-one_way-business": {"excellent_brl": 1100, "good_brl": 1500},
}

# Destinos one-way iniciais (ordem estável p/ testes/preview).
ONE_WAY_HOT_DESTINATIONS: list[str] = [
    "MIA", "JFK", "LAX", "SFO", "LHR", "CDG", "LIS", "MAD", "AMS", "FCO",
]

HOT_ONE_WAY_ROUTE_KEYS: frozenset[str] = frozenset(
    ONE_WAY_ROUTE_THRESHOLDS.keys()
)


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
    if route_key in ONE_WAY_ROUTE_THRESHOLDS:
        return dict(ONE_WAY_ROUTE_THRESHOLDS[route_key])
    if route_key in ABSOLUTE_CEILING_BRL:
        return {"excellent_brl": None, "good_brl": ABSOLUTE_CEILING_BRL[route_key]}
    return None


def scaled_levels(levels: dict | None, rate: float | None) -> dict | None:
    """Escala tetos USD→BRL multiplicando por `rate`.

    Os valores em ROUTE_THRESHOLDS/ABSOLUTE_CEILING_BRL são USD (ver
    docstring do módulo). Para comparar com preço já normalizado em BRL,
    multiplicamos por `rate`. Sem `rate` confiável devolvemos `None` —
    o chamador deve bloquear o alerta.
    """
    if levels is None or rate is None:
        return None
    out: dict = {}
    for key, value in levels.items():
        out[key] = None if value is None else round(float(value) * rate, 2)
    return out


def hot_routes() -> list[Route]:
    """Filtra `all_routes()` para apenas as rotas em `HOT_ROUTE_KEYS`."""
    return [r for r in all_routes() if r.key in HOT_ROUTE_KEYS]


def _region_for(destination: str) -> str:
    for region, dests in REGIONS.items():
        if destination in dests:
            return region
    return ""


def one_way_hot_routes() -> list[Route]:
    """Rotas one-way business quentes (PR F1).

    Construídas com `trip_type=ONE_WAY` → `Route.key` no namespace
    `GRU-XX-one_way-business`, isolado do round_trip. Não usa a chave
    canônica do PR A. Round_trip (`hot_routes`) permanece inalterado.
    """
    return [
        Route(
            origin="GRU",
            destination=d,
            region=_region_for(d),
            trip_type=TripType.ONE_WAY,
        )
        for d in ONE_WAY_HOT_DESTINATIONS
    ]

