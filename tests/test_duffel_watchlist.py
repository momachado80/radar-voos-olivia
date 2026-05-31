"""Testes do PR #67 — watchlist premium Londres/Paris setembro no Duffel.

Cobre os requisitos do goal:
1. Watchlist constrói exatamente as 8 combinações rota/datas.
2. Watchlist é consultada ANTES da rota genérica.
3. Datas round-trip renderizam corretamente no Telegram.
4. Oferta confirmada da watchlist envia 🟢.
5. Sem oferta → linha de status segura (não falha).
6. Cap respeitado.
7. Nenhuma chamada /air/orders.
8. Sem leak: token/offer_id/URL/payload/passageiro.
9. Comportamento Duffel GRU-MIA preservado após a watchlist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.duffel_status import (
    DuffelWatchlistSummary,
    humanize_duffel_watchlist_status,
)
from flight_mapper.duffel_watchlist import (
    DuffelWatchEntry,
    DuffelWatchlistState,
    build_september_watchlist,
)
from flight_mapper.monitor import DUFFEL_PROVEN_ROUTE, Monitor
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message, _source_status_block


# ----------------- helpers -----------------


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages: list[str] = []   # standalone (send_alert)
        self.grouped: list[str] = []    # agrupadas (send) — PR #71

    def send_alert(self, quote, decision, priority=False) -> bool:
        self.messages.append(format_alert(quote, decision, priority=priority))
        return self.ok

    def send(self, text) -> bool:
        self.grouped.append(text)
        return self.ok


class _ScriptedDuffel:
    """Provider de teste com quote_for_dates (watchlist) + quote (genérico).
    Registra a ORDEM das chamadas para verificar priorização."""

    def __init__(self, by_dates=None, by_route=None):
        self.calls: list[tuple] = []
        self._by_dates = by_dates or {}
        self._by_route = by_route or {}

    def quote_for_dates(self, route, outbound_date, return_date=None, *, cabin="business"):
        self.calls.append(("dates", route.key, outbound_date, return_date, cabin))
        return self._by_dates.get((route.key, outbound_date, return_date))

    def quote(self, route):
        self.calls.append(("route", route.key))
        return self._by_route.get(route.key)


def _wl_quote(route, ob, ret, *, price_brl=1500.0, airline="AF") -> Quote:
    """Quote round-trip business confirmado p/ uma entrada de watchlist.

    Preço BRL-nativo abaixo dos tetos LHR (1700) / CDG (2400) → dispara
    o ceiling (alerta), exercitando o caminho 🟢."""
    return Quote(
        route=route, price_brl=price_brl, deep_link=None,
        departure_date=ob, return_date=ret, source="duffel",
        amount=price_brl, currency="BRL", amount_brl_estimated=price_brl,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline=airline,
    )


def _monitor(provider, notifier, tmp_path, *, gen_cap=0, wl=None,
             wl_cap=0, wl_state=None, mode="grouped_push"):
    # PR #73: o default de produto virou `daily_only` (sem push standalone).
    # Estes testes documentam a mensagem AGRUPADA do PR #71, hoje opt-in
    # via `grouped_push`; por isso o helper força esse modo.
    return Monitor(
        provider=object(), notifier=notifier,
        store=PriceStore(tmp_path / "main.json"),
        duffel_provider=provider,
        duffel_store=PriceStore(tmp_path / "duffel.json"),
        duffel_max_requests=gen_cap,
        duffel_watchlist=wl or [],
        duffel_watchlist_max_requests=wl_cap,
        duffel_watchlist_state=wl_state,
        duffel_order_flow_alert_mode=mode,
    )


# ----------------- 1. build 8 combos -----------------


def test_watchlist_builds_exactly_eight_combinations():
    wl = build_september_watchlist()
    assert len(wl) == 8
    combos = {
        (e.route.origin, e.route.destination, e.route.trip_type,
         e.outbound_date, e.return_date)
        for e in wl
    }
    expected = set()
    for dest in ("LHR", "CDG"):
        for ob in ("2026-09-02", "2026-09-03"):
            for ret in ("2026-09-12", "2026-09-13"):
                expected.add(("GRU", dest, TripType.ROUND_TRIP, ob, ret))
    assert combos == expected
    # Todas business round-trip.
    assert all(e.route.cabin == Cabin.BUSINESS for e in wl)
    assert all(e.route.trip_type == TripType.ROUND_TRIP for e in wl)


def test_watchlist_order_lhr_before_cdg():
    wl = build_september_watchlist()
    assert [e.route.destination for e in wl] == (
        ["LHR"] * 4 + ["CDG"] * 4
    )
    # Primeiras 4 são LHR nas 4 combinações de data, em ordem.
    assert (wl[0].outbound_date, wl[0].return_date) == ("2026-09-02", "2026-09-12")
    assert (wl[3].outbound_date, wl[3].return_date) == ("2026-09-03", "2026-09-13")


def test_watchlist_history_keys_are_date_specific_and_isolated():
    wl = build_september_watchlist()
    keys = [e.history_key for e in wl]
    assert len(set(keys)) == 8  # todas distintas
    assert all("::duffel::" in k for k in keys)
    assert keys[0] == "GRU-LHR-business::duffel::2026-09-02_2026-09-12"


# ----------------- 2. watchlist before generic -----------------


def test_watchlist_queried_before_generic_route(tmp_path):
    wl = build_september_watchlist()
    provider = _ScriptedDuffel()  # tudo None → sem alertas, só registra ordem
    monitor = _monitor(
        provider, _StubNotifier(), tmp_path,
        gen_cap=1, wl=wl, wl_cap=2,
        wl_state=DuffelWatchlistState(path=None, offset=0),
    )
    monitor.run_duffel_confirmations()  # routes=None → genérico = proven first
    kinds = [c[0] for c in provider.calls]
    # As 2 chamadas da watchlist (dates) vêm ANTES da chamada genérica (route).
    assert kinds == ["dates", "dates", "route"]
    # E a genérica é a rota PROVADA GRU-MIA one_way.
    assert provider.calls[-1] == ("route", DUFFEL_PROVEN_ROUTE.key)


# ----------------- 6. cap respeitado -----------------


def test_watchlist_cap_respected(tmp_path):
    wl = build_september_watchlist()
    provider = _ScriptedDuffel()
    monitor = _monitor(
        provider, _StubNotifier(), tmp_path,
        gen_cap=0, wl=wl, wl_cap=2,
        wl_state=DuffelWatchlistState(path=None, offset=0),
    )
    monitor.run_duffel_confirmations(routes=[])
    dates_calls = [c for c in provider.calls if c[0] == "dates"]
    assert len(dates_calls) == 2  # cap=2 → só 2 combinações por ciclo


def test_watchlist_rotation_covers_all_over_cycles(tmp_path):
    wl = build_september_watchlist()
    state = DuffelWatchlistState(path=None, offset=0)
    seen: set[tuple] = set()
    for _ in range(4):  # cap 2 × 4 ciclos = 8 combinações
        provider = _ScriptedDuffel()
        monitor = _monitor(
            provider, _StubNotifier(), tmp_path,
            gen_cap=0, wl=wl, wl_cap=2, wl_state=state,
        )
        monitor.run_duffel_confirmations(routes=[])
        for c in provider.calls:
            if c[0] == "dates":
                seen.add((c[1], c[2], c[3]))
    # Cobertura completa das 8 combinações em 4 ciclos.
    assert len(seen) == 8


# ----------------- 3 & 4. round-trip dates render + 🟢 -----------------


def test_watchlist_confirmed_offer_sends_green_with_roundtrip_dates(tmp_path):
    wl = build_september_watchlist()
    cdg = wl[4]  # primeira combinação CDG (Paris): ob 09-02 ret 09-12
    provider = _ScriptedDuffel(by_dates={
        (cdg.route.key, cdg.outbound_date, cdg.return_date):
            _wl_quote(cdg.route, cdg.outbound_date, cdg.return_date,
                      price_brl=1500.0, airline="AF"),
    })
    notifier = _StubNotifier()
    monitor = _monitor(
        provider, notifier, tmp_path,
        gen_cap=0, wl=wl, wl_cap=1,
        wl_state=DuffelWatchlistState(path=None, offset=4),  # começa no CDG
    )
    result = monitor.run_duffel_confirmations(routes=[])
    assert result.duffel_watchlist_summary.confirmed_alerts == 1
    # PR #71: order_flow NÃO envia standalone — vai p/ a mensagem AGRUPADA.
    assert notifier.messages == []
    assert len(notifier.grouped) == 1
    msg = notifier.grouped[0]
    assert "🟡 Ofertas confirmadas pela Duffel — compra pendente" in msg
    assert "São Paulo → Paris" in msg
    assert "2026-09-02 → 2026-09-12" in msg
    assert "Executiva" in msg
    assert "EXECUTIVA CONFIRMADA" not in msg
    assert "AF" in msg
    assert "link_status=order_flow" in msg
    assert "Sem link direto de compra. Verificar no Duffel Dashboard." in msg
    assert result.duffel_group_summary.grouped == 1
    assert result.duffel_group_summary.message_sent is True


def test_watchlist_london_offer_renders_londres(tmp_path):
    wl = build_september_watchlist()
    lhr = wl[0]
    provider = _ScriptedDuffel(by_dates={
        (lhr.route.key, lhr.outbound_date, lhr.return_date):
            _wl_quote(lhr.route, lhr.outbound_date, lhr.return_date),
    })
    notifier = _StubNotifier()
    monitor = _monitor(
        provider, notifier, tmp_path,
        gen_cap=0, wl=wl, wl_cap=1,
        wl_state=DuffelWatchlistState(path=None, offset=0),
    )
    monitor.run_duffel_confirmations(routes=[])
    # PR #71: renderizado na mensagem agrupada.
    assert "São Paulo → Londres" in notifier.grouped[0]


# ----------------- 5. no offer → safe status -----------------


def test_watchlist_no_offer_is_safe_status_not_failure(tmp_path):
    wl = build_september_watchlist()
    provider = _ScriptedDuffel()  # todas None
    notifier = _StubNotifier()
    monitor = _monitor(
        provider, notifier, tmp_path,
        gen_cap=0, wl=wl, wl_cap=2,
        wl_state=DuffelWatchlistState(path=None, offset=0),
    )
    result = monitor.run_duffel_confirmations(routes=[])
    s = result.duffel_watchlist_summary
    assert s is not None and s.enabled is True
    assert s.checked == 2 and s.confirmed_alerts == 0
    assert notifier.messages == []  # nada enviado, sem falha
    line = humanize_duffel_watchlist_status(s)
    assert line == (
        "Duffel watchlist Londres/Paris: consultada neste ciclo; "
        "0 ofertas confirmadas."
    )


def test_watchlist_summary_disabled_renders_no_line():
    assert humanize_duffel_watchlist_status(None) is None
    assert humanize_duffel_watchlist_status(
        DuffelWatchlistSummary(enabled=False, checked=0, confirmed_alerts=0)
    ) is None


def test_watchlist_summary_alerts_phrasing():
    one = DuffelWatchlistSummary(
        enabled=True, checked=1, confirmed_alerts=1, business_alerts=1,
    )
    assert humanize_duffel_watchlist_status(one) == (
        "Duffel watchlist Londres/Paris: 1 oferta executiva confirmada, "
        "compra pendente; sem link direto."
    )
    two = DuffelWatchlistSummary(
        enabled=True, checked=2, confirmed_alerts=2, business_alerts=2,
    )
    two_line = humanize_duffel_watchlist_status(two)
    assert two_line.startswith("Duffel watchlist Londres/Paris:")
    assert "2 ofertas executivas confirmadas" in two_line
    assert "compra pendente; sem link direto." in two_line


def test_watchlist_summary_economy_phrasing():
    eco = DuffelWatchlistSummary(
        enabled=True, checked=1, confirmed_alerts=1, economy_alerts=1,
    )
    assert humanize_duffel_watchlist_status(eco) == (
        "Duffel watchlist Londres/Paris: 1 oferta econômica muito boa, "
        "compra pendente; sem link direto."
    )
    both = DuffelWatchlistSummary(
        enabled=True, checked=2, confirmed_alerts=2,
        business_alerts=1, economy_alerts=1,
    )
    line = humanize_duffel_watchlist_status(both)
    assert "1 oferta executiva confirmada" in line
    assert "1 oferta econômica muito boa" in line


# ----------------- report integration -----------------


def test_report_includes_watchlist_line(tmp_path):
    from flight_mapper.monitor import MonitorResult
    store = PriceStore(tmp_path / "s.json")
    wl_summary = DuffelWatchlistSummary(
        enabled=True, checked=2, confirmed_alerts=0,
    )
    msg = _build_message(
        MonitorResult(scanned=0, quotes_received=0, alerts_sent=0),
        store, datetime.now(timezone.utc),
        duffel_watchlist_summary=wl_summary,
    )
    assert "🧭 Status das fontes" in msg
    assert (
        "Duffel watchlist Londres/Paris: consultada neste ciclo; "
        "0 ofertas confirmadas." in msg
    )


def test_report_omits_watchlist_line_when_none(tmp_path):
    store = PriceStore(tmp_path / "s.json")
    block = _source_status_block(store, [], [], duffel_watchlist_summary=None)
    assert "watchlist" not in block.lower()


# ----------------- 7 & 8. no /air/orders + no leak (real provider) -----------------


def test_watchlist_uses_offer_requests_never_orders(tmp_path):
    payload = {"data": {"offers": [{
        "id": "off_secret", "total_amount": "963", "total_currency": "EUR",
        "owner": {"iata_code": "AF"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T22:30:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business"}]}]},
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

    provider = DuffelProvider(
        access_token="tok_secret_zzz", urlopen_impl=fake_urlopen,
    )
    entry = build_september_watchlist()[4]  # CDG
    provider.quote_for_dates(entry.route, entry.outbound_date, entry.return_date)
    assert "offer_requests" in captured["url"]
    assert "orders" not in captured["url"]
    assert "payments" not in captured["url"]
    assert captured["method"] == "POST"
    assert "tok_secret_zzz" not in captured["url"]


def test_watchlist_alert_no_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("EUR_BRL_RATE", "6.0")
    payload = {"data": {"offers": [{
        "id": "off_fixture_xyz", "total_amount": "963", "total_currency": "EUR",
        "owner": {"iata_code": "AF"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T22:30:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business",
                                           "passenger_id": "pas_leak_1"}]}]},
            {"segments": [{"departing_at": "2026-09-12T10:00:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business"}]}]},
        ],
    }]}}

    def fake_urlopen(req, timeout=20):
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    provider = DuffelProvider(
        access_token="sentinel_tok_777", urlopen_impl=fake_urlopen,
    )
    wl = build_september_watchlist()
    notifier = _StubNotifier()
    monitor = _monitor(
        provider, notifier, tmp_path,
        gen_cap=0, wl=wl, wl_cap=1,
        wl_state=DuffelWatchlistState(path=None, offset=4),
    )
    monitor.run_duffel_confirmations(routes=[])
    # PR #71: a oferta entra na mensagem AGRUPADA (sem leak).
    assert notifier.grouped, "esperava 1 mensagem agrupada"
    msg = notifier.grouped[0]
    for sentinel in (
        "off_fixture_xyz", "pas_leak_1", "sentinel_tok_777",
        "api.duffel.com", "https://", "Bearer", "total_amount",
        "cabin_class", "order_id", "offer_id",
    ):
        assert sentinel not in msg, f"LEAK no alerta watchlist: {sentinel!r}"


# ----------------- 9. GRU-MIA genérico preservado após watchlist -----------------


def test_generic_proven_route_preserved_after_watchlist(tmp_path):
    wl = build_september_watchlist()
    # Watchlist sem oferta; genérico GRU-MIA com oferta confirmada.
    mia_quote = Quote(
        route=DUFFEL_PROVEN_ROUTE, price_brl=600.0, deep_link=None,
        departure_date="2026-09-01", return_date=None, source="duffel",
        amount=600.0, currency="BRL", amount_brl_estimated=600.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ONE_WAY, airline="CM",
    )
    provider = _ScriptedDuffel(
        by_route={DUFFEL_PROVEN_ROUTE.key: mia_quote},
    )
    notifier = _StubNotifier()
    monitor = _monitor(
        provider, notifier, tmp_path,
        gen_cap=1, wl=wl, wl_cap=1,
        wl_state=DuffelWatchlistState(path=None, offset=0),
    )
    result = monitor.run_duffel_confirmations()
    # Genérico ainda processa a rota provada (summary genérico presente).
    assert result.duffel_summary is not None
    assert result.duffel_summary.enabled is True
    # A rota provada foi consultada (após a watchlist).
    assert ("route", DUFFEL_PROVEN_ROUTE.key) in provider.calls
    # GRU-MIA one_way 600 (excellent_brl 700) → alerta enviado.
    assert result.duffel_confirmed_alerts == 1
