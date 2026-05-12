"""Humanização de códigos IATA, construção de URLs de busca e validação."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse


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


SEARCH_BASE_URL = "https://search.aviasales.com/flights/"

# Domínios cujas URLs consideramos "acionáveis" sem validação adicional dos params
# (ex.: Kiwi devolve deep_link próprio bem-formado).
_TRUSTED_DOMAINS = {"www.kiwi.com", "kiwi.com"}

# Parâmetros que toda URL nossa deve conter para ser considerada acionável.
# `locale` foi adicionado para forçar Aviasales a servir em inglês quando
# possível (evita preview e página em russo).
_REQUIRED_AVIASALES_PARAMS = frozenset(
    {"origin_iata", "destination_iata", "depart_date", "trip_class", "locale"}
)


def build_search_url(
    origin: str,
    destination: str,
    departure: str | None = None,
    return_date: str | None = None,
    locale: str = "en",
) -> str | None:
    """URL parametrizada de busca no Aviasales.

    Retorna `None` quando faltam dados essenciais (origem, destino ou data de ida).
    Preferimos não devolver link a devolver link frágil que abre busca quebrada.

    Datas no formato ISO (YYYY-MM-DD). `locale` força idioma da busca
    (default `en` para evitar Aviasales servir russo, que é o fallback do
    domínio search.aviasales.com).
    """
    if not origin or not destination or not departure:
        return None
    try:
        datetime.fromisoformat(departure)
        if return_date:
            datetime.fromisoformat(return_date)
    except (ValueError, TypeError):
        return None

    params: list[tuple[str, str]] = [
        ("origin_iata", origin),
        ("destination_iata", destination),
        ("depart_date", departure),
    ]
    if return_date:
        params.append(("return_date", return_date))
    params.extend(
        [
            ("adults", "1"),
            ("children", "0"),
            ("infants", "0"),
            ("trip_class", "1"),
            ("currency", "brl"),
            ("locale", locale),
            ("marker_locale", locale),
        ]
    )
    return f"{SEARCH_BASE_URL}?{urlencode(params)}"


def is_actionable_url(url: str | None) -> bool:
    """Aprova apenas URLs com formato acionável e comercialmente útil.

    Aprova:
    - URLs do nosso `build_search_url` (search.aviasales.com com `locale` + params mínimos)
    - Deep links do Kiwi (`*.kiwi.com`)

    Rejeita:
    - Padrão antigo `https://www.aviasales.com/search/GRUMIA` (path-encoded quebrado)
    - Hosts com TLD `.ru` ou subdomínios russos (`ru.aviasales.com` etc.)
    - search.aviasales.com sem `locale` (cairia em russo por default)
    - Esquemas não-HTTP, URLs vazias, domínios desconhecidos
    """
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    if not host:
        return False

    # Rejeita domínios russos
    if host.endswith(".ru") or host.startswith("ru."):
        return False

    if host == "search.aviasales.com":
        qs = parse_qs(parsed.query)
        # Exigir locale para evitar fallback para russo
        if "locale" not in qs:
            return False
        return _REQUIRED_AVIASALES_PARAMS.issubset(qs.keys())

    if host in _TRUSTED_DOMAINS:
        return True

    return False
