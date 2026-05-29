"""Testes do PR #68 — alertas de econômica + link_status explícito.

Cobre os requisitos do goal:
1. Alerta business Duffel diz order_flow e sem link direto.
2. Alerta economy Duffel é gerável com cabine econômica.
3. Alerta economy usa headline de econômica, não business.
4. Alerta business inalterado exceto link_status.
5. Provider com deep_link real mostra link clicável (direct_link).
6. Link auxiliar de busca rotulado como não garantido (auxiliary_search).
7. Nenhuma chamada /air/orders.
8. Sem leak token/offer_id/payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.notifier import (
    LINK_STATUS_AUX,
    LINK_STATUS_DIRECT,
    LINK_STATUS_NONE,
    LINK_STATUS_ORDER_FLOW,
    format_alert,
    link_status_for,
)
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    LEVEL_GOOD,
    Decision,
)
from flight_mapper.thresholds import levels_for


_RT_LHR = Route("GRU", "LHR", "Europa",
                trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ceiling(level=LEVEL_GOOD, threshold=3300.0, score=70) -> Decision:
    return Decision(
        alert=True, reason="abaixo do alvo", criterion=CRITERION_CEILING,
        threshold=threshold, level=level, score=score,
    )


def _duffel_quote(cabin: Cabin, *, amount=550.0, brl=3300.0) -> Quote:
    return Quote(
        route=_RT_LHR, price_brl=brl, deep_link=None,
        departure_date="2026-09-02", return_date="2026-09-12",
        source="duffel", amount=amount, currency="EUR",
        amount_brl_estimated=brl, fx_rate=6.0,
        cabin=cabin, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline="TP",
    )


# ----------------- link_status_for unit -----------------


def test_link_status_duffel_is_order_flow():
    assert link_status_for(_duffel_quote(Cabin.BUSINESS)) == LINK_STATUS_ORDER_FLOW
    assert link_status_for(_duffel_quote(Cabin.ECONOMY)) == LINK_STATUS_ORDER_FLOW


def test_link_status_direct_for_real_deep_link():
    q = Quote(
        route=Route("GRU", "CDG", "Europa"), price_brl=2000.0,
        deep_link="https://www.kiwi.com/deep?x=1",
        departure_date="2026-09-02", return_date=None, source="kiwi",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    assert link_status_for(q) == LINK_STATUS_DIRECT


def test_link_status_auxiliary_for_manual_purchase():
    q = Quote(
        route=Route("GRU", "MIA", "EUA"), price_brl=2000.0, deep_link=None,
        departure_date="2026-09-02", return_date=None, source="manual_purchase",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    assert link_status_for(q) == LINK_STATUS_AUX


def test_link_status_none_for_sourceless_no_link():
    q = Quote(
        route=Route("GRU", "MIA", "EUA"), price_brl=2000.0, deep_link=None,
        departure_date="2026-09-02", return_date=None, source="travelpayouts",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.UNKNOWN, cabin_confirmed=False, trip_type=TripType.ONE_WAY,
    )
    assert link_status_for(q) == LINK_STATUS_NONE


# ----------------- economy thresholds (additive) -----------------


def test_economy_thresholds_present_and_separate():
    assert levels_for("GRU-LHR-economy") == {"excellent_brl": 550, "good_brl": 750}
    assert levels_for("GRU-CDG-economy") == {"excellent_brl": 550, "good_brl": 750}
    assert levels_for("GRU-MIA-one_way-economy") == {"excellent_brl": 250, "good_brl": 400}
    # Business thresholds inalterados.
    assert levels_for("GRU-LHR-business") == {"excellent_brl": 1700, "good_brl": 2000}


# ----------------- 1 & 4. business order_flow + link_status -----------------


def test_business_alert_says_order_flow_and_no_direct_link():
    # PR #69: business Duffel order_flow ⇒ 🟡 compra pendente (não 🟢).
    msg = format_alert(_duffel_quote(Cabin.BUSINESS), _ceiling())
    assert "booking_flow: order_flow (sem link direto de compra)" in msg
    assert "🔗 link_status: order_flow" in msg
    assert "🟡 Oferta confirmada, compra pendente" in msg
    assert "EXECUTIVA CONFIRMADA" not in msg
    # Não promete link direto.
    assert "link_status: direct_link" not in msg


# ----------------- 2 & 3. economy alert + headline -----------------


def test_economy_alert_is_pending_with_economy_cabin():
    # PR #69: economy Duffel order_flow também é 🟡 compra pendente — mas a
    # cabine Econômica fica visível no título e no corpo (sinal preservado).
    msg = format_alert(_duffel_quote(Cabin.ECONOMY), _ceiling())
    headline = msg.splitlines()[0]
    assert "🟡 Oferta confirmada, compra pendente — Econômica" in headline
    assert "EXECUTIVA CONFIRMADA" not in headline
    assert "Business" not in headline
    # cabine econômica confirmada explícita.
    assert "cabine econômica confirmada" in msg
    # order_flow + link_status presentes também na econômica.
    assert "🔗 link_status: order_flow" in msg
    assert "compra direta ainda não disponível no robô." in msg
    assert "verificar no Duffel Dashboard" in msg


def test_economy_alert_shows_currency_brl_target_dates_airline():
    msg = format_alert(_duffel_quote(Cabin.ECONOMY), _ceiling(threshold=3300.0))
    assert "550 EUR" in msg              # moeda original
    assert "≈ R$ 3.300" in msg           # estimativa BRL
    assert "alvo R$ 3.300" in msg        # alvo
    assert "2026-09-02 → 2026-09-12" in msg  # datas round-trip
    assert "🛫 Companhia: TP" in msg     # airline
    assert "Duffel" in msg               # provider


# ----------------- 5 & 6. direct link + auxiliary labeling -----------------


def test_direct_link_provider_shows_clickable_link():
    q = Quote(
        route=Route("GRU", "CDG", "Europa"), price_brl=2000.0,
        deep_link="https://www.kiwi.com/deep?x=1",
        departure_date="2026-09-02", return_date=None, source="kiwi",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    msg = format_alert(q, _ceiling(threshold=2400.0))
    assert '🔎 <a href="https://www.kiwi.com/deep?x=1">Conferir busca</a>' in msg
    assert "🔗 link_status: direct_link" in msg


def test_auxiliary_search_link_labeled_not_guaranteed():
    q = Quote(
        route=Route("GRU", "MIA", "EUA"), price_brl=2000.0, deep_link=None,
        departure_date="2026-09-02", return_date=None, source="manual_purchase",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    msg = format_alert(q, _ceiling(threshold=2400.0))
    assert "Links auxiliares de pesquisa, não oferta confirmada." in msg
    assert "🔗 link_status: auxiliary_search" in msg


# ----------------- 7 & 8. provider economy: offer_requests only + no leak -----------------


def test_economy_provider_uses_offer_requests_never_orders():
    payload = {"data": {"offers": [{
        "id": "off_secret", "total_amount": "550", "total_currency": "EUR",
        "owner": {"iata_code": "TP"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T10:00:00",
                           "marketing_carrier": {"iata_code": "TP"},
                           "passengers": [{"cabin_class": "economy"}]}]},
            {"segments": [{"departing_at": "2026-09-12T10:00:00",
                           "marketing_carrier": {"iata_code": "TP"},
                           "passengers": [{"cabin_class": "economy"}]}]},
        ],
    }]}}
    captured: dict = {}

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    provider = DuffelProvider(access_token="tok_x", urlopen_impl=fake_urlopen)
    q = provider.quote_for_dates(
        _RT_LHR, "2026-09-02", "2026-09-12", cabin="economy",
    )
    assert q is not None
    assert q.cabin == Cabin.ECONOMY and q.cabin_confirmed is True
    assert captured["body"]["data"]["cabin_class"] == "economy"
    assert "offer_requests" in captured["url"]
    assert "orders" not in captured["url"] and "payments" not in captured["url"]
    assert captured["method"] == "POST"
    assert "tok_x" not in captured["url"]


def test_economy_alert_no_leak(monkeypatch):
    payload = {"data": {"offers": [{
        "id": "off_fixture_leak", "total_amount": "550", "total_currency": "EUR",
        "owner": {"iata_code": "TP"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T10:00:00",
                           "marketing_carrier": {"iata_code": "TP"},
                           "passengers": [{"cabin_class": "economy",
                                           "passenger_id": "pas_leak"}]}]},
            {"segments": [{"departing_at": "2026-09-12T10:00:00",
                           "marketing_carrier": {"iata_code": "TP"},
                           "passengers": [{"cabin_class": "economy"}]}]},
        ],
    }]}}

    def fake_urlopen(req, timeout=20):
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setenv("EUR_BRL_RATE", "6.0")
    provider = DuffelProvider(
        access_token="sentinel_tok_leak", urlopen_impl=fake_urlopen,
    )
    q = provider.quote_for_dates(
        _RT_LHR, "2026-09-02", "2026-09-12", cabin="economy",
    )
    msg = format_alert(q, _ceiling(threshold=4000.0))
    for sentinel in (
        "off_fixture_leak", "pas_leak", "sentinel_tok_leak",
        "api.duffel.com", "https://", "Bearer", "total_amount",
        "cabin_class", "offer_id", "order_id",
    ):
        assert sentinel not in msg, f"LEAK no alerta economy: {sentinel!r}"
    # PR #69: economy Duffel order_flow ⇒ 🟡 compra pendente.
    assert "🟡 Oferta confirmada, compra pendente" in msg


# ----------------- 4 (cont). non-Duffel economy headline unchanged -----------------


def test_non_duffel_economy_keeps_legacy_headline():
    q = Quote(
        route=Route("GRU", "MIA", "EUA"), price_brl=2000.0, deep_link=None,
        departure_date="2026-09-02", return_date=None, source="mock",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.ECONOMY, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    msg = format_alert(q, Decision(
        alert=True, reason="queda", criterion=CRITERION_AVERAGE_DROP,
        average=3000.0, drop_pct=0.33, score=None,
    ))
    assert "Econômica em promoção" in msg
    assert "💸 ECONÔMICA MUITO BOA" not in msg  # economy headline é só Duffel
