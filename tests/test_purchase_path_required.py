"""Testes do PR #69 — exigir caminho de compra antes de alerta verde acionável.

Regra de produto: oferta sem caminho de compra direto NÃO é alerta verde
totalmente acionável; é "oferta confirmada, compra pendente". Duffel
(booking_flow=order_flow) cai nessa categoria; providers com deep_link real
seguem verdes acionáveis.

Cobre os requisitos do goal:
1. Duffel order_flow NÃO é alerta verde totalmente acionável.
2. Duffel order_flow aparece como confirmado com compra pendente.
3. Provider com link direto segue verde acionável.
4. Wording inclui "compra direta ainda não disponível".
5. Não insinua "clique para comprar" para Duffel.
6. Nenhuma chamada /air/orders.
7. Sem leak token/offer_id/payload.
8. Detecção Duffel existente ainda funciona.
"""

from __future__ import annotations

import json

import pytest

from flight_mapper.detector import (
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    LEVEL_GOOD,
    Decision,
)
from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.notifier import format_alert, link_status_for, LINK_STATUS_ORDER_FLOW
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType


_RT = Route("GRU", "LHR", "Europa",
            trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS)


def _ceiling(level=LEVEL_GOOD, threshold=14400.0, score=70) -> Decision:
    return Decision(
        alert=True, reason="abaixo do alvo", criterion=CRITERION_CEILING,
        threshold=threshold, level=level, score=score,
    )


def _duffel_quote(cabin=Cabin.BUSINESS) -> Quote:
    return Quote(
        route=_RT, price_brl=5784.0, deep_link=None,
        departure_date="2026-09-02", return_date="2026-09-12",
        source="duffel", amount=964.0, currency="EUR",
        amount_brl_estimated=5784.0, fx_rate=6.0,
        cabin=cabin, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline="AF",
    )


def _kiwi_direct_quote() -> Quote:
    return Quote(
        route=Route("GRU", "CDG", "Europa", cabin=Cabin.BUSINESS),
        price_brl=2000.0, deep_link="https://www.kiwi.com/deep?x=1",
        departure_date="2026-09-02", return_date=None, source="kiwi",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )


# ----------------- 1 & 2. Duffel order_flow → pendente, não verde -----------------


def test_duffel_order_flow_not_fully_actionable_green():
    msg = format_alert(_duffel_quote(), _ceiling())
    # Não rotula como verde totalmente acionável.
    assert "🟢 EXECUTIVA CONFIRMADA" not in msg
    assert "💸 ECONÔMICA MUITO BOA" not in msg
    # link_status é order_flow, não direct_link.
    assert "🔗 link_status: order_flow" in msg
    assert "link_status: direct_link" not in msg


def test_duffel_order_flow_appears_as_purchase_pending():
    msg = format_alert(_duffel_quote(), _ceiling())
    headline = msg.splitlines()[0]
    assert "🟡 Oferta confirmada, compra pendente" in headline
    assert "booking_flow: order_flow" in msg
    assert "Ação: verificar no Duffel Dashboard." in msg
    # Resumo honesto.
    assert "Oferta confirmada, mas sem caminho de compra direto." in msg


def test_duffel_economy_order_flow_also_pending():
    headline = format_alert(_duffel_quote(Cabin.ECONOMY), _ceiling()).splitlines()[0]
    assert "🟡 Oferta confirmada, compra pendente" in headline
    assert "💸 ECONÔMICA MUITO BOA" not in headline


# ----------------- 3. direct-link provider remains green actionable -----------------


def test_direct_link_provider_remains_green_actionable():
    msg = format_alert(_kiwi_direct_quote(), _ceiling(level=LEVEL_EXCELLENT, threshold=2400.0))
    # Mantém o caminho acionável: nível forte + link clicável.
    assert "🚨 EXCELENTE" in msg or "🎯 BOM" in msg
    assert '🔎 <a href="https://www.kiwi.com/deep?x=1">Conferir busca</a>' in msg
    assert "🔗 link_status: direct_link" in msg
    # Não é rebaixado a compra pendente.
    assert "compra pendente" not in msg


def test_link_status_for_duffel_is_order_flow():
    assert link_status_for(_duffel_quote()) == LINK_STATUS_ORDER_FLOW


# ----------------- 4 & 5. wording + não insinua clique para comprar -----------------


def test_wording_includes_compra_direta_indisponivel():
    msg = format_alert(_duffel_quote(), _ceiling())
    assert "compra direta ainda não disponível" in msg


def test_duffel_does_not_imply_click_to_buy():
    msg = format_alert(_duffel_quote(), _ceiling())
    lowered = msg.lower()
    assert "clique para comprar" not in lowered
    assert "clique e compre" not in lowered
    assert "comprar agora" not in lowered
    # Sem hyperlink de compra/checkout para Duffel.
    assert "<a href=" not in msg
    # Não diz "sem compra automática" como se fosse compra (wording antiga).
    assert "sem compra automática" not in msg


# ----------------- 6 & 7. no /air/orders + no leak (real provider) -----------------


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_no_orders_call_and_no_leak_end_to_end(monkeypatch):
    payload = {"data": {"offers": [{
        "id": "off_secret_69", "total_amount": "964", "total_currency": "EUR",
        "owner": {"iata_code": "AF"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T10:00:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business",
                                           "passenger_id": "pas_secret_69"}]}]},
            {"segments": [{"departing_at": "2026-09-12T10:00:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business"}]}]},
        ],
    }]}}
    captured: dict = {}

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setenv("EUR_BRL_RATE", "6.0")
    provider = DuffelProvider(
        access_token="sentinel_tok_69", urlopen_impl=fake_urlopen,
    )
    q = provider.quote_for_dates(_RT, "2026-09-02", "2026-09-12", cabin="business")
    # 6. só offer_requests, nunca orders/payments.
    assert "offer_requests" in captured["url"]
    assert "orders" not in captured["url"] and "payments" not in captured["url"]
    assert captured["method"] == "POST"
    # 7. alerta não vaza nada sensível.
    msg = format_alert(q, _ceiling(threshold=8000.0))
    for sentinel in (
        "off_secret_69", "pas_secret_69", "sentinel_tok_69",
        "api.duffel.com", "https://", "Bearer", "total_amount",
        "cabin_class", "offer_id", "order_id",
    ):
        assert sentinel not in msg, f"LEAK PR#69: {sentinel!r}"
    # 8. detecção Duffel ainda funciona (cabine business confirmada + preço).
    assert q is not None
    assert q.cabin == Cabin.BUSINESS and q.cabin_confirmed is True
    assert "🟡 Oferta confirmada, compra pendente" in msg
