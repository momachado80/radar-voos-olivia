"""Testes do PR #65 — observabilidade do pass Duffel no relatório diário
+ priorização da rota PROVADA (GRU-MIA one_way business).

Cobre os requisitos do goal:
1. duffel_result entra no status do relatório diário;
2. alerta confirmado aparece como status da fonte;
3. bloqueio por câmbio EUR→BRL ausente aparece como status seguro;
4. acima do teto aparece como status seguro;
5. sem oferta aparece como status seguro;
6. token/flag ausente aparece como status seguro;
7. GRU-MIA one_way é selecionada primeiro;
8. sem leak (offer_id, token, URL, payload, order_id, passageiro).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.duffel_status import (
    DUFFEL_ABOVE_THRESHOLD,
    DUFFEL_ALERT_SENT,
    DUFFEL_BLOCKED_CABIN,
    DUFFEL_BLOCKED_FX,
    DUFFEL_BLOCKED_SUSPICIOUS,
    DUFFEL_DISABLED,
    DUFFEL_NO_OFFER,
    DuffelStatusSummary,
    humanize_duffel_status,
)
from flight_mapper.monitor import DUFFEL_PROVEN_ROUTE, Monitor
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message, _source_status_block


FIX = Path(__file__).parent / "fixtures"
_RT = Route("GRU", "MIA", "EUA", trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_returning(payload: dict, captured: dict | None = None):
    def fake(req, timeout=20):
        if captured is not None:
            captured["url"] = req.full_url
        return _FakeResp(json.dumps(payload).encode("utf-8"))
    return fake


class _StubNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages: list[str] = []

    def send_alert(self, quote, decision, priority=False) -> bool:
        from flight_mapper.notifier import format_alert
        self.messages.append(format_alert(quote, decision, priority=priority))
        return self.ok


class _ScriptedDuffel:
    def __init__(self, quotes):
        self._quotes = list(quotes)
        self.calls = 0
        self.routes_seen: list[str] = []

    def quote(self, route):
        self.routes_seen.append(route.key)
        idx = self.calls
        self.calls += 1
        return self._quotes[idx] if idx < len(self._quotes) else None


def _quote(price_brl=900.0, *, currency="BRL", amount=None,
           amount_brl=None, fx=None, cabin=Cabin.BUSINESS,
           confirmed=True, route=_RT) -> Quote:
    return Quote(
        route=route, price_brl=price_brl, deep_link=None,
        departure_date="2026-09-10", return_date=None, source="duffel",
        amount=amount if amount is not None else price_brl,
        currency=currency,
        amount_brl_estimated=(amount_brl if amount_brl is not None else
                              (price_brl if currency == "BRL" else None)),
        fx_rate=fx, cabin=cabin, cabin_confirmed=confirmed,
        trip_type=TripType.ONE_WAY, airline="CM",
    )


def _monitor(duffel_provider, notifier, tmp_path, max_requests=1):
    return Monitor(
        provider=_ScriptedDuffel([]), notifier=notifier,
        store=PriceStore(tmp_path / "main.json"),
        duffel_provider=duffel_provider,
        duffel_store=PriceStore(tmp_path / "duffel.json"),
        duffel_max_requests=max_requests,
    )


# ----------------- 7. Rota PROVADA primeiro -----------------


def test_proven_route_is_gru_mia_one_way_business():
    assert DUFFEL_PROVEN_ROUTE.origin == "GRU"
    assert DUFFEL_PROVEN_ROUTE.destination == "MIA"
    assert DUFFEL_PROVEN_ROUTE.trip_type == TripType.ONE_WAY
    assert DUFFEL_PROVEN_ROUTE.cabin == Cabin.BUSINESS
    assert DUFFEL_PROVEN_ROUTE.key == "GRU-MIA-one_way-business"


def test_duffel_pass_queries_proven_route_first(tmp_path):
    duffel = _ScriptedDuffel([None])  # cap=1 → só a 1ª rota é consultada
    monitor = _monitor(duffel, _StubNotifier(), tmp_path, max_requests=1)
    monitor.run_duffel_confirmations()  # routes=None → usa priorização interna
    assert duffel.routes_seen == ["GRU-MIA-one_way-business"]


# ----------------- 2..6. Outcomes do summary -----------------


def test_summary_alert_sent(tmp_path):
    # BRL-nativo abaixo do teto GRU-MIA RT → alerta enviado.
    duffel = _ScriptedDuffel([_quote(price_brl=1000.0)])
    monitor = _monitor(duffel, _StubNotifier(), tmp_path)
    result = monitor.run_duffel_confirmations([_RT])
    assert result.duffel_summary.outcome == DUFFEL_ALERT_SENT
    assert result.duffel_summary.confirmed_alerts == 1
    assert result.duffel_summary.enabled is True


def test_summary_blocked_fx(tmp_path, monkeypatch):
    monkeypatch.delenv("EUR_BRL_RATE", raising=False)
    eur = _quote(price_brl=900.0, currency="EUR", amount=900.0,
                 amount_brl=None, fx=None)
    duffel = _ScriptedDuffel([eur])
    monitor = _monitor(duffel, _StubNotifier(), tmp_path)
    result = monitor.run_duffel_confirmations([_RT])
    assert result.duffel_summary.outcome == DUFFEL_BLOCKED_FX
    assert result.duffel_summary.confirmed_alerts == 0


def test_summary_above_threshold(tmp_path):
    # Preço BRL bem acima do teto good_brl (GRU-MIA RT good=1300) → sem alerta.
    duffel = _ScriptedDuffel([_quote(price_brl=9000.0)])
    monitor = _monitor(duffel, _StubNotifier(), tmp_path)
    result = monitor.run_duffel_confirmations([_RT])
    assert result.duffel_summary.outcome == DUFFEL_ABOVE_THRESHOLD
    assert result.duffel_summary.confirmed_alerts == 0


def test_summary_no_offer(tmp_path):
    duffel = _ScriptedDuffel([None])
    monitor = _monitor(duffel, _StubNotifier(), tmp_path)
    result = monitor.run_duffel_confirmations([_RT])
    assert result.duffel_summary.outcome == DUFFEL_NO_OFFER
    assert result.duffel_summary.requests == 1


def test_summary_blocked_cabin(tmp_path):
    unconfirmed = _quote(price_brl=1000.0, cabin=Cabin.ECONOMY, confirmed=False)
    duffel = _ScriptedDuffel([unconfirmed])
    monitor = _monitor(duffel, _StubNotifier(), tmp_path)
    result = monitor.run_duffel_confirmations([_RT])
    assert result.duffel_summary.outcome == DUFFEL_BLOCKED_CABIN


def test_summary_blocked_suspicious(tmp_path, monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    # US$ 50 ≈ R$275, abaixo do piso de sanidade business.
    cheap = _quote(price_brl=275.0, currency="USD", amount=50.0,
                   amount_brl=275.0, fx=5.5)
    duffel = _ScriptedDuffel([cheap])
    monitor = _monitor(duffel, _StubNotifier(), tmp_path)
    result = monitor.run_duffel_confirmations([_RT])
    assert result.duffel_summary.outcome == DUFFEL_BLOCKED_SUSPICIOUS


def test_summary_disabled_when_provider_none(tmp_path):
    monitor = Monitor(
        provider=_ScriptedDuffel([]), notifier=_StubNotifier(),
        store=PriceStore(tmp_path / "m.json"),
        duffel_provider=None, duffel_max_requests=1,
    )
    result = monitor.run_duffel_confirmations()
    assert result.duffel_summary.outcome == DUFFEL_DISABLED
    assert result.duffel_summary.enabled is False


# ----------------- 1. Summary entra no relatório (🧭) -----------------


def test_source_block_omits_duffel_line_when_summary_none(tmp_path):
    """Backward compat: sem summary → nenhuma linha Duffel (relatório
    existente inalterado)."""
    store = PriceStore(tmp_path / "s.json")
    block = _source_status_block(store, [], [], serpapi_summary=None)
    assert "Duffel" not in block


def test_source_block_includes_duffel_line_when_summary_present(tmp_path):
    store = PriceStore(tmp_path / "s.json")
    summary = DuffelStatusSummary(
        enabled=True, requests=1, confirmed_alerts=1, outcome=DUFFEL_ALERT_SENT,
    )
    block = _source_status_block(
        store, [], [], serpapi_summary=None, duffel_summary=summary,
    )
    assert "Duffel: ativa; 1 oferta confirmada (compra pendente)." in block


def test_build_message_includes_duffel_status(tmp_path):
    from flight_mapper.monitor import MonitorResult
    store = PriceStore(tmp_path / "s.json")
    summary = DuffelStatusSummary(
        enabled=True, requests=1, confirmed_alerts=0, outcome=DUFFEL_BLOCKED_FX,
    )
    msg = _build_message(
        MonitorResult(scanned=0, quotes_received=0, alerts_sent=0),
        store, datetime.now(timezone.utc), duffel_summary=summary,
    )
    assert "🧭 Status das fontes" in msg
    assert "Duffel: ativa, mas bloqueada por câmbio EUR→BRL ausente." in msg


def test_build_message_no_duffel_line_by_default(tmp_path):
    from flight_mapper.monitor import MonitorResult
    store = PriceStore(tmp_path / "s.json")
    msg = _build_message(
        MonitorResult(scanned=0, quotes_received=0, alerts_sent=0),
        store, datetime.now(timezone.utc),
    )
    assert "Duffel" not in msg


# ----------------- 8. No leak no relatório -----------------


def test_no_leak_in_report_with_real_duffel_payload(tmp_path, monkeypatch):
    """Pipeline real: provider → quote → pass → relatório. Nenhum
    offer_id/token/URL/payload/passageiro pode aparecer no texto do 🧭."""
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    sample = json.loads((FIX / "duffel_business_gru_mia.json").read_text())
    captured: dict = {}
    provider = DuffelProvider(
        access_token="sentinel_tok_999",
        urlopen_impl=_urlopen_returning(sample, captured),
    )
    monitor = _monitor(provider, _StubNotifier(), tmp_path)
    # Roda contra a rota RT p/ exercitar o caminho completo.
    result = monitor.run_duffel_confirmations([_RT])
    summary = result.duffel_summary
    assert summary is not None
    line = humanize_duffel_status(summary)
    # A frase do 🧭 jamais contém dados sensíveis.
    for sentinel in (
        "off_fixture_business_001", "pas_fixture_001", "sentinel_tok_999",
        "api.duffel.com", "https://", "total_amount", "cabin_class",
        "order", "Bearer",
    ):
        assert sentinel not in line, f"LEAK na linha Duffel: {sentinel!r}"
    # Token nunca vai na URL (vai no header Authorization).
    assert "sentinel_tok_999" not in captured.get("url", "")
    # E o summary é um dataclass de contadores — sem campos de payload.
    from dataclasses import fields
    field_names = {f.name for f in fields(summary)}
    assert field_names == {"enabled", "requests", "confirmed_alerts", "outcome"}


def test_summary_object_has_no_sensitive_fields():
    """Guard de schema: DuffelStatusSummary só tem contadores + outcome."""
    from dataclasses import fields
    names = {f.name for f in fields(DuffelStatusSummary)}
    forbidden = {
        "offer_id", "token", "access_token", "url", "deep_link",
        "payload", "order_id", "passenger", "passengers", "request_body",
    }
    assert not (names & forbidden)


# ----------------- integração cmd_cycle (smoke) -----------------


def test_cmd_cycle_threads_duffel_summary_to_report(tmp_path, monkeypatch):
    """cmd_cycle com Duffel ligado + notifier injeta a linha Duffel no
    relatório enviado (sem rede real: urlopen do provider é mockado e o
    Telegram é capturado)."""
    import flight_mapper.__main__ as main_mod
    from flight_mapper.config import Config

    monkeypatch.setenv("DUFFEL_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "tok_cycle_abc")
    monkeypatch.setenv("DUFFEL_MAX_REQUESTS_PER_CYCLE", "1")
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")

    sent: dict = {}

    # Captura o texto enviado ao Telegram sem rede.
    def fake_send(self, text):
        sent["text"] = text
        return True
    monkeypatch.setattr(
        "flight_mapper.notifier.TelegramNotifier.send", fake_send,
    )

    # Provider Duffel real, mas urlopen → sem oferta (data vazio).
    real_make = main_mod._make_duffel_provider

    def patched_make(config):
        prov = real_make(config)
        if prov is not None:
            prov._urlopen_impl = _urlopen_returning({"data": {"offers": []}})
        return prov
    monkeypatch.setattr(main_mod, "_make_duffel_provider", patched_make)

    # Usa mock provider primário p/ não tocar rede.
    class _Args:
        mock = True
    main_mod.cmd_cycle(_Args())

    assert "text" in sent, "heartbeat deveria ter sido enviado"
    assert "🧭 Status das fontes" in sent["text"]
    assert "Duffel:" in sent["text"]
    # Sem leak do token na mensagem.
    assert "tok_cycle_abc" not in sent["text"]
