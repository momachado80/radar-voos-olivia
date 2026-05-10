"""Humanização de códigos IATA e construção de URLs de busca."""

from __future__ import annotations


AIRPORTS: dict[str, str] = {
    "GRU": "São Paulo",
    "CGH": "São Paulo",
    "CDG": "Paris",
    "ORY": "Paris",
    "LHR": "Londres",
    "LGW": "Londres",
    "JFK": "Nova York",
    "EWR": "Nova York",
    "MIA": "Miami",
    "SFO": "São Francisco",
    "LAX": "Los Angeles",
    "ORD": "Chicago",
    "DXB": "Dubai",
    "DOH": "Doha",
    "FRA": "Frankfurt",
    "MAD": "Madri",
    "LIS": "Lisboa",
    "FCO": "Roma",
    "AMS": "Amsterdã",
    "HKG": "Hong Kong",
    "SIN": "Singapura",
    "BOS": "Boston",
    "IAD": "Washington",
    "ZRH": "Zurique",
    "NRT": "Tóquio",
    "ICN": "Seul",
}


def airport_label(code: str) -> str:
    """`CDG` -> `CDG Paris`. Aeroporto desconhecido retorna só a sigla."""
    city = AIRPORTS.get(code)
    return f"{code} {city}" if city else code


def route_city_label(origin: str, destination: str) -> str:
    """`GRU`, `CDG` -> `São Paulo → Paris`. Faz fallback para a sigla quando faltar cidade."""
    return f"{AIRPORTS.get(origin, origin)} → {AIRPORTS.get(destination, destination)}"


def route_airport_label(origin: str, destination: str) -> str:
    """`GRU`, `CDG` -> `GRU → CDG`."""
    return f"{origin} → {destination}"


def humanize_route(origin: str, destination: str) -> str:
    """Combinação cidade + sigla sem redundância.

    - Ambos conhecidos: `São Paulo → Paris (GRU → CDG)`.
    - Pelo menos um desconhecido em que cidade != sigla: idem com fallback.
    - Ambos desconhecidos (cidade == sigla nas duas pontas): só `GRU → CDG`.
    """
    city = route_city_label(origin, destination)
    iata = route_airport_label(origin, destination)
    if city == iata:
        return iata
    return f"{city} ({iata})"


def build_search_url(
    origin: str,
    destination: str,
    departure: str | None = None,
    return_date: str | None = None,
) -> str:
    """URL de busca no Aviasales.

    Sem datas: `https://www.aviasales.com/search/{O}{D}`.
    Com data de ida: codifica `DDMM`. Com volta: idem + sufixo `1` (1 passageiro).
    Mesmo padrão que `TravelpayoutsProvider._search_url` usa hoje.
    """
    base = f"https://www.aviasales.com/search/{origin}{destination}"
    if not departure:
        return base
    try:
        from datetime import datetime
        dep = datetime.fromisoformat(departure).strftime("%d%m")
    except (ValueError, TypeError):
        return base
    if return_date:
        try:
            from datetime import datetime
            ret = datetime.fromisoformat(return_date).strftime("%d%m")
            return f"https://www.aviasales.com/search/{origin}{dep}{destination}{ret}1"
        except (ValueError, TypeError):
            pass
    return f"https://www.aviasales.com/search/{origin}{dep}{destination}1"
