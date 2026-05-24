"""Testes do PR #56 — validação SerpApi prioriza candidatos mais
fortes (economy_pool + raw_pool combinados).

Cobre o bug observado em produção em 24/05 21:15 UTC: o relatório
mostrou GRU-MIA US$208 em 💸 Econômica possível, sem nenhuma elevação
para 🟡 Verificação manual — porque a validação SerpApi via
_maybe_validate_with_serpapi recebia só raw_pool, e os sinais mais
fortes caíam em economy_pool antes.
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
from flight_mapper.state import PriceStore
from flight_mapper.status import (
    _build_message,
    _select_serpapi_validation_candidates,
    _validation_priority_key,
)


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


def _result() -> MonitorResult:
    return MonitorResult(scanned=3, quotes_received=3, alerts_sent=0, notes=[])


class _FakeResp:
    def __init__(self, b: bytes) -> None:
        self._b = b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._b


def _tp_strong(store, key, origin, dest, usd, brl, trip="one_way"):
    """Travelpayouts raw signal — sem cabine confirmada."""
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10",
        "return_date": None if trip == "one_way" else "2026-09-17",
        "source": "travelpayouts", "currency": "USD",
        "amount": usd, "amount_brl_estimated": brl, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False,
        "deep_link": None,
    }


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


def _hop1_oneway_with_booking_token() -> dict:
    """Payload one-way: 1 offer business com booking_token."""
    return {
        "search_parameters": {
            "engine": "google_flights", "type": "2",
            "travel_class": "business", "currency": "USD",
            "outbound_date": "2026-09-10",
        },
        "best_flights": [{
            "type": "One way", "price": 208,
            "flights": [{
                "airline": "LATAM", "travel_class": "Business",
            }],
            "booking_token": "BK_TOKEN_THAT_MUST_NEVER_LEAK_oneway_xxx",
        }],
    }


def _booking_options_google_post() -> dict:
    return {
        "search_parameters": {"currency": "USD"},
        "booking_options": [{
            "together": {
                "book_with": "Google",
                "price": 208,
                "booking_request": {
                    "url": "https://www.google.com/travel/clk?token=secret_xxx",
                    "method": "POST",
                    "post_data": "secret_post_body=value",
                },
            },
        }],
    }


# ----------------- _validation_priority_key (PR #56) -----------------


def test_priority_key_orders_muito_forte_before_boa():
    """deal=muito_forte (region_band=forte) ordena antes de deal=boa."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # USD 220 < 250 (piso forte EUA one_way) → deal=muito_forte
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)
        # USD 300 entre 250 e 350 → deal=boa
        _tp_strong(store, "GRU-LAX-one_way-business", "GRU", "LAX", 300.0, 1650.0)

        # Forte vai primeiro mesmo se vier depois na lista
        items = [
            ("GRU-LAX-one_way-business", 1650.0),
            ("GRU-MIA-one_way-business", 1210.0),
        ]
        sorted_items = sorted(
            items, key=lambda it: _validation_priority_key(it, store),
        )
        assert sorted_items[0][0] == "GRU-MIA-one_way-business"
        assert sorted_items[1][0] == "GRU-LAX-one_way-business"


def test_priority_key_orders_by_price_within_same_deal():
    """Dois sinais "muito_forte": menor preço vai primeiro."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # Ambos USD < 250 → deal=muito_forte; sort por preço
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 230.0, 1265.0)
        _tp_strong(store, "GRU-LAX-one_way-business", "GRU", "LAX", 200.0, 1100.0)
        items = [
            ("GRU-MIA-one_way-business", 1265.0),
            ("GRU-LAX-one_way-business", 1100.0),
        ]
        sorted_items = sorted(
            items, key=lambda it: _validation_priority_key(it, store),
        )
        assert sorted_items[0][0] == "GRU-LAX-one_way-business"


# ----------------- _select_serpapi_validation_candidates aceita "boa" -----------------


def test_select_candidates_accepts_both_forte_and_boa_bands():
    """PR #56: agora aceitamos ambas as bands. PR #52 só aceitava forte."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # Forte
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)
        # Boa
        _tp_strong(store, "GRU-LAX-one_way-business", "GRU", "LAX", 300.0, 1650.0)
        pool = [
            ("GRU-MIA-one_way-business", 1210.0),
            ("GRU-LAX-one_way-business", 1650.0),
        ]
        cands = _select_serpapi_validation_candidates(
            store, pool, max_n=5,
        )
    keys = [c.key for c in cands]
    assert "GRU-MIA-one_way-business" in keys
    assert "GRU-LAX-one_way-business" in keys


# ----------------- integração: economy_pool entra na fila de validação -----------------


def test_economy_candidate_is_validated_and_elevated_to_manual_check(
    monkeypatch, tmp_path,
):
    """REPRODUÇÃO DO BUG DO PR #56: candidato GRU-MIA US$208 em
    economy_pool ANTES nunca era validado (só raw_pool entrava).
    Agora deve ser elevado para 🟡 quando SerpApi confirma cabine
    business + booking_options."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    monkeypatch.setenv("SERPAPI_VALIDATION_MAX_PER_CYCLE", "1")
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
        # GRU-MIA US$208 one_way + R$1.144 cai em economy_pool
        # (preço entre piso eco 1000 e piso biz 2500) com region_band="forte"
        _tp_strong(
            store, "GRU-MIA-one_way-business", "GRU", "MIA",
            208.0, 1144.0,
        )
        body = _build_message(_result(), store, _now())

    # Deve aparecer em 🟡, NÃO em 💸
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    economy = body.split("💸 Econômica possível")[1].split("👀")[0]
    assert "São Paulo → Miami" in manual
    assert "São Paulo → Miami" not in economy
    assert "Validado por SerpApi" in body
    assert "cabine business" in body
    assert "google_post_only" in body.lower()


def test_raw_pool_candidate_still_eligible(monkeypatch, tmp_path):
    """Sanity: candidato que cai em raw_pool (não economy_plausible)
    continua sendo validado depois do PR #56."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    monkeypatch.setenv("SERPAPI_VALIDATION_MAX_PER_CYCLE", "1")
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
        # BRL=2600 fica ACIMA do piso business one_way (2500) →
        # NÃO é economy_plausible → cai em raw_pool.
        _tp_strong(
            store, "GRU-MIA-one_way-business", "GRU", "MIA",
            220.0, 2600.0,
        )
        body = _build_message(_result(), store, _now())

    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    observation = body.split("👀 Sinais em observação")[1].split("🛡️")[0]
    assert "São Paulo → Miami" in manual
    assert "São Paulo → Miami" not in observation


def test_cap_one_respected_with_combined_pool(monkeypatch, tmp_path):
    """3 candidatos fortes (1 em economy_pool + 2 em raw_pool), cap=1:
    só o mais forte (sorted por prioridade) é validado."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    monkeypatch.setenv("SERPAPI_VALIDATION_MAX_PER_CYCLE", "1")
    _patch_config_data_dir(monkeypatch, tmp_path)

    search_payload = _hop1_oneway_with_booking_token()
    booking_payload = _booking_options_google_post()
    calls = {"search": 0, "booking": 0}

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            calls["booking"] += 1
            return _FakeResp(json.dumps(booking_payload).encode())
        calls["search"] += 1
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # 3 candidatos com cabine não confirmada
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 210.0, 1155.0)  # forte, economy_pool
        _tp_strong(store, "GRU-LAX-one_way-business", "GRU", "LAX", 230.0, 2700.0)  # forte, raw_pool
        _tp_strong(store, "GRU-JFK-one_way-business", "GRU", "JFK", 240.0, 2800.0)  # forte, raw_pool
        body = _build_message(_result(), store, _now())

    # cap=1 → exatamente 1 chamada de search + 1 booking_options
    assert calls["search"] == 1
    assert calls["booking"] == 1


def test_priority_picks_muito_forte_over_boa(monkeypatch, tmp_path):
    """2 candidatos: 1 muito_forte e 1 boa. cap=1 → muito_forte ganha."""
    monkeypatch.setenv("SERPAPI_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_NO_NETWORK")
    monkeypatch.setenv("SERPAPI_VALIDATION_MAX_PER_CYCLE", "1")
    _patch_config_data_dir(monkeypatch, tmp_path)

    search_payload = _hop1_oneway_with_booking_token()
    booking_payload = _booking_options_google_post()
    seen_destinations: list[str] = []

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            return _FakeResp(json.dumps(booking_payload).encode())
        # Captura destino do URL (rastreável)
        for part in url.split("&"):
            if part.startswith("arrival_id="):
                seen_destinations.append(part.split("=", 1)[1])
        return _FakeResp(json.dumps(search_payload).encode())

    import flight_mapper.serpapi_client as sp
    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # Adicionado em ordem invertida — o sort por prioridade deve
        # ainda escolher MIA primeiro.
        _tp_strong(store, "GRU-LAX-one_way-business", "GRU", "LAX", 300.0, 1650.0)  # boa
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 220.0, 1210.0)  # muito_forte
        body = _build_message(_result(), store, _now())

    # SerpApi recebeu MIA (muito_forte), não LAX (boa)
    assert seen_destinations == ["MIA"]


def test_env_off_no_change(monkeypatch, tmp_path):
    """SerpApi validation desligada: pipeline 100% como antes do PR #56."""
    monkeypatch.delenv("SERPAPI_VALIDATION_ENABLED", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    _patch_config_data_dir(monkeypatch, tmp_path)

    import flight_mapper.serpapi_client as sp
    def _no_urlopen(req, *a, **k):
        raise AssertionError("urlopen NÃO deveria ser chamado")
    monkeypatch.setattr(sp, "urlopen", _no_urlopen)

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    economy = body.split("💸 Econômica possível")[1].split("👀")[0]
    assert "São Paulo → Miami" in economy
    assert "Validado por SerpApi" not in body


def test_no_secret_leak_with_combined_pool(monkeypatch, tmp_path):
    """Mesmo com pool combinado + validação rodando, relatório NUNCA
    contém URL completa, token, query string ou post_data."""
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
        _tp_strong(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())

    forbidden = (
        "BK_TOKEN_THAT_MUST_NEVER_LEAK", "DEP_TOKEN_THAT_MUST_NEVER_LEAK",
        "secret_xxx", "secret_post_body", "post_data",
        "?token=", "?ref=", "?cart=", "https://", "http://",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK no relatório: {needle!r}"
