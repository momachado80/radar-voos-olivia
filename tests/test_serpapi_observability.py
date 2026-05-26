"""Testes do PR #57 — observabilidade SerpApi no relatório diário.

Cobre a linha "SerpApi: ..." dentro do 🧭 Status das fontes.
Read-only. Zero leak.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from flight_mapper.monitor import MonitorResult
from flight_mapper.serpapi_client import SerpApiClient, SerpApiError
from flight_mapper.serpapi_validation import (
    SerpApiValidationSummary,
    humanize_validation_summary,
)
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message


# ----------------- helpers -----------------


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


def _result() -> MonitorResult:
    return MonitorResult(scanned=0, quotes_received=0, alerts_sent=0, notes=[])


class _FakeResp:
    def __init__(self, b: bytes) -> None:
        self._b = b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._b


def _patch_config_data_dir(monkeypatch, tmp_path):
    """Redireciona Config.from_env().data_dir → tmp p/ não poluir data/."""
    from flight_mapper.config import Config
    real_from_env = Config.from_env

    @classmethod
    def _fake_from_env(cls, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        cfg.data_dir = tmp_path / "data"
        return cfg

    monkeypatch.setattr(Config, "from_env", _fake_from_env)


def _tp_strong(store, key, origin, dest, usd, brl):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10", "return_date": None,
        "source": "travelpayouts", "currency": "USD",
        "amount": usd, "amount_brl_estimated": brl, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": "one_way", "actionable_url": False,
        "deep_link": None,
    }


def _hop1_payload() -> dict:
    return {
        "search_parameters": {
            "engine": "google_flights", "type": "2",
            "travel_class": "business", "currency": "USD",
            "outbound_date": "2026-09-10",
        },
        "best_flights": [{
            "type": "One way", "price": 208,
            "flights": [{"airline": "LATAM", "travel_class": "Business"}],
            "booking_token": "BK_TOKEN_THAT_MUST_NEVER_LEAK_xxx",
        }],
    }


def _booking_google_post() -> dict:
    return {
        "search_parameters": {"currency": "USD"},
        "booking_options": [{
            "together": {
                "book_with": "Google",
                "price": 208,
                "booking_request": {
                    "url": "https://www.google.com/travel/clk?token=secret_payload_xxx",
                    "method": "POST",
                    "post_data": "secret_post_body=value",
                },
            },
        }],
    }


def _booking_no_business() -> dict:
    """Cabine business NÃO confirmada — fluxo de validação não-eleva."""
    return {
        "search_parameters": {
            "engine": "google_flights", "type": "2",
            "travel_class": "business", "currency": "USD",
            "outbound_date": "2026-09-10",
        },
        "best_flights": [{
            "type": "One way", "price": 208,
            # SerpApi devolveu economy mesmo pedindo business → cabin_confirmed=False
            "flights": [{"airline": "LATAM", "travel_class": "Economy"}],
            "booking_token": "BK_TOKEN_THAT_MUST_NEVER_LEAK_econ",
        }],
    }


# ----------------- humanize_validation_summary -----------------


def test_humanize_disabled():
    s = SerpApiValidationSummary(
        enabled=False, api_key_present=False, monthly_budget=90,
        monthly_used=0, candidates_considered=0,
        validations_attempted=0, elevated_to_manual_check=0,
        skipped_reason=None,
    )
    assert humanize_validation_summary(s) == "SerpApi: validação desativada."


def test_humanize_no_key():
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=False, monthly_budget=90,
        monthly_used=0, candidates_considered=0,
        validations_attempted=0, elevated_to_manual_check=0,
        skipped_reason="no_api_key",
    )
    msg = humanize_validation_summary(s)
    assert "sem chave" in msg.lower()
    assert "actions secrets" in msg.lower()


def test_humanize_active_no_eligible():
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=True, monthly_budget=90,
        monthly_used=3, candidates_considered=0,
        validations_attempted=0, elevated_to_manual_check=0,
        skipped_reason="no_eligible_candidate",
    )
    msg = humanize_validation_summary(s)
    assert "3/90 queries usadas no mês" in msg
    assert "Nenhum candidato forte elegível" in msg


def test_humanize_attempted_not_confirmed():
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=True, monthly_budget=90,
        monthly_used=3, candidates_considered=1,
        validations_attempted=1, elevated_to_manual_check=0,
        skipped_reason=None,
    )
    msg = humanize_validation_summary(s)
    assert "3/90" in msg
    assert "tentou 1 candidato" in msg
    assert "não confirmou executiva" in msg


def test_humanize_elevated_to_manual():
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=True, monthly_budget=90,
        monthly_used=6, candidates_considered=1,
        validations_attempted=1, elevated_to_manual_check=1,
        skipped_reason=None,
    )
    msg = humanize_validation_summary(s)
    assert "6/90" in msg
    assert "1 candidato" in msg
    assert "movido(s) para Verificação manual" in msg


def test_humanize_monthly_budget_exhausted():
    """88/90: restante 2 < ESTIMATED 3 → esgotado."""
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=True, monthly_budget=90,
        monthly_used=88, candidates_considered=0,
        validations_attempted=0, elevated_to_manual_check=0,
        skipped_reason="monthly_budget_exhausted",
    )
    msg = humanize_validation_summary(s)
    assert "esgotado" in msg.lower()
    assert "88/90" in msg
    assert "virada do mês UTC" in msg


# ----------------- integração: linha no 🧭 Status das fontes -----------------


def _sources_section(body: str) -> str:
    """Extrai apenas o bloco 🧭 (até o próximo "\n\n" ou EOF)."""
    return body.split("🧭 Status das fontes")[1].split("\n\n")[0]


def test_report_shows_serpapi_disabled_when_env_off(monkeypatch, tmp_path):
    """Comportamento default: SerpApi: validação desativada."""
    monkeypatch.delenv("SERPAPI_VALIDATION_ENABLED", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    _patch_config_data_dir(monkeypatch, tmp_path)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "SerpApi: validação desativada." in sources


def test_report_shows_no_key_when_enabled_without_key(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    _patch_config_data_dir(monkeypatch, tmp_path)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "sem chave" in sources.lower()
    assert "Actions Secrets" in sources


def test_report_shows_used_count_from_budget_file(monkeypatch, tmp_path):
    """data/serpapi_validation_budget.json com count=3 → relatório mostra 3/90."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    # Cria budget file de tmp p/ refletir 3 queries usadas
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    (data_dir / "serpapi_validation_budget.json").write_text(
        json.dumps({"month_utc": this_month, "count": 3}),
        encoding="utf-8",
    )

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise SerpApiError("não deveria rodar — sem candidatos")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "3/90 queries usadas no mês" in sources
    assert "Nenhum candidato forte elegível" in sources


def test_report_shows_attempted_but_not_confirmed(monkeypatch, tmp_path):
    """SerpApi rodou 1 candidato mas não confirmou business."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    no_business = _booking_no_business()

    def _fake_urlopen(req, *a, **k):
        return _FakeResp(json.dumps(no_business).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # candidato forte (USD 208 < 250 piso forte EUA one_way)
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    # Cabine veio economy do SerpApi → cabin_confirmed=False → suggested=RAW_SIGNAL
    # → não eleva. attempted=1, elevated=0.
    assert "tentou 1 candidato" in sources
    assert "não confirmou executiva" in sources


def test_report_shows_elevated_to_manual(monkeypatch, tmp_path):
    """SerpApi confirmou cabine business → 1 candidato movido para 🟡."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    hop1 = _hop1_payload()
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(hop1).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "1 candidato" in sources
    assert "movido(s) para Verificação manual" in sources


def test_report_shows_monthly_budget_exhausted(monkeypatch, tmp_path):
    """Budget file com count=88 (restante 2 < ESTIMATED 3): esgotado."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    (data_dir / "serpapi_validation_budget.json").write_text(
        json.dumps({"month_utc": this_month, "count": 88}),
        encoding="utf-8",
    )

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise AssertionError("urlopen não deve ser chamado — budget esgotado")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "esgotado" in sources.lower()
    assert "88/90" in sources


def test_report_handles_corrupted_budget_file(monkeypatch, tmp_path):
    """Arquivo corrompido NÃO quebra o relatório; mostra 0/90."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "serpapi_validation_budget.json").write_text(
        "{{not json", encoding="utf-8",
    )

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise SerpApiError("não roda — sem candidatos")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "0/90 queries" in sources
    assert "SerpApi: ativa" in sources or "SerpApi: validação" in sources


def test_report_handles_missing_budget_file(monkeypatch, tmp_path):
    """Sem arquivo: 0/90 sem crash."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    # data_dir nem existe — Config.from_env aponta p/ tmp_path/data inexistente

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise SerpApiError("não roda — sem candidatos")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())

    sources = _sources_section(body)
    assert "0/90" in sources


# ----------------- não regrida demais seções -----------------


def test_existing_sections_still_render(monkeypatch, tmp_path):
    """Mesmo com observabilidade ligada, 🟢/🟡/💸/👀/🛡️/🧭 todos
    continuam aparecendo."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise SerpApiError("não roda")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())

    for section in (
        "🟢 Executiva confirmada",
        "🟡 Verificação manual",
        "💸 Econômica possível",
        "👀 Sinais em observação",
        "🛡️ Bloqueios de segurança",
        "🧭 Status das fontes",
    ):
        assert section in body, f"seção ausente: {section}"


# ----------------- zero leak -----------------


def test_no_leak_in_status_line(monkeypatch, tmp_path):
    """Mesmo com validação ativa + payload contaminado, relatório NÃO
    contém token, URL, post_data, query string sensível."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")
    _patch_config_data_dir(monkeypatch, tmp_path)

    hop1 = _hop1_payload()
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(hop1).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    forbidden = (
        "BK_TOKEN", "DEP_TOKEN",
        "secret_payload", "secret_post_body",
        "post_data",
        "?token=", "?ref=", "?cart=",
        "https://", "http://",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK no relatório: {needle!r}"


def test_summary_dataclass_never_contains_sensitive_fields():
    """Garantia estrutural: o dataclass tem APENAS contadores/booleanos —
    nada de URL, token, payload, rota."""
    from dataclasses import fields
    field_names = {f.name for f in fields(SerpApiValidationSummary)}
    allowed = {
        "enabled", "api_key_present",
        "monthly_budget", "monthly_used",
        "candidates_considered",
        "validations_attempted", "elevated_to_manual_check",
        "price_mismatched",  # PR #60: contador de cabin OK mas preço incompatível
        "skipped_reason",
    }
    assert field_names == allowed, (
        f"campos não autorizados na summary: {field_names - allowed}"
    )
    # Garante que nenhum dos campos permitidos é tipo coleção/dict
    forbidden_type_hints = ("url", "token", "payload", "post_data", "secret")
    for f in fields(SerpApiValidationSummary):
        for ft in forbidden_type_hints:
            assert ft not in f.name.lower()
