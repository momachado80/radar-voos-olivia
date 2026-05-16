"""Origens, destinos e agrupamentos por região."""

from dataclasses import dataclass
from enum import Enum


class TripType(str, Enum):
    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"
    # Fase 2: combinar ida barata GRU→A com volta barata B→GRU. O modelo
    # já aceita o valor; nenhuma busca open-jaw é feita ainda.
    OPEN_JAW_CANDIDATE = "open_jaw_candidate"


class Cabin(str, Enum):
    ECONOMY = "economy"
    BUSINESS = "business"
    # Provider não confirmou a classe. Camadas superiores não devem
    # rotular como "Business"/"Excelente" sem confirmação.
    UNKNOWN = "unknown"


ORIGINS = ["GRU"]

EUROPE = ["LHR", "CDG", "FRA", "MAD", "LIS", "FCO", "AMS", "ZRH"]
USA = ["JFK", "MIA", "LAX", "ORD", "IAD", "BOS", "SFO"]
ASIA = ["NRT", "ICN", "HKG", "SIN", "DXB", "DOH"]

REGIONS = {
    "Europa": EUROPE,
    "EUA": USA,
    "Ásia": ASIA,
}

PRIORITY_KEYS = frozenset({
    "GRU-SFO-business",
    "GRU-JFK-business",
    "GRU-LHR-business",
    "GRU-CDG-business",
})


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    region: str
    # Defaults preservam exatamente o comportamento atual (business
    # ida-e-volta). `.key` segue no formato legado nesta fase — a chave
    # canônica composta só passa a ser usada na fase de migração.
    trip_type: TripType = TripType.ROUND_TRIP
    cabin: Cabin = Cabin.BUSINESS

    @property
    def key(self) -> str:
        return self.legacy_key

    @property
    def legacy_key(self) -> str:
        """Formato histórico usado em data/price_history.json, thresholds,
        PRIORITY_KEYS e HOT_ROUTE_KEYS. Mantido estável p/ não quebrar
        histórico nem disparo enquanto a migração não está ativa."""
        return f"{self.origin}-{self.destination}-business"

    @property
    def canonical_key(self) -> str:
        """Chave composta nova (origin-dest-trip_type-cabin). Ainda não
        consumida — introduzida para as fases de migração/thresholds."""
        return (
            f"{self.origin}-{self.destination}-"
            f"{self.trip_type.value}-{self.cabin.value}"
        )


def is_priority(route: Route) -> bool:
    return route.key in PRIORITY_KEYS


def all_routes() -> list[Route]:
    routes: list[Route] = []
    for region, destinations in REGIONS.items():
        for origin in ORIGINS:
            for destination in destinations:
                routes.append(Route(origin=origin, destination=destination, region=region))
    return routes
