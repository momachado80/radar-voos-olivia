"""Testes do PR #55 — orçamento MENSAL SerpApi (substituindo o diário
do PR #54). Cron mantém */15.

Cobre:
1. Workflow YAML: cron */15 mantido + env MONTHLY_BUDGET=90, sem mais
   DAILY_BUDGET.
2. Config: default 90, parse, inválido → default, cap 0..10000, negativo → 0.
3. SerpApiValidationBudget (schema novo {month_utc, count}):
   load/save/reset_if_new_month/add_queries/remaining.
4. Defesa: arquivo ausente, malformado, schema antigo (date_utc) →
   reset silencioso para schema novo.
5. validate_cycle_candidates:
   - bloqueia quando remaining < ESTIMATED_QUERIES_PER_VALIDATION;
   - reset em virada de mês UTC;
   - budget=0 desliga;
   - sucesso normal incrementa por ESTIMATED_QUERIES_PER_VALIDATION (=3).
6. Zero leak no arquivo persistido.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from flight_mapper.serpapi_client import SerpApiClient, SerpApiError
from flight_mapper.serpapi_validation import (
    DEFAULT_MONTHLY_BUDGET,
    ESTIMATED_QUERIES_PER_VALIDATION,
    SerpApiValidationBudget,
    SerpApiValidationCandidate,
    SerpApiValidationConfig,
    validate_cycle_candidates,
)


# ----------------- cron + env no workflow -----------------


def test_workflow_cron_still_every_15_minutes():
    """PR #55 mantém o cron */15 do PR #54. Sem regressão p/ */30."""
    import yaml
    path = Path(".github/workflows/flight-mapper.yml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    on = doc.get(True) or doc.get("on") or {}
    schedule = on.get("schedule") or []
    crons = [s.get("cron") for s in schedule if isinstance(s, dict)]
    assert "*/15 * * * *" in crons, f"cron */15 ausente: {crons}"


def test_workflow_env_has_monthly_budget_not_daily():
    """Step Run cycle: substitui DAILY_BUDGET por MONTHLY_BUDGET=90."""
    import yaml
    path = Path(".github/workflows/flight-mapper.yml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    step = next(
        s for s in doc["jobs"]["cycle"]["steps"]
        if s.get("name") == "Run cycle"
    )
    env = step.get("env") or {}
    # Novo
    assert env.get("SERPAPI_VALIDATION_MONTHLY_BUDGET") == "90"
    # Antigo removido (PR #55 substitui o orçamento diário)
    assert "SERPAPI_VALIDATION_DAILY_BUDGET" not in env, (
        "DAILY_BUDGET deve ser removido em favor de MONTHLY_BUDGET"
    )
    # Gates antigos preservados
    assert env.get("SERPAPI_VALIDATION_ENABLED") == "true"
    assert env.get("SERPAPI_VALIDATION_MAX_PER_CYCLE") == "1"


# ----------------- config: monthly_budget -----------------


def test_config_default_monthly_budget():
    """Sem env, default = DEFAULT_MONTHLY_BUDGET = 90."""
    cfg = SerpApiValidationConfig.from_env(env={})
    assert cfg.monthly_budget == DEFAULT_MONTHLY_BUDGET == 90


def test_config_parses_monthly_budget_from_env():
    cfg = SerpApiValidationConfig.from_env(
        env={"SERPAPI_VALIDATION_MONTHLY_BUDGET": "15"},
    )
    assert cfg.monthly_budget == 15


def test_config_invalid_monthly_budget_falls_back_to_default():
    for v in ["abc", ""]:
        cfg = SerpApiValidationConfig.from_env(
            env={"SERPAPI_VALIDATION_MONTHLY_BUDGET": v},
        )
        assert cfg.monthly_budget == DEFAULT_MONTHLY_BUDGET, v


def test_config_negative_monthly_budget_becomes_zero():
    """Valor negativo → 0 (desliga validação explicitamente)."""
    cfg = SerpApiValidationConfig.from_env(
        env={"SERPAPI_VALIDATION_MONTHLY_BUDGET": "-1"},
    )
    assert cfg.monthly_budget == 0


def test_config_caps_monthly_budget_at_10000():
    """Teto generoso (cobre planos pagos), mas blinda contra typo."""
    cfg = SerpApiValidationConfig.from_env(
        env={"SERPAPI_VALIDATION_MONTHLY_BUDGET": "9999999"},
    )
    assert cfg.monthly_budget == 10000


def test_estimated_queries_per_validation_constant():
    """Custo conservador = 3 queries (worst case round-trip)."""
    assert ESTIMATED_QUERIES_PER_VALIDATION == 3


# ----------------- SerpApiValidationBudget (schema novo) -----------------


def test_budget_load_missing_file_returns_this_month_zero():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "nonexistent.json"
        b = SerpApiValidationBudget.load(p)
    assert b.count == 0
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert b.month_utc == this_month


def test_budget_load_corrupt_file_returns_this_month_zero():
    """Defesa: JSON malformado nunca quebra o pipeline."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        p.write_text("not json {{{", encoding="utf-8")
        b = SerpApiValidationBudget.load(p)
    assert b.count == 0


def test_budget_load_legacy_daily_schema_migrates_silently():
    """PR #54 escrevia {date_utc, count}. PR #55 lê {month_utc, count}.
    Schema antigo é ignorado → reset transparente p/ mês atual."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        p.write_text(
            json.dumps({"date_utc": "2026-05-24", "count": 19}),
            encoding="utf-8",
        )
        b = SerpApiValidationBudget.load(p)
    # Schema novo: month_utc do mês atual, count zerado
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert b.month_utc == this_month
    assert b.count == 0


def test_budget_load_unexpected_extra_keys_ignored():
    """Schema-strict: campos extras (incluindo legados) são ignorados."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        p.write_text(
            json.dumps({
                "month_utc": "2026-05",
                "count": 42,
                "leaked_token": "BK_TOKEN_SHOULD_NOT_BE_HERE",
                "leaked_url": "https://google.com/clk?token=secret",
                "date_utc": "2026-05-24",  # legado, ignorado
            }),
            encoding="utf-8",
        )
        b = SerpApiValidationBudget.load(p)
    assert b.month_utc == "2026-05"
    assert b.count == 42


def test_budget_save_only_writes_month_and_count():
    """Schema fechado: NUNCA grava token, URL, payload — só
    month_utc + count."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        b = SerpApiValidationBudget(month_utc="2026-05", count=12)
        b.save(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw.keys()) == {"month_utc", "count"}
    assert raw == {"month_utc": "2026-05", "count": 12}


def test_budget_save_with_none_path_is_noop():
    b = SerpApiValidationBudget(month_utc="2026-05", count=1)
    b.save(None)  # não deve levantar


def test_budget_reset_on_new_month():
    b = SerpApiValidationBudget(month_utc="2026-04", count=90)
    b2 = b.reset_if_new_month("2026-05")
    assert b2.count == 0
    assert b2.month_utc == "2026-05"


def test_budget_no_reset_same_month():
    b = SerpApiValidationBudget(month_utc="2026-05", count=15)
    b2 = b.reset_if_new_month("2026-05")
    assert b2.count == 15
    assert b2.month_utc == "2026-05"


def test_budget_add_queries_immutable():
    b = SerpApiValidationBudget(month_utc="2026-05", count=10)
    b2 = b.add_queries(3)
    assert b.count == 10  # original imutável
    assert b2.count == 13
    assert b2.month_utc == b.month_utc


def test_budget_add_queries_negative_clamped_to_zero():
    """Defesa: nunca decrementa o contador."""
    b = SerpApiValidationBudget(month_utc="2026-05", count=10)
    b2 = b.add_queries(-5)
    assert b2.count == 10  # não diminui


def test_budget_remaining():
    b = SerpApiValidationBudget(month_utc="2026-05", count=30)
    assert b.remaining(90) == 60
    assert b.remaining(30) == 0
    assert b.remaining(20) == 0  # nunca negativo


# ----------------- integração: monthly budget guard -----------------


def _candidate(key: str = "GRU-MIA-one_way-business") -> SerpApiValidationCandidate:
    return SerpApiValidationCandidate(
        key=key, origin="GRU", destination="MIA",
        outbound_date="2026-09-10", return_date=None,
        travel_class="business", expected_usd=220.0,
    )


def test_validate_cycle_blocks_when_remaining_below_estimated_cost(tmp_path):
    """Budget=2 (remaining < 3=ESTIMATED): zero chamadas."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, monthly_budget=2, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate()], cfg, lambda key: _M(), budget_path=budget_path,
    )
    assert calls["n"] == 0  # bloqueado: 2 < 3
    assert out == {}


def test_validate_cycle_consumes_estimated_cost_per_validation(tmp_path):
    """Cada validação tentada consome ESTIMATED_QUERIES_PER_VALIDATION
    (3) do budget — independente de sucesso ou falha."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=2, monthly_budget=10, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate("k1"), _candidate("k2")],
        cfg, lambda key: _M(), budget_path=budget_path,
    )
    # 2 validações tentadas (max_per_cycle=2; budget 10 cobre 2×3=6)
    assert calls["n"] == 2
    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    assert saved["count"] == 6  # 2 × 3 = 6


def test_validate_cycle_stops_when_budget_runs_out_mid_loop(tmp_path):
    """Budget=5: cobre 1 validação (3 queries), bloqueia a 2ª
    (remaining 2 < 3)."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=3, monthly_budget=5, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate("k1"), _candidate("k2"), _candidate("k3")],
        cfg, lambda key: _M(), budget_path=budget_path,
    )
    assert calls["n"] == 1
    assert len(out) == 1
    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    assert saved["count"] == 3


def test_validate_cycle_resets_budget_on_new_month(tmp_path):
    """Arquivo do mês passado → reset automático no mês atual."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, monthly_budget=90, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    # Budget cheio no MÊS PASSADO
    budget_path.write_text(
        json.dumps({"month_utc": "2020-01", "count": 90}),
        encoding="utf-8",
    )
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate()], cfg, lambda key: _M(), budget_path=budget_path,
    )
    # Reset no mês atual → validou 1
    assert calls["n"] == 1
    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert saved["month_utc"] == this_month
    assert saved["count"] == 3  # 1 validação × 3 queries


def test_validate_cycle_monthly_budget_zero_disables(tmp_path):
    """monthly_budget=0 → no-op silencioso, mesmo com enabled=true."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, monthly_budget=0, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate()], cfg, lambda key: _M(), budget_path=budget_path,
    )
    assert calls["n"] == 0
    assert out == {}


def test_validate_cycle_legacy_daily_file_does_not_crash(tmp_path):
    """Arquivo com schema antigo {date_utc, count} → migração silenciosa
    (reset para mês atual), nada quebra."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, monthly_budget=90, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    budget_path.write_text(
        json.dumps({"date_utc": "2026-05-24", "count": 19}),
        encoding="utf-8",
    )
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate()], cfg, lambda key: _M(), budget_path=budget_path,
    )
    # Migração: count antigo descartado → validou 1
    assert calls["n"] == 1
    # Arquivo re-escrito no schema novo
    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    assert set(saved.keys()) == {"month_utc", "count"}
    assert saved["count"] == 3
    assert "date_utc" not in saved


# ----------------- defesa: zero leak no arquivo de budget -----------------


def test_budget_file_never_contains_sensitive_payload(tmp_path):
    """Garantia: após múltiplos ciclos com payload contaminado em erros
    SerpApi, o arquivo persistido NUNCA contém nada além de
    {month_utc, count}."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, monthly_budget=90, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    SENTINEL_PAYLOADS = [
        "BK_TOKEN_THAT_MUST_NEVER_LEAK_xxxxxxxxxxxxxxxx",
        "DEP_TOKEN_THAT_MUST_NEVER_LEAK_yyyyyyyyyyyyyyy",
        "BR_secret_payload_xxxxxxx",
        "https://www.google.com/travel/clk?token=secret_xyz",
        "?post_data=secret",
        "carriers=SECRET_CARRIER",
        "price=999.99_secret",
    ]
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            raise SerpApiError(SENTINEL_PAYLOADS[0])
    for i in range(3):
        validate_cycle_candidates(
            [_candidate(f"k{i}")], cfg, lambda key: _M(),
            budget_path=budget_path,
        )
    saved_text = budget_path.read_text(encoding="utf-8")
    for sentinel in SENTINEL_PAYLOADS:
        assert sentinel not in saved_text, (
            f"LEAK no arquivo de budget: {sentinel!r}"
        )
    saved = json.loads(saved_text)
    assert set(saved.keys()) == {"month_utc", "count"}
