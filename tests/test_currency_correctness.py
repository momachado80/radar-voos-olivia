"""Garantias de correção econômica de moeda no caminho do alerta.

Cobre o bug crítico: Travelpayouts devolve USD, o radar rotulava como
BRL e mandava "R$ 2.079" para tarifas que são US$ 2.079 (≈ R$ 11k+).
"""

from __future__ import annotations

from pathlib import Path

from flight_mapper.currency import (
    CURRENCY_USD,
    USD_BRL_RATE_ENV,
    get_usd_brl_rate,
    to_brl,
)
from flight_mapper.detector import CRITERION_CEILING, LEVEL_GOOD, Decision
from flight_mapper.formatting import format_price
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Route
from flight_mapper.state import PriceStore

_ROUTE = Route("GRU", "JFK", "EUA")


class _UsdProvider:
    """Mimetiza TravelpayoutsProvider: devolve USD, converte via env rate."""

    def __init__(self, usd_amount: float):
        self.usd_amount = usd_amount

    def quote(self, route: Route) -> Quote:
        rate = get_usd_brl_rate()
        brl = to_brl(self.usd_amount, CURRENCY_USD, rate)
        return Quote(
            route=route,
            price_brl=brl if brl is not None else self.usd_amount,
            deep_link=None,
            departure_date="2026-11-10",
            return_date="2026-11-17",
            source="travelpayouts",
            amount=self.usd_amount,
            currency=CURRENCY_USD,
            amount_brl_estimated=brl,
            fx_rate=rate,
        )


class _CaptureNotifier:
    def __init__(self):
        self.alerts: list[tuple[Quote, object]] = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append((quote, decision))
        return True

    def send(self, text):  # pragma: no cover
        return True


def _monitor(provider, notifier, store):
    from flight_mapper.monitor import Monitor

    return Monitor(
        provider=provider, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
        manual_purchase_fallback=True,
    )


# ---------- USD nunca é formatado como R$ ----------

def test_usd_price_never_rendered_as_plain_brl():
    rendered = format_price(2079.0, CURRENCY_USD, 11226.6, 5.4)
    assert rendered == "US$ 2.079 ≈ R$ 11.227"
    # nunca o número USD cru como "R$ 2.079"
    assert "R$ 2.079" not in rendered
    assert "≈ R$" in rendered


def test_usd_alert_message_shows_usd_and_estimated_brl(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")
    quote = _UsdProvider(2079.0).quote(_ROUTE)
    decision = Decision(
        alert=True, reason="", criterion=CRITERION_CEILING,
        threshold=2100.0 * 5.4, level=LEVEL_GOOD, score=70,
    )
    body = format_alert(quote, decision, priority=True)
    # Rule 6: valor original US$ + estimativa ≈ R$ + linha de câmbio
    assert "US$ 2.079 ≈ R$ 11.227" in body
    assert "Câmbio usado: USD_BRL_RATE=5.40" in body
    # jamais "R$ 2.079" cru (o bug original)
    assert "R$ 2.079" not in body.replace("≈ R$", "")


# ---------- USD convertido antes de comparar com thresholds ----------

def test_usd_converted_before_threshold_comparison(monkeypatch, tmp_path: Path):
    """US$ 1.500 com câmbio 5.4 → R$ 8.100; teto GRU-JFK (1800 USD) escala
    para R$ 9.720 → alerta dispara corretamente em BRL."""
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")
    provider = _UsdProvider(1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = _monitor(provider, notifier, store).run_once([_ROUTE])

    assert result.currency_blocked == 0
    assert result.alerts_sent == 1
    sent_quote, _ = notifier.alerts[0]
    # preço normalizado para BRL
    assert sent_quote.price_brl == round(1500.0 * 5.4, 2)
    assert sent_quote.currency == CURRENCY_USD
    assert sent_quote.amount == 1500.0


def test_usd_above_scaled_threshold_does_not_alert(monkeypatch, tmp_path: Path):
    """US$ 5.000 → R$ 27.000, muito acima do teto escalado → sem alerta."""
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")
    provider = _UsdProvider(5000.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = _monitor(provider, notifier, store).run_once([_ROUTE])

    assert result.currency_blocked == 0
    assert result.alerts_sent == 0


# ---------- Alerta bloqueado sem câmbio confiável / moeda desconhecida ----------

def test_alert_blocked_when_no_fx_rate(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(USD_BRL_RATE_ENV, raising=False)
    provider = _UsdProvider(1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = _monitor(provider, notifier, store).run_once([_ROUTE])

    assert result.currency_blocked == 1
    assert result.alerts_sent == 0
    assert notifier.alerts == []
    assert any(
        "alerta bloqueado: câmbio USD_BRL_RATE ausente ou inválido" in n
        for n in result.notes
    )
    # nada empurrado para o histórico
    assert store.get("GRU-JFK-business").prices == []


def test_alert_blocked_when_rate_invalid(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(USD_BRL_RATE_ENV, "abc")
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = _monitor(_UsdProvider(1500.0), notifier, store).run_once([_ROUTE])
    assert result.currency_blocked == 1
    assert result.alerts_sent == 0
    assert notifier.alerts == []


def test_alert_blocked_when_rate_zero_or_negative(monkeypatch, tmp_path: Path):
    for bad in ("0", "0.0", "-5.4"):
        monkeypatch.setenv(USD_BRL_RATE_ENV, bad)
        notifier = _CaptureNotifier()
        store = PriceStore(tmp_path / f"h_{bad}.json")
        result = _monitor(_UsdProvider(1500.0), notifier, store).run_once([_ROUTE])
        assert result.currency_blocked == 1, bad
        assert result.alerts_sent == 0, bad
        assert notifier.alerts == [], bad


def test_no_misleading_brl_ever_sent_for_usd(monkeypatch, tmp_path: Path):
    """Test 9: alerta de preço USD nunca sai com R$ cru do valor USD."""
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")
    provider = _UsdProvider(1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    _monitor(provider, notifier, store).run_once([_ROUTE])
    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    assert "US$ 1.500 ≈ R$ 8.100" in body
    assert "Câmbio usado: USD_BRL_RATE=5.40" in body
    # o número USD cru jamais aparece como "R$ 1.500"
    assert "R$ 1.500" not in body


def test_manual_fallback_shows_usd_and_estimated_brl(monkeypatch, tmp_path: Path):
    """Test 9 / Regra 7: manual fallback (sem link comercial) também mostra
    US$ + ≈ R$ + linha de câmbio, nunca R$ enganoso."""
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.5")
    provider = _UsdProvider(1878.0)  # GRU→LHR business do bug real
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    _monitor(provider, notifier, store).run_once([_ROUTE])

    sent_quote, decision = notifier.alerts[0]
    assert sent_quote.source == "manual_purchase"
    assert sent_quote.currency == CURRENCY_USD
    body = format_alert(sent_quote, decision, priority=True)
    assert "US$ 1.878 ≈ R$ 10.329" in body
    assert "Câmbio usado: USD_BRL_RATE=5.50" in body
    assert "R$ 1.878" not in body  # nunca o USD cru como R$


def test_old_price_history_without_currency_loads(monkeypatch, tmp_path: Path):
    """Test 8: price_history.json no schema antigo (sem campos de moeda)
    carrega e o relatório diário não mostra R$ enganoso."""
    import json

    from flight_mapper.status import StatusState, maybe_send_status

    hist = tmp_path / "price_history.json"
    hist.write_text(
        json.dumps(
            {
                "GRU-JFK-business": {
                    "prices": [1919.0, 1917.0],
                    "last_alert_at": None,
                    "last_alert_price": None,
                    "last_quote": {
                        "origin": "GRU",
                        "destination": "JFK",
                        "price_brl": 1917.0,
                        "departure_date": "2026-11-10",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    store = PriceStore(hist)
    assert store.get("GRU-JFK-business").prices == [1919.0, 1917.0]

    class _Stub:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return True

    notifier = _Stub()
    from flight_mapper.monitor import MonitorResult

    maybe_send_status(
        result=MonitorResult(scanned=10, quotes_received=5, alerts_sent=0),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )
    body = notifier.sent[0]
    assert "R$ 1.917" not in body
    assert "R$ 1.919" not in body
    assert "moeda não confirmada" in body


def test_alert_blocked_when_currency_unknown(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")

    class _EurProvider:
        def quote(self, route):
            return Quote(
                route=route, price_brl=1500.0, deep_link=None,
                departure_date="2026-11-10", return_date="2026-11-17",
                source="travelpayouts", amount=1500.0, currency="EUR",
                amount_brl_estimated=None,
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = _monitor(_EurProvider(), notifier, store).run_once([_ROUTE])

    assert result.currency_blocked == 1
    assert result.alerts_sent == 0
    assert notifier.alerts == []


# ---------- price_history mantém compatibilidade ----------

def test_price_history_schema_backward_compatible(monkeypatch, tmp_path: Path):
    """Quote legada (só price_brl) continua válida e history grava price_brl."""
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")

    class _BrlProvider:
        def quote(self, route):
            # caminho legado: só price_brl → __post_init__ assume BRL
            return Quote(
                route=route, price_brl=1500.0, deep_link=None,
                departure_date="2026-11-10", return_date="2026-11-17",
                source="kiwi",
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = _monitor(_BrlProvider(), notifier, store).run_once([_ROUTE])

    assert result.currency_blocked == 0
    h = store.get("GRU-JFK-business")
    assert h.prices == [1500.0]
    assert h.last_quote["price_brl"] == 1500.0
    # campos novos presentes no dump, sem quebrar o schema antigo
    assert h.last_quote["currency"] == "BRL"
    assert h.last_quote["amount"] == 1500.0


def test_legacy_quote_defaults_to_confirmed_brl():
    q = Quote(
        route=_ROUTE, price_brl=1234.0, deep_link=None,
        departure_date="2026-11-10", return_date=None, source="kiwi",
    )
    assert q.currency == "BRL"
    assert q.amount == 1234.0
    assert q.amount_brl_estimated == 1234.0


# ---------- mensagem mostra moeda correta para BRL nativo ----------

def test_brl_native_quote_still_shows_plain_brl():
    q = Quote(
        route=_ROUTE, price_brl=12500.0, deep_link=None,
        departure_date="2026-11-10", return_date="2026-11-17", source="kiwi",
    )
    decision = Decision(
        alert=True, reason="", criterion=CRITERION_CEILING,
        threshold=13000.0, level=LEVEL_GOOD, score=70,
    )
    body = format_alert(q, decision, priority=True)
    assert "R$ 12.500" in body
    assert "US$" not in body
    assert "conversão estimada" not in body
