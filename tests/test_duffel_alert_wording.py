"""Testes do PR #66 — polimento da copy do alerta Duffel confirmado.

Foca SÓ em formatação/copy (notifier + formatting). Não toca detector,
pricing, provider, workflows nem data/*.

Cobre os requisitos do goal:
1. alerta Duffel EUR mostra o preço original em EUR;
2. mostra a estimativa BRL quando há fx_rate;
3. NÃO diz "moeda não confirmada";
4. mantém "Oferta confirmada por Duffel; sem compra automática.";
5. mantém "verificar no Duffel Dashboard.";
6. não expõe URL/token/offer_id/payload;
7. alerta NÃO-Duffel permanece inalterado;
+ headline enfatiza EXECUTIVA CONFIRMADA (score vira linha secundária).
"""

from __future__ import annotations

import pytest

from flight_mapper.detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    LEVEL_GOOD,
    Decision,
)
from flight_mapper.formatting import (
    format_eur,
    format_fx_line,
    format_price,
)
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType


def _duffel_eur_quote(*, fx_rate=6.0, amount_brl=5784.0, airline="CM") -> Quote:
    return Quote(
        route=Route("GRU", "MIA", "EUA",
                    trip_type=TripType.ONE_WAY, cabin=Cabin.BUSINESS),
        price_brl=amount_brl if amount_brl is not None else 964.0,
        deep_link=None, departure_date="2026-09-10", return_date=None,
        source="duffel", amount=964.0, currency="EUR",
        amount_brl_estimated=amount_brl, fx_rate=fx_rate,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ONE_WAY, airline=airline,
    )


def _ceiling_good(score=40) -> Decision:
    return Decision(
        alert=True, reason="abaixo do alvo", criterion=CRITERION_CEILING,
        threshold=6000.0, level=LEVEL_GOOD, score=score,
    )


# ----------------- format_price / format_eur unit -----------------


def test_format_eur_basic():
    assert format_eur(964.0) == "964 EUR"
    assert format_eur(5784.0) == "5.784 EUR"


def test_format_price_eur_with_estimate():
    assert format_price(964.0, "EUR", 5784.0, 6.0) == "964 EUR ≈ R$ 5.784"


def test_format_price_eur_without_estimate_is_not_unconfirmed():
    out = format_price(964.0, "EUR", None, None)
    assert "moeda não confirmada" not in out
    assert "964 EUR" in out
    assert "EUR_BRL_RATE" in out  # explica que falta câmbio


def test_format_fx_line_currency_param():
    assert format_fx_line(6.0, "EUR") == "Câmbio usado: EUR_BRL_RATE=6.00"
    # back-compat: sem currency → USD.
    assert format_fx_line(5.5) == "Câmbio usado: USD_BRL_RATE=5.50"


def test_format_price_usd_unchanged():
    # Regressão: USD continua idêntico.
    assert format_price(1878.0, "USD", 10329.0, 5.5) == "US$ 1.878 ≈ R$ 10.329"


# ----------------- 1,2,3. EUR price wording -----------------


def test_duffel_eur_alert_renders_original_eur_price():
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    assert "964 EUR" in msg


def test_duffel_eur_alert_renders_brl_estimate_with_fx():
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    assert "≈ R$ 5.784" in msg
    assert "EUR_BRL_RATE=6.00" in msg


def test_duffel_eur_alert_does_not_say_moeda_nao_confirmada():
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    assert "moeda não confirmada" not in msg


def test_duffel_eur_price_line_exact_format():
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    assert (
        "💰 964 EUR ≈ R$ 5.784 (câmbio EUR_BRL_RATE=6.00; alvo R$ 6.000)"
        in msg
    )
    # Câmbio NÃO deve duplicar em linha própria.
    assert "Câmbio usado:" not in msg


def test_duffel_eur_without_fx_still_no_unconfirmed():
    # fx ausente → BRL indisponível, mas nunca "moeda não confirmada".
    q = _duffel_eur_quote(fx_rate=None, amount_brl=None)
    msg = format_alert(q, _ceiling_good())
    assert "moeda não confirmada" not in msg
    assert "964 EUR" in msg


# ----------------- 4,5. mantém copy obrigatória -----------------


def test_duffel_alert_links_to_google_flights():
    # PR #76: oferta Duffel confirmada → link de busca pré-preenchida no GF.
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    assert "Buscar esta oferta no Google Flights" in msg
    assert "google.com/travel/flights" in msg
    assert (
        "Busca pré-preenchida a partir da oferta confirmada pela Duffel" in msg
    )


def test_duffel_alert_keeps_order_flow_link_status():
    # O link_status segue order_flow (GF é busca, não a oferta travada).
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    assert "🔗 link_status: order_flow" in msg


# ----------------- headline emphasis + score secondary -----------------


def test_duffel_headline_is_pending_not_score():
    # PR #76: Duffel order_flow → 🟡 "buscar no Google Flights" (não green);
    # o score nunca vai no título.
    msg = format_alert(_duffel_eur_quote(), _ceiling_good(score=40))
    headline = msg.splitlines()[0]
    assert "🟡 Oferta confirmada" in headline
    assert "buscar no Google Flights" in headline
    assert "EXECUTIVA CONFIRMADA" not in headline
    assert "Score" not in headline
    assert "🎯 BOM" not in headline
    assert "🚨 EXCELENTE" not in headline


def test_duffel_score_is_secondary_line():
    msg = format_alert(_duffel_eur_quote(), _ceiling_good(score=40))
    assert "Score operacional: 40/100" in msg


def test_duffel_headline_pending_even_when_excellent():
    # Mesmo preço excelente, Duffel order_flow ⇒ segue 🟡 (busca GF), não 🟢.
    d = Decision(
        alert=True, reason="excelente", criterion=CRITERION_CEILING,
        threshold=6000.0, level=LEVEL_EXCELLENT, score=92,
    )
    headline = format_alert(_duffel_eur_quote(), d).splitlines()[0]
    assert "🟡 Oferta confirmada" in headline
    assert "buscar no Google Flights" in headline
    assert "EXECUTIVA CONFIRMADA" not in headline
    assert "Score" not in headline


def test_duffel_no_score_line_when_score_none():
    d = Decision(
        alert=True, reason="abaixo do alvo", criterion=CRITERION_CEILING,
        threshold=6000.0, level=LEVEL_GOOD, score=None,
    )
    msg = format_alert(_duffel_eur_quote(), d)
    assert "Score operacional" not in msg
    assert "🟡 Oferta confirmada" in msg
    assert "buscar no Google Flights" in msg


# ----------------- 6. no leak (sensitive sentinels only) -----------------


def test_duffel_alert_no_leak():
    # PR #76: o alerta agora tem o link legítimo do Google Flights, então
    # `https://` deixa de ser sentinela. Checamos os SENSÍVEIS + que o único
    # host é o google.com.
    msg = format_alert(_duffel_eur_quote(), _ceiling_good())
    for sentinel in (
        "Bearer", "api.duffel.com",
        "offer_id", "off_", "order_id", "token", "?token=",
        "passenger", "total_amount", "cabin_class",
    ):
        assert sentinel not in msg, f"LEAK no alerta Duffel: {sentinel!r}"
    import re
    hosts = re.findall(r'href="https://([^/"]+)', msg)
    # PR #86: Kiwi /deep entrou como segundo atalho de busca — host
    # intencional (URL só com rota+datas). Whitelist: Google + Kiwi.
    assert hosts and all(
        h in ("www.google.com", "www.kiwi.com") for h in hosts
    ), hosts


# ----------------- 7. não-Duffel inalterado -----------------


def test_non_duffel_business_usd_alert_unchanged():
    """Travelpayouts/Kiwi-estilo (USD business confirmado) preserva o
    título com nível + Score e a linha de câmbio própria."""
    q = Quote(
        route=Route("GRU", "JFK", "EUA", cabin=Cabin.BUSINESS),
        price_brl=10000.0, deep_link="https://www.kiwi.com/x",
        departure_date="2026-09-10", return_date="2026-09-20",
        source="kiwi", amount=1878.0, currency="USD",
        amount_brl_estimated=10329.0, fx_rate=5.5,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP,
    )
    d = Decision(
        alert=True, reason="excelente", criterion=CRITERION_CEILING,
        threshold=11000.0, level=LEVEL_EXCELLENT, score=88,
    )
    msg = format_alert(q, d)
    headline = msg.splitlines()[0]
    # Mantém o comportamento legado: nível + score no título.
    assert "🚨 EXCELENTE — Score 88/100" in headline
    assert "Business em promoção" in headline
    assert "EXECUTIVA CONFIRMADA" not in msg
    # Câmbio USD continua em linha própria (não embutido no preço).
    assert "Câmbio usado: USD_BRL_RATE=5.50" in msg
    # Preço USD inalterado.
    assert "US$ 1.878 ≈ R$ 10.329" in msg


def test_non_duffel_economy_alert_unchanged():
    q = Quote(
        route=Route("GRU", "MIA", "EUA", cabin=Cabin.BUSINESS),
        price_brl=2000.0, deep_link=None,
        departure_date="2026-09-10", return_date=None,
        source="mock", amount=2000.0, currency="BRL",
        amount_brl_estimated=2000.0,
        cabin=Cabin.ECONOMY, cabin_confirmed=True,
        trip_type=TripType.ONE_WAY,
    )
    d = Decision(
        alert=True, reason="queda", criterion=CRITERION_AVERAGE_DROP,
        average=3000.0, drop_pct=0.33, score=None,
    )
    msg = format_alert(q, d)
    assert "Econômica em promoção" in msg
    assert "EXECUTIVA CONFIRMADA" not in msg
