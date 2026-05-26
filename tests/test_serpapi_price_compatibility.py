"""Testes do PR #60 — compatibilidade de preço SerpApi antes de
elevar candidato a 🟡 Verificação manual.

Bug observado em produção: Travelpayouts mostrou GRU-MIA US$208 sem
cabine confirmada. SerpApi validou cabine business mas em
~USD1137 com google_post_only. Relatório elevou o item p/ 🟡 e disse
"1 candidato validado e movido", o que sugeria que a tarifa US$208
foi confirmada como business — mas SerpApi tinha encontrado OUTRA
executiva, em preço muito diferente.

Solução: elevar para 🟡 só quando cabine business confirmada AND preço
SerpApi compatível com o sinal original (≤ expected×1.25 OR |Δ|≤US$100).
Caso incompatível: candidato fica em 💸/👀 com nota informativa.
"""

from __future__ import annotations

import json
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
from flight_mapper.serpapi_client import SerpApiClient, SerpApiError
from flight_mapper.serpapi_validation import (
    PRICE_COMPATIBILITY_ABS_USD,
    PRICE_COMPATIBILITY_RATIO,
    RC_PRICE_MISMATCH,
    RC_VALIDATION_OK,
    SerpApiValidationCandidate,
    SerpApiValidationResult,
    SerpApiValidationSummary,
    humanize_price_mismatch_note,
    humanize_validation_summary,
    price_is_compatible,
    validate_with_serpapi,
)
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message


# ----------------- price_is_compatible (puro) -----------------


def test_price_compatible_real_world_208_vs_1137():
    """Caso real do bug: US$208 (Travelpayouts) vs ~US$1137 (SerpApi).
    Razão 5.4×, |Δ|=929 — claramente INCOMPATÍVEL."""
    assert price_is_compatible(208.0, 1137.0) is False


def test_price_compatible_1000_vs_1070_within_ratio():
    """Razão 1.07× < 1.25 → COMPATÍVEL."""
    assert price_is_compatible(1000.0, 1070.0) is True


def test_price_compatible_1000_vs_1300_exceeds_both():
    """Razão 1.3× > 1.25 AND |Δ|=300 > 100 → INCOMPATÍVEL."""
    assert price_is_compatible(1000.0, 1300.0) is False


def test_price_compatible_within_abs_delta():
    """Razão 1.5× mas |Δ|=50 < 100 → COMPATÍVEL via fallback abs."""
    # expected=200, sp=250 → ratio 1.25 (=boundary), |Δ|=50
    assert price_is_compatible(200.0, 250.0) is True
    # expected=100, sp=150 → ratio 1.5 > 1.25, mas |Δ|=50 → compatível
    assert price_is_compatible(100.0, 150.0) is True


def test_price_compatible_serpapi_cheaper_than_expected():
    """SerpApi < expected (achou mais barato) → compatível (ratio < 1)."""
    assert price_is_compatible(1000.0, 950.0) is True
    assert price_is_compatible(1000.0, 200.0) is True  # achado raro


def test_price_compatible_with_none_is_false():
    """Conservador: faltando qualquer um dos preços → não compatível."""
    assert price_is_compatible(None, 1000.0) is False
    assert price_is_compatible(1000.0, None) is False
    assert price_is_compatible(None, None) is False


def test_price_compatible_zero_or_invalid_expected():
    """expected_usd <= 0 → não compatível (evita div por zero)."""
    assert price_is_compatible(0, 100.0) is False
    assert price_is_compatible(-10, 100.0) is False


def test_price_compatible_strings_handled_safely():
    """Tipos inválidos → não compatível (defesa)."""
    assert price_is_compatible("abc", 100.0) is False
    assert price_is_compatible(100.0, "xyz") is False


# ----------------- validate_with_serpapi: price field -----------------


class _FakeResp:
    def __init__(self, b: bytes) -> None:
        self._b = b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self) -> bytes:
        return self._b


def _hop1_oneway_with_business_price(price_usd: float) -> dict:
    """Payload one-way: oferta business com preço variável."""
    return {
        "search_parameters": {
            "engine": "google_flights", "type": "2",
            "travel_class": "business", "currency": "USD",
            "outbound_date": "2026-09-10",
        },
        "best_flights": [{
            "type": "One way", "price": price_usd,
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
                "price": 1137,
                "booking_request": {
                    "url": "https://www.google.com/travel/clk?token=secret_xxx",
                    "method": "POST",
                    "post_data": "secret_post_body=value",
                },
            },
        }],
    }


def test_validate_with_compatible_price_elevates(monkeypatch):
    """expected=1000, SerpApi devolve 1070 → compatível → MANUAL_CHECK."""
    search = _hop1_oneway_with_business_price(1070)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business",
            origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=1000.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.cabin_confirmed is True
    assert res.price_compatible is True
    assert res.suggested_decision is OperationalDecision.CONFIRMED_MANUAL_CHECK
    assert RC_VALIDATION_OK in res.reason_codes


def test_validate_with_incompatible_price_does_not_elevate(monkeypatch):
    """Caso real do bug: expected=208, SerpApi devolve 1137 → incompatível.
    suggested_decision deve ser RAW_SIGNAL (NÃO eleva)."""
    search = _hop1_oneway_with_business_price(1137)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business",
            origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=208.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    # Cabine business CONFIRMADA, mas preço diverge muito
    assert res.cabin_confirmed is True
    assert res.price_usd == 1137.0
    assert res.price_compatible is False
    # NÃO eleva — fica em RAW_SIGNAL (status.py keeps in 💸/👀 + nota)
    assert res.suggested_decision is OperationalDecision.RAW_SIGNAL
    assert RC_PRICE_MISMATCH in res.reason_codes


def test_validate_1300_vs_1000_incompatible(monkeypatch):
    """expected=1000, SerpApi=1300 → ratio 1.3, |Δ|=300 → incompatível."""
    search = _hop1_oneway_with_business_price(1300)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business",
            origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=1000.0,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.price_compatible is False
    assert res.suggested_decision is OperationalDecision.RAW_SIGNAL


def test_validate_no_expected_usd_falls_back_to_incompatible(monkeypatch):
    """candidate sem expected_usd → conservador: não-compatível, não eleva."""
    search = _hop1_oneway_with_business_price(1000)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    with patch.object(sp, "urlopen", _fake_urlopen):
        cand = SerpApiValidationCandidate(
            key="GRU-MIA-one_way-business",
            origin="GRU", destination="MIA",
            outbound_date="2026-09-10", return_date=None,
            travel_class="business", expected_usd=None,
        )
        res = validate_with_serpapi(cand, SerpApiClient("K"))

    assert res.cabin_confirmed is True
    assert res.price_compatible is False
    assert res.suggested_decision is OperationalDecision.RAW_SIGNAL


# ----------------- humanize_price_mismatch_note -----------------


def _result_with_price(sp_usd: float | None) -> SerpApiValidationResult:
    return SerpApiValidationResult(
        key="GRU-MIA-business",
        provider="serpapi",
        cabin_confirmed=True,
        price_usd=sp_usd,
        price_brl=None,
        carriers=("LATAM",),
        actionability=BookingActionability.GOOGLE_POST_ONLY,
        suggested_decision=OperationalDecision.RAW_SIGNAL,
        reason_codes=(RC_PRICE_MISMATCH,),
        price_compatible=False,
    )


def test_humanize_price_mismatch_with_both_prices():
    res = _result_with_price(1137.0)
    msg = humanize_price_mismatch_note(res, 208.0)
    assert "USD 1137" in msg
    assert "US$ 208" in msg
    assert "não confirmou a tarifa original" in msg


def test_humanize_price_mismatch_without_expected():
    res = _result_with_price(1137.0)
    msg = humanize_price_mismatch_note(res, None)
    assert "USD 1137" in msg
    assert "não confirmou a tarifa original" in msg


def test_humanize_price_mismatch_without_sp_price():
    res = _result_with_price(None)
    msg = humanize_price_mismatch_note(res, 208.0)
    assert "cabine business" in msg
    assert "preço diferente" in msg


def test_humanize_price_mismatch_never_leaks_url_or_token():
    res = _result_with_price(1137.0)
    msg = humanize_price_mismatch_note(res, 208.0)
    forbidden = (
        "https://", "http://", "?token=", "?ref=", "?cart=",
        "post_data", "secret_payload", "BK_TOKEN", "DEP_TOKEN",
    )
    for needle in forbidden:
        assert needle not in msg, f"LEAK: {needle!r}"


# ----------------- humanize_validation_summary (PR #60 branch) -----------------


def test_humanize_summary_with_price_mismatch():
    """Novo branch: cabine OK mas preço diferente."""
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=True, monthly_budget=90,
        monthly_used=6, candidates_considered=1,
        validations_attempted=1,
        elevated_to_manual_check=0,
        price_mismatched=1,
        skipped_reason=None,
    )
    msg = humanize_validation_summary(s)
    assert "encontrou executiva" in msg
    assert "preço diferente do sinal original" in msg
    assert "não confirmou a tarifa" in msg
    # NÃO usa o vocabulário enganador
    assert "validado" not in msg
    assert "movido(s)" not in msg


def test_humanize_summary_elevated_still_works():
    """Compatível (elevated) NÃO regrida com o novo branch."""
    s = SerpApiValidationSummary(
        enabled=True, api_key_present=True, monthly_budget=90,
        monthly_used=6, candidates_considered=1,
        validations_attempted=1,
        elevated_to_manual_check=1,
        price_mismatched=0,
        skipped_reason=None,
    )
    msg = humanize_validation_summary(s)
    assert "validado" in msg
    assert "movido(s) para Verificação manual" in msg


# ----------------- integração _build_message: caso real do bug -----------------


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


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


def _result() -> MonitorResult:
    return MonitorResult(scanned=1, quotes_received=1, alerts_sent=0, notes=[])


def test_real_bug_208_vs_1137_does_not_elevate(monkeypatch, tmp_path):
    """REPRODUÇÃO DO BUG: Travelpayouts US$208 + SerpApi US$1137 →
    NÃO eleva para 🟡 e mostra nota explícita em 💸."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")

    search = _hop1_oneway_with_business_price(1137)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # GRU-MIA US$208 → cai em economy_pool (forte)
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    # NÃO está em 🟡 — fica em 💸
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    economy = body.split("💸 Econômica possível")[1].split("👀")[0]
    assert "São Paulo → Miami" not in manual
    assert "São Paulo → Miami" in economy
    # Nota informativa no bloco de econômica
    assert "SerpApi encontrou executiva" in economy
    assert "USD 1137" in economy or "USD 1,137" in economy or "1137" in economy
    assert "não confirmou a tarifa original" in economy
    # Resumo executivo NÃO diz "movido"
    leitura = body.split("🧠 Leitura do ciclo")[1].split("\n\n")[0]
    assert "movido(s)" not in leitura
    assert "validado" not in leitura.split("SerpApi")[0]  # antes do prefixo
    # Linha SerpApi no 🧭 menciona price mismatch
    sources = body.split("🧭 Status das fontes")[1].split("\n\n")[0]
    assert "encontrou executiva" in sources
    assert "preço diferente do sinal original" in sources


def test_compatible_price_still_elevates(monkeypatch, tmp_path):
    """Sanity: caso compatível continua sendo elevado (não-regressão).

    Usa expected_usd=220 (cai em "forte" EUA one_way) + SerpApi=240
    (ratio 1.09 → compatível)."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")

    search = _hop1_oneway_with_business_price(240)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # USD 220 < piso forte EUA one_way (250) → forte → candidato elegível.
        # BRL alto força a rota a cair em raw_pool (não economy_plausible).
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 2700.0)
        body = _build_message(_result(), store, _now())

    # Está em 🟡 (elevado — preço SerpApi compatível com Travelpayouts)
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    assert "São Paulo → Miami" in manual
    assert "Validado por SerpApi" in manual


def test_no_alert_reason_no_contradiction_with_manual_check(
    monkeypatch, tmp_path,
):
    """Quando há item em 🟡, a frase final NÃO pode dizer 'sem
    oportunidade' — deve reconhecer a verificação manual."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")

    search = _hop1_oneway_with_business_price(240)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 2700.0)
        body = _build_message(_result(), store, _now())

    # Frase final reconhece a verificação manual
    assert "há verificação manual" in body
    assert "Conferir o bloco 🟡" in body
    # NÃO diz "sem oportunidade" ou "Sem alerta confirmado: nenhuma"
    assert "Sem alerta confirmado: nenhuma" not in body


def test_no_alert_reason_serpapi_mismatch_only(monkeypatch, tmp_path):
    """Quando SerpApi achou business mas em preço incompatível e
    NÃO há outra oportunidade, a frase final explica isso."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")

    search = _hop1_oneway_with_business_price(1137)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    # Frase final explica price mismatch sem promessa indevida
    assert (
        "SerpApi encontrou executiva na rota, mas em preço diferente "
        "do sinal original"
    ) in body
    assert "tarifa original não foi confirmada" in body


def test_full_report_no_leak_price_mismatch(monkeypatch, tmp_path):
    """Mesmo com cenário price-mismatch + nota informativa, NUNCA
    aparece token/URL/post_data no relatório."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE")

    search = _hop1_oneway_with_business_price(1137)
    booking = _booking_google_post()

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking).encode())
        return _FakeResp(json.dumps(search).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    forbidden = (
        "BK_TOKEN", "DEP_TOKEN",
        "post_data", "secret_payload", "secret_post_body",
        "?token=", "?ref=", "?cart=",
        "https://", "http://",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK: {needle!r}"
