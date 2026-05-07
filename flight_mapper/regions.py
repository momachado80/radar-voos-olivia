"""Origens, destinos e agrupamentos por região."""

from dataclasses import dataclass


ORIGINS = ["GRU", "CGH"]

EUROPE = ["LHR", "CDG", "FRA", "MAD", "LIS", "FCO", "AMS", "ZRH"]
USA = ["JFK", "MIA", "LAX", "ORD", "IAD", "BOS"]
ASIA = ["NRT", "ICN", "HKG", "SIN", "DXB", "DOH"]

REGIONS = {
    "Europa": EUROPE,
    "EUA": USA,
    "Ásia": ASIA,
}


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    region: str

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}-business"


def all_routes() -> list[Route]:
    routes: list[Route] = []
    for region, destinations in REGIONS.items():
        for origin in ORIGINS:
            for destination in destinations:
                routes.append(Route(origin=origin, destination=destination, region=region))
    return routes
