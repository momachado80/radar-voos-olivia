"""Testes da integração de produção do Duffel (read-only confirmed offers).

Cobre os requisitos do goal:
1. Duffel desligado → nenhuma chamada, comportamento inalterado.
2. Token ausente → sem crash.
3. Fixture business + preço → quote confirmado.
4. Economy / sem cabine → bloqueado (None).
5. Sem ofertas → nenhum alerta.
6. Telegram 🟢 contém o texto de oferta Duffel confirmada.
7. Telegram NÃO expõe offer_id / token / payload cru.
8. NUNCA chama /air/orders (só offer_requests).

Sem rede real, sem secrets, sem Telegram real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flight_mapper.config import Config
from flight_mapper.detector import (
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    Decision,
)
from flight_mapper.duffel_provider import DuffelProvider
from flight_mapper.monitor import Monitor
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore


FIX = Path(__file__).parent / "fixtures"
_ROUTE = Route("GRU", "MIA", "EUA", trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS)


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
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
        return _FakeResp(json.dumps(payload).encode("utf-8"))
    return fake


class _StubNotifier:
    def __init__(self):
        self.messages: list[str] = []
        self.alerts: list[Quote] = []
        self.grouped: list[str] = []  # agrupadas (send) — PR #71

    def send_alert(self, quote, decision, priority=False) -> bool:
        self.alerts.append(quote)
        self.messages.append(format_alert(quote, decision, priority=priority))
        return True

    def send(self, text) -> bool:
        self.grouped.append(text)
        return True


class _ScriptedDuffel:
    """Provider de teste que devolve quotes pré-programados. Conta chamadas."""
    def __init__(self, quotes):
        self._quotes = list(quotes)
        self.calls = 0

    def quote(self, route):
        self.calls += 1
        idx = self.calls - 1
        if idx < len(self._quotes):
            return self._quotes[idx]
        return None


def _confirmed_quote(price_brl=8000.0, route=_ROUTE) -> Quote:
    return Quote(
        route=route,
        price_brl=price_brl,
        deep_link=None,
        departure_date="2026-09-10",
        return_date=None,
        source="duffel",
        amount=price_brl,
        currency="BRL",
        amount_brl_estimated=price_brl,
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
        trip_type=TripType.ONE_WAY,
        airline="CM",
    )


def _monitor(duffel_provider, notifier, tmp_path, max_requests=1):
    store = PriceStore(tmp_path / "main.json")
    duffel_store = PriceStore(tmp_path / "duffel.json")
    return Monitor(
        provider=_ScriptedDuffel([]),  # primário irrelevante para o pass Duffel
        notifier=notifier,
        store=store,
        duffel_provider=duffel_provider,
        duffel_store=duffel_store,
        duffel_max_requests=max_requests,
    )


# ----------------- 1. Desligado → no-op -----------------


def test_duffel_disabled_means_no_calls(tmp_path):
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "main.json")
    monitor = Monitor(
        provider=_ScriptedDuffel([]), notifier=notifier, store=store,
        duffel_provider=None, duffel_max_requests=1,
    )
    result = monitor.run_duffel_confirmations()
    assert result.duffel_requests == 0
    assert result.duffel_confirmed_alerts == 0
    assert notifier.alerts == []


def test_duffel_cap_zero_means_no_calls(tmp_path):
    notifier = _StubNotifier()
    duffel = _ScriptedDuffel([_confirmed_quote()])
    monitor = _monitor(duffel, notifier, tmp_path, max_requests=0)
    result = monitor.run_duffel_confirmations()
    assert duffel.calls == 0
    assert result.duffel_confirmed_alerts == 0
    assert notifier.alerts == []


def test_config_duffel_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DUFFEL_PROVIDER_ENABLED", raising=False)
    cfg = Config.from_env()
    assert cfg.duffel_provider_enabled is False
    assert cfg.duffel_max_requests_per_cycle == 1


def test_config_duffel_enabled_when_set(monkeypatch):
    monkeypatch.setenv("DUFFEL_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "tok_xyz")
    monkeypatch.setenv("DUFFEL_MAX_REQUESTS_PER_CYCLE", "3")
    cfg = Config.from_env()
    assert cfg.duffel_provider_enabled is True
    assert cfg.duffel_access_token == "tok_xyz"
    assert cfg.duffel_max_requests_per_cycle == 3


def test_config_duffel_cap_invalid_falls_back_to_one(monkeypatch):
    monkeypatch.setenv("DUFFEL_MAX_REQUESTS_PER_CYCLE", "not-a-number")
    cfg = Config.from_env()
    assert cfg.duffel_max_requests_per_cycle == 1


# ----------------- 2. Token ausente → sem crash -----------------


def test_provider_missing_token_returns_none_no_crash():
    p = DuffelProvider(access_token="")
    assert p.quote(_ROUTE) is None


def test_make_duffel_provider_none_when_enabled_but_no_token(monkeypatch, capsys):
    from flight_mapper.__main__ import _make_duffel_provider
    monkeypatch.setenv("DUFFEL_PROVIDER_ENABLED", "true")
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    cfg = Config.from_env()
    assert _make_duffel_provider(cfg) is None
    err = capsys.readouterr().err
    assert "DUFFEL_ACCESS_TOKEN" in err


def test_make_duffel_provider_none_when_disabled(monkeypatch):
    from flight_mapper.__main__ import _make_duffel_provider
    monkeypatch.setenv("DUFFEL_PROVIDER_ENABLED", "false")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "tok")
    cfg = Config.from_env()
    assert _make_duffel_provider(cfg) is None


# ----------------- 3. Business + preço → quote confirmado -----------------


def test_provider_business_fixture_produces_confirmed_quote(monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    sample = json.loads((FIX / "duffel_business_gru_mia.json").read_text())
    p = DuffelProvider(
        access_token="sekrit", urlopen_impl=_urlopen_returning(sample),
    )
    q = p.quote(_ROUTE)
    assert q is not None
    assert q.source == "duffel"
    assert q.cabin == Cabin.BUSINESS and q.cabin_confirmed is True
    assert q.deep_link is None  # order_flow
    assert q.amount == 4321.50
    assert q.currency == "USD"
    assert q.amount_brl_estimated == round(4321.50 * 5.5, 2)
    assert q.airline == "LA"


# ----------------- 4. Economy / sem cabine → bloqueado -----------------


def test_provider_economy_fixture_returns_none(monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    sample = json.loads((FIX / "duffel_economy_gru_mia.json").read_text())
    p = DuffelProvider(
        access_token="sekrit", urlopen_impl=_urlopen_returning(sample),
    )
    assert p.quote(_ROUTE) is None


# ----------------- 5. Sem ofertas → nenhum alerta -----------------


def test_provider_empty_fixture_returns_none(monkeypatch):
    sample = json.loads((FIX / "duffel_empty.json").read_text())
    p = DuffelProvider(
        access_token="sekrit", urlopen_impl=_urlopen_returning(sample),
    )
    assert p.quote(_ROUTE) is None


def test_pass_no_offers_sends_no_alert(tmp_path):
    notifier = _StubNotifier()
    duffel = _ScriptedDuffel([None])  # provider devolve None (sem oferta)
    monitor = _monitor(duffel, notifier, tmp_path)
    result = monitor.run_duffel_confirmations([_ROUTE])
    assert duffel.calls == 1
    assert result.duffel_confirmed_alerts == 0
    assert notifier.alerts == []


def test_provider_http_error_returns_none(monkeypatch):
    from urllib.error import HTTPError

    def boom(req, timeout=20):
        raise HTTPError(url="x", code=429, msg="rate", hdrs=None, fp=None)

    p = DuffelProvider(access_token="sekrit", urlopen_impl=boom)
    assert p.quote(_ROUTE) is None


# ----------------- 6 & 7. Telegram wording + no leak -----------------


def test_alert_contains_duffel_confirmed_wording(tmp_path):
    notifier = _StubNotifier()
    # BRL-nativo abaixo do teto GRU-MIA RT (excellent 1100) → alerta.
    duffel = _ScriptedDuffel([_confirmed_quote(price_brl=1000.0)])
    monitor = _monitor(duffel, notifier, tmp_path)
    result = monitor.run_duffel_confirmations([_ROUTE])
    assert result.duffel_confirmed_alerts == 1
    # PR #71: order_flow não envia standalone — vai p/ a mensagem AGRUPADA.
    assert notifier.messages == []
    assert len(notifier.grouped) == 1
    msg = notifier.grouped[0]
    assert "🟡 Ofertas confirmadas pela Duffel — compra pendente" in msg
    assert "Duffel" in msg
    assert "Executiva" in msg
    assert "link_status=order_flow" in msg
    assert "CM" in msg
    assert "Sem link direto de compra. Verificar no Duffel Dashboard." in msg


def test_alert_does_not_leak_offer_id_token_or_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    # Fixture com sentinelas (offer_id / passenger_id) — não podem vazar.
    # O quote vem do parser REAL (sanitização real); renderizamos o alerta
    # diretamente para inspecionar a mensagem, independente do teto.
    sample = json.loads((FIX / "duffel_business_gru_mia.json").read_text())
    captured: dict = {}
    p = DuffelProvider(
        access_token="sentinel_token_zzz",
        urlopen_impl=_urlopen_returning(sample, captured),
    )
    quote = p.quote(_ROUTE)
    assert quote is not None
    decision = Decision(
        alert=True, reason="t", criterion=CRITERION_CEILING,
        threshold=9000.0, level=LEVEL_EXCELLENT, score=90,
    )
    msg = format_alert(quote, decision)
    for sentinel in (
        "off_fixture_business_001",   # offer_id
        "pas_fixture_001",            # passenger_id
        "sentinel_token_zzz",         # token
        "api.duffel.com",             # URL crua / domínio da API
        "https://",
        "total_amount",               # chave de payload cru
        "cabin_class",
    ):
        assert sentinel not in msg, f"LEAK no alerta Duffel: {sentinel!r}"
    # Token nunca vai na URL (vai no header Authorization).
    assert "sentinel_token_zzz" not in captured.get("url", "")


# ----------------- 8. Nunca chama /air/orders -----------------


def test_provider_only_calls_offer_requests_never_orders(monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    sample = json.loads((FIX / "duffel_business_gru_mia.json").read_text())
    captured: dict = {}
    p = DuffelProvider(
        access_token="tok", urlopen_impl=_urlopen_returning(sample, captured),
    )
    p.quote(_ROUTE)
    url = captured["url"]
    assert "offer_requests" in url
    assert "orders" not in url
    assert "payments" not in url
    # POST (Offer Request), nunca outro verbo de criação de order.
    assert captured["method"] == "POST"


def test_duffel_api_url_is_only_offer_requests():
    """Guard estrutural: o único endpoint Duffel construído é
    /air/offer_requests. Nenhum módulo monta URL de /air/orders ou
    /air/payments (comentários explicativos podem citar o nome)."""
    from flight_mapper.actionability_readiness import DUFFEL_API_URL
    assert DUFFEL_API_URL.endswith("/air/offer_requests")
    import flight_mapper.duffel_provider as dp
    import flight_mapper.actionability_readiness as ar
    for mod in (dp, ar):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        # A URL completa de criação de order/payment NUNCA aparece no código.
        assert "api.duffel.com/air/orders" not in src
        assert "api.duffel.com/air/payments" not in src


# ----------------- gates: moeda / sanidade -----------------


def test_pass_blocks_when_currency_unconvertible(tmp_path, monkeypatch):
    # EUR sem EUR_BRL_RATE → amount_brl_estimated None → bloqueado, sem alerta.
    monkeypatch.delenv("EUR_BRL_RATE", raising=False)
    eur_quote = Quote(
        route=_ROUTE, price_brl=900.0, deep_link=None,
        departure_date="2026-09-10", return_date=None, source="duffel",
        amount=900.0, currency="EUR", amount_brl_estimated=None,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
        airline="CM",
    )
    notifier = _StubNotifier()
    duffel = _ScriptedDuffel([eur_quote])
    monitor = _monitor(duffel, notifier, tmp_path)
    result = monitor.run_duffel_confirmations([_ROUTE])
    assert result.duffel_blocked == 1
    assert notifier.alerts == []


def test_pass_blocks_suspicious_low_price(tmp_path, monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.5")
    # US$ 50 business round-trip ≈ R$275, abaixo do piso de sanidade.
    cheap = Quote(
        route=_ROUTE, price_brl=275.0, deep_link=None,
        departure_date="2026-09-10", return_date=None, source="duffel",
        amount=50.0, currency="USD", amount_brl_estimated=275.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True, trip_type=TripType.ONE_WAY,
        airline="CM",
    )
    notifier = _StubNotifier()
    duffel = _ScriptedDuffel([cheap])
    monitor = _monitor(duffel, notifier, tmp_path)
    result = monitor.run_duffel_confirmations([_ROUTE])
    assert result.duffel_blocked == 1
    assert notifier.alerts == []


def test_pass_respects_request_cap(tmp_path):
    notifier = _StubNotifier()
    # 3 rotas disponíveis mas cap=1 → só 1 chamada.
    duffel = _ScriptedDuffel([_confirmed_quote(), _confirmed_quote(), _confirmed_quote()])
    monitor = _monitor(duffel, notifier, tmp_path, max_requests=1)
    routes = [
        Route("GRU", "MIA", "EUA"),
        Route("GRU", "JFK", "EUA"),
        Route("GRU", "LHR", "Europa"),
    ]
    monitor.run_duffel_confirmations(routes)
    assert duffel.calls == 1


def test_duffel_history_isolated_from_main_store(tmp_path):
    """O pass Duffel NUNCA escreve no store principal (relatórios intactos)."""
    notifier = _StubNotifier()
    main_store = PriceStore(tmp_path / "main.json")
    duffel_store = PriceStore(tmp_path / "duffel.json")
    duffel = _ScriptedDuffel([_confirmed_quote(price_brl=3000.0)])
    monitor = Monitor(
        provider=_ScriptedDuffel([]), notifier=notifier, store=main_store,
        duffel_provider=duffel, duffel_store=duffel_store, duffel_max_requests=1,
    )
    monitor.run_duffel_confirmations([_ROUTE])
    # Store principal vazio; chave Duffel só no store isolado.
    assert list(main_store.keys()) == []
    assert any("::duffel" in k for k in duffel_store.keys())
