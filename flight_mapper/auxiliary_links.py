"""Links auxiliares de pesquisa para o alerta manual.

Quando não há link comercial confiável (Aviasales bloqueado, Kiwi sem cobertura),
o alerta manual passa a oferecer links clicáveis de pesquisa em buscadores e
agregadores externos. Esses links NÃO são oferta confirmada — apenas atalhos
de pesquisa para a usuária conferir manualmente.

Regras invioláveis:
- Nunca emitir URL do Aviasales (família search.aviasales.com / aviasales.ru).
- Funções puras: sem rede, sem I/O, sem estado.
- Cada URL deve conter origem, destino, data de ida e classe executiva (business).
"""

from __future__ import annotations

from urllib.parse import quote_plus

from .providers import Quote
from .regions import Route


def build_google_flights_search_url(
    route: Route, departure_date: str, return_date: str | None = None
) -> str:
    """URL de busca no Google Flights via query parametrizada.

    Usa o endpoint genérico de busca do Google com a query semântica.
    É menos preciso que deep-link nativo, mas é estável e nunca quebra.
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
    """URL de busca no Kayak. Path estável: /flights/ORIG-DEST/DEP[/RET]/business."""
    parts = [route.origin, route.destination]
    path = f"{parts[0]}-{parts[1]}/{departure_date}"
    if return_date:
        path += f"/{return_date}"
    return f"https://www.kayak.com/flights/{path}/business"


def build_expedia_search_url(
    route: Route, departure_date: str, return_date: str | None = None
) -> str:
    """URL de busca na Expedia.

    O deep-link próprio da Expedia tem muitos parâmetros frágeis (codifica
    legs em JSON-like); preferimos uma busca de Google direcionada ao site,
    que é estável e leva direto ao painel correto.
    """
    if return_date:
        q = (
            f"site:expedia.com flights {route.origin} to {route.destination} "
            f"{departure_date} {return_date} business class"
        )
    else:
        q = (
            f"site:expedia.com flights {route.origin} to {route.destination} "
            f"{departure_date} business class"
        )
    return f"https://www.google.com/search?q={quote_plus(q)}"


def build_auxiliary_links(quote: Quote) -> list[tuple[str, str]]:
    """Lista ordenada de (label, url) para o alerta manual.

    A ordem reflete preferência: Google Flights > Kayak > Expedia.
    """
    return [
        (
            "Pesquisar no Google Flights",
            build_google_flights_search_url(
                quote.route, quote.departure_date, quote.return_date
            ),
        ),
        (
            "Pesquisar no Kayak",
            build_kayak_search_url(
                quote.route, quote.departure_date, quote.return_date
            ),
        ),
        (
            "Pesquisar na Expedia",
            build_expedia_search_url(
                quote.route, quote.departure_date, quote.return_date
            ),
        ),
    ]
