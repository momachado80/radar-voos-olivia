"""Origens, destinos e agrupamentos por região."""

from dataclasses import dataclass


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

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}-business"


def is_priority(route: Route) -> bool:
    return route.key in PRIORITY_KEYS


def all_routes() -> list[Route]:
    routes: list[Route] = []
    for region, destinations in REGIONS.items():
        for origin in ORIGINS:
            for destination in destinations:
                routes.append(Route(origin=origin, destination=destination, region=region))
    return routes
