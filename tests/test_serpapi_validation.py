"""Testes do PR #52 — integração SerpApi como validador opcional
de candidatos a 🟡 Verificação manual.

Princípio: SerpApi nunca eleva sinal a 🟢. Read-only, opt-in via env.
Defesa de zero leak em qualquer caminho do relatório.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from flight_mapper.booking_actionability import (
    BookingActionability,
    OperationalDecision,
)
from flight_mapper.monitor import MonitorResult
from flight_mapper.serpapi_client import (
    SerpApiClient,
    SerpApiError,
)
from flight_mapper.serpapi_validation import (
    SerpApiValidationCandidate,
    SerpApiValidationConfig,
    SerpApiValidationResult,
    humanize_validation_note,
    validate_cycle_candidates,
    validate_with_serpapi,
)
from flight_mapper.state import PriceStore
from flight_mapper.status import (
    _build_message,
    _select_serpapi_validation_candidates,
)


FIX = Path(__file__).parent / "fixtures"


# ----------------- helpers -----------------


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


def _result() -> MonitorResult:
    return MonitorResult(scanned=2, quotes_received=2, alerts_sent=0, notes=[])


class _FakeResp:
    def __init__(self, b: bytes) -> None:
        self._b = b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._b


def _tp_signal(store, key, origin, dest, usd, brl, trip="one_way", ret=None):
    """Travelpayouts raw signal — sem cabine confirmada."""
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10",
        "return_date": ret,
        "source": "travelpayouts", "currency": "USD",
        "amount": usd, "amount_brl_estimated": brl, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False,
        "deep_link": None,
    }


# ----------------- config -----------------


def test_config_from_env_default_disabled():
    cfg = SerpApiValidationConfig.from_env(env={})
    assert cfg.enabled is False
    assert cfg.max_per_cycle == 1
    assert cfg.api_key is None


def test_config_from_env_enabled_truthy():
    for v in ["true", "True", "TRUE", "yes", "y", "1"]:
        cfg = SerpApiValidationConfig.from_env(
            env={"SERPAPI_VALIDATION_ENABLED": v, "SERPAPI_API_KEY": "K"},
        )
        assert cfg.enabled is True, v
    for v in ["false", "FALSE", "no", "0", "", "abc"]:
        cfg = SerpApiValidationConfig.from_env(
            env={"SERPAPI_VALIDATION_ENABLED": v, "SERPAPI_API_KEY": "K"},
        )
        assert cfg.enabled is False, v


def test_config_max_per_cycle_capped_at_3():
    for raw, expected in [
        ("0", 1), ("1", 1), ("2", 2), ("3", 3),
        ("5", 3), ("99", 3),
        ("abc", 1), ("", 1),
    ]:
        cfg = SerpApiValidationConfig.from_env(
            env={"SERPAPI_VALIDATION_MAX_PER_CYCLE": raw},
        )
        assert cfg.max_per_cycle == expected, raw


# ----------------- validate_cycle_candidates gates -----------------


def test_validate_cycle_disabled_returns_empty(monkeypatch):
    cfg = SerpApiValidationConfig(enabled=False, max_per_cycle=1, api_key="K")
    cand = SerpApiValidationCandidate(
        key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
        outbound_date="2026-09-10", return_date=None,
        travel_class="business", expected_usd=220.0,
    )
    # Se chamasse a rede, o monkeypatch_factory abaixo daria erro
    def _no_factory(api_key):
        raise AssertionError("client_factory NÃO deveria ser chamada")
    assert validate_cycle_candidates([cand], cfg, _no_factory) == {}


def test_validate_cycle_without_api_key_returns_empty():
    cfg = SerpApiValidationConfig(enabled=True, max_per_cycle=1, api_key=None)
    cand = SerpApiValidationCandidate(
        key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
        outbound_date="2026-09-10", return_date=None,
        travel_class="business", expected_usd=220.0,
    )
    def _no_factory(api_key):
        raise AssertionError("client_factory NÃO deveria ser chamada")
    assert validate_cycle_candidates([cand], cfg, _no_factory) == {}


def test_validate_cycle_empty_candidates_returns_empty():
    cfg = SerpApiValidationConfig(enabled=True, max_per_cycle=1, api_key="K")
    assert validate_cycle_candidates([], cfg) == {}


def test_validate_cycle_caps_at_max_per_cycle():
    """3 candidatos, cap=1 → só 1 validação executada."""
    cfg = SerpApiValidationConfig(enabled=True, max_per_cycle=1, api_key="K")
    calls = {"n": 0}
    class _MockClient:
        def __init__(self, *a, **k): pass
        def search_google_flights(self, **kw):
            calls["n"] += 1
            raise SerpApiError("teste")
    cands = [
        SerpApiValidationCandidate(
            key=f"k{i}", origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=200.0 + i,
        ) for i in range(3)
    ]
    out = validate_cycle_candidates(cands, cfg, lambda key: _MockClient())
    assert calls["n"] == 1
    assert len(out) == 1


# ----------------- validate_with_serpapi: one_way -----------------


def _hop1_oneway_with_booking_token() -> dict:
    """Payload one-way: offer business já tem booking_token."""
    return {
        "search_parameters": {
            "engine": "google_flights", "type": "2",
            "travel_class": "business", "currency": "USD",
            "outbound_date": "2026-09-10",
        },
        "best_flights": [{
            "type": "One way", "price": 220,
            "flights": [{
                "airline": "LATAM", "travel_class": "Business",
            }],
            "booking_token": "BK_TOKEN_THAT_MUST_NEVER_LEAK_oneway_xyz_xxx",
        }],
    }


def _booking_options_google_post() -> dict:
    """Booking options: tudo Google POST → google_post_only."""
    return {
        "search_parameters": {"currency": "USD"},
        "booking_options": [{
            "together": {
                "book_with": "BoA",
                "price": 220,
                "booking_request": {
                    "url": "https://www.google.com/travel/clk/redirect?token=secret_payload_x",
                    "method": "POST",
                    "post_data": "secret_post_body=value",
                },
            },
        }],
    }


def test_validate_oneway_google_post_only_suggests_manual_check():
    search_payload = _hop1_oneway_with_booking_token()
    booking_payload = _booking_options_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking_payload).encode())
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=220.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.provider == "serpapi"
    assert res.cabin_confirmed is True
    assert res.price_usd == 220.0
    assert "LATAM" in res.carriers
    assert res.actionability is BookingActionability.GOOGLE_POST_ONLY
    assert res.suggested_decision is OperationalDecision.CONFIRMED_MANUAL_CHECK
    assert "validation_ok" in res.reason_codes


def test_validate_search_failure_returns_silent_result():
    """Qualquer erro no hop 1 → resultado vazio com reason code,
    nenhuma exceção propaga."""
    def _fake_urlopen(req, *a, **k):
        raise SerpApiError("simulated")

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=220.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.cabin_confirmed is False
    assert res.suggested_decision is OperationalDecision.RAW_SIGNAL
    assert "search_failed" in res.reason_codes


def test_validate_no_booking_token_returns_silent_result():
    """Offer business existe mas sem booking_token → reason code claro."""
    payload = {
        "search_parameters": {
            "engine": "google_flights", "type": "2",
            "travel_class": "business",
            "outbound_date": "2026-09-10",
        },
        "best_flights": [{
            "type": "One way", "price": 220,
            "flights": [{"airline": "LATAM", "travel_class": "Business"}],
            # sem booking_token
        }],
    }

    def _fake_urlopen(req, *a, **k):
        return _FakeResp(json.dumps(payload).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=220.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.cabin_confirmed is False
    assert "no_booking_token_in_search" in res.reason_codes


def test_validate_booking_options_failure_keeps_cabin_info():
    """Hop final falha → mantém info de cabin/price; actionability=ERROR,
    sugestão ainda é MANUAL_CHECK (cabine confirmada já é útil)."""
    search_payload = _hop1_oneway_with_booking_token()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            raise SerpApiError("booking_options unavailable")
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=220.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.cabin_confirmed is True
    assert res.actionability is BookingActionability.ERROR
    assert res.suggested_decision is OperationalDecision.CONFIRMED_MANUAL_CHECK
    assert "booking_options_failed" in res.reason_codes


# ----------------- never CONFIRMED_ACTIONABLE -----------------


def test_validation_never_returns_confirmed_actionable():
    """Mesmo com booking_options airline_simple_link, validação SerpApi
    NUNCA sugere CONFIRMED_ACTIONABLE — só MANUAL_CHECK (princípio)."""
    search_payload = _hop1_oneway_with_booking_token()
    airline_payload = {
        "search_parameters": {"currency": "USD"},
        "booking_options": [{
            "together": {
                "book_with": "Latam Airlines",
                "price": 220,
                "booking_request": {
                    "url": "https://www.latam.com/checkout?token=x",
                },
            },
        }],
    }

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(airline_payload).encode())
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business", origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=220.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.actionability is BookingActionability.AIRLINE_SIMPLE_LINK
    # Mesmo com airline_simple_link, sugestão é MANUAL_CHECK
    assert res.suggested_decision is OperationalDecision.CONFIRMED_MANUAL_CHECK


# ----------------- humanize note -----------------


def test_humanize_note_never_contains_url_token_post_data():
    res = SerpApiValidationResult(
        key="k", provider="serpapi",
        cabin_confirmed=True, price_usd=220.0, price_brl=None,
        carriers=("LATAM",),
        actionability=BookingActionability.GOOGLE_POST_ONLY,
        suggested_decision=OperationalDecision.CONFIRMED_MANUAL_CHECK,
        reason_codes=("validation_ok",),
    )
    msg = humanize_validation_note(res)
    assert "cabine business" in msg
    assert "google_post_only" in msg.lower()
    assert "Ação sugerida" in msg
    # defesa
    forbidden = (
        "http://", "https://", "?token=", "?ref=", "?cart=",
        "post_data", "secret_payload", "secret_post_body",
        "BK_TOKEN", "DEP_TOKEN",
    )
    for needle in forbidden:
        assert needle not in msg, f"LEAK: {needle!r}"


# ----------------- candidate selection -----------------


def test_select_candidates_skips_non_business():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # rota economy — não deve virar candidato
        _tp_signal(store, "GRU-MIA-one_way-economy", "GRU", "MIA", 220.0, 1210.0)
        cands = _select_serpapi_validation_candidates(
            store, [("GRU-MIA-one_way-economy", 1210.0)], max_n=3,
        )
    assert cands == []


def test_select_candidates_skips_weak_price():
    """USD 280 (entre forte 250 e boa 350) NÃO qualifica (só "forte")."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # 280 > 250 (forte EUA one_way) → cai em "boa", não "forte"
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 280.0, 1540.0)
        cands = _select_serpapi_validation_candidates(
            store, [("GRU-MIA-one_way-business", 1540.0)], max_n=3,
        )
    assert cands == []


def test_select_candidates_picks_strong_price():
    """USD 220 < 250 → "forte" → vira candidato."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)
        cands = _select_serpapi_validation_candidates(
            store, [("GRU-MIA-one_way-business", 1210.0)], max_n=3,
        )
    assert len(cands) == 1
    assert cands[0].travel_class == "business"
    assert cands[0].origin == "GRU"
    assert cands[0].destination == "MIA"


def test_select_candidates_respects_max_n():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)
        _tp_signal(store, "GRU-LAX-one_way-business", "GRU", "LAX", 230.0, 1265.0)
        _tp_signal(store, "GRU-JFK-one_way-business", "GRU", "JFK", 240.0, 1320.0)
        cands = _select_serpapi_validation_candidates(
            store, [
                ("GRU-MIA-one_way-business", 1210.0),
                ("GRU-LAX-one_way-business", 1265.0),
                ("GRU-JFK-one_way-business", 1320.0),
            ], max_n=1,
        )
    assert len(cands) == 1


# ----------------- integração com _build_message -----------------


def test_build_message_default_off_no_validation_call(monkeypatch):
    """Default DESLIGADO: nenhuma chamada SerpApi, nenhum efeito no relatório."""
    monkeypatch.delenv("SERPAPI_VALIDATION_ENABLED", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise AssertionError("urlopen NÃO deveria ser chamado")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)
        body = _build_message(_result(), store, _now())

    # Rota cai em 💸 Econômica possível (band='forte' → não-ignore)
    # ou em 👀 — qualquer um deles é OK; o que NÃO pode ter é
    # validação SerpApi sendo executada.
    assert "Validado por SerpApi" not in body


def test_build_message_enabled_without_api_key_is_silent(monkeypatch):
    """Env on mas sem SERPAPI_API_KEY: zero chamadas, nenhum quebra."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise AssertionError("urlopen NÃO deveria ser chamado")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)
        body = _build_message(_result(), store, _now())

    assert "Validado por SerpApi" not in body


def _patch_config_data_dir(monkeypatch, tmp_path):
    """Redireciona Config.from_env().data_dir para tmp — evita poluir
    data/* real do repo com `data/serpapi_validation_budget.json`
    durante testes de integração."""
    from flight_mapper.config import Config
    real_from_env = Config.from_env

    @classmethod
    def _fake_from_env(cls, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        cfg.data_dir = tmp_path / "data"
        return cfg

    monkeypatch.setattr(Config, "from_env", _fake_from_env)


def test_build_message_enabled_elevates_to_manual_check(monkeypatch, tmp_path):
    """Cenário real: env ligada + SERPAPI_API_KEY + USD forte → SerpApi
    confirma cabine business + google_post_only → 🟡 com nota."""
    # Sinal forte: USD 220 < 250 (piso forte EUA one_way)
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    monkeypatch.setenv("SERPAPI_VALIDATION_MAX_PER_CYCLE", "1")
    # PR #54: integração com Config p/ budget_path — redireciona
    # data_dir para tmp p/ não escrever em data/* real do repo.
    _patch_config_data_dir(monkeypatch, tmp_path)

    search_payload = _hop1_oneway_with_booking_token()
    booking_payload = _booking_options_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking_payload).encode())
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # Cria UM sinal raw business forte (a rota PRECISA estar no
        # raw_pool após partição — sem cabine confirmada + sem
        # economy_plausible). A rota é -business em key (não
        # -one_way-economy), e o preço é tão baixo que cai em
        # economy_plausible se eco<=brl<biz. Vamos usar -one_way-business
        # com BRL=1210 (entre 1000 econômica e 2500 business): cai em
        # economy_pool. Então SerpApi NÃO valida (só raw_pool).
        # Para forçar raw, vamos usar BRL=2600 (acima do piso business
        # one_way) — então NÃO é economy_plausible (1000<=2600<2500
        # é falso porque 2600>2500), logo cai em raw_pool.
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 2600.0)
        body = _build_message(_result(), store, _now())

    # Validação rodou e elevou para 🟡
    assert "Validado por SerpApi" in body
    assert "cabine business" in body
    assert "google_post_only" in body.lower()
    assert "verificar manualmente" in body.lower()
    # Aparece em 🟡, não em 🟢
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    actionable = body.split("🟢 Executiva confirmada")[1].split("🟡")[0]
    assert "São Paulo → Miami" in manual
    assert "São Paulo → Miami" not in actionable


def test_build_message_validation_error_does_not_crash(monkeypatch, tmp_path):
    """Erro do SerpApi em qualquer hop: relatório continua, sinal
    permanece em observação."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    _patch_config_data_dir(monkeypatch, tmp_path)

    def _fake_urlopen(req, *a, **k):
        raise SerpApiError("simulated outage")

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 2600.0)
        body = _build_message(_result(), store, _now())

    # Não eleva — não tem nota de validação no relatório
    assert "Validado por SerpApi" not in body
    # Mas o relatório continua existindo e estruturado
    assert "📊 Ciclo recente" in body
    assert "🛡️ Bloqueios de segurança" in body


# ----------------- defesa global de leak -----------------


def test_build_message_with_validation_no_leak(monkeypatch, tmp_path):
    """Mesmo com validação rodando: stdout do relatório NUNCA contém
    token bruto, URL completa, query string nem post_data."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    _patch_config_data_dir(monkeypatch, tmp_path)

    search_payload = _hop1_oneway_with_booking_token()
    booking_payload = _booking_options_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking_payload).encode())
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 2600.0)
        body = _build_message(_result(), store, _now())

    forbidden = (
        "BK_TOKEN_THAT_MUST_NEVER_LEAK",
        "DEP_TOKEN_THAT_MUST_NEVER_LEAK",
        "secret_payload",
        "secret_post_body",
        "post_data",
        "?token=",
        "?ref=",
        "?cart=",
        "https://",
        "http://",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK no relatório: {needle!r}"


# ----------------- garantias gerais -----------------


def test_validation_not_consumed_by_motor():
    """serpapi_validation só é consumido por status.py / CLI. Não pode
    aparecer em monitor/providers/detector/notifier/state."""
    for mod in (
        "monitor.py", "providers.py", "notifier.py",
        "detector.py", "state.py",
    ):
        src = (Path("flight_mapper") / mod).read_text(encoding="utf-8")
        assert "serpapi_validation" not in src, mod
        assert "validate_with_serpapi" not in src, mod
