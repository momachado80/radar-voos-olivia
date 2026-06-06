"""Testes do PR #78 — semântica do relatório diário com Duffel + Google Flights.

Travamos:
1. Oferta Duffel confirmada bloqueia "nenhuma cabine confirmada" no rodapé.
2. Rodapé menciona oferta Duffel confirmada com busca Google Flights.
3. Genérico vs broad scan sem contradição.
4. Linha-resumo "Duffel total" combina genérico + broad.
5. SerpApi orçamento esgotado NÃO diz "gastou X queries neste ciclo".
6. Link Google Flights (PR #76) intacto e rotulado como busca pré-preenchida.
7. link_status segue order_flow.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flight_mapper.cycle_summary import CycleSnapshot, compute_changes
from flight_mapper.duffel_status import (
    DUFFEL_ALERT_SENT,
    DUFFEL_ABOVE_THRESHOLD,
    DuffelStatusSummary,
    DuffelWatchlistSummary,
    humanize_duffel_status,
    humanize_duffel_total_status,
    humanize_duffel_watchlist_status,
)
from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import _no_alert_reason, _source_status_block


# ---------------- 1 & 2. Rodapé final ----------------


def test_footer_does_not_say_no_cabin_confirmed_when_duffel_has_offer():
    """Quando o Duffel acabou de confirmar uma oferta, o rodapé NÃO pode
    dizer "nenhuma cabine confirmada" — o sinal ESTÁ confirmado, só não
    tem link de compra direto."""
    result = MonitorResult(
        scanned=10, quotes_received=8, alerts_sent=0, cabin_blocked=5,
    )
    note = _no_alert_reason(result, duffel_pending_count=1)
    assert "nenhuma cabine confirmada" not in note


def test_footer_mentions_duffel_offer_with_google_flights():
    result = MonitorResult(
        scanned=10, quotes_received=8, alerts_sent=0, cabin_blocked=5,
    )
    note = _no_alert_reason(result, duffel_pending_count=1)
    assert note == (
        "ℹ️ Sem link direto de compra. Há 1 oferta Duffel confirmada "
        "com busca Google Flights."
    )


def test_footer_plural_phrasing_for_multiple_duffel_offers():
    result = MonitorResult(scanned=10, quotes_received=8, alerts_sent=0)
    note = _no_alert_reason(result, duffel_pending_count=3)
    assert "3 ofertas Duffel confirmadas com busca Google Flights" in note


def test_footer_direct_link_alert_wording():
    result = MonitorResult(scanned=1, quotes_received=1, alerts_sent=1)
    note = _no_alert_reason(result, duffel_pending_count=0)
    # PR #78: explícito que é link direto (Kiwi/composto), não Google Flights.
    assert "link direto" in note
    assert note.startswith("🔥")


def test_footer_fallback_when_no_duffel_and_no_alert():
    # Caminho legado: sem Duffel, sem alerta, há cabin_blocked → frase antiga.
    result = MonitorResult(
        scanned=10, quotes_received=8, alerts_sent=0, cabin_blocked=4,
    )
    note = _no_alert_reason(result, duffel_pending_count=0)
    assert "nenhuma cabine confirmada" in note


# ---------------- 3. Genérico vs broad sem contradição ----------------


def test_broad_says_no_new_offers_when_generic_already_confirmed():
    """Bug do relatório real: broad scan diz '0 confirmadas' quando o
    genérico já achou 1 — soa como se o Duffel não tivesse achado nada."""
    summary = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=0, pool="broad",
    )
    line = humanize_duffel_watchlist_status(summary, generic_confirmed=1)
    # NÃO diz "0 ofertas confirmadas" (que soa contraditório com o genérico).
    assert "0 ofertas confirmadas" not in line
    assert "3 rotas consultadas" in line
    assert "sem novas ofertas neste bloco" in line


def test_broad_keeps_zero_offers_phrase_when_generic_also_zero():
    """Se ninguém achou nada, a frase explícita 'Y=0 confirmadas' está OK."""
    summary = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=0, pool="broad",
    )
    line = humanize_duffel_watchlist_status(summary, generic_confirmed=0)
    assert "0 ofertas confirmadas" in line
    assert "0 com link Google Flights" in line


# ---------------- 4. Linha-resumo "Duffel total" ----------------


def test_duffel_total_combines_generic_and_broad():
    gen = DuffelStatusSummary(
        enabled=True, requests=1, confirmed_alerts=1, outcome=DUFFEL_ALERT_SENT,
    )
    broad = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=2,
        business_alerts=1, economy_alerts=1, pool="broad",
    )
    line = humanize_duffel_total_status(gen, broad)
    assert line == (
        "Duffel total: 3 ofertas confirmadas com busca Google Flights; "
        "link_status=order_flow."
    )


def test_duffel_total_singular_when_only_one_offer():
    gen = DuffelStatusSummary(
        enabled=True, requests=1, confirmed_alerts=1, outcome=DUFFEL_ALERT_SENT,
    )
    broad = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=0, pool="broad",
    )
    line = humanize_duffel_total_status(gen, broad)
    assert "1 oferta confirmada" in line
    assert "link_status=order_flow" in line


def test_duffel_total_none_when_nothing_confirmed():
    gen = DuffelStatusSummary(
        enabled=True, requests=1, confirmed_alerts=0,
        outcome=DUFFEL_ABOVE_THRESHOLD,
    )
    broad = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=0, pool="broad",
    )
    # Sem oferta nenhuma → omite a linha (evita repetição com as outras).
    assert humanize_duffel_total_status(gen, broad) is None


# ---------------- 5. SerpApi esgotado não diz "gastou X queries" ----------------


def test_serpapi_exhausted_replaces_gastou_phrase():
    """Bug real: o snapshot anterior tinha 87 queries, o atual tem 90
    (esgotado). O delta = 3 era ruído — não houve gasto novo no ciclo."""
    prev = CycleSnapshot(
        snapshot_at="2026-05-30T10:00:00+00:00", latest_prices={},
        manual_check_keys=(), serpapi_used=87, serpapi_elevated=0,
        serpapi_budget_exhausted=False,
    )
    curr = CycleSnapshot(
        snapshot_at="2026-06-01T10:00:00+00:00", latest_prices={},
        manual_check_keys=(), serpapi_used=90, serpapi_elevated=0,
        serpapi_budget_exhausted=True,
    )
    changes = compute_changes(prev, curr, serpapi_monthly_budget=90)
    # Frase do goal.
    assert any(
        "SerpApi já consumiu 90/90 queries no mês; validação pausada." in c
        for c in changes
    )
    # E não a frase enganosa.
    for c in changes:
        assert "gastou" not in c


def test_serpapi_gastou_phrase_still_used_when_not_exhausted():
    """Sanity: se NÃO está esgotado, a frase antiga continua válida."""
    prev = CycleSnapshot(
        snapshot_at="2026-05-30T10:00:00+00:00", latest_prices={},
        manual_check_keys=(), serpapi_used=10, serpapi_elevated=0,
    )
    curr = CycleSnapshot(
        snapshot_at="2026-06-01T10:00:00+00:00", latest_prices={},
        manual_check_keys=(), serpapi_used=13, serpapi_elevated=0,
        serpapi_budget_exhausted=False,
    )
    changes = compute_changes(prev, curr, serpapi_monthly_budget=90)
    assert any("gastou 3 queries" in c for c in changes)


# ---------------- integração: 🧭 ponta a ponta ----------------


def test_source_block_renders_three_distinct_duffel_lines(tmp_path):
    store = PriceStore(tmp_path / "s.json")
    gen = DuffelStatusSummary(
        enabled=True, requests=1, confirmed_alerts=1, outcome=DUFFEL_ALERT_SENT,
    )
    broad = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=0, pool="broad",
    )
    block = _source_status_block(
        store, [], [], duffel_summary=gen, duffel_watchlist_summary=broad,
    )
    # As três linhas Duffel, sem contradição entre si.
    assert "Duffel genérico: 1 oferta confirmada com busca Google Flights." in block
    assert "Duffel broad scan: 3 rotas consultadas; sem novas ofertas neste bloco." in block
    assert "Duffel total: 1 oferta confirmada com busca Google Flights; link_status=order_flow." in block
    # Garantia "link_status=order_flow" presente em algum lugar.
    assert "link_status=order_flow" in block


# ---------------- 6 & 7. PR #76 preservado ----------------


def test_google_flights_helper_still_produces_search_url():
    """O helper do PR #76 continua intacto — link de busca pré-preenchida."""
    from flight_mapper.google_flights_link import duffel_google_flights_url
    from flight_mapper.providers import Quote
    from flight_mapper.regions import Cabin, Route, TripType

    q = Quote(
        route=Route("GRU", "MIA", "EUA", cabin=Cabin.BUSINESS),
        price_brl=5870.0, deep_link=None,
        departure_date="2026-08-04", return_date=None,
        source="duffel", amount=978.0, currency="EUR",
        amount_brl_estimated=5870.0, fx_rate=6.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ONE_WAY, airline="CM",
    )
    url = duffel_google_flights_url(q)
    assert url.startswith("https://www.google.com/travel/flights")


def test_link_status_stays_order_flow_for_duffel():
    """`link_status` não muda — Google Flights é busca, não checkout."""
    from flight_mapper.notifier import LINK_STATUS_ORDER_FLOW, link_status_for
    from flight_mapper.providers import Quote
    from flight_mapper.regions import Cabin, Route, TripType

    q = Quote(
        route=Route("GRU", "MIA", "EUA", cabin=Cabin.BUSINESS),
        price_brl=5870.0, deep_link=None,
        departure_date="2026-08-04", return_date=None, source="duffel",
        amount=978.0, currency="EUR", amount_brl_estimated=5870.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ONE_WAY, airline="CM",
    )
    assert link_status_for(q) == LINK_STATUS_ORDER_FLOW
