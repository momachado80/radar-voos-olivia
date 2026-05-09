from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flight_mapper.cycle_state import CycleState
from flight_mapper.detector import DROP_THRESHOLD, MIN_SAMPLES, evaluate
from flight_mapper.monitor import Monitor
from flight_mapper.providers import MockProvider, Quote, TravelpayoutsProvider
from flight_mapper.regions import PRIORITY_KEYS, Route, all_routes, is_priority
from flight_mapper.state import HISTORY_WINDOW, PriceStore, RouteHistory


def test_all_routes_covers_each_region():
    routes = all_routes()
    regions = {route.region for route in routes}
    assert regions == {"Europa", "EUA", "Ásia"}
    assert all(r.origin == "GRU" for r in routes)
    assert len(routes) >= 20


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


def test_priority_threshold_alerts_on_smaller_drop():
    history = RouteHistory(prices=[10000.0] * MIN_SAMPLES)
    normal = evaluate(history, 8200.0)  # 18% drop, abaixo do limite normal de 25%
    assert normal.alert is False
    priority = evaluate(history, 8200.0, priority=True)
    assert priority.alert is True


def test_priority_dedupe_window_is_shorter():
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    history = RouteHistory(
        prices=[10000.0] * MIN_SAMPLES,
        last_alert_at=(now - timedelta(hours=15)).isoformat(),
        last_alert_price=8000.0,
    )
    normal = evaluate(history, 8000.0, now=now)  # ainda dentro de 24h
    assert normal.alert is False
    priority = evaluate(history, 8000.0, now=now, priority=True)  # fora dos 12h
    assert priority.alert is True


def test_priority_keys_cover_target_routes():
    assert "GRU-SFO-business" in PRIORITY_KEYS
    assert "GRU-LAS-business" in PRIORITY_KEYS
    assert "GRU-LHR-business" in PRIORITY_KEYS
    assert "GRU-CDG-business" in PRIORITY_KEYS
    assert is_priority(Route("GRU", "SFO", "EUA")) is True
    assert is_priority(Route("GRU", "LAS", "EUA")) is True
    assert is_priority(Route("GRU", "LHR", "Europa")) is True
    assert is_priority(Route("GRU", "FRA", "Europa")) is False
    assert is_priority(Route("GRU", "JFK", "EUA")) is False


def test_sfo_present_in_routes():
    assert any(r.destination == "SFO" for r in all_routes())


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


def test_travelpayouts_search_url_with_round_trip():
    route = Route("GRU", "LHR", "Europa")
    url = TravelpayoutsProvider._search_url(route, "2026-06-15", "2026-06-22")
    assert url == "https://www.aviasales.com/search/GRU1506LHR22061"


def test_travelpayouts_search_url_one_way():
    route = Route("GRU", "LHR", "Europa")
    url = TravelpayoutsProvider._search_url(route, "2026-06-15", None)
    assert url == "https://www.aviasales.com/search/GRU1506LHR1"


def test_travelpayouts_search_url_falls_back_when_date_invalid():
    route = Route("GRU", "LHR", "Europa")
    url = TravelpayoutsProvider._search_url(route, "", None)
    assert url == "https://www.aviasales.com/search/GRULHR"


def test_mock_provider_returns_quote_for_every_route():
    provider = MockProvider(seed=1)
    quote = provider.quote(Route("GRU", "LHR", "Europa"))
    assert isinstance(quote, Quote)
    assert quote.price_brl > 0


class _StubNotifier:
    def __init__(self):
        self.alerts: list[tuple[str, float, float, bool]] = []

    def send_alert(self, quote, average, drop_pct, priority=False):
        self.alerts.append((quote.route.key, average, drop_pct, priority))
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


def test_monitor_run_cycle_includes_priority_plus_chunk(tmp_path: Path):
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
    # rotas prioritárias + 5 do chunk
    assert result.scanned == len(PRIORITY_KEYS) + 5
    assert cycle.cursor == 5
    # cursor avança apenas sobre as não-prioritárias
    keys_in_history = set(store.keys())
    for priority_key in PRIORITY_KEYS:
        assert priority_key in keys_in_history


def test_monitor_priority_routes_scanned_every_cycle(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    cycle = CycleState.load(tmp_path / "c.json")
    monitor = Monitor(
        provider=MockProvider(seed=0),
        notifier=None,
        store=store,
        cycle=cycle,
        chunk_size=5,
    )
    for _ in range(3):
        monitor.run_cycle()
    # Após 3 ciclos, prioritárias devem ter 3 amostras cada
    for key in PRIORITY_KEYS:
        assert len(store.get(key).prices) == 3
