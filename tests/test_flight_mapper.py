from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flight_mapper.cycle_state import CycleState
from flight_mapper.detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    DROP_THRESHOLD,
    MIN_SAMPLES,
    _within_dedupe,
    evaluate,
    evaluate_ceiling,
)
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
    assert "GRU-JFK-business" in PRIORITY_KEYS
    assert "GRU-LHR-business" in PRIORITY_KEYS
    assert "GRU-CDG-business" in PRIORITY_KEYS
    assert is_priority(Route("GRU", "SFO", "EUA")) is True
    assert is_priority(Route("GRU", "LHR", "Europa")) is True
    assert is_priority(Route("GRU", "FRA", "Europa")) is False


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


def test_mock_provider_returns_quote_for_every_route():
    provider = MockProvider(seed=1)
    quote = provider.quote(Route("GRU", "LHR", "Europa"))
    assert isinstance(quote, Quote)
    assert quote.price_brl > 0


class _StubNotifier:
    def __init__(self):
        self.alerts: list[tuple[str, str, bool]] = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append((quote.route.key, decision.criterion, priority))
        return True

    def send(self, text):  # pragma: no cover - not used in these tests
        return True


def test_evaluate_ceiling_fires_below_threshold():
    history = RouteHistory()
    decision = evaluate_ceiling(history, 1500.0, "GRU-LHR-business")
    assert decision.alert is True
    assert decision.criterion == CRITERION_CEILING
    assert decision.threshold == 1700


def test_evaluate_ceiling_silent_above_threshold():
    """GRU-LHR: good_brl=2000. Preço 2500 está acima → silencia."""
    history = RouteHistory()
    decision = evaluate_ceiling(history, 2500.0, "GRU-LHR-business")
    assert decision.alert is False
    assert decision.criterion == CRITERION_CEILING


def test_evaluate_ceiling_unknown_route_returns_no_alert():
    history = RouteHistory()
    decision = evaluate_ceiling(history, 100.0, "XYZ-ABC-business")
    assert decision.alert is False
    assert decision.threshold is None
    assert "sem teto" in decision.reason


def test_evaluate_ceiling_respects_dedupe():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = RouteHistory(
        last_alert_at=(now - timedelta(hours=2)).isoformat(),
        last_alert_price=1500.0,
    )
    # mesmo preço dentro da janela: dedupe
    decision = evaluate_ceiling(history, 1500.0, "GRU-LHR-business", now=now)
    assert decision.alert is False
    # preço ainda menor: dispara mesmo dentro da janela
    decision_lower = evaluate_ceiling(history, 1400.0, "GRU-LHR-business", now=now)
    assert decision_lower.alert is True


def test_evaluate_legacy_decision_carries_criterion():
    history = RouteHistory(prices=[10000.0] * MIN_SAMPLES)
    decision = evaluate(history, 7000.0)
    assert decision.alert is True
    assert decision.criterion == CRITERION_AVERAGE_DROP


# ---------- Dedupe inteligente (_within_dedupe testado direto) ----------

def _history_with_alert(now: datetime, hours_ago: float, price: float) -> RouteHistory:
    return RouteHistory(
        last_alert_at=(now - timedelta(hours=hours_ago)).isoformat(),
        last_alert_price=price,
    )


def test_within_dedupe_blocks_when_price_equal():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 2, 2000.0)
    assert _within_dedupe(history, 2000.0, now, 24) is True


def test_within_dedupe_blocks_when_price_worse():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 2, 2000.0)
    assert _within_dedupe(history, 2100.0, now, 24) is True


def test_within_dedupe_blocks_when_improvement_too_small():
    """Melhora R$ 50 (2.5% de R$ 2000): abaixo dos dois thresholds → dedupe."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 2, 2000.0)
    assert _within_dedupe(history, 1950.0, now, 24) is True


def test_within_dedupe_breaks_with_brl_threshold():
    """R$ 250 melhor numa rota cara (% < 5%): BRL >= 200 libera."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 2, 8000.0)
    # 8000 -> 7750 = R$ 250 (3.1%). BRL passa, % falha. OR libera.
    assert _within_dedupe(history, 7750.0, now, 24) is False


def test_within_dedupe_breaks_with_pct_threshold():
    """6% melhor numa rota barata (BRL < 200): % >= 5% libera."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 2, 1000.0)
    # 1000 -> 940 = R$ 60 (6%). % passa, BRL falha. OR libera.
    assert _within_dedupe(history, 940.0, now, 24) is False


def test_within_dedupe_breaks_with_both_thresholds():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 2, 5000.0)
    # 5000 -> 4500 = R$ 500 (10%). Ambos passam.
    assert _within_dedupe(history, 4500.0, now, 24) is False


def test_within_dedupe_window_expired_releases_regardless():
    """Fora da janela: libera mesmo sem melhoria."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = _history_with_alert(now, 30, 1500.0)
    assert _within_dedupe(history, 1500.0, now, 24) is False


def test_within_dedupe_no_prior_alert_releases():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    history = RouteHistory()
    assert _within_dedupe(history, 1500.0, now, 24) is False


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
    # 6000 BRL > teto 1700 da rota → ceiling silencia, legacy dispara
    _key, criterion, _priority = notifier.alerts[0]
    assert criterion == CRITERION_AVERAGE_DROP


def test_monitor_ceiling_wins_over_legacy(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    routes = [Route("GRU", "LHR", "Europa")]

    expensive = MockProvider(seed=42, baseline=10000.0, jitter=0.0)
    warmup_monitor = Monitor(provider=expensive, notifier=None, store=store)
    for _ in range(MIN_SAMPLES):
        warmup_monitor.run_once(routes)

    # 1500 BRL: <= teto 1700 (ceiling firing) E queda > 25% (legacy firing) — ceiling vence
    cheap = MockProvider(seed=99, baseline=1500.0, jitter=0.0)
    notifier = _StubNotifier()
    cheap_monitor = Monitor(provider=cheap, notifier=notifier, store=store)
    cheap_monitor.run_once(routes)

    assert len(notifier.alerts) == 1
    _key, criterion, _priority = notifier.alerts[0]
    assert criterion == CRITERION_CEILING


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
