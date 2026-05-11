from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import StatusState, maybe_send_status


class _StubNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok

    def send_alert(self, *args, **kwargs):  # pragma: no cover - unused
        return True


def _result(scanned: int = 12, quotes: int = 6, alerts: int = 0) -> MonitorResult:
    return MonitorResult(
        scanned=scanned,
        quotes_received=quotes,
        alerts_sent=alerts,
        notes=[],
    )


def _populate(store: PriceStore, prices: dict[str, list[float]]) -> None:
    for key, values in prices.items():
        history = store.get(key)
        for value in values:
            history.push(value)
    store.save()


def test_first_run_sends(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    state = StatusState()
    notifier = _StubNotifier()
    state_path = tmp_path / "status.json"

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=state_path,
    )

    assert decision.action == "sent"
    assert decision.reason == "first_run"
    assert len(notifier.sent) == 1
    assert state_path.exists()
    assert state.last_report_at is not None


def test_throttle_blocks_within_window(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    state = StatusState(last_report_at=(now - timedelta(hours=23)).isoformat())
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=tmp_path / "status.json",
        now=now,
    )

    assert decision.action == "skipped"
    assert decision.reason == "throttled"
    assert notifier.sent == []


def test_sends_after_window(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    state = StatusState(last_report_at=(now - timedelta(hours=25)).isoformat())
    notifier = _StubNotifier()
    state_path = tmp_path / "status.json"

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=state_path,
        now=now,
    )

    assert decision.action == "sent"
    assert decision.reason == "window_elapsed"
    assert len(notifier.sent) == 1
    assert state.last_report_at == now.isoformat()


def test_top3_ordering(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(
        store,
        {
            "GRU-LHR-business": [3000.0, 1800.0],
            "GRU-MIA-business": [1207.0],
            "GRU-ORD-business": [1631.0],
            "GRU-FRA-business": [3322.0],
            "GRU-SFO-business": [1997.0],
        },
    )
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    assert decision.action == "sent"
    body = notifier.sent[0]
    miami_idx = body.index("GRU → MIA")
    ord_idx = body.index("GRU → ORD")
    lhr_idx = body.index("GRU → LHR")
    assert miami_idx < ord_idx < lhr_idx
    assert "GRU → FRA" not in body
    assert "GRU → SFO" not in body
    assert "São Paulo → Miami" in body
    assert "São Paulo → Chicago" in body
    assert "São Paulo → Londres" in body


def test_daily_report_omits_aviasales_links(tmp_path: Path):
    """Relatório diário é heartbeat — não traz links acionáveis nem genéricos."""
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [1207.0], "GRU-LHR-business": [1800.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # padrão antigo
    assert "https://www.aviasales.com/search" not in body
    # padrão novo (parametrizado) também não
    assert "search.aviasales.com/flights" not in body
    # nada de hyperlinks
    assert "<a href" not in body
    assert "conferir" not in body
    assert "Abrir oferta" not in body


def test_status_includes_regional_best_section(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(
        store,
        {
            "GRU-LHR-business": [1800.0],
            "GRU-FRA-business": [3322.0],   # Europa, mais caro que LHR
            "GRU-MIA-business": [1207.0],   # EUA
            "GRU-ORD-business": [1631.0],   # EUA, mais caro que MIA
            "GRU-DXB-business": [2798.0],   # Ásia
        },
    )
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "🌎 Melhor por região" in body
    # menor preço de cada região vence
    assert "Europa: GRU → LHR" in body
    assert "EUA: GRU → MIA" in body
    assert "Ásia: GRU → DXB" in body
    # rota não-vencedora não aparece no bloco regional
    assert "Europa: GRU → FRA" not in body
    assert "EUA: GRU → ORD" not in body


def test_status_uses_brl_with_dot_separator(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [10000.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "R$ 10.000" in body
    assert "R$ 10,000" not in body


def test_top3_handles_unknown_airport(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"XYZ-ABC-business": [999.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "XYZ → ABC" in body
    # relatório diário não inclui links — nem para rotas conhecidas, nem desconhecidas
    assert "https://www.aviasales.com/search" not in body
    assert "search.aviasales.com/flights" not in body


def test_does_not_persist_when_send_fails(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    state_path = tmp_path / "status.json"
    state = StatusState()
    notifier = _StubNotifier(ok=False)

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=state_path,
    )

    assert decision.action == "failed"
    assert decision.reason == "telegram_send_failed"
    assert state.last_report_at is None
    assert not state_path.exists()


def test_degraded_template_when_zero_quotes(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(scanned=12, quotes=0, alerts=0),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    assert decision.action == "sent"
    body = notifier.sent[0]
    assert "0 cotações" in body
    assert "Top 3" not in body


def test_empty_store_does_not_crash(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(scanned=12, quotes=6, alerts=0),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    assert decision.action == "sent"
    assert "Sem histórico disponível" in notifier.sent[0]


def test_no_notifier_skips_cleanly(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    state_path = tmp_path / "status.json"

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=None,
        state_path=state_path,
    )

    assert decision.action == "skipped"
    assert decision.reason == "no_notifier"
    assert not state_path.exists()


def test_status_state_round_trip(tmp_path: Path):
    path = tmp_path / "status.json"
    state = StatusState(last_report_at="2026-05-10T12:00:00+00:00")
    state.save(path)
    reloaded = StatusState.load(path)
    assert reloaded.last_report_at == "2026-05-10T12:00:00+00:00"


def test_status_state_load_handles_corrupt_file(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("not json", encoding="utf-8")
    state = StatusState.load(path)
    assert state.last_report_at is None
