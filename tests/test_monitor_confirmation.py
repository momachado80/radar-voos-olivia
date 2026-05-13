"""Tests for FASE C: second-fetch confirmation and last_quote population."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from flight_mapper.airports import build_search_url
from flight_mapper.detector import (
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    LEVEL_GOOD,
)
from flight_mapper.monitor import Monitor
from flight_mapper.providers import Quote
from flight_mapper.regions import Route
from flight_mapper.state import PriceStore


class _ScriptedProvider:
    """Provider de teste: devolve quotes pré-programadas. Conta chamadas."""

    def __init__(self, quotes: list[Quote | None]):
        self._iter: Iterator[Quote | None] = iter(quotes)
        self.calls = 0

    def quote(self, route: Route) -> Quote | None:
        self.calls += 1
        try:
            return next(self._iter)
        except StopIteration:
            return None


class _StubNotifier:
    def __init__(self):
        self.alerts: list[Quote] = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append(quote)
        return True

    def send(self, text):  # pragma: no cover
        return True


def _quote(price: float, route: Route) -> Quote:
    return Quote(
        route=route,
        price_brl=price,
        deep_link=f"https://www.kiwi.com/deep/{route.origin}-{route.destination}-2026-06-15",
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="travelpayouts",
    )


_ROUTE_LHR = Route("GRU", "LHR", "Europa")


def test_alert_sent_when_second_quote_confirms(tmp_path: Path):
    """First quote 1500 (<= excellent 1700), second 1500 → confirma, envia."""
    provider = _ScriptedProvider([_quote(1500.0, _ROUTE_LHR), _quote(1500.0, _ROUTE_LHR)])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert result.stale_quotes_skipped == 0
    assert result.actionable_links_generated == 1
    assert provider.calls == 2  # primeira + confirmação
    assert len(notifier.alerts) == 1


def test_alert_skipped_when_second_quote_missing(tmp_path: Path):
    """First quote alerta, segunda devolve None → stale, não envia."""
    provider = _ScriptedProvider([_quote(1500.0, _ROUTE_LHR), None])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.stale_quotes_skipped == 1
    assert len(notifier.alerts) == 0
    assert provider.calls == 2


def test_alert_skipped_when_second_quote_above_tolerance(tmp_path: Path):
    """First quote 1500, segunda 2500 (66% acima) → stale."""
    provider = _ScriptedProvider([_quote(1500.0, _ROUTE_LHR), _quote(2500.0, _ROUTE_LHR)])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.stale_quotes_skipped == 1
    assert provider.calls == 2


def test_alert_sent_when_second_quote_within_tolerance(tmp_path: Path):
    """First quote 1500, segunda 1570 (~4.7% acima, abaixo da tolerância 5%) → confirma."""
    provider = _ScriptedProvider([_quote(1500.0, _ROUTE_LHR), _quote(1570.0, _ROUTE_LHR)])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert provider.calls == 2


def test_no_second_call_when_no_initial_alert(tmp_path: Path):
    """Quote acima de good_brl → sem alerta, sem segunda chamada."""
    provider = _ScriptedProvider([_quote(2500.0, _ROUTE_LHR)])  # acima de 2000
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.stale_quotes_skipped == 0
    assert provider.calls == 1


def test_confirmation_bypassed_when_flag_off(tmp_path: Path):
    """confirm_alerts=False pula segunda chamada."""
    provider = _ScriptedProvider([_quote(1500.0, _ROUTE_LHR)])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store, confirm_alerts=False)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert provider.calls == 1


def test_actionable_link_check_blocks_alert(tmp_path: Path):
    """Mesmo confirmado, se link não for acionável → não envia, conta non_actionable."""
    bad_quote = Quote(
        route=_ROUTE_LHR,
        price_brl=1500.0,
        deep_link="https://www.aviasales.com/search/GRULHR",  # padrão antigo, não acionável
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="travelpayouts",
    )
    provider = _ScriptedProvider([bad_quote, bad_quote])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.non_actionable_links_skipped == 1
    assert len(notifier.alerts) == 0


def test_last_quote_populated_on_every_quote(tmp_path: Path):
    """Mesmo sem alerta, last_quote é gravado."""
    provider = _ScriptedProvider([_quote(2500.0, _ROUTE_LHR)])  # sem alerta
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=None, store=store)

    monitor.run_once([_ROUTE_LHR])

    lq = store.get("GRU-LHR-business").last_quote
    assert lq is not None
    assert lq["price_brl"] == 2500.0
    assert lq["origin"] == "GRU"
    assert lq["destination"] == "LHR"
    assert lq["actionable_url"] is True


def test_last_quote_records_actionable_url_field(tmp_path: Path):
    """Quando o deep_link é acionável, last_quote.actionable_url=True."""
    provider = _ScriptedProvider([_quote(2500.0, _ROUTE_LHR)])
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=None, store=store)

    monitor.run_once([_ROUTE_LHR])

    lq = store.get("GRU-LHR-business").last_quote
    assert lq["actionable_url"] is True
    assert "kiwi.com" in lq["deep_link"]


def test_decision_carries_level_excellent(tmp_path: Path):
    """Preço excelente → Decision.level == 'excellent', alerta enviado."""
    provider = _ScriptedProvider([_quote(1500.0, _ROUTE_LHR), _quote(1500.0, _ROUTE_LHR)])
    notifier = _StubNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(provider=provider, notifier=notifier, store=store)

    captured = []

    class _Capture(_StubNotifier):
        def send_alert(self, quote, decision, priority=False):
            captured.append(decision)
            return True

    monitor.notifier = _Capture()
    monitor.run_once([_ROUTE_LHR])

    assert len(captured) == 1
    assert captured[0].level == LEVEL_EXCELLENT
    assert captured[0].criterion == CRITERION_CEILING


def test_decision_carries_level_good(tmp_path: Path):
    """GRU-LHR good_brl=2000. Preço 1900 → level=good."""
    provider = _ScriptedProvider([_quote(1900.0, _ROUTE_LHR), _quote(1900.0, _ROUTE_LHR)])
    store = PriceStore(tmp_path / "h.json")

    captured = []

    class _Capture:
        def send_alert(self, quote, decision, priority=False):
            captured.append(decision)
            return True

    monitor = Monitor(provider=provider, notifier=_Capture(), store=store)
    monitor.run_once([_ROUTE_LHR])

    assert len(captured) == 1
    assert captured[0].level == LEVEL_GOOD
