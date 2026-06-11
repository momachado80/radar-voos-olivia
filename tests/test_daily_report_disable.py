"""Testes do PR #85 — desligar o heartbeat diário do Telegram.

A Olivia pediu pra desligar o "Radar de Voos Olivia — relatório diário"
(~200 linhas, 1x/dia) porque os alertas em tempo real do `grouped_push`
(PR #80) já entregam o que importa. Esse PR adiciona
`Config.daily_report_enabled` (default True por compat) controlado pela
env `DAILY_REPORT_ENABLED` ("false" desliga).

O que tem que continuar funcionando MESMO COM O HEARTBEAT DESLIGADO:
- alertas em tempo real `grouped_push` (não passam por `maybe_send_status`);
- alertas executivos confirmados standalone;
- ciclo completo (Duffel/Travelpayouts/store) — só o último step de
  heartbeat é gated.
"""

from __future__ import annotations

import os

import pytest

from flight_mapper.config import Config


# ----------------- 1. Config lê a env -----------------


def test_default_daily_report_enabled_is_true(monkeypatch):
    """Sem env: ligado (compat com testes e usuários fora da produção
    da Olivia)."""
    monkeypatch.delenv("DAILY_REPORT_ENABLED", raising=False)
    assert Config.from_env().daily_report_enabled is True


def test_env_false_disables_daily_report(monkeypatch):
    monkeypatch.setenv("DAILY_REPORT_ENABLED", "false")
    assert Config.from_env().daily_report_enabled is False


def test_env_false_is_case_insensitive(monkeypatch):
    for value in ("FALSE", "False", "fAlSe", " false "):
        monkeypatch.setenv("DAILY_REPORT_ENABLED", value)
        assert Config.from_env().daily_report_enabled is False, value


@pytest.mark.parametrize(
    "value",
    ["true", "TRUE", "1", "yes", "on", "", "0", "lixo"],
)
def test_env_anything_other_than_false_keeps_enabled(value, monkeypatch):
    """Fail-safe: só 'false' (case-insensitive) desliga. Qualquer outro
    valor — inclusive lixo — mantém ligado (não queremos desligar por
    typo)."""
    monkeypatch.setenv("DAILY_REPORT_ENABLED", value)
    assert Config.from_env().daily_report_enabled is True, value


# ----------------- 2. Dataclass default não quebra construção -----------------


def test_config_can_be_constructed_without_daily_report_enabled():
    """Compat: callers existentes que não conhecem o campo seguem
    funcionando (default True)."""
    c = Config(
        telegram_bot_token=None, telegram_chat_id=None,
        travelpayouts_token=None, kiwi_api_key=None,
        data_dir=os.path.curdir,
    )
    assert c.daily_report_enabled is True


# ----------------- 3. Workflow está com DAILY_REPORT_ENABLED=false -----------------


def test_production_workflow_has_daily_report_disabled():
    """Garantia operacional: a Olivia roda em produção com desligado.
    Se alguém remover sem perceber, esse teste pega.

    Lê o YAML como texto (sem pyyaml) — assertion simples, evita
    dependência nova e não conflita com o ambiente local sem yaml."""
    from pathlib import Path
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github" / "workflows" / "flight-mapper.yml"
    )
    text = workflow.read_text(encoding="utf-8")
    assert 'DAILY_REPORT_ENABLED: "false"' in text, (
        "Produção da Olivia deve manter o heartbeat diário desligado "
        "(`grouped_push` cobre os alertas em tempo real)."
    )


# ----------------- 4. cmd_cycle pula o relatório quando desligado -----------------


def test_cmd_cycle_skips_heartbeat_when_disabled(tmp_path, monkeypatch, capsys):
    """Com DAILY_REPORT_ENABLED=false, o ciclo nem tenta enviar — sai com
    log explícito `status action=skipped reason=daily_report_disabled`.
    NÃO há aviso de "Telegram heartbeat FAILED" (não houve tentativa)."""
    from flight_mapper.__main__ import main

    monkeypatch.setenv("DAILY_REPORT_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "FAKE")
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)

    # Sentinela: se algo chamar maybe_send_status, o teste falha.
    sentinel = {"called": False}
    import flight_mapper.__main__ as main_mod

    def _explode(*args, **kwargs):
        sentinel["called"] = True
        raise AssertionError(
            "maybe_send_status não deveria ser chamado quando "
            "DAILY_REPORT_ENABLED=false"
        )

    monkeypatch.setattr(main_mod, "maybe_send_status", _explode)

    rc = main(["cycle", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert sentinel["called"] is False
    assert "status action=skipped reason=daily_report_disabled" in out
    # NÃO deve ter o aviso de falha do Telegram — não houve tentativa.
    assert "Telegram heartbeat FAILED" not in out
    assert "notifier ausente" not in out


def test_cmd_cycle_still_sends_heartbeat_when_enabled(
    tmp_path, monkeypatch, capsys,
):
    """Sanity check: sem a env (ou com qualquer valor != 'false'), o ciclo
    chama maybe_send_status normalmente. Garante que o gate não afetou o
    caminho ligado."""
    from flight_mapper.__main__ import main

    monkeypatch.delenv("DAILY_REPORT_ENABLED", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "FAKE")
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)

    from flight_mapper.notifier import TelegramNotifier
    monkeypatch.setattr(
        TelegramNotifier, "send", lambda self, text: True,
    )

    rc = main(["cycle", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    # Quando ligado, o output tem "status action=" com alguma reason
    # (sent/throttled/etc), não a nova reason `daily_report_disabled`.
    assert "status action=" in out
    assert "daily_report_disabled" not in out
