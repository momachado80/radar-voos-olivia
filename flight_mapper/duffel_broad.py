"""Pool broad de candidatos Duffel (PR #77).

Substitui o foco exclusivo Londres/Paris setembro (PR #67) por uma varredura
mais ampla: 8 rotas premium × business + economy × one_way + round_trip, com
datas dinâmicas (hoje+90d). A watchlist Londres/Paris vira opt-in.

Princípio: maximizar a chance de achar uma oferta confirmada qualquer pelo
Duffel — não overfit a 2 destinos. As entradas reusam `DuffelWatchEntry`
(rota + datas + cabine) e o mesmo `DuffelWatchlistState` de rotação, então a
máquina de cooldown/agrupamento/Google Flights (PR #71/#76) segue intacta.

Read-only: estas entradas só geram Offer Requests. NUNCA criam order/payment.
Datas/rotas/cabines são públicas — sem dado sensível."""

from __future__ import annotations

from datetime import date, timedelta

from .duffel_watchlist import DuffelWatchEntry
from .regions import Cabin, Route, TripType


# 8 rotas premium do escopo broad (todas têm thresholds business em
# `flight_mapper/thresholds.py`; AMS é o representante "AMS ou FRA"). Ordem
# diversifica regiões (EUA, Europa) p/ rotação não concentrar em um destino.
#
# PR #81: escopo AMPLIADO a pedido da Olivia — econômica E executiva, em
# várias regiões: América do Sul/Central/Norte, Canadá, Europa, China/Japão.
# Todos os destinos abaixo têm teto business E economy (round-trip e
# one-way) em `thresholds.py` — sem teto, a oferta nunca vira alerta.
# Ordem intercala regiões p/ a rotação não concentrar em um bloco só.
BROAD_ROUTE_SPECS: tuple[tuple[str, str, str], ...] = (
    # América do Sul (voos curtos, promo agressiva):
    ("EZE", "América do Sul", "Buenos Aires"),
    ("SCL", "América do Sul", "Santiago"),
    ("BOG", "América do Sul", "Bogotá"),
    ("LIM", "América do Sul", "Lima"),
    # América do Norte:
    ("MIA", "EUA", "Miami"),
    ("JFK", "EUA", "Nova York"),
    ("ORD", "EUA", "Chicago"),
    # América Central / Caribe:
    ("CUN", "América Central", "Cancún"),
    ("PTY", "América Central", "Cidade do Panamá"),
    ("SJO", "América Central", "San José"),
    # Canadá:
    ("YYZ", "Canadá", "Toronto"),
    ("YUL", "Canadá", "Montreal"),
    # Europa:
    ("LHR", "Europa", "Londres"),
    ("CDG", "Europa", "Paris"),
    ("MAD", "Europa", "Madri"),
    ("LIS", "Europa", "Lisboa"),
    ("FCO", "Europa", "Roma"),
    ("AMS", "Europa", "Amsterdã"),
    ("FRA", "Europa", "Frankfurt"),
    # China / Japão:
    ("NRT", "Ásia", "Tóquio"),
    ("HND", "Ásia", "Tóquio"),
    ("PVG", "Ásia", "Xangai"),
    ("PEK", "Ásia", "Pequim"),
    ("HKG", "Ásia", "Hong Kong"),
)

# Janela conservadora p/ a busca: ~90 dias à frente é uma janela típica de
# oferta business; 10 noites é uma duração razoável p/ round-trip.
BROAD_LOOKAHEAD_DAYS = 90
BROAD_TRIP_NIGHTS = 10


def _broad_dates(today: date | None = None) -> tuple[str, str]:
    """Datas (ida, volta) p/ a varredura broad. Função pura — `today`
    injetável p/ testes."""
    t = today or date.today()
    out = t + timedelta(days=BROAD_LOOKAHEAD_DAYS)
    ret = out + timedelta(days=BROAD_TRIP_NIGHTS)
    return out.strftime("%Y-%m-%d"), ret.strftime("%Y-%m-%d")


def build_broad_candidate_pool(
    today: date | None = None,
) -> list[DuffelWatchEntry]:
    """Pool broad: 8 rotas × {business, economy} × {one_way, round_trip}
    com datas dinâmicas. Total = 32 entradas (8 × 2 × 2).

    Ordem: para cada cabine (business primeiro), business+round_trip,
    business+one_way, etc., para cada rota — assim a rotação cobre cabines
    e trip_types ao longo dos ciclos, em vez de varrer 8 round_trip business
    seguidos. Londres/Paris ficam no meio (não primeiro), de propósito."""
    outbound, return_date = _broad_dates(today)
    entries: list[DuffelWatchEntry] = []
    # Intercala (cabine, trip_type) p/ a rotação diversificar.
    cabin_trips = (
        ("business", TripType.ROUND_TRIP),
        ("business", TripType.ONE_WAY),
        ("economy", TripType.ROUND_TRIP),
        ("economy", TripType.ONE_WAY),
    )
    for cabin, trip in cabin_trips:
        for dest, region, _city in BROAD_ROUTE_SPECS:
            route = Route(
                origin="GRU", destination=dest, region=region,
                trip_type=trip, cabin=Cabin.BUSINESS,  # campo `cabin` da
                # Route é só rotulagem da rota; a busca usa entry.cabin.
            )
            entries.append(
                DuffelWatchEntry(
                    route=route,
                    outbound_date=outbound,
                    return_date=(
                        return_date if trip == TripType.ROUND_TRIP else ""
                    ),
                    cabin=cabin,
                )
            )
    return entries


# Modos de seleção de rotas Duffel (PR #77).
DUFFEL_ROUTE_MODE_BROAD = "broad"          # default — varredura ampla
DUFFEL_ROUTE_MODE_WATCHLIST = "watchlist"  # opt-in — Londres/Paris set/2026
DUFFEL_ROUTE_MODE_DISABLED = "disabled"    # nenhum Offer Request

DUFFEL_ROUTE_MODES = (
    DUFFEL_ROUTE_MODE_BROAD,
    DUFFEL_ROUTE_MODE_WATCHLIST,
    DUFFEL_ROUTE_MODE_DISABLED,
)
