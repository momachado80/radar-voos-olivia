"""Cruzamento Duffel → Google Flights (PR #76).

A Duffel confirma a oferta (rota, datas, cabine, preço, cia) mas NÃO devolve
link clicável de compra (`order_flow`). Aqui montamos um link de **busca
PRÉ-PREENCHIDA** no Google Flights a partir dos dados confirmados, para a
usuária conferir/comprar com 1-2 cliques.

Honestidade (regra de produto):
- NÃO é a oferta da Duffel travada — é o Google buscando agora, do zero, com
  os mesmos filtros. Preço e disponibilidade podem variar.
- Por isso `link_status` da Duffel CONTINUA `order_flow` (não viramos
  `direct_link`): este link é um atalho de busca, não checkout da oferta.

Sanitização: o URL contém APENAS dados públicos — origem/destino (IATA),
datas e cabine. NUNCA offer_id, token, preço, payload ou dado de passageiro.
Função pura: sem rede, sem I/O, sem estado.
"""

from __future__ import annotations

from .auxiliary_links import build_google_flights_query_url
from .providers import Quote
from .regions import Cabin, TripType


def duffel_google_flights_url(quote: Quote) -> str | None:
    """URL de busca pré-preenchida no Google Flights p/ uma oferta Duffel
    confirmada. `None` quando faltam dados mínimos (origem/destino/ida).

    Só usa campos PÚBLICOS do Quote (rota IATA + datas + cabine). NUNCA
    inclui offer_id/token/preço/payload/passageiro."""
    if quote is None:
        return None
    route = getattr(quote, "route", None)
    if route is None or not route.origin or not route.destination:
        return None
    departure = getattr(quote, "departure_date", None)
    if not departure:
        return None
    # Volta só em round_trip COM return_date (igual ao resto do alerta).
    show_return = (
        quote.trip_type == TripType.ROUND_TRIP and bool(quote.return_date)
    )
    return_date = quote.return_date if show_return else None
    # Cabine: usa a do quote; default business (rota é monitorada como exec).
    cabin = quote.cabin if quote.cabin in (Cabin.BUSINESS, Cabin.ECONOMY) else Cabin.BUSINESS
    return build_google_flights_query_url(
        route, departure, return_date, cabin=cabin,
    )
