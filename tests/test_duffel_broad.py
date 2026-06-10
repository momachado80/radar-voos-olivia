"""Testes do PR #77 — pool broad de candidatos Duffel.

Garante que:
1. O pool broad inclui Londres e Paris MAS não os prioriza exclusivamente.
2. A rotação cobre mais que Londres/Paris (cap 3 × 4 ciclos = 12 entradas
   distintas).
3. Business E economy estão presentes.
4. One-way E round-trip estão presentes.
5. O link Google Flights do PR #76 continua sendo gerado.
6. `DUFFEL_ROUTE_MODE` aceita broad/watchlist/disabled, fallback p/ broad.
7. A linha do 🧭 muda conforme o pool ("Duffel broad scan: ..." vs
   "Duffel watchlist Londres/Paris: ...").
8. `direct_link` (Kiwi) continua superior — não é afetado pelo pool Duffel.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from flight_mapper.config import Config
from flight_mapper.duffel_broad import (
    BROAD_ROUTE_SPECS,
    DUFFEL_ROUTE_MODE_BROAD,
    DUFFEL_ROUTE_MODE_DISABLED,
    DUFFEL_ROUTE_MODE_WATCHLIST,
    build_broad_candidate_pool,
)
from flight_mapper.duffel_status import (
    DuffelWatchlistSummary,
    humanize_duffel_watchlist_status,
)
from flight_mapper.regions import Cabin, TripType


# ----------------- 1. pool inclui Londres/Paris sem priorizá-los -----------------


def test_broad_pool_includes_london_and_paris():
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    dests = {e.route.destination for e in pool}
    assert "LHR" in dests, "Londres deve continuar monitorada"
    assert "CDG" in dests, "Paris deve continuar monitorada"


def test_broad_pool_covers_all_routes():
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    dests = {e.route.destination for e in pool}
    expected = {dest for dest, _, _ in BROAD_ROUTE_SPECS}
    assert dests == expected
    # PR #81: escopo ampliado — N destinos × 2 cabines × 2 trip_types.
    assert len(pool) == len(BROAD_ROUTE_SPECS) * 4


def test_broad_pool_spans_multiple_regions():
    """PR #81: o pool cobre várias regiões (não só Europa/EUA): América do
    Sul/Central/Norte, Canadá, Europa, Ásia."""
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    regions = {e.route.region for e in pool}
    assert "América do Sul" in regions
    assert "América Central" in regions
    assert "Canadá" in regions
    assert "Ásia" in regions
    assert "Europa" in regions
    assert "EUA" in regions


def test_broad_pool_does_not_prioritize_london_or_paris_exclusively():
    """Londres/Paris continuam monitoradas, mas NÃO nas primeiras slots —
    a ordem começa pela América do Sul (escopo ampliado PR #81)."""
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    dests = [e.route.destination for e in pool]
    # A primeira entrada NÃO é Londres nem Paris.
    assert pool[0].route.destination not in ("LHR", "CDG")
    # Londres e Paris estão no pool, mas depois das Américas.
    assert "LHR" in dests and "CDG" in dests
    assert dests.index("LHR") > 0
    assert dests.index("CDG") > 0
    # Há destinos não-europeus ANTES de Londres na ordem de rotação.
    assert dests.index("LHR") > dests.index("EZE")


# ----------------- 2. rotação cobre mais que Londres/Paris -----------------


def test_rotation_with_cap_three_covers_more_than_london_paris():
    """Simula 4 ciclos com cap=3 e offset rotativo; espera-se que a janela
    cumulativa cubra mais que LHR/CDG."""
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    n = len(pool)
    cap = 3
    seen_dests: set[str] = set()
    offset = 0
    for _ in range(4):  # 4 ciclos × cap 3 = 12 entradas distintas
        for i in range(cap):
            idx = (offset + i) % n
            seen_dests.add(pool[idx].route.destination)
        offset = (offset + cap) % n
    # Cobre pelo menos 4 rotas além de LHR/CDG.
    extras = seen_dests - {"LHR", "CDG"}
    assert len(extras) >= 4, f"rotação ficou estreita: {seen_dests}"


# ----------------- 3 & 4. business+economy, one_way+round_trip -----------------


def test_broad_pool_has_business_and_economy():
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    cabins = {e.cabin for e in pool}
    assert cabins == {"business", "economy"}


def test_broad_pool_has_one_way_and_round_trip():
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    trips = {e.route.trip_type for e in pool}
    assert trips == {TripType.ONE_WAY, TripType.ROUND_TRIP}
    # Round-trip TEM return_date; one-way NÃO.
    for e in pool:
        if e.route.trip_type == TripType.ROUND_TRIP:
            assert e.return_date, f"round-trip sem return_date: {e}"
        else:
            assert not e.return_date, f"one-way com return_date: {e}"


def test_broad_pool_dates_are_dynamic_not_hardcoded_september():
    """As datas não são mais o setembro/2026 hardcoded — derivam de `today`."""
    pool_jun = build_broad_candidate_pool(today=date(2026, 6, 1))
    pool_dec = build_broad_candidate_pool(today=date(2026, 12, 1))
    assert pool_jun[0].outbound_date != pool_dec[0].outbound_date


# ----------------- 6. modos do config -----------------


def test_config_default_route_mode_is_broad(monkeypatch):
    monkeypatch.delenv("DUFFEL_ROUTE_MODE", raising=False)
    assert Config.from_env().duffel_route_mode == DUFFEL_ROUTE_MODE_BROAD


def test_config_invalid_route_mode_falls_back_to_broad(monkeypatch):
    monkeypatch.setenv("DUFFEL_ROUTE_MODE", "screaming_broad")
    assert Config.from_env().duffel_route_mode == DUFFEL_ROUTE_MODE_BROAD


def test_config_accepts_all_three_route_modes(monkeypatch):
    for m in (DUFFEL_ROUTE_MODE_BROAD, DUFFEL_ROUTE_MODE_WATCHLIST,
              DUFFEL_ROUTE_MODE_DISABLED):
        monkeypatch.setenv("DUFFEL_ROUTE_MODE", m.upper())
        assert Config.from_env().duffel_route_mode == m


# ----------------- 7. 🧭 frase conforme o pool -----------------


def test_humanize_uses_broad_scan_prefix():
    s = DuffelWatchlistSummary(
        enabled=True, checked=8, confirmed_alerts=1, business_alerts=1,
        pool="broad",
    )
    line = humanize_duffel_watchlist_status(s)
    assert line is not None
    # Frase do goal: "Duffel broad scan: X rotas consultadas; Y ofertas
    # confirmadas; Z com link Google Flights."
    assert line.startswith("Duffel broad scan:")
    assert "8 rotas consultadas" in line
    assert "1 ofertas confirmadas" in line
    assert "1 com link Google Flights" in line
    # Não é a frase legada da watchlist.
    assert "Londres/Paris" not in line


def test_humanize_keeps_watchlist_prefix_for_opt_in():
    s = DuffelWatchlistSummary(
        enabled=True, checked=2, confirmed_alerts=1, business_alerts=1,
        pool="watchlist",
    )
    line = humanize_duffel_watchlist_status(s)
    assert line.startswith("Duffel watchlist Londres/Paris:")
    assert "1 oferta executiva confirmada" in line


def test_humanize_broad_zero_offers_still_says_routes_consultadas():
    s = DuffelWatchlistSummary(
        enabled=True, checked=3, confirmed_alerts=0, pool="broad",
    )
    line = humanize_duffel_watchlist_status(s)
    assert "Duffel broad scan: 3 rotas consultadas" in line
    assert "0 ofertas confirmadas" in line


# ----------------- 5 & 8. Google Flights link preservado; Kiwi superior -----------------


def test_broad_pool_quote_still_gets_google_flights_url():
    """Uma quote Duffel gerada para a 1ª entrada do pool broad deve produzir
    o link do Google Flights (PR #76 preservado)."""
    from flight_mapper.google_flights_link import duffel_google_flights_url
    from flight_mapper.providers import Quote

    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    e = pool[0]  # primeira entrada do pool (escopo ampliado PR #81)
    q = Quote(
        route=e.route,
        price_brl=4000.0, deep_link=None,
        departure_date=e.outbound_date,
        return_date=e.return_date or None,
        source="duffel",
        amount=730.0, currency="EUR", amount_brl_estimated=4000.0,
        fx_rate=5.5,
        cabin=e.cabin_enum, cabin_confirmed=True,
        trip_type=e.route.trip_type, airline="LA",
    )
    url = duffel_google_flights_url(q)
    assert url is not None
    assert url.startswith("https://www.google.com/travel/flights")
    # URL não tem dado sensível: nada de preço, moeda, token ou offer_id.
    # PR #83: o NOME da companhia ("LATAM") é INTENCIONAL — narra a busca
    # pra cia da oferta (cia é info pública, já exibida no alerta).
    for sensitive in ("730", "4000", "EUR", "token", "offer", "passenger"):
        assert sensitive not in url, url


def test_kiwi_direct_link_still_superior_under_broad_mode():
    """O modo broad é só pool de candidatos do Duffel; direct_link (Kiwi)
    continua sendo classificado como link real de checkout."""
    from flight_mapper.notifier import LINK_STATUS_DIRECT, link_status_for
    from flight_mapper.providers import Quote
    from flight_mapper.regions import Route

    kiwi = Quote(
        route=Route("GRU", "CDG", "Europa", cabin=Cabin.BUSINESS),
        price_brl=2000.0, deep_link="https://www.kiwi.com/deep?x=1",
        departure_date="2026-09-02", return_date=None, source="kiwi",
        amount=2000.0, currency="BRL", amount_brl_estimated=2000.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
    )
    assert link_status_for(kiwi) == LINK_STATUS_DIRECT


# ----------------- estrutural: sem /air/orders, sem PII -----------------


def test_broad_module_never_references_orders_or_payments():
    src = Path(__file__).resolve().parents[1] / "flight_mapper" / "duffel_broad.py"
    text = src.read_text(encoding="utf-8")
    for forbidden in ("/air/orders", "/air/payments", "create_order",
                      "create_payment", "passenger"):
        assert forbidden not in text, f"módulo broad não pode citar {forbidden!r}"


# ----------------- PR #81: cobertura de teto + calibração "só promoção" -----------------


def test_every_broad_entry_has_a_threshold():
    """INVARIANTE CRÍTICA (PR #81): toda entrada do pool — cada combinação
    rota × cabine × trip_type — precisa ter teto em `thresholds.py`. Sem teto,
    `levels_for(threshold_key)` é None → `evaluate_ceiling` devolve alert=False
    e a oferta NUNCA vira alerta (falha silenciosa). Este teste impede que um
    destino entre no pool sem o teto correspondente."""
    from flight_mapper.thresholds import levels_for

    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    faltando = [
        (e.route.destination, e.cabin, e.route.trip_type.value, e.threshold_key)
        for e in pool
        if levels_for(e.threshold_key) is None
    ]
    assert not faltando, f"entradas sem teto (nunca alertariam): {faltando}"


def test_broad_entries_cover_business_and_economy_thresholds():
    """Cada destino do pool tem teto TANTO business QUANTO economy (round-trip
    e one-way), já que a Olivia pediu promoções nas duas cabines."""
    from flight_mapper.thresholds import levels_for

    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    business = {e.threshold_key for e in pool if e.cabin == "business"}
    economy = {e.threshold_key for e in pool if e.cabin == "economy"}
    assert business and economy
    assert all(levels_for(k) is not None for k in business)
    assert all(levels_for(k) is not None for k in economy)
    # Garante que economy não é só Europa: há chave economy de cada região.
    assert "GRU-EZE-economy" in economy           # América do Sul
    assert "GRU-CUN-economy" in economy           # América Central
    assert "GRU-YYZ-economy" in economy           # Canadá
    assert "GRU-NRT-economy" in economy           # Ásia
    assert "GRU-MIA-economy" in economy           # EUA


def _decision_for(threshold_key, price_usd, rate=5.5):
    """Helper: roda o detector de teto (escalado USD→BRL) p/ um preço em USD
    e devolve se dispara alerta — reproduz o gate real do pass Duffel."""
    from flight_mapper.detector import evaluate_ceiling
    from flight_mapper.state import RouteHistory

    price_brl = price_usd * rate
    decision = evaluate_ceiling(
        RouteHistory(), price_brl, threshold_key, priority=False, brl_rate=rate,
    )
    return decision.alert


def test_promo_offers_alert_expensive_offers_stay_silent():
    """Calibração "só promoções (poucas, boas)": preço de promo dispara;
    preço de tabela fica em silêncio. Cobre as novas regiões em economy E
    business (USD escalado por uma taxa fixa de teste)."""
    # (threshold_key, preço_promo_USD_alerta, preço_caro_USD_silencio)
    casos = [
        ("GRU-EZE-economy", 170, 400),    # América do Sul econômica
        ("GRU-EZE-business", 580, 1200),  # América do Sul executiva
        ("GRU-CUN-economy", 350, 700),    # América Central econômica
        ("GRU-NRT-economy", 720, 1300),   # Ásia econômica
        ("GRU-NRT-business", 2300, 3500), # Ásia executiva
        ("GRU-YYZ-economy", 480, 900),    # Canadá econômica
        ("GRU-MIA-economy", 380, 800),    # EUA econômica
    ]
    for tkey, promo, caro in casos:
        assert _decision_for(tkey, promo), f"{tkey} {promo}USD devia ALERTAR"
        assert not _decision_for(tkey, caro), f"{tkey} {caro}USD devia silenciar"


def test_economy_thresholds_are_tighter_than_business_same_route():
    """Sanidade da calibração: para o mesmo destino, o teto economy é menor
    que o business (econômica boa custa menos que executiva boa)."""
    from flight_mapper.thresholds import levels_for

    for dest in ("EZE", "NRT", "MIA", "YYZ", "CUN"):
        eco = levels_for(f"GRU-{dest}-economy")["good_brl"]
        biz = levels_for(f"GRU-{dest}-business")["good_brl"]
        assert eco < biz, f"economy {dest} ({eco}) deveria ser < business ({biz})"
