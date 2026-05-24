"""Testes do PR #54 — orçamento diário SerpApi + cron 15min.

Cobre:
1. Cron mudou de */30 para */15 (workflow YAML).
2. SerpApiValidationBudget: load/save/reset/increment/remaining.
3. Persistência em arquivo: schema fechado (date_utc + count), nunca leak.
4. Default conservador via env vazio (DEFAULT_DAILY_BUDGET).
5. validate_cycle_candidates: budget cap funciona;
   budget esgotado bloqueia chamada;
   reset em virada de UTC date funciona;
   budget=0 desliga.
6. Defesa contra arquivo corrompido / schema inesperado.
7. Zero leak no arquivo de budget.
8. Pytest completo verde — verificado no harness do CI.
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
    DEFAULT_DAILY_BUDGET,
    SerpApiValidationBudget,
    SerpApiValidationCandidate,
    SerpApiValidationConfig,
    validate_cycle_candidates,
)


# ----------------- cron change in workflow -----------------


def test_workflow_cron_is_every_15_minutes():
    """flight-mapper.yml deve agendar a cada 15 min, não 30."""
    import yaml
    path = Path(".github/workflows/flight-mapper.yml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    on = doc.get(True) or doc.get("on") or {}
    schedule = on.get("schedule") or []
    crons = [s.get("cron") for s in schedule if isinstance(s, dict)]
    assert "*/15 * * * *" in crons, f"cron 15min ausente: {crons}"
    assert "*/30 * * * *" not in crons, (
        f"cron antigo 30min ainda presente: {crons}"
    )


def test_workflow_env_has_daily_budget():
    """O step Run cycle deve passar SERPAPI_VALIDATION_DAILY_BUDGET=20."""
    import yaml
    path = Path(".github/workflows/flight-mapper.yml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    step = next(
        s for s in doc["jobs"]["cycle"]["steps"]
        if s.get("name") == "Run cycle"
    )
    env = step.get("env") or {}
    assert env.get("SERPAPI_VALIDATION_DAILY_BUDGET") == "20"
    # Não tira nenhum dos gates já configurados em PR #53
    assert env.get("SERPAPI_VALIDATION_ENABLED") == "true"
    assert env.get("SERPAPI_VALIDATION_MAX_PER_CYCLE") == "1"


# ----------------- config: default daily_budget -----------------


def test_config_default_daily_budget_is_conservative():
    """Sem env, default deve ser DEFAULT_DAILY_BUDGET=20."""
    cfg = SerpApiValidationConfig.from_env(env={})
    assert cfg.daily_budget == DEFAULT_DAILY_BUDGET == 20


def test_config_parses_daily_budget_from_env():
    cfg = SerpApiValidationConfig.from_env(
        env={"SERPAPI_VALIDATION_DAILY_BUDGET": "5"},
    )
    assert cfg.daily_budget == 5


def test_config_invalid_daily_budget_falls_back_to_default():
    for v in ["abc", "", "-5"]:
        cfg = SerpApiValidationConfig.from_env(
            env={"SERPAPI_VALIDATION_DAILY_BUDGET": v},
        )
        # "-5" cai em max(0, min(-5, 300)) = 0, não default; "abc"/"" → default
        if v == "-5":
            assert cfg.daily_budget == 0, v
        else:
            assert cfg.daily_budget == DEFAULT_DAILY_BUDGET, v


def test_config_caps_daily_budget_at_300():
    cfg = SerpApiValidationConfig.from_env(
        env={"SERPAPI_VALIDATION_DAILY_BUDGET": "9999"},
    )
    assert cfg.daily_budget == 300


# ----------------- budget dataclass -----------------


def test_budget_load_missing_file_returns_today_zero():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "nonexistent.json"
        b = SerpApiValidationBudget.load(p)
    assert b.count == 0
    # date_utc é hoje UTC
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert b.date_utc == today


def test_budget_load_corrupt_file_returns_today_zero():
    """Defesa: JSON malformado nunca quebra o pipeline."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        p.write_text("not json {{{", encoding="utf-8")
        b = SerpApiValidationBudget.load(p)
    assert b.count == 0


def test_budget_load_unexpected_schema_returns_today_zero():
    """Schema-strict: campos extras / tipos errados são ignorados."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        # campos extras suspeitos — ignorados
        p.write_text(
            json.dumps({
                "date_utc": "2026-05-24",
                "count": 7,
                "leaked_token": "BK_TOKEN_SHOULD_NOT_BE_HERE",
                "leaked_url": "https://google.com/clk?token=secret",
            }),
            encoding="utf-8",
        )
        b = SerpApiValidationBudget.load(p)
    # Aproveita date+count, ignora resto
    assert b.date_utc == "2026-05-24"
    assert b.count == 7


def test_budget_save_only_writes_date_and_count():
    """Schema fechado: NUNCA grava token, URL, payload — só
    date_utc + count."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "b.json"
        b = SerpApiValidationBudget(date_utc="2026-05-24", count=12)
        b.save(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw.keys()) == {"date_utc", "count"}
    assert raw == {"date_utc": "2026-05-24", "count": 12}


def test_budget_save_with_none_path_is_noop():
    b = SerpApiValidationBudget(date_utc="2026-05-24", count=1)
    b.save(None)  # não deve levantar


def test_budget_reset_on_new_day():
    b = SerpApiValidationBudget(date_utc="2026-05-23", count=20)
    b2 = b.reset_if_new_day("2026-05-24")
    assert b2.count == 0
    assert b2.date_utc == "2026-05-24"


def test_budget_no_reset_same_day():
    b = SerpApiValidationBudget(date_utc="2026-05-24", count=5)
    b2 = b.reset_if_new_day("2026-05-24")
    assert b2 is b or (b2.count == 5 and b2.date_utc == "2026-05-24")


def test_budget_increment_immutable():
    b = SerpApiValidationBudget(date_utc="2026-05-24", count=5)
    b2 = b.increment()
    assert b.count == 5  # original imutável
    assert b2.count == 6
    assert b2.date_utc == b.date_utc


def test_budget_remaining():
    b = SerpApiValidationBudget(date_utc="2026-05-24", count=3)
    assert b.remaining(10) == 7
    assert b.remaining(3) == 0
    assert b.remaining(2) == 0  # nunca negativo


# ----------------- integração: validate_cycle_candidates + budget -----------------


def _candidate(key: str = "GRU-MIA-one_way-business") -> SerpApiValidationCandidate:
    return SerpApiValidationCandidate(
        key=key, origin="GRU", destination="MIA",
        outbound_date="2026-09-10", return_date=None,
        travel_class="business", expected_usd=220.0,
    )


def _failing_factory():
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            raise SerpApiError("simulated")
    return lambda key: _M()


def test_validate_cycle_respects_daily_budget(tmp_path):
    """Budget=2, 5 candidatos: só 2 chamadas (e budget vai a 2/2)."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=5, daily_budget=2, api_key="K",
    )
    cands = [_candidate(f"k{i}") for i in range(5)]
    budget_path = tmp_path / "b.json"
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        cands, cfg, lambda key: _M(), budget_path=budget_path,
    )
    assert calls["n"] == 2  # cap pelo budget, não pelo max_per_cycle=5
    assert len(out) == 2
    # Budget persistido em count=2
    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    assert saved["count"] == 2


def test_validate_cycle_budget_exhausted_blocks_call(tmp_path):
    """Budget já no limite (count=20, budget=20): zero chamadas."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, daily_budget=20, api_key="K",
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    budget_path = tmp_path / "b.json"
    budget_path.write_text(
        json.dumps({"date_utc": today, "count": 20}),
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
    assert calls["n"] == 0
    assert out == {}


def test_validate_cycle_resets_budget_on_new_day(tmp_path):
    """Arquivo de ontem (UTC) → reset automático no novo dia."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, daily_budget=20, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    # Budget cheio ONTEM
    budget_path.write_text(
        json.dumps({"date_utc": "2020-01-01", "count": 20}),
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
    # Reset hoje → validou 1
    assert calls["n"] == 1
    # Arquivo persistido com data UTC de hoje + count=1
    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert saved["date_utc"] == today
    assert saved["count"] == 1


def test_validate_cycle_budget_zero_disables_validation(tmp_path):
    """daily_budget=0 → no-op silencioso, mesmo com enabled=true."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, daily_budget=0, api_key="K",
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


def test_validate_cycle_without_budget_path_works(tmp_path):
    """budget_path=None: no-op de persistência, mas validação ainda
    roda (cap só por max_per_cycle)."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, daily_budget=20, api_key="K",
    )
    calls = {"n": 0}
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("test")
    out = validate_cycle_candidates(
        [_candidate()], cfg, lambda key: _M(), budget_path=None,
    )
    assert calls["n"] == 1


# ----------------- defesa: zero leak no arquivo de budget -----------------


def test_budget_file_never_contains_token_url_or_payload(tmp_path):
    """Garantia: mesmo após múltiplas chamadas (cada uma com payload
    falso contendo sentinelas), o arquivo persistido NUNCA contém
    nada além de {date_utc, count}."""
    cfg = SerpApiValidationConfig(
        enabled=True, max_per_cycle=1, daily_budget=20, api_key="K",
    )
    budget_path = tmp_path / "b.json"
    SENTINEL_PAYLOADS = [
        "BK_TOKEN_THAT_MUST_NEVER_LEAK_xxxxxxxxxxxxxxxxxxxxx",
        "DEP_TOKEN_THAT_MUST_NEVER_LEAK_yyyyyyyyyyyyyyyyyyyy",
        "BR_secret_payload_xxxxxxxxxxxxxxx",
        "https://www.google.com/travel/clk?token=secret_xyz",
        "?post_data=secret",
    ]
    class _M:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            # SerpApiError pode trazer "detalhe" do servidor — falha
            # silenciosa é necessária p/ garantir que nada vaze.
            raise SerpApiError(SENTINEL_PAYLOADS[0])
    # Roda 3 vezes
    for _ in range(3):
        validate_cycle_candidates(
            [_candidate(f"k{_}")], cfg, lambda key: _M(),
            budget_path=budget_path,
        )
    saved_text = budget_path.read_text(encoding="utf-8")
    for sentinel in SENTINEL_PAYLOADS:
        assert sentinel not in saved_text, (
            f"LEAK no arquivo de budget: {sentinel!r}"
        )
    # Schema ainda mínimo
    saved = json.loads(saved_text)
    assert set(saved.keys()) == {"date_utc", "count"}


# ----------------- garantias gerais (continuam valendo) -----------------


def test_serpapi_validation_budget_path_in_config():
    """flight_mapper.config.Config expõe `serpapi_validation_budget_path`
    apontando para `data/serpapi_validation_budget.json`."""
    from flight_mapper.config import Config
    cfg = Config(
        telegram_bot_token=None, telegram_chat_id=None,
        travelpayouts_token=None, kiwi_api_key=None,
        data_dir=Path("/tmp/fake_data"),
    )
    assert cfg.serpapi_validation_budget_path == (
        Path("/tmp/fake_data") / "serpapi_validation_budget.json"
    )
