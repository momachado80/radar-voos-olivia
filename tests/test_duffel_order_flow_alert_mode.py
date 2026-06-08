"""Testes do PR #73 — suprimir push standalone de ofertas Duffel order_flow
"compra pendente" por padrão (modo `daily_only`).

Regra de produto: só ofertas com caminho de compra DIRETO (`direct_link`)
geram push standalone imediato. Duffel `order_flow` não tem checkout direto,
então por padrão NÃO vira push — só resumo no relatório diário.

Cobre os requisitos do goal:
1. Default `daily_only` NÃO envia push agrupado standalone do Duffel.
2. Default `daily_only` ainda resume o order_flow no relatório diário.
3. `grouped_push` preserva o comportamento agrupado do PR #71.
4. `disabled` suprime o conteúdo "compra pendente" do Telegram (só logs).
5. Provider `direct_link` continua enviando alerta standalone imediato.
6. Nenhuma chamada /air/orders.
7. Sem leak de token/offer_id/payload/passageiro no relatório diário.
"""

from __future__ import annotations

import json
from pathlib import Path

from flight_mapper.config import (
    DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY,
    DUFFEL_ORDER_FLOW_ALERT_MODES,
    Config,
)
from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.duffel_watchlist import (
    DuffelWatchlistState,
    build_september_watchlist,
)
from flight_mapper.monitor import Monitor, MonitorResult
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore
from flight_mapper.status import StatusState, maybe_send_status


REPO = Path(__file__).resolve().parents[1]


class _Notifier:
    """Captura alertas standalone (send_alert) e mensagens/relatórios (send)."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.standalone: list[str] = []
        self.grouped: list[str] = []

    def send_alert(self, quote, decision, priority=False) -> bool:
        self.standalone.append(format_alert(quote, decision, priority=priority))
        return self.ok

    def send(self, text) -> bool:
        self.grouped.append(text)
        return self.ok


class _ScriptedDuffel:
    def __init__(self, by_dates=None):
        self.by_dates = by_dates or {}

    def quote_for_dates(self, route, ob, ret, *, cabin="business"):
        return self.by_dates.get((route.key, ob, ret, cabin))

    def quote(self, route):
        return None


def _q(dest, cabin, price_brl, *, airline="AF",
       dep="2026-09-02", ret="2026-09-12") -> Quote:
    return Quote(
        route=Route("GRU", dest, "Europa",
                    trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS),
        price_brl=price_brl, deep_link=None, departure_date=dep, return_date=ret,
        source="duffel", amount=price_brl, currency="BRL",
        amount_brl_estimated=price_brl, cabin=cabin, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline=airline,
    )


def _monitor(provider, notifier, tmp_path, *, wl, wl_cap, mode, wl_offset=0):
    return Monitor(
        provider=object(), notifier=notifier,
        store=PriceStore(tmp_path / "m.json"),
        duffel_provider=provider,
        duffel_store=PriceStore(tmp_path / "d.json"),
        duffel_max_requests=0,
        duffel_watchlist=wl, duffel_watchlist_max_requests=wl_cap,
        duffel_watchlist_state=DuffelWatchlistState(path=None, offset=wl_offset),
        duffel_order_flow_alert_mode=mode,
    )


def _wl_with_confirmed(n):
    """Watchlist business+economy + dicionário com `n` combos confirmados."""
    wl = build_september_watchlist(cabins=("business", "economy"))
    by = {}
    for e in wl[:n]:
        by[(e.route.key, e.outbound_date, e.return_date, e.cabin)] = _q(
            e.route.destination, e.cabin_enum,
            600.0 if e.cabin == "economy" else 1500.0,
        )
    return wl, _ScriptedDuffel(by_dates=by)


def _report(group_summary, mode, tmp_path) -> str:
    """Renderiza o relatório diário com o `group_summary`/`mode` dados,
    isolando o Duffel (sem duffel_summary/watchlist). Devolve o texto."""
    notifier = _Notifier()
    maybe_send_status(
        result=MonitorResult(scanned=1, quotes_received=1, alerts_sent=0, notes=[]),
        store=PriceStore(tmp_path / "report.json"),
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
        duffel_group_summary=group_summary,
        duffel_order_flow_alert_mode=mode,
    )
    assert len(notifier.grouped) == 1, "esperava 1 relatório diário"
    return notifier.grouped[0]


# ---------------- config ----------------


def test_config_default_mode_is_daily_only(monkeypatch):
    monkeypatch.delenv("DUFFEL_ORDER_FLOW_ALERT_MODE", raising=False)
    assert Config.from_env().duffel_order_flow_alert_mode == "daily_only"


def test_config_invalid_mode_falls_back_to_daily_only(monkeypatch):
    monkeypatch.setenv("DUFFEL_ORDER_FLOW_ALERT_MODE", "screaming_push")
    assert Config.from_env().duffel_order_flow_alert_mode == "daily_only"


def test_config_accepts_all_three_modes(monkeypatch):
    for m in DUFFEL_ORDER_FLOW_ALERT_MODES:
        monkeypatch.setenv("DUFFEL_ORDER_FLOW_ALERT_MODE", m.upper())
        assert Config.from_env().duffel_order_flow_alert_mode == m


# ---------------- 1. daily_only não faz push ----------------


def test_daily_only_sends_no_standalone_grouped_push(tmp_path):
    wl, provider = _wl_with_confirmed(3)
    notifier = _Notifier()
    m = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=3,
                 mode=DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY)
    result = m.run_duffel_confirmations(routes=[])
    # Nenhum push: nem standalone, nem agrupado.
    assert notifier.standalone == []
    assert notifier.grouped == []
    # Mas as ofertas foram coletadas p/ o relatório diário.
    gs = result.duffel_group_summary
    assert gs.mode == "daily_only"
    assert gs.message_sent is False
    assert gs.confirmed_pending == 3
    assert len(gs.top_offers) == 3


def test_default_monitor_mode_is_daily_only(tmp_path):
    """Monitor sem `mode` explícito ⇒ default seguro daily_only (sem push)."""
    wl, provider = _wl_with_confirmed(2)
    notifier = _Notifier()
    m = Monitor(
        provider=object(), notifier=notifier,
        store=PriceStore(tmp_path / "m.json"),
        duffel_provider=provider, duffel_store=PriceStore(tmp_path / "d.json"),
        duffel_max_requests=0, duffel_watchlist=wl,
        duffel_watchlist_max_requests=2,
        duffel_watchlist_state=DuffelWatchlistState(path=None, offset=0),
    )
    assert m.duffel_order_flow_alert_mode == "daily_only"
    m.run_duffel_confirmations(routes=[])
    assert notifier.grouped == []


# ---------------- 2. daily_only resume no relatório ----------------


def test_daily_only_report_includes_summary_line(tmp_path):
    wl, provider = _wl_with_confirmed(3)
    m = _monitor(provider, _Notifier(), tmp_path, wl=wl, wl_cap=3,
                 mode=DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY)
    gs = m.run_duffel_confirmations(routes=[]).duffel_group_summary
    text = _report(gs, "daily_only", tmp_path)
    assert (
        "Duffel order_flow (resumo do ciclo): 3 ofertas confirmadas, "
        "compra pendente; sem link direto." in text
    )


def test_daily_only_report_lists_top3_section(tmp_path):
    wl, provider = _wl_with_confirmed(3)
    m = _monitor(provider, _Notifier(), tmp_path, wl=wl, wl_cap=3,
                 mode=DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY)
    gs = m.run_duffel_confirmations(routes=[]).duffel_group_summary
    text = _report(gs, "daily_only", tmp_path)
    # PR #76: seção lista as ofertas com link de busca no Google Flights.
    assert "🟡 Ofertas confirmadas pela Duffel — buscar no Google Flights" in text
    assert "São Paulo →" in text
    assert "Buscar no Google Flights" in text
    assert "google.com/travel/flights" in text
    assert "Preço e disponibilidade podem variar; confira antes de comprar." in text
    # No máximo 3 itens listados.
    assert "3. " in text
    assert "4. " not in text


# ---------------- 3. grouped_push preserva PR #71 ----------------


def test_grouped_push_sends_grouped_message(tmp_path):
    wl, provider = _wl_with_confirmed(3)
    notifier = _Notifier()
    m = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=3,
                 mode="grouped_push")
    result = m.run_duffel_confirmations(routes=[])
    assert notifier.standalone == []
    assert len(notifier.grouped) == 1
    assert "🟡 Ofertas confirmadas pela Duffel — buscar no Google Flights" in notifier.grouped[0]
    assert result.duffel_group_summary.message_sent is True


def test_grouped_push_report_shows_debug_counts_line(tmp_path):
    wl, provider = _wl_with_confirmed(2)
    m = _monitor(provider, _Notifier(), tmp_path, wl=wl, wl_cap=2,
                 mode="grouped_push")
    gs = m.run_duffel_confirmations(routes=[]).duffel_group_summary
    text = _report(gs, "grouped_push", tmp_path)
    # Linha de debug com contadores (Y agrupadas / Z suprimidas).
    assert "agrupadas" in text and "suprimidas por cooldown" in text
    # NÃO renderiza a seção 🟡 de top-3 (exclusiva do daily_only).
    assert "🟡 Ofertas confirmadas pela Duffel — buscar no Google Flights" not in text


# ---------------- 4. disabled suprime do Telegram ----------------


def test_disabled_sends_no_push(tmp_path):
    wl, provider = _wl_with_confirmed(3)
    notifier = _Notifier()
    m = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=3,
                 mode="disabled")
    result = m.run_duffel_confirmations(routes=[])
    assert notifier.standalone == []
    assert notifier.grouped == []
    assert result.duffel_group_summary.message_sent is False


def test_disabled_report_omits_order_flow_content(tmp_path):
    wl, provider = _wl_with_confirmed(3)
    m = _monitor(provider, _Notifier(), tmp_path, wl=wl, wl_cap=3,
                 mode="disabled")
    gs = m.run_duffel_confirmations(routes=[]).duffel_group_summary
    text = _report(gs, "disabled", tmp_path)
    # Nada de conteúdo order_flow no relatório (isolamos os demais summaries).
    assert "🟡 Ofertas business confirmadas (Duffel)" not in text
    assert "Buscar no Google Flights" not in text
    assert "Duffel order_flow" not in text


# ---------------- 5. direct_link continua standalone ----------------


def test_direct_link_provider_still_standalone(tmp_path):
    class _DirectProvider:
        def quote(self, route):
            return Quote(
                route=route, price_brl=1500.0,
                deep_link="https://www.kiwi.com/deep?x=1",
                departure_date="2026-09-02", return_date="2026-09-12",
                source="kiwi", amount=1500.0, currency="BRL",
                amount_brl_estimated=1500.0, cabin=Cabin.BUSINESS,
                cabin_confirmed=True, trip_type=TripType.ROUND_TRIP,
            )

    notifier = _Notifier()
    monitor = Monitor(
        provider=_DirectProvider(), notifier=notifier,
        store=PriceStore(tmp_path / "main.json"),
    )
    monitor.run_once([Route("GRU", "LHR", "Europa", cabin=Cabin.BUSINESS)])
    # Direct-link nunca é agrupado/suprimido pela política order_flow.
    assert notifier.grouped == []


# ---------------- 6. sem /air/orders ----------------


def test_no_air_orders_reference():
    for mod in ("monitor.py", "notifier.py", "status.py", "duffel_status.py",
                "config.py"):
        src = (REPO / "flight_mapper" / mod).read_text(encoding="utf-8")
        assert "api.duffel.com/air/orders" not in src
        assert "api.duffel.com/air/payments" not in src


# ---------------- 7. sem leak no relatório diário ----------------


def test_daily_only_report_no_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("EUR_BRL_RATE", "6.0")
    # Teto é USD → escala USD→BRL (rate distinta do EUR, expõe o cenário do
    # bug corrigido: teto não deve usar a taxa EUR da oferta).
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    payload = {"data": {"offers": [{
        "id": "off_secret_daily", "total_amount": "963", "total_currency": "EUR",
        "owner": {"iata_code": "AF"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T22:30:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business",
                                           "passenger_id": "pas_secret_daily"}]}]},
            {"segments": [{"departing_at": "2026-09-12T10:00:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business"}]}]},
        ],
    }]}}

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=20):
        return _Resp(json.dumps(payload).encode("utf-8"))

    provider = DuffelProvider(
        access_token="sentinel_tok_daily", urlopen_impl=fake_urlopen,
    )
    wl = build_september_watchlist(cabins=("business",))
    m = _monitor(provider, _Notifier(), tmp_path, wl=wl, wl_cap=1,
                 mode=DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY)
    gs = m.run_duffel_confirmations(routes=[]).duffel_group_summary
    assert gs.confirmed_pending >= 1
    text = _report(gs, "daily_only", tmp_path)
    for sentinel in (
        "off_secret_daily", "pas_secret_daily", "sentinel_tok_daily",
        "api.duffel.com", "Bearer", "total_amount", "cabin_class",
        "order_id", "offer_id", "payment_id",
    ):
        assert sentinel not in text, f"LEAK no relatório diário: {sentinel!r}"
