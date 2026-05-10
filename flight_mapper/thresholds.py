"""Tetos de preço absoluto por rota.

Valores iniciais calibrados ~5-10% abaixo dos preços observados em
`data/price_history.json` no momento da configuração. Não disparam
alerta nos preços atuais — só em queda real.

Ajuste manualmente conforme acompanhar o histórico real das rotas.
"""

from __future__ import annotations


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


def ceiling_for(route_key: str) -> float | None:
    return ABSOLUTE_CEILING_BRL.get(route_key)
