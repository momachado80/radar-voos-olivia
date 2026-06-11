"""Testes do PR #86 — atalho de busca Kiwi (/deep) nos alertas Duffel.

A Tequila API segue fechada (invitation-only desde 2024) e a via
Travelpayouts exige 50k MAU. O que restou — e é DOCUMENTADO (Travelpayouts
Help Center, "Kiwi.com affiliate links") — é o deep link público de busca:
`https://www.kiwi.com/deep?from=GRU&to=LHR&departure=...[&return=...]`.

Este PR adiciona esse link como SEGUNDO atalho nos alertas Duffel (depois
do Google Flights). Invariantes:
1. É busca pré-preenchida, NÃO oferta travada ⇒ link_status segue
   order_flow. O URL NUNCA entra em quote.deep_link (host kiwi.com seria
   classificado como direct_link e mentiria "compra direta").
2. URL só com rota IATA + datas (públicos). Sem offer_id/token/preço.
3. O formato /deep não documenta cabine ⇒ pra executiva a mensagem avisa
   "ajuste a cabine"; pra econômica (default do Kiwi) não precisa.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from flight_mapper.auxiliary_links import build_kiwi_deep_link
from flight_mapper.detector import CRITERION_CEILING, LEVEL_GOOD, Decision
from flight_mapper.notifier import (
    LINK_STATUS_ORDER_FLOW,
    build_duffel_pending_offer,
    format_alert,
    format_grouped_duffel_pending,
    link_status_for,
)
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType


def _quote(cabin=Cabin.BUSINESS, *, trip=TripType.ROUND_TRIP,
           ret="2026-09-12") -> Quote:
    return Quote(
        route=Route("GRU", "LHR", "Europa", trip_type=trip,
                    cabin=Cabin.BUSINESS),
        price_brl=12000.0, deep_link=None,
        departure_date="2026-09-02", return_date=ret,
        source="duffel", amount=2000.0, currency="EUR",
        amount_brl_estimated=12000.0, fx_rate=6.0,
        cabin=cabin, cabin_confirmed=True, trip_type=trip, airline="BA",
    )


def _decision():
    return Decision(alert=True, reason="x", criterion=CRITERION_CEILING,
                    threshold=14000.0, level=LEVEL_GOOD, score=85)


# ----------------- 1. builder -----------------


def test_kiwi_deep_link_roundtrip_format():
    url = build_kiwi_deep_link(
        Route("GRU", "LHR", "Europa"), "2026-09-02", "2026-09-12",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.kiwi.com"
    assert parsed.path == "/deep"
    q = parse_qs(parsed.query)
    assert q == {
        "from": ["GRU"], "to": ["LHR"],
        "departure": ["2026-09-02"], "return": ["2026-09-12"],
    }


def test_kiwi_deep_link_oneway_omits_return():
    url = build_kiwi_deep_link(Route("GRU", "MIA", "EUA"), "2026-08-05")
    q = parse_qs(urlparse(url).query)
    assert "return" not in q
    assert q["from"] == ["GRU"] and q["to"] == ["MIA"]


def test_kiwi_deep_link_none_when_missing_essentials():
    assert build_kiwi_deep_link(Route("", "LHR", "Europa"), "2026-09-02") is None
    assert build_kiwi_deep_link(Route("GRU", "", "Europa"), "2026-09-02") is None
    assert build_kiwi_deep_link(Route("GRU", "LHR", "Europa"), "") is None
    assert build_kiwi_deep_link(None, "2026-09-02") is None


def test_kiwi_deep_link_only_documented_params():
    """Só from/to/departure/return — nada de cabine/preço/marker inventado.
    Params não documentados podem quebrar silenciosamente; não chutamos."""
    url = build_kiwi_deep_link(
        Route("GRU", "NRT", "Ásia"), "2026-09-02", "2026-09-12",
    )
    q = parse_qs(urlparse(url).query)
    assert set(q.keys()) == {"from", "to", "departure", "return"}


# ----------------- 2. CRÍTICO: busca Kiwi NÃO vira direct_link -----------------


def test_duffel_quote_with_kiwi_search_keeps_order_flow():
    """A trava central do PR: o atalho Kiwi NÃO entra em quote.deep_link.
    Se entrasse, link_status_for veria host kiwi.com e classificaria
    direct_link — push de "compra imediata" pra um link de BUSCA. Mentira.
    """
    q = _quote()
    assert q.deep_link is None  # o builder do alerta não toca o quote
    assert link_status_for(q) == LINK_STATUS_ORDER_FLOW
    # Mesmo depois de formatar o alerta (que gera o link Kiwi), o quote
    # permanece intacto.
    format_alert(q, _decision())
    assert q.deep_link is None
    assert link_status_for(q) == LINK_STATUS_ORDER_FLOW


def test_alert_still_says_order_flow_with_kiwi_link_present():
    msg = format_alert(_quote(), _decision())
    assert "🔗 link_status: order_flow" in msg
    assert "link_status: direct_link" not in msg


# ----------------- 3. alerta standalone -----------------


def test_alert_includes_kiwi_link_after_google_flights():
    msg = format_alert(_quote(), _decision())
    assert '🥝 <a href="https://www.kiwi.com/deep?' in msg
    assert "Buscar no Kiwi" in msg
    # Google Flights continua primeiro (link primário, respeita cabine).
    assert msg.index("Buscar esta oferta no Google Flights") < msg.index(
        "Buscar no Kiwi"
    )


def test_alert_business_warns_kiwi_opens_economy():
    msg = format_alert(_quote(Cabin.BUSINESS), _decision())
    assert "Kiwi abre em econômica — ajuste a cabine" in msg


def test_alert_economy_has_no_cabin_warning():
    msg = format_alert(_quote(Cabin.ECONOMY), _decision())
    assert "Buscar no Kiwi" in msg
    assert "ajuste a cabine" not in msg


def test_alert_oneway_kiwi_url_has_no_return():
    msg = format_alert(
        _quote(trip=TripType.ONE_WAY, ret=None), _decision(),
    )
    import re
    m = re.search(r'href="(https://www\.kiwi\.com/deep[^"]+)"', msg)
    assert m, "esperava o link Kiwi no alerta"
    q = parse_qs(urlparse(m.group(1)).query)
    assert "return" not in q


# ----------------- 4. mensagem agrupada (grouped_push) -----------------


def test_pending_offer_carries_kiwi_url():
    offer = build_duffel_pending_offer(_quote(), _decision())
    assert offer.kiwi_url is not None
    assert offer.kiwi_url.startswith("https://www.kiwi.com/deep?")


def test_grouped_message_renders_kiwi_link_and_warning():
    offer = build_duffel_pending_offer(_quote(Cabin.BUSINESS), _decision())
    msg = format_grouped_duffel_pending([offer])
    assert "Buscar no Kiwi" in msg
    assert "Kiwi abre em econômica — ajuste a cabine" in msg
    # Google Flights primeiro.
    assert msg.index("Buscar no Google Flights") < msg.index("Buscar no Kiwi")


def test_grouped_message_economy_no_warning():
    offer = build_duffel_pending_offer(_quote(Cabin.ECONOMY), _decision())
    msg = format_grouped_duffel_pending([offer])
    assert "Buscar no Kiwi" in msg
    assert "ajuste a cabine" not in msg


# ----------------- 5. sanitização -----------------


def test_kiwi_url_never_contains_sensitive_data():
    offer = build_duffel_pending_offer(_quote(), _decision())
    for sensitive in ("2000", "12000", "EUR", "token", "offer_",
                      "passenger", "payload", "BA"):
        assert sensitive not in offer.kiwi_url, (
            f"URL Kiwi não pode conter {sensitive!r}: {offer.kiwi_url}"
        )
