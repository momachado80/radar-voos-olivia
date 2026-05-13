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

# Locale e currency canônicos para os links que nós geramos.
# Escolha pragmática: Aviasales tende a servir Inglês americano com mais
# consistência quando recebe `en-us`. USD é a currency mais "neutra" — BRL é
# aceito mas com freqüência o preview cai em russo/RUB no fallback.
DEFAULT_LOCALE = "en-us"
DEFAULT_CURRENCY = "usd"

# Parâmetros que toda URL nossa deve conter para ser considerada acionável.
_REQUIRED_AVIASALES_PARAMS = frozenset(
    {"origin_iata", "destination_iata", "depart_date", "trip_class", "locale", "currency"}
)


def build_search_url(
    origin: str,
    destination: str,
    departure: str | None = None,
    return_date: str | None = None,
    locale: str = DEFAULT_LOCALE,
    currency: str = DEFAULT_CURRENCY,
) -> str | None:
    """URL parametrizada de busca no Aviasales.

    Retorna `None` quando faltam dados essenciais (origem, destino ou data de ida).
    Preferimos não devolver link a devolver link frágil que abre busca quebrada.

    Datas no formato ISO (YYYY-MM-DD). Defaults `en-us` + `usd` evitam fallback
    do Aviasales para russo/RUB.
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
            ("currency", currency),
            ("locale", locale),
            ("marker_locale", locale),
        ]
    )
    return f"{SEARCH_BASE_URL}?{urlencode(params)}"


def is_actionable_url(url: str | None) -> bool:
    """Aprova apenas URLs comercialmente úteis.

    **Aviasales bloqueado por completo**: evidência real mostrou que o
    domínio redireciona para experiência russa mesmo com `locale=en-us` +
    `currency=usd`. Qualquer host contendo `aviasales` é rejeitado.

    Aprova:
    - Deep links do Kiwi (`*.kiwi.com`) — Kiwi serve multilíngue por padrão

    Rejeita:
    - Qualquer host que contenha `aviasales` (search/www/aviasales.ru/ru.aviasales.com/etc.)
    - Hosts com TLD `.ru` ou subdomínio `ru.*`
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

    # Bloqueio total Aviasales (todos os domínios da família)
    if "aviasales" in host:
        return False

    # Rejeita domínios russos (gerais)
    if host.endswith(".ru") or host.startswith("ru."):
        return False

    if host in _TRUSTED_DOMAINS:
        return True

    # Subdomínios de Kiwi
    if host.endswith(".kiwi.com"):
        return True

    return False
