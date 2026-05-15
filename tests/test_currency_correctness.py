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
    rendered = format_price(2079.0, CURRENCY_USD, 11226.6)
    assert rendered.startswith("US$ 2,079")
    assert "conversão estimada" in rendered
    # Não pode haver "R$" sem o qualificador de estimativa
    assert "R$" in rendered  # aparece, mas só dentro de "≈ ... estimada"
    assert "≈ R$" in rendered


def test_usd_alert_message_shows_usd_and_estimated_brl(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.4")
    quote = _UsdProvider(2079.0).quote(_ROUTE)
    decision = Decision(
        alert=True, reason="", criterion=CRITERION_CEILING,
        threshold=2100.0 * 5.4, level=LEVEL_GOOD, score=70,
    )
    body = format_alert(quote, decision, priority=True)
    assert "US$ 2,079" in body
    assert "≈ R$" in body
    assert "conversão estimada" in body
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
    assert any("ALERTA BLOQUEADO" in n for n in result.notes)
    # nada empurrado para o histórico
    assert store.get("GRU-JFK-business").prices == []


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
