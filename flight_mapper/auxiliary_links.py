"""Links auxiliares de pesquisa para o alerta manual.

Quando não há link comercial confiável (Aviasales bloqueado, Kiwi sem cobertura),
o alerta manual passa a oferecer links clicáveis de pesquisa em buscadores
externos. Esses links NÃO são oferta confirmada — apenas atalhos de pesquisa
para a usuária conferir manualmente.

Regras invioláveis:
- Nunca emitir URL do Aviasales (família `search.aviasales.com` / `aviasales.ru`
  / qualquer subdomínio).
- Funções puras: sem rede, sem I/O, sem estado.
- Cada URL inclui origem, destino, data de ida (e de volta quando há) e a
  classe executiva (`business`).
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .providers import Quote
from .regions import Route


def build_google_search_url(
    route: Route, departure_date: str, return_date: str | None = None
) -> str:
    """Busca genérica no Google. Sempre estável — fallback principal.

    Formato: `https://www.google.com/search?q=GRU+CDG+2026-06-09+business+class+flights`
    """
    parts = [
        route.origin,
        route.destination,
        departure_date,
    ]
    if return_date:
        parts.append(return_date)
    parts.extend(["business", "class", "flights"])
    return f"https://www.google.com/search?q={quote_plus(' '.join(parts))}"


def build_google_flights_query_url(
    route: Route, departure_date: str, return_date: str | None = None
) -> str:
    """URL de busca no Google Flights via query parametrizada.

    Usa o endpoint `/travel/flights?q=...`. É menos preciso que deep-link
    nativo (que tem parâmetros frágeis baseados em IDs internos), mas é
    estável: o Google interpreta a query semanticamente.
    """
    if return_date:
        q = (
            f"flights from {route.origin} to {route.destination} "
            f"{departure_date} return {return_date} business class"
        )
    else:
        q = (
            f"flights from {route.origin} to {route.destination} "
            f"{departure_date} business class"
        )
    return f"https://www.google.com/travel/flights?q={quote_plus(q)}"


def build_kayak_search_url(
    route: Route, departure_date: str, return_date: str | None = None
) -> str:
    """URL de busca no Kayak.

    Path estável e bem documentado: `/flights/ORIG-DEST/DEP[/RET]/business`.
    Mantemos pois Kayak não muda esse esquema há anos.
    """
    path = f"{route.origin}-{route.destination}/{departure_date}"
    if return_date:
        path += f"/{return_date}"
    return f"https://www.kayak.com/flights/{path}/business"


def build_auxiliary_search_links(quote: Quote) -> list[tuple[str, str]]:
    """Lista ordenada de (label, url) para o alerta manual.

    Ordem reflete robustez:
    1. Google Search — query genérica, sempre funciona.
    2. Google Flights — query semântica, funciona na maioria dos casos.
    3. Kayak — path estável conhecido.
    """
    return [
        (
            "Pesquisar no Google",
            build_google_search_url(
                quote.route, quote.departure_date, quote.return_date
            ),
        ),
        (
            "Pesquisar no Google Flights",
            build_google_flights_query_url(
                quote.route, quote.departure_date, quote.return_date
            ),
        ),
        (
            "Pesquisar no Kayak",
            build_kayak_search_url(
                quote.route, quote.departure_date, quote.return_date
            ),
        ),
    ]
