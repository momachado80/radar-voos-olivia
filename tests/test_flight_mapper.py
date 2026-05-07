from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flight_mapper.cycle_state import CycleState
from flight_mapper.detector import DROP_THRESHOLD, MIN_SAMPLES, evaluate
from flight_mapper.monitor import Monitor
from flight_mapper.providers import MockProvider, Quote
from flight_mapper.regions import Route, all_routes
from flight_mapper.state import HISTORY_WINDOW, PriceStore, RouteHistory


def test_all_routes_covers_each_region():
    routes = all_routes()
    regions = {route.region for route in routes}
    assert regions == {"Europa", "EUA", "Ásia"}
    assert len(routes) > 30


def test_route_history_push_caps_window():
    history = RouteHistory()
    for value in range(HISTORY_WINDOW + 10):
        history.push(float(value))
    assert len(history.prices) == HISTORY_WINDOW
    assert history.prices[0] == float(10)


def test_route_history_average_none_when_empty():
    assert RouteHistory().average is None


def test_price_store_round_trip(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-LHR-business")
    history.push(8000.0)
    history.push(7500.0)
    store.save()

    reopened = PriceStore(tmp_path / "h.json")
    assert reopened.get("GRU-LHR-business").prices == [8000.0, 7500.0]


def test_detector_waits_for_min_samples():
    history = RouteHistory(prices=[8000.0])
    decision = evaluate(history, 5000.0)
    assert decision.alert is False
    assert "acumulando" in decision.reason


def test_detector_alerts_on_significant_drop():
    history = RouteHistory(prices=[10000.0] * MIN_SAMPLES)
    decision = evaluate(history, 7000.0)
    assert decision.alert is True
    assert decision.drop_pct is not None and decision.drop_pct >= DROP_THRESHOLD


def test_detector_skips_minor_drop():
    history = RouteHistory(prices=[10000.0] * MIN_SAMPLES)
    decision = evaluate(history, 9000.0)
    assert decision.alert is False


def test_detector_dedupes_within_24h_unless_lower(monkeypatch):
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    history = RouteHistory(
        prices=[10000.0] * MIN_SAMPLES,
        last_alert_at=(now - timedelta(hours=2)).isoformat(),
        last_alert_price=7000.0,
    )
    same_price = evaluate(history, 7000.0, now=now)
    assert same_price.alert is False
    lower_price = evaluate(history, 6500.0, now=now)
    assert lower_price.alert is True


def test_cycle_state_advances_and_wraps(tmp_path: Path):
    state = CycleState.load(tmp_path / "c.json")
    start, end = state.next_chunk(total=10, chunk_size=4)
    assert (start, end) == (0, 4)
    state.advance(total=10, chunk_size=4)
    assert state.cursor == 4
    state.advance(total=10, chunk_size=4)
    state.advance(total=10, chunk_size=4)
    assert state.cursor == 2  # wrap


def test_cycle_state_persists(tmp_path: Path):
    path = tmp_path / "c.json"
    state = CycleState.load(path)
    state.cursor = 7
    state.save()
    assert CycleState.load(path).cursor == 7


def test_mock_provider_returns_quote_for_every_route():
    provider = MockProvider(seed=1)
    quote = provider.quote(Route("GRU", "LHR", "Europa"))
    assert isinstance(quote, Quote)
    assert quote.price_brl > 0


class _StubNotifier:
    def __init__(self):
        self.alerts: list[tuple[str, float, float]] = []

    def send_alert(self, quote, average, drop_pct):
        self.alerts.append((quote.route.key, average, drop_pct))
        return True

    def send(self, text):  # pragma: no cover - not used in these tests
        return True


def test_monitor_alerts_after_history_warmup(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    expensive_provider = MockProvider(seed=42, baseline=10000.0, jitter=0.0)
    monitor = Monitor(provider=expensive_provider, notifier=None, store=store)
    routes = [Route("GRU", "LHR", "Europa")]

    for _ in range(MIN_SAMPLES):
        monitor.run_once(routes)
    assert store.get("GRU-LHR-business").average == pytest.approx(10000.0)

    cheap_provider = MockProvider(seed=99, baseline=6000.0, jitter=0.0)
    notifier = _StubNotifier()
    cheap_monitor = Monitor(provider=cheap_provider, notifier=notifier, store=store)
    cheap_monitor.run_once(routes)
    assert len(notifier.alerts) == 1


def test_monitor_run_cycle_processes_chunk_and_advances(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    cycle = CycleState.load(tmp_path / "c.json")
    monitor = Monitor(
        provider=MockProvider(seed=0),
        notifier=None,
        store=store,
        cycle=cycle,
        chunk_size=5,
    )
    result = monitor.run_cycle()
    assert result.scanned == 5
    assert cycle.cursor == 5
