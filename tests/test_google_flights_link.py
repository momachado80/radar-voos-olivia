"""Testes do PR #76 — cruzamento Duffel → Google Flights.

A oferta Duffel confirmada (order_flow, sem checkout) gera um link de BUSCA
pré-preenchida no Google Flights (rota/datas/cabine). NÃO é a oferta travada
— por isso link_status segue order_flow. URL só com dados públicos.
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

import pytest

from flight_mapper.google_flights_link import duffel_google_flights_url
from flight_mapper.notifier import (
    LINK_STATUS_ORDER_FLOW,
    build_duffel_pending_offer,
    link_status_for,
)
from flight_mapper.detector import Decision, CRITERION_CEILING, LEVEL_GOOD
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType


def _duffel_quote(cabin=Cabin.BUSINESS, *, dep="2026-09-02", ret="2026-09-12",
                  trip=TripType.ROUND_TRIP, dest="LHR") -> Quote:
    return Quote(
        route=Route("GRU", dest, "Europa", trip_type=trip, cabin=Cabin.BUSINESS),
        price_brl=5778.0, deep_link=None, departure_date=dep, return_date=ret,
        source="duffel", amount=963.0, currency="EUR",
        amount_brl_estimated=5778.0, fx_rate=6.0,
        cabin=cabin, cabin_confirmed=True, trip_type=trip, airline="TP",
    )


def _ceiling(threshold=12000.0):
    return Decision(alert=True, reason="x", criterion=CRITERION_CEILING,
                    threshold=threshold, level=LEVEL_GOOD, score=80)


# ----------------- URL build -----------------


def test_url_built_for_roundtrip_business():
    url = duffel_google_flights_url(_duffel_quote())
    assert url.startswith("https://www.google.com/travel/flights?q=")
    q = unquote_plus(url.split("q=", 1)[1])
    assert "from GRU to LHR" in q
    assert "2026-09-02" in q
    assert "return 2026-09-12" in q
    assert "business class" in q


def test_url_oneway_omits_return():
    url = duffel_google_flights_url(
        _duffel_quote(trip=TripType.ONE_WAY, ret=None),
    )
    q = unquote_plus(url.split("q=", 1)[1])
    assert "from GRU to LHR" in q
    assert "return" not in q


def test_url_economy_uses_economy_class():
    url = duffel_google_flights_url(_duffel_quote(cabin=Cabin.ECONOMY))
    q = unquote_plus(url.split("q=", 1)[1])
    assert "economy class" in q
    assert "business class" not in q


def test_url_none_when_missing_origin_or_departure():
    q = _duffel_quote()
    q2 = Quote(
        route=Route("", "LHR", "Europa"), price_brl=1.0, deep_link=None,
        departure_date="2026-09-02", return_date=None, source="duffel",
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    assert duffel_google_flights_url(q2) is None


# ----------------- sanitização: só dados públicos -----------------


def test_url_never_contains_price_token_or_amount():
    url = duffel_google_flights_url(_duffel_quote())
    for sensitive in ("963", "5778", "EUR", "token", "offer", "TP"):
        assert sensitive not in url, f"URL não deve conter {sensitive!r}: {url}"
    # Host é exclusivamente o Google.
    host = re.match(r"https://([^/]+)/", url).group(1)
    assert host == "www.google.com"


# ----------------- integração com DuffelPendingOffer -----------------


def test_pending_offer_carries_search_url():
    offer = build_duffel_pending_offer(_duffel_quote(), _ceiling())
    assert offer.search_url is not None
    assert offer.search_url.startswith("https://www.google.com/travel/flights")


# ----------------- link_status segue order_flow (não vira direct) -----------------


def test_link_status_still_order_flow_with_google_search():
    # O link de busca NÃO é a oferta travada ⇒ classificação continua
    # order_flow (Duffel), não direct_link. Garante que a máquina de
    # cooldown/agrupamento não trate isto como link real de compra.
    assert link_status_for(_duffel_quote()) == LINK_STATUS_ORDER_FLOW


# ----------------- PR #83: filtro de companhia narra a busca -----------------


def test_duffel_search_url_appends_airline_name_when_known():
    """A oferta Duffel traz `airline='TP'` → URL apende `on TAP Air Portugal`
    e o Google Flights filtra os resultados pra essa cia (search mais próxima
    da oferta exata)."""
    from urllib.parse import unquote_plus
    url = duffel_google_flights_url(_duffel_quote())  # fixture: airline="TP"
    assert url is not None
    q = unquote_plus(url.split("q=", 1)[1])
    assert q.endswith("on TAP Air Portugal")


def test_duffel_search_url_omits_filter_when_airline_unknown():
    """Sigla não mapeada (`ZZ`) ⇒ sem filtro; URL é o legado PR #76 (compat).
    Não inventamos nome de cia."""
    from urllib.parse import unquote_plus
    q = _duffel_quote()
    q.airline = "ZZ"
    url = duffel_google_flights_url(q)
    assert url is not None
    text = unquote_plus(url.split("q=", 1)[1])
    assert " on " not in text.split("class", 1)[1]


def test_duffel_search_url_omits_filter_when_airline_none():
    """Sem `airline` (None): URL idêntica à anterior ao PR #83."""
    from urllib.parse import unquote_plus
    q = _duffel_quote()
    q.airline = None
    url = duffel_google_flights_url(q)
    text = unquote_plus(url.split("q=", 1)[1])
    assert "on " not in text.split("class", 1)[1].lower()


def test_kiwi_direct_link_still_beats_duffel():
    # Se um dia a Kiwi devolver deep_link real, ela continua sendo
    # direct_link (alerta verde imediato) — Duffel não rebaixa esse caminho.
    kiwi = Quote(
        route=Route("GRU", "CDG", "Europa", cabin=Cabin.BUSINESS),
        price_brl=2000.0, deep_link="https://www.kiwi.com/deep?x=1",
        departure_date="2026-09-02", return_date=None, source="kiwi",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    from flight_mapper.notifier import LINK_STATUS_DIRECT
    assert link_status_for(kiwi) == LINK_STATUS_DIRECT
