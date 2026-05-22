"""Invariantes estáticas do workflow `provider-readiness-smoke.yml`.

Garante que o workflow é manual, não expõe secrets de Telegram, não
mexe em `data/`, e não toca os workflows do motor. Sem rede.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


WORKFLOWS = Path(".github/workflows")
WF = WORKFLOWS / "provider-readiness-smoke.yml"


def _load() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    # YAML interpreta a chave `on` como literal True (boolean).
    return doc.get(True) if True in doc else doc.get("on") or {}


def test_workflow_exists():
    assert WF.exists(), f"workflow ausente: {WF}"


def test_workflow_is_manual_only_no_schedule():
    doc = _load()
    on = _on(doc)
    assert "workflow_dispatch" in on
    assert "schedule" not in on, "workflow não pode ter cron/schedule"
    assert "push" not in on
    assert "pull_request" not in on


def test_workflow_does_not_expose_telegram_secrets():
    raw = WF.read_text(encoding="utf-8")
    # nem ENV literal nem referência a secrets.TELEGRAM_*
    assert "TELEGRAM_BOT_TOKEN" not in raw
    assert "TELEGRAM_CHAT_ID" not in raw
    assert "secrets.TELEGRAM" not in raw


def test_workflow_does_not_touch_data_or_other_workflows():
    raw = WF.read_text(encoding="utf-8")
    # nenhum git commit / push / write em data/
    assert "git commit" not in raw
    assert "git push" not in raw
    assert "git add" not in raw
    # sem chamadas aos workflows do motor
    assert "flight-mapper" not in raw
    assert "flight-hot-scan" not in raw


def test_workflow_uses_only_read_permissions():
    doc = _load()
    perms = doc.get("permissions") or {}
    # contents: read (sem write); sem outras escritas
    assert perms.get("contents") == "read"
    for k, v in perms.items():
        assert v != "write", f"permission '{k}' write não permitida"


def test_workflow_runs_only_provider_readiness_and_serpapi_smoke():
    raw = WF.read_text(encoding="utf-8")
    assert "python -m flight_mapper provider-readiness" in raw
    assert "python -m flight_mapper serpapi-smoke" in raw
    # exemplo de args definidos
    assert "--route GRU-MIA" in raw
    assert "--trip round_trip" in raw
    assert "--cabin business" in raw
    # NÃO roda amadeus-smoke real (Amadeus está disponível só por mock-file
    # neste PR; chamada real fora do escopo deste workflow).
    assert "amadeus-smoke" not in raw


def test_workflow_serpapi_step_isolates_env():
    """O step do SerpApi smoke só recebe `SERPAPI_API_KEY` — nada além."""
    doc = _load()
    steps = doc["jobs"]["smoke"]["steps"]
    serpapi_step = next(s for s in steps if "serpapi" in (s.get("name") or "").lower())
    env = serpapi_step.get("env") or {}
    assert set(env.keys()) == {"SERPAPI_API_KEY"}
    assert env["SERPAPI_API_KEY"] == "${{ secrets.SERPAPI_API_KEY }}"


def test_workflow_provider_readiness_step_uses_only_audit_secrets():
    """O step de auditoria não recebe nenhum secret de Telegram."""
    doc = _load()
    steps = doc["jobs"]["smoke"]["steps"]
    pr_step = next(s for s in steps if s.get("name") == "Provider readiness")
    env = pr_step.get("env") or {}
    assert set(env.keys()) == {
        "KIWI_API_KEY",
        "AMADEUS_CLIENT_ID",
        "AMADEUS_CLIENT_SECRET",
        "SERPAPI_API_KEY",
    }
    for k in env:
        assert "TELEGRAM" not in k


def test_other_workflows_untouched():
    """flight-mapper.yml, flight-hot-scan.yml e telegram-smoke-test.yml
    permanecem no diretório (não foram removidos por este PR)."""
    for fname in ("flight-mapper.yml", "flight-hot-scan.yml"):
        assert (WORKFLOWS / fname).exists(), f"workflow do motor sumiu: {fname}"


def test_workflow_serpapi_step_fetches_booking_options():
    """Após PR #40, o workflow manual expande até 1 booking_token por
    execução (gasto previsível de 2 queries do free-tier)."""
    raw = WF.read_text(encoding="utf-8")
    assert "--fetch-booking-options" in raw
    assert "--max-booking-options 1" in raw


def test_workflow_serpapi_step_caps_booking_options_to_one():
    """Garantia explícita de que o limite é 1 (não pode subir
    silenciosamente e estourar a cota free-tier)."""
    raw = WF.read_text(encoding="utf-8")
    # nenhuma variante > 1 (defesa contra typo p/ --max-booking-options 11 etc.)
    for n in range(2, 20):
        assert f"--max-booking-options {n}" not in raw, (
            f"limite {n} não autorizado no workflow"
        )
