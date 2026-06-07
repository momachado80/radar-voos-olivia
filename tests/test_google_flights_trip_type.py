"""Testes do PR #79 — trip_type explícito no link Google Flights.

Bug real: GRU-MIA one-way de 978 EUR virou round-trip no Google Flights
(R$ 10.212). A query precisa codificar one-way EXPLICITAMENTE p/ o Google
não escolher round-trip por default.

Garante:
1. One-way Duffel: URL contém "one way" + "on YYYY-MM-DD" + cabine.
2. One-way não contém return date / "round trip".
3. Round-trip: URL contém "round trip" + "departing" + "return" + datas.
4. Cabine business/economy preservada (token mantido).
5. Rota GRU-MIA preservada.
6. Telegram rotula "somente ida" no alerta one-way.
7. Telegram rotula "ida e volta" no alerta round-trip.
8. Sem leak token/offer_id/payload/passageiro.
"""

from __future__ import annotations

from urllib.parse import unquote_plus

import pytest

from flight_mapper.auxiliary_links import build_google_flights_query_url
from flight_mapper.detector import (
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    Decision,
)
from flight_mapper.google_flights_link import duffel_google_flights_url
from flight_mapper.notifier import (
    LINK_STATUS_ORDER_FLOW,
    build_duffel_pending_offer,
    format_alert,
    format_grouped_duffel_pending,
    link_status_for,
)
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType


def _quote(
    *, trip: TripType, dep: str = "2026-08-05", ret: str | None = None,
    dest: str = "MIA", cabin: Cabin = Cabin.BUSINESS,
    region: str = "EUA", airline: str = "CM",
) -> Quote:
    return Quote(
        route=Route("GRU", dest, region, trip_type=trip, cabin=Cabin.BUSINESS),
        price_brl=5871.0, deep_link=None, departure_date=dep, return_date=ret,
        source="duffel", amount=978.0, currency="EUR",
        amount_brl_estimated=5871.0, fx_rate=6.0,
        cabin=cabin, cabin_confirmed=True, trip_type=trip, airline=airline,
    )


def _ceiling(threshold: float = 6000.0) -> Decision:
    return Decision(
        alert=True, reason="x", criterion=CRITERION_CEILING,
        threshold=threshold, level=LEVEL_EXCELLENT, score=92,
    )


def _q_param(url: str) -> str:
    return unquote_plus(url.split("q=", 1)[1])


# ---------------- 1 & 2. one-way: explícito + sem return date ----------------


def test_oneway_url_contains_explicit_one_way_intent():
    url = duffel_google_flights_url(_quote(trip=TripType.ONE_WAY))
    q = _q_param(url)
    # Token explícito que o Google reconhece como one-way.
    assert "one way" in q.lower()
    # Data de ida + cabine preservadas.
    assert "2026-08-05" in q
    assert "business class" in q


def test_oneway_url_does_not_invent_return_date():
    url = duffel_google_flights_url(_quote(trip=TripType.ONE_WAY))
    q = _q_param(url).lower()
    # Nenhum "return", "returning", "round trip" — exatamente o bug
    # que fazia o Google abrir round-trip com data fake.
    assert "return" not in q
    assert "round trip" not in q
    # Nenhum hint do tipo "+10 dias" ou data adicional.
    assert q.count("2026-") == 1  # só a data de ida


def test_oneway_url_ignores_stray_return_date_attribute():
    """Mesmo que `return_date` venha preenchido por engano num quote
    one-way, o helper Duffel não deve usá-lo (gate `show_return`)."""
    q = _quote(trip=TripType.ONE_WAY, ret="2026-08-15")
    url = duffel_google_flights_url(q)
    decoded = _q_param(url).lower()
    assert "return" not in decoded
    assert "round trip" not in decoded
    assert "2026-08-15" not in decoded


# ---------------- 3. round-trip: ambas as datas + intenção explícita ----------------


def test_roundtrip_url_contains_explicit_round_trip_and_both_dates():
    q = _quote(
        trip=TripType.ROUND_TRIP, dep="2026-09-02", ret="2026-09-12",
        dest="LHR", region="Europa",
    )
    url = duffel_google_flights_url(q)
    qstr = _q_param(url).lower()
    assert "round trip" in qstr
    assert "2026-09-02" in qstr
    assert "2026-09-12" in qstr
    assert "departing" in qstr or "departure" in qstr
    assert "return" in qstr
    assert "business class" in qstr


# ---------------- 4. cabine preservada (business + economy) ----------------


def test_cabin_business_preserved_in_url():
    url = duffel_google_flights_url(_quote(trip=TripType.ONE_WAY, cabin=Cabin.BUSINESS))
    qstr = _q_param(url).lower()
    assert "business class" in qstr
    assert "economy" not in qstr


def test_cabin_economy_preserved_in_url():
    url = duffel_google_flights_url(_quote(trip=TripType.ONE_WAY, cabin=Cabin.ECONOMY))
    qstr = _q_param(url).lower()
    assert "economy class" in qstr
    assert "business class" not in qstr


# ---------------- 5. rota preservada ----------------


def test_route_origin_destination_preserved():
    url = duffel_google_flights_url(_quote(trip=TripType.ONE_WAY))
    qstr = _q_param(url)
    assert "from GRU to MIA" in qstr


# ---------------- 6 & 7. Telegram label trip_type ----------------


def test_oneway_alert_labels_search_as_somente_ida():
    msg = format_alert(_quote(trip=TripType.ONE_WAY), _ceiling())
    assert "Busca Google Flights: somente ida, cabine executiva." in msg
    # Aviso honesto continua presente (PR #76).
    assert "Preço e disponibilidade podem variar" in msg


def test_roundtrip_alert_labels_search_as_ida_e_volta():
    msg = format_alert(
        _quote(trip=TripType.ROUND_TRIP, dep="2026-09-02", ret="2026-09-12",
               dest="LHR", region="Europa"),
        _ceiling(),
    )
    assert "Busca Google Flights: ida e volta, cabine executiva." in msg


def test_economy_alert_labels_cabin_economica():
    msg = format_alert(
        _quote(trip=TripType.ONE_WAY, cabin=Cabin.ECONOMY), _ceiling(),
    )
    assert "Busca Google Flights: somente ida, cabine econômica." in msg


# ---------------- grouped message also carries the trip label ----------------


def test_grouped_message_includes_trip_type_sublabel():
    offers = [
        build_duffel_pending_offer(
            _quote(trip=TripType.ONE_WAY), _ceiling(),
        ),
        build_duffel_pending_offer(
            _quote(
                trip=TripType.ROUND_TRIP, dep="2026-09-02", ret="2026-09-12",
                dest="LHR", region="Europa",
            ),
            _ceiling(),
        ),
    ]
    text = format_grouped_duffel_pending(offers)
    assert "Busca Google Flights: somente ida" in text
    assert "Busca Google Flights: ida e volta" in text


# ---------------- 8. no leak; link_status stays order_flow ----------------


def test_url_never_leaks_sensitive_data():
    url = duffel_google_flights_url(_quote(trip=TripType.ONE_WAY))
    for sensitive in (
        "978", "5871", "EUR", "BRL", "CM",  # preço/moeda/cia
        "offer", "token", "passenger", "payload",
        "off_", "pas_", "Bearer", "api.duffel.com",
    ):
        assert sensitive not in url, f"URL não pode conter {sensitive!r}: {url}"


def test_link_status_remains_order_flow_with_new_url_format():
    # O atalho de busca Google Flights NÃO promove o quote a direct_link.
    assert link_status_for(_quote(trip=TripType.ONE_WAY)) == LINK_STATUS_ORDER_FLOW
    assert link_status_for(
        _quote(trip=TripType.ROUND_TRIP, ret="2026-09-12")
    ) == LINK_STATUS_ORDER_FLOW


# ---------------- direct helper (auxiliary_links) -----------------


def test_helper_oneway_no_return_token():
    """Sanity direto no builder: one-way nunca produz token 'return'."""
    route = Route("GRU", "MIA", "EUA")
    url = build_google_flights_query_url(route, "2026-08-05", None, cabin=Cabin.BUSINESS)
    qstr = _q_param(url).lower()
    assert "one way" in qstr
    assert "return" not in qstr


def test_helper_roundtrip_has_both_dates():
    route = Route("GRU", "LHR", "Europa")
    url = build_google_flights_query_url(
        route, "2026-09-02", "2026-09-12", cabin=Cabin.BUSINESS,
    )
    qstr = _q_param(url).lower()
    assert "round trip" in qstr
    assert "2026-09-02" in qstr
    assert "2026-09-12" in qstr
