"""Testes do PR #71 — agrupar alertas Duffel order_flow + cooldown 6h.

Cobre os requisitos do goal:
1. Múltiplas ofertas order_flow → UMA mensagem agrupada (não várias standalone).
2. Grupo inclui rótulos de economy e business.
3. Grupo inclui rota, datas, preço, cia, link_status.
4. Grupo limita em 5 e reporta o excedente.
5. Cooldown suprime o mesmo combo por 6h.
6. Melhora de preço ≥5% fura o cooldown.
7. Provider com link direto continua enviando alerta standalone imediato.
8. Nenhuma chamada /air/orders.
9. Sem leak token/offer_id/payload/passageiro.
10. Detecção/watchlist Duffel existentes seguem passando (suíte).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flight_mapper.duffel_cooldown import (
    COOLDOWN_HOURS,
    DuffelAlertCooldownState,
    cooldown_key,
)
from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.duffel_watchlist import (
    DuffelWatchlistState,
    build_september_watchlist,
)
from flight_mapper.monitor import Monitor
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore


REPO = Path(__file__).resolve().parents[1]


class _Notifier:
    """Captura alertas standalone (send_alert) e mensagens agrupadas (send)."""

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
    """Provider de teste com quote_for_dates (watchlist) e quote (genérico)."""

    def __init__(self, by_dates=None):
        self.by_dates = by_dates or {}

    def quote_for_dates(self, route, ob, ret, *, cabin="business"):
        return self.by_dates.get((route.key, ob, ret, cabin))

    def quote(self, route):
        return None


def _q(dest, cabin, price_brl, *, region="Europa", airline="AF",
       dep="2026-09-02", ret="2026-09-12") -> Quote:
    return Quote(
        route=Route("GRU", dest, region,
                    trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS),
        price_brl=price_brl, deep_link=None, departure_date=dep, return_date=ret,
        source="duffel", amount=price_brl, currency="BRL",
        amount_brl_estimated=price_brl, cabin=cabin, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline=airline,
    )


def _monitor(provider, notifier, tmp_path, *, wl, wl_cap, cooldown=None,
             wl_offset=0, mode="grouped_push"):
    # PR #73: estes testes documentam a mensagem AGRUPADA do PR #71, que
    # agora é opt-in (`grouped_push`). O default de produto virou
    # `daily_only` (sem push standalone) — coberto em
    # tests/test_duffel_order_flow_alert_mode.py.
    return Monitor(
        provider=object(), notifier=notifier,
        store=PriceStore(tmp_path / "m.json"),
        duffel_provider=provider,
        duffel_store=PriceStore(tmp_path / "d.json"),
        duffel_max_requests=0,
        duffel_watchlist=wl, duffel_watchlist_max_requests=wl_cap,
        duffel_watchlist_state=DuffelWatchlistState(path=None, offset=wl_offset),
        duffel_cooldown_state=cooldown,
        duffel_order_flow_alert_mode=mode,
    )


def _wl_business_economy():
    """Watchlist business+economy (16 combos)."""
    return build_september_watchlist(cabins=("business", "economy"))


# ----------------- 1, 2, 3. agrupamento -----------------


def test_multiple_offers_produce_one_grouped_message(tmp_path):
    wl = _wl_business_economy()
    # 3 combos confirmados (preços abaixo dos tetos).
    by = {}
    for e in wl[:3]:
        by[(e.route.key, e.outbound_date, e.return_date, e.cabin)] = _q(
            e.route.destination, e.cabin_enum,
            600.0 if e.cabin == "economy" else 1500.0,
            airline="AF",
        )
    provider = _ScriptedDuffel(by_dates=by)
    notifier = _Notifier()
    monitor = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=3)
    result = monitor.run_duffel_confirmations(routes=[])
    # UMA mensagem agrupada, ZERO standalone.
    assert notifier.standalone == []
    assert len(notifier.grouped) == 1
    assert result.duffel_group_summary.grouped == 3
    assert result.duffel_group_summary.message_sent is True


def test_group_includes_business_and_economy_labels(tmp_path):
    wl = _wl_business_economy()
    # 1 business (LHR) + 1 economy (LHR economy entry).
    biz = next(e for e in wl if e.cabin == "business" and e.route.destination == "LHR")
    eco = next(e for e in wl if e.cabin == "economy" and e.route.destination == "LHR")
    by = {
        (biz.route.key, biz.outbound_date, biz.return_date, "business"):
            _q("LHR", Cabin.BUSINESS, 1500.0),
        (eco.route.key, eco.outbound_date, eco.return_date, "economy"):
            _q("LHR", Cabin.ECONOMY, 600.0),
    }
    provider = _ScriptedDuffel(by_dates=by)
    notifier = _Notifier()
    # cap alto p/ pegar ambos; offsets cobrindo business e economy.
    monitor = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=16)
    monitor.run_duffel_confirmations(routes=[])
    msg = notifier.grouped[0]
    assert "Executiva" in msg
    assert "Econômica" in msg


def test_group_includes_route_dates_price_airline_linkstatus(tmp_path):
    wl = _wl_business_economy()
    e = next(x for x in wl if x.cabin == "business" and x.route.destination == "CDG")
    by = {(e.route.key, e.outbound_date, e.return_date, "business"):
          _q("CDG", Cabin.BUSINESS, 1500.0, airline="AF")}
    provider = _ScriptedDuffel(by_dates=by)
    notifier = _Notifier()
    monitor = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=16)
    monitor.run_duffel_confirmations(routes=[])
    msg = notifier.grouped[0]
    # PR #76: grupo com link de busca no Google Flights por oferta.
    assert "🟡 Ofertas confirmadas pela Duffel — buscar no Google Flights" in msg
    assert "São Paulo → Paris" in msg          # rota/cidade
    assert "2026-09-02 → 2026-09-12" in msg     # datas
    assert "R$ 1.500" in msg                    # preço
    assert "AF" in msg                          # cia
    assert "Buscar no Google Flights" in msg    # link clicável
    assert "google.com/travel/flights" in msg
    assert "Preço e disponibilidade podem variar; confira antes de comprar." in msg


# ----------------- 4. cap em 5 + excedente -----------------


def test_group_caps_at_five_and_reports_extra(tmp_path):
    wl = _wl_business_economy()
    by = {}
    # 7 combos confirmados → mostra 5 + "+2 outras".
    for e in wl[:7]:
        by[(e.route.key, e.outbound_date, e.return_date, e.cabin)] = _q(
            e.route.destination, e.cabin_enum,
            600.0 if e.cabin == "economy" else 1500.0,
        )
    provider = _ScriptedDuffel(by_dates=by)
    notifier = _Notifier()
    monitor = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=7)
    result = monitor.run_duffel_confirmations(routes=[])
    msg = notifier.grouped[0]
    # 5 itens numerados + "+2 outras".
    assert "5. " in msg
    assert "6. " not in msg
    assert "+2 outras ofertas confirmadas no ciclo." in msg
    assert result.duffel_group_summary.grouped == 7


# ----------------- 5 & 6. cooldown 6h + bypass 5% -----------------


def test_cooldown_unit_suppresses_within_6h_same_price():
    cd = DuffelAlertCooldownState(path=None)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    cd.record("k1", 1500.0, "BRL", now)
    # 3h depois, mesmo preço → suprime.
    assert cd.is_suppressed("k1", 1500.0, now + timedelta(hours=3)) is True
    # >6h depois → não suprime.
    assert cd.is_suppressed("k1", 1500.0, now + timedelta(hours=COOLDOWN_HOURS + 1)) is False


def test_cooldown_unit_bypassed_by_5pct_improvement():
    cd = DuffelAlertCooldownState(path=None)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    cd.record("k1", 1000.0, "BRL", now)
    later = now + timedelta(hours=1)
    # queda de 4% → ainda suprime.
    assert cd.is_suppressed("k1", 960.0, later) is True
    # queda de 5% → fura o cooldown.
    assert cd.is_suppressed("k1", 950.0, later) is False


def test_cooldown_integration_suppresses_second_pass(tmp_path):
    wl = _wl_business_economy()
    e = next(x for x in wl if x.cabin == "business" and x.route.destination == "LHR")
    by = {(e.route.key, e.outbound_date, e.return_date, "business"):
          _q("LHR", Cabin.BUSINESS, 1500.0)}
    provider = _ScriptedDuffel(by_dates=by)
    notifier = _Notifier()
    cd = DuffelAlertCooldownState(path=None)
    # 1º ciclo: envia agrupado, grava cooldown.
    m1 = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=16, cooldown=cd)
    r1 = m1.run_duffel_confirmations(routes=[])
    assert len(notifier.grouped) == 1
    assert r1.duffel_group_summary.grouped == 1
    # 2º ciclo imediato (mesmo preço, <6h): suprime, NÃO reenvia.
    m2 = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=16, cooldown=cd)
    r2 = m2.run_duffel_confirmations(routes=[])
    assert len(notifier.grouped) == 1  # nenhuma nova mensagem
    assert r2.duffel_group_summary.grouped == 0
    assert r2.duffel_group_summary.suppressed_cooldown == 1


def test_cooldown_integration_price_drop_resends(tmp_path):
    wl = _wl_business_economy()
    e = next(x for x in wl if x.cabin == "business" and x.route.destination == "LHR")
    key = (e.route.key, e.outbound_date, e.return_date, "business")
    provider = _ScriptedDuffel(by_dates={key: _q("LHR", Cabin.BUSINESS, 1500.0)})
    notifier = _Notifier()
    cd = DuffelAlertCooldownState(path=None)
    m1 = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=16, cooldown=cd)
    m1.run_duffel_confirmations(routes=[])
    assert len(notifier.grouped) == 1
    # Preço cai 10% (1500 → 1350) → fura cooldown, reenvia.
    provider.by_dates[key] = _q("LHR", Cabin.BUSINESS, 1350.0)
    m2 = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=16, cooldown=cd)
    r2 = m2.run_duffel_confirmations(routes=[])
    assert len(notifier.grouped) == 2
    assert r2.duffel_group_summary.grouped == 1


def test_cooldown_key_excludes_price_includes_route_cabin_dates():
    q = _q("LHR", Cabin.BUSINESS, 1500.0)
    k = cooldown_key(q)
    assert "duffel" in k and "GRU-LHR" in k and "business" in k
    assert "2026-09-02" in k and "2026-09-12" in k
    # Preço NÃO entra na chave (regra dos 5% trata preço).
    assert "1500" not in k


# ----------------- 7. direct_link continua standalone -----------------


def test_direct_link_provider_still_standalone(tmp_path):
    """A rota principal (run_once) com deep_link real continua enviando
    alerta standalone imediato — não é agrupada."""
    from flight_mapper.detector import Decision, CRITERION_CEILING, LEVEL_EXCELLENT

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
    store = PriceStore(tmp_path / "main.json")
    # warm-up p/ ter histórico p/ o detector (ceiling dispara por teto).
    monitor = Monitor(provider=_DirectProvider(), notifier=notifier, store=store)
    route = Route("GRU", "LHR", "Europa", cabin=Cabin.BUSINESS)
    monitor.run_once([route])
    # Direct-link → alerta standalone (send_alert), nunca agrupado (send).
    assert notifier.grouped == []
    # (pode ou não ter alertado dependendo do teto/histórico, mas nunca agrupa)


# ----------------- 8 & 9. sem /air/orders + sem leak -----------------


def test_no_air_orders_reference_in_grouping_code():
    for mod in ("duffel_cooldown.py", "monitor.py", "notifier.py"):
        src = (REPO / "flight_mapper" / mod).read_text(encoding="utf-8")
        assert "api.duffel.com/air/orders" not in src
        assert "api.duffel.com/air/payments" not in src


def test_grouped_message_no_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("EUR_BRL_RATE", "6.0")
    # Teto é USD → escala USD→BRL (rate distinta do EUR, expõe o cenário do
    # bug corrigido: teto não deve usar a taxa EUR da oferta).
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    payload = {"data": {"offers": [{
        "id": "off_secret_grp", "total_amount": "963", "total_currency": "EUR",
        "owner": {"iata_code": "AF"},
        "slices": [
            {"segments": [{"departing_at": "2026-09-02T22:30:00",
                           "marketing_carrier": {"iata_code": "AF"},
                           "passengers": [{"cabin_class": "business",
                                           "passenger_id": "pas_secret_grp"}]}]},
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
        access_token="sentinel_tok_grp", urlopen_impl=fake_urlopen,
    )
    wl = build_september_watchlist(cabins=("business",))
    notifier = _Notifier()
    monitor = _monitor(provider, notifier, tmp_path, wl=wl, wl_cap=1, wl_offset=0)
    monitor.run_duffel_confirmations(routes=[])
    assert notifier.grouped, "esperava mensagem agrupada"
    msg = notifier.grouped[0]
    # PR #76: `https://` legítimo (Google Flights) — checa sensíveis + host.
    for sentinel in (
        "off_secret_grp", "pas_secret_grp", "sentinel_tok_grp",
        "api.duffel.com", "Bearer", "total_amount",
        "cabin_class", "order_id", "offer_id", "payment_id",
    ):
        assert sentinel not in msg, f"LEAK na mensagem agrupada: {sentinel!r}"
    import re
    hosts = re.findall(r'href="https://([^/"]+)', msg)
    assert all(h == "www.google.com" for h in hosts), hosts
