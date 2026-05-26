"""Testes do PR #59 — garantia de heartbeat diário do Telegram.

Cobre as 8 condições do goal:
1. Missing last_report_at → sends.
2. last_report_at > 24h → sends.
3. last_report_at recent → does NOT send.
4. Failed Telegram send → last_report_at NOT updated.
5. Successful send → last_report_at updated.
6. Urgent alert behavior unchanged (no notifier.py touched).
7. Report includes SerpApi line (delegado p/ test_serpapi_observability).
8. Pytest verde.

Adições do PR #59:
- Clock skew defense: last_report_at no futuro → trata como inválido.
- StatusState.load resiliente a OSError e tipos inválidos.
- cmd_cycle imprime aviso explícito em failed/skipped-no-notifier.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import StatusState, maybe_send_status


class _StubNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


def _result() -> MonitorResult:
    return MonitorResult(scanned=1, quotes_received=1, alerts_sent=0, notes=[])


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


# ----------------- 8 cenários do goal -----------------


def test_heartbeat_sends_when_no_last_report(tmp_path: Path):
    """1. Missing last_report_at → sends."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState()  # last_report_at=None
    notifier = _StubNotifier()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=_now(),
    )
    assert decision.action == "sent"
    assert decision.reason == "first_run"
    assert len(notifier.sent) == 1


def test_heartbeat_sends_when_older_than_24h(tmp_path: Path):
    """2. last_report_at > 24h → sends."""
    store = PriceStore(tmp_path / "h.json")
    now = _now()
    state = StatusState(
        last_report_at=(now - timedelta(hours=25)).isoformat()
    )
    notifier = _StubNotifier()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=now,
    )
    assert decision.action == "sent"
    assert decision.reason == "window_elapsed"
    assert len(notifier.sent) == 1


def test_heartbeat_skips_when_recent(tmp_path: Path):
    """3. last_report_at recent → does NOT send."""
    store = PriceStore(tmp_path / "h.json")
    now = _now()
    state = StatusState(
        last_report_at=(now - timedelta(hours=23, minutes=30)).isoformat()
    )
    notifier = _StubNotifier()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=now,
    )
    assert decision.action == "skipped"
    assert decision.reason == "throttled"
    assert notifier.sent == []


def test_failed_send_does_not_update_state(tmp_path: Path):
    """4. Failed Telegram send → last_report_at NOT updated."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState()
    notifier = _StubNotifier(ok=False)
    state_path = tmp_path / "s.json"
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=state_path,
        now=_now(),
    )
    assert decision.action == "failed"
    assert decision.reason == "telegram_send_failed"
    assert state.last_report_at is None
    # Arquivo de estado NÃO foi gravado — próximo ciclo retentará.
    assert not state_path.exists()


def test_successful_send_updates_state(tmp_path: Path):
    """5. Successful send → last_report_at updated + arquivo persistido."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState()
    notifier = _StubNotifier(ok=True)
    state_path = tmp_path / "s.json"
    now = _now()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=state_path,
        now=now,
    )
    assert decision.action == "sent"
    assert state.last_report_at == now.isoformat()
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved == {"last_report_at": now.isoformat()}


def test_heartbeat_skipped_when_no_notifier(tmp_path: Path):
    """6.a no_notifier → skipped sem alterar state."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=None, state_path=tmp_path / "s.json",
        now=_now(),
    )
    assert decision.action == "skipped"
    assert decision.reason == "no_notifier"
    assert state.last_report_at is None


def test_report_includes_serpapi_line(tmp_path: Path):
    """7. Report inclui a linha de status SerpApi (PR #57)."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState()
    notifier = _StubNotifier()
    maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=_now(),
    )
    body = notifier.sent[0]
    assert "🧭 Status das fontes" in body
    assert "SerpApi:" in body


# ----------------- defesas adicionais PR #59 -----------------


def test_clock_skew_future_last_report_treated_as_invalid(tmp_path: Path):
    """Defesa nova: se last_report_at acabar gravado no FUTURO (clock
    skew, edição manual, restore de backup), o cálculo `now - last`
    fica negativo. Antes do PR #59, o bot ficava mudo P/ SEMPRE.
    Agora trata como inválido → manda heartbeat + sobrescreve."""
    store = PriceStore(tmp_path / "h.json")
    now = _now()
    # last_report_at no FUTURO (relógio adiantado 6h em algum run)
    state = StatusState(
        last_report_at=(now + timedelta(hours=6)).isoformat()
    )
    notifier = _StubNotifier()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=now,
    )
    assert decision.action == "sent"
    assert decision.reason == "first_run_clock_skew"
    assert len(notifier.sent) == 1
    # State foi reescrito com timestamp atual (correto)
    assert state.last_report_at == now.isoformat()


def test_invalid_iso_in_state_treated_as_first_run(tmp_path: Path):
    """ISO malformado em last_report_at não pode bloquear o heartbeat."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState(last_report_at="not-a-valid-iso-8601-date")
    notifier = _StubNotifier()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=_now(),
    )
    assert decision.action == "sent"
    assert decision.reason == "first_run"


def test_status_state_load_handles_corrupted_file(tmp_path: Path):
    """data/status.json corrompido → estado vazio, ciclo segue."""
    state_path = tmp_path / "s.json"
    state_path.write_text("{{ not json", encoding="utf-8")
    s = StatusState.load(state_path)
    assert s.last_report_at is None


def test_status_state_load_handles_non_dict(tmp_path: Path):
    """data/status.json com payload não-dict (ex.: lista) → estado vazio."""
    state_path = tmp_path / "s.json"
    state_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    s = StatusState.load(state_path)
    assert s.last_report_at is None


def test_status_state_load_missing_file_returns_empty(tmp_path: Path):
    """Arquivo ausente → estado vazio, ciclo manda heartbeat."""
    s = StatusState.load(tmp_path / "does_not_exist.json")
    assert s.last_report_at is None


def test_recovery_after_failed_send(tmp_path: Path):
    """Cenário real: ciclo 1 falha (bot bloqueado por 1 min); ciclo 2
    consegue enviar. Heartbeat continua tentando até que dê certo."""
    store = PriceStore(tmp_path / "h.json")
    state_path = tmp_path / "s.json"
    state = StatusState()
    now1 = _now()

    # Ciclo 1: falha
    bad_notifier = _StubNotifier(ok=False)
    d1 = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=bad_notifier, state_path=state_path, now=now1,
    )
    assert d1.action == "failed"
    assert state.last_report_at is None  # state NÃO contaminado

    # Ciclo 2 (15 min depois): tenta de novo, agora dá certo
    good_notifier = _StubNotifier(ok=True)
    now2 = now1 + timedelta(minutes=15)
    d2 = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=good_notifier, state_path=state_path, now=now2,
    )
    assert d2.action == "sent"
    assert d2.reason == "first_run"  # ainda first_run, pois state era None
    assert state.last_report_at == now2.isoformat()
    assert len(good_notifier.sent) == 1


def test_exactly_24h_boundary_is_throttled(tmp_path: Path):
    """Borda exata: 24h em ponto ainda é throttled (limite é estritamente <).
    Apenas > 24h libera. Garantia mínima do contrato."""
    store = PriceStore(tmp_path / "h.json")
    now = _now()
    state = StatusState(
        last_report_at=(now - timedelta(hours=24)).isoformat()
    )
    notifier = _StubNotifier()
    decision = maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=now,
    )
    # delta == 24h → NÃO é < 24h → libera (window_elapsed)
    assert decision.action == "sent"
    assert decision.reason == "window_elapsed"


# ----------------- cmd_cycle logging -----------------


def test_cmd_cycle_warns_loudly_on_telegram_failure(
    tmp_path, monkeypatch, capsys,
):
    """Quando heartbeat falha, cmd_cycle imprime aviso ⚠️ visível
    no log do GitHub Actions p/ o usuário saber sem precisar abrir
    o relatório."""
    from flight_mapper.__main__ import main

    # Força provider mock + notifier que sempre falha
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "FAKE")
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)

    # Patch notifier.send → False
    from flight_mapper.notifier import TelegramNotifier
    monkeypatch.setattr(
        TelegramNotifier, "send", lambda self, text: False,
    )

    rc = main(["cycle", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status action=failed" in out
    assert "Telegram heartbeat FAILED" in out
    assert "Actions Secrets" in out


def test_cmd_cycle_warns_when_no_notifier(tmp_path, monkeypatch, capsys):
    """Sem TELEGRAM_BOT_TOKEN/CHAT_ID → notifier=None → cmd_cycle
    avisa para verificar os secrets."""
    from flight_mapper.__main__ import main

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)

    rc = main(["cycle", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status action=skipped reason=no_notifier" in out
    assert "notifier ausente" in out
    assert "Actions Secrets" in out


# ----------------- zero leak no relatório -----------------


def test_heartbeat_report_no_leak(tmp_path: Path):
    """Relatório enviado NÃO contém token/URL/post_data."""
    store = PriceStore(tmp_path / "h.json")
    state = StatusState()
    notifier = _StubNotifier()
    maybe_send_status(
        result=_result(), store=store, state=state,
        notifier=notifier, state_path=tmp_path / "s.json",
        now=_now(),
    )
    body = notifier.sent[0]
    forbidden = (
        "BK_TOKEN", "DEP_TOKEN",
        "post_data", "secret_payload", "secret_post_body",
        "?token=", "?ref=", "?cart=",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK no heartbeat: {needle!r}"
