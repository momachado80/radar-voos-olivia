"""Testes do PR #58 — resumo executivo + detecção de mudanças no
relatório diário do Telegram.

Cobre:
1. CycleSnapshot dataclass: load/save/empty + schema fechado.
2. compute_changes: novos manual_check, quedas, altas, novas rotas,
   SerpApi delta.
3. format_executive_reading: cenários (nenhuma oportunidade /
   manual_check / executive / sem sinais).
4. derive_main_bottleneck: maior contador.
5. Integração com _build_message: as 2 seções aparecem; ordem correta.
6. Defesas: arquivo ausente / corrompido / schema antigo / I/O fail.
7. Zero leak em qualquer cenário.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from flight_mapper.cycle_summary import (
    MAX_CHANGE_LINES,
    PRICE_CHANGE_THRESHOLD_PCT,
    CycleSnapshot,
    compute_changes,
    derive_main_bottleneck,
    format_executive_reading,
)
from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message


# ----------------- helpers -----------------


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


def _result(**kwargs) -> MonitorResult:
    base = dict(scanned=0, quotes_received=0, alerts_sent=0, notes=[])
    base.update(kwargs)
    return MonitorResult(**base)


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


def _tp_signal(store, key, origin, dest, usd, brl, cabin_confirmed=False):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10", "return_date": None,
        "source": "travelpayouts" if not cabin_confirmed else "kiwi",
        "currency": "USD" if not cabin_confirmed else "BRL",
        "amount": usd if not cabin_confirmed else brl,
        "amount_brl_estimated": brl,
        "fx_rate": 5.5,
        "cabin": "business" if cabin_confirmed else "unknown",
        "cabin_confirmed": cabin_confirmed,
        "trip_type": "one_way",
        "actionable_url": False,
        "deep_link": None,
    }


# ----------------- CycleSnapshot -----------------


def test_snapshot_empty():
    e = CycleSnapshot.empty()
    assert e.snapshot_at == ""
    assert e.latest_prices == {}
    assert e.manual_check_keys == ()
    assert e.serpapi_used == 0
    assert e.serpapi_elevated == 0


def test_snapshot_load_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "nope.json"
        s = CycleSnapshot.load(p)
    assert s == CycleSnapshot.empty()


def test_snapshot_load_corrupted_file_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.json"
        p.write_text("{{{not json", encoding="utf-8")
        s = CycleSnapshot.load(p)
    assert s == CycleSnapshot.empty()


def test_snapshot_load_ignores_unexpected_keys_and_types():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.json"
        p.write_text(
            json.dumps({
                "snapshot_at": "2026-05-25T00:00:00+00:00",
                "latest_prices": {"GRU-MIA": 1144.0, "bad": "not_a_float"},
                "manual_check_keys": ["GRU-LHR", 42, None],
                "serpapi_used": 3,
                "serpapi_elevated": "abc",  # inválido
                "leaked_token": "BK_TOKEN_xxx",
                "leaked_url": "https://example.com/?token=x",
            }),
            encoding="utf-8",
        )
        s = CycleSnapshot.load(p)
    assert s.latest_prices == {"GRU-MIA": 1144.0}
    assert s.manual_check_keys == ("GRU-LHR",)
    assert s.serpapi_used == 3
    assert s.serpapi_elevated == 0  # inválido → 0


def test_snapshot_save_schema_closed():
    """save() escreve APENAS os 5 campos autorizados."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.json"
        s = CycleSnapshot(
            snapshot_at="2026-05-25T00:00:00+00:00",
            latest_prices={"GRU-MIA": 1144.0},
            manual_check_keys=("GRU-LHR",),
            serpapi_used=3,
            serpapi_elevated=1,
        )
        s.save(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw.keys()) == {
        "snapshot_at", "latest_prices",
        "manual_check_keys", "serpapi_used", "serpapi_elevated",
    }


def test_snapshot_save_path_none_is_noop():
    CycleSnapshot.empty().save(None)  # não levanta


def test_snapshot_save_never_contains_sensitive_strings():
    """Mesmo se valores forem contaminados com strings tipo URL/token,
    o save() não vai adicionar campos extras."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.json"
        # latest_prices é dict[str, float] — strings de chave seguras
        s = CycleSnapshot(
            snapshot_at="2026-05-25T00:00:00+00:00",
            latest_prices={"GRU-MIA-business": 1144.0},
            manual_check_keys=("GRU-LHR-business",),
            serpapi_used=3,
            serpapi_elevated=0,
        )
        s.save(p)
        raw_text = p.read_text(encoding="utf-8")
    forbidden = (
        "BK_TOKEN", "DEP_TOKEN", "post_data",
        "secret_payload", "?token=", "?ref=", "?cart=",
        "https://", "http://",
    )
    for needle in forbidden:
        assert needle not in raw_text


# ----------------- compute_changes -----------------


def _snap(**kwargs):
    """Snapshot builder com defaults."""
    base = dict(
        snapshot_at="2026-05-25T00:00:00+00:00",
        latest_prices={},
        manual_check_keys=(),
        serpapi_used=0,
        serpapi_elevated=0,
    )
    base.update(kwargs)
    return CycleSnapshot(**base)


def test_compute_changes_first_cycle_returns_empty():
    """prev.snapshot_at vazio = primeiro ciclo registrado → sem
    base de comparação → lista vazia."""
    prev = CycleSnapshot.empty()
    curr = _snap(latest_prices={"GRU-MIA": 1144.0})
    assert compute_changes(prev, curr) == []


def test_compute_changes_no_change_returns_empty():
    prev = _snap(latest_prices={"GRU-MIA": 1144.0})
    curr = _snap(latest_prices={"GRU-MIA": 1144.0})
    assert compute_changes(prev, curr) == []


def test_compute_changes_below_threshold_ignored():
    """Mudança < 5% é ruído — ignora."""
    prev = _snap(latest_prices={"GRU-MIA": 1000.0})
    curr = _snap(latest_prices={"GRU-MIA": 1040.0})  # +4%
    assert compute_changes(prev, curr) == []


def test_compute_changes_price_drop():
    prev = _snap(latest_prices={"GRU-MIA-business": 1500.0})
    curr = _snap(latest_prices={"GRU-MIA-business": 1100.0})  # -27%
    changes = compute_changes(prev, curr)
    assert len(changes) == 1
    assert "GRU → MIA" in changes[0]
    assert "caiu" in changes[0]
    assert "↘️" in changes[0]


def test_compute_changes_price_rise():
    prev = _snap(latest_prices={"GRU-MIA-business": 1000.0})
    curr = _snap(latest_prices={"GRU-MIA-business": 1500.0})  # +50%
    changes = compute_changes(prev, curr)
    assert len(changes) == 1
    assert "subiu" in changes[0]
    assert "↗️" in changes[0]


def test_compute_changes_new_manual_check_first():
    """Prioridade #1: novo candidato em manual_check vem antes de
    preços."""
    prev = _snap(
        latest_prices={"GRU-MIA-business": 1500.0},
        manual_check_keys=(),
    )
    curr = _snap(
        latest_prices={"GRU-MIA-business": 800.0},  # -47%
        manual_check_keys=("GRU-LHR-business",),
    )
    changes = compute_changes(prev, curr)
    assert "subiu para Verificação manual" in changes[0]
    assert "GRU → LHR" in changes[0]


def test_compute_changes_caps_at_max_lines():
    prev = _snap(latest_prices={f"GRU-K{i}-business": 1000.0 for i in range(10)})
    curr = _snap(
        latest_prices={f"GRU-K{i}-business": 700.0 for i in range(10)},  # -30%
    )
    changes = compute_changes(prev, curr)
    assert len(changes) <= MAX_CHANGE_LINES


def test_compute_changes_serpapi_elevated():
    prev = _snap(serpapi_used=3, serpapi_elevated=0)
    curr = _snap(
        snapshot_at="2026-05-25T01:00:00+00:00",
        latest_prices={},
        serpapi_used=6, serpapi_elevated=1,
    )
    changes = compute_changes(prev, curr)
    assert any("SerpApi confirmou 1 candidato" in c for c in changes)


def test_compute_changes_serpapi_attempted_no_confirm():
    prev = _snap(serpapi_used=3, serpapi_elevated=0)
    curr = _snap(
        snapshot_at="2026-05-25T01:00:00+00:00",
        latest_prices={},
        serpapi_used=6, serpapi_elevated=0,
    )
    changes = compute_changes(prev, curr)
    assert any(
        "SerpApi gastou 3 queries" in c and "não confirmou executiva" in c
        for c in changes
    )


def test_compute_changes_new_route():
    prev = _snap(latest_prices={"GRU-MIA-business": 1000.0})
    curr = _snap(
        latest_prices={
            "GRU-MIA-business": 1000.0,
            "GRU-LAX-business": 800.0,
        },
    )
    changes = compute_changes(prev, curr)
    assert any("nova cotação" in c and "GRU → LAX" in c for c in changes)


# ----------------- format_executive_reading -----------------


def test_reading_with_actionable():
    msg = format_executive_reading(
        actionable_count=1, manual_check_count=0,
        best_signal_label=None, best_signal_has_cabin=False,
        serpapi_one_liner="SerpApi: ativa; 6/90 queries.",
        main_bottleneck=None,
    )
    assert "1 oportunidade" in msg
    assert "executiva acionável" in msg
    assert "bloco 🟢" in msg


def test_reading_with_manual_check_only():
    msg = format_executive_reading(
        actionable_count=0, manual_check_count=2,
        best_signal_label=None, best_signal_has_cabin=False,
        serpapi_one_liner="",
        main_bottleneck=None,
    )
    assert "2 candidatos" in msg
    assert "Verificação manual" in msg
    assert "bloco 🟡" in msg


def test_reading_with_no_opportunity_shows_best_signal():
    msg = format_executive_reading(
        actionable_count=0, manual_check_count=0,
        best_signal_label="GRU → MIA por US$ 208",
        best_signal_has_cabin=False,
        serpapi_one_liner="SerpApi: ativa; 3/90 queries.",
        main_bottleneck="12 sinais sem cabine confirmada",
    )
    assert "Não há executiva acionável agora" in msg
    assert "GRU → MIA por US$ 208" in msg
    assert "sem cabine confirmada" in msg
    assert "SerpApi: ativa; 3/90" in msg
    assert "Gargalo principal: 12 sinais sem cabine confirmada" in msg


def test_reading_with_no_signals_at_all():
    msg = format_executive_reading(
        actionable_count=0, manual_check_count=0,
        best_signal_label=None, best_signal_has_cabin=False,
        serpapi_one_liner="SerpApi: validação desativada.",
        main_bottleneck=None,
    )
    assert "Sem sinais relevantes" in msg
    assert "SerpApi: validação desativada" in msg


# ----------------- derive_main_bottleneck -----------------


def test_bottleneck_all_zero_returns_none():
    assert derive_main_bottleneck(
        cabin_blocked=0, suspicious_blocked=0,
        currency_blocked=0, non_actionable_links_skipped=0,
    ) is None


def test_bottleneck_picks_highest_counter():
    msg = derive_main_bottleneck(
        cabin_blocked=12, suspicious_blocked=3,
        currency_blocked=1, non_actionable_links_skipped=0,
    )
    assert msg == "12 sinais sem cabine confirmada"


def test_bottleneck_with_suspicious_winning():
    msg = derive_main_bottleneck(
        cabin_blocked=2, suspicious_blocked=15,
        currency_blocked=1, non_actionable_links_skipped=0,
    )
    assert "15" in msg
    assert "suspeitos" in msg


# ----------------- integração: _build_message -----------------


def test_report_has_both_overview_sections(monkeypatch, tmp_path):
    monkeypatch.delenv("SERPAPI_VALIDATION_ENABLED", raising=False)
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())
    assert "🧠 Leitura do ciclo" in body
    assert "📈 Mudanças desde o último ciclo" in body


def test_report_overview_appears_before_section_blocks(monkeypatch, tmp_path):
    """🧠 + 📈 devem aparecer ANTES de 📊 e dos blocos decisórios."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())
    pos_leitura = body.find("🧠 Leitura do ciclo")
    pos_mudancas = body.find("📈 Mudanças desde o último ciclo")
    pos_ciclo = body.find("📊 Ciclo recente")
    pos_executiva = body.find("🟢 Executiva confirmada")
    assert pos_leitura < pos_mudancas < pos_ciclo < pos_executiva


def test_report_no_opportunity_shows_best_raw(monkeypatch, tmp_path):
    """Sem executiva confirmada, leitura cita o melhor sinal bruto."""
    monkeypatch.delenv("SERPAPI_VALIDATION_ENABLED", raising=False)
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        body = _build_message(_result(), store, _now())
    leitura = body.split("🧠 Leitura do ciclo")[1].split("\n\n")[0]
    assert "Não há executiva acionável" in leitura
    assert "GRU → MIA" in leitura
    assert "US$ 208" in leitura
    assert "sem cabine confirmada" in leitura


def test_report_first_cycle_says_no_change(monkeypatch, tmp_path):
    """Sem snapshot prévio → 📈 diz 'Sem mudança relevante'."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())
    mudancas = body.split("📈 Mudanças desde o último ciclo")[1].split("\n\n")[0]
    assert "Sem mudança relevante desde o último ciclo" in mudancas


def test_report_detects_price_drop_between_cycles(monkeypatch, tmp_path):
    """Roda 2 ciclos: 1º com preço alto, 2º com preço baixo. 2º
    relatório deve listar a queda."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # ciclo 1: preço alto
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 350.0, 1925.0)
        _build_message(_result(), store, _now())
        # ciclo 2: preço caiu para US$ 208 / R$ 1144
        store.get("GRU-MIA-one_way-business").push(1144.0)
        store.get("GRU-MIA-one_way-business").last_quote["amount"] = 208.0
        store.get("GRU-MIA-one_way-business").last_quote["amount_brl_estimated"] = 1144.0
        body2 = _build_message(_result(), store, _now())
    mudancas = body2.split("📈 Mudanças desde o último ciclo")[1].split("\n\n")[0]
    assert "caiu" in mudancas
    assert "GRU → MIA" in mudancas


def test_report_overview_no_leak(monkeypatch, tmp_path):
    """Mesmo com diversos sinais + snapshot persistido, relatório NÃO
    contém token, URL, post_data, query string sensível."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _tp_signal(store, "GRU-MIA-one_way-business", "GRU", "MIA", 208.0, 1144.0)
        _tp_signal(store, "GRU-LHR-business", "GRU", "LHR", 800.0, 4400.0)
        body = _build_message(_result(), store, _now())
        # Roda 2º ciclo p/ exercitar compute_changes
        body2 = _build_message(_result(), store, _now())
    forbidden = (
        "BK_TOKEN", "DEP_TOKEN",
        "secret_payload", "post_data",
        "?token=", "?ref=", "?cart=",
        "https://", "http://",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK ciclo 1: {needle!r}"
        assert needle not in body2, f"LEAK ciclo 2: {needle!r}"


def test_report_existing_sections_still_render(monkeypatch, tmp_path):
    """Não-regressão: PR #58 não remove nenhuma seção existente."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())
    for section in (
        "🧠 Leitura do ciclo",
        "📈 Mudanças desde o último ciclo",
        "📊 Ciclo recente",
        "🟢 Executiva confirmada",
        "🟡 Verificação manual",
        "💸 Econômica possível",
        "👀 Sinais em observação",
        "🛡️ Bloqueios de segurança",
        "🧭 Status das fontes",
    ):
        assert section in body, f"seção ausente: {section}"


def test_report_corrupted_snapshot_does_not_break(monkeypatch, tmp_path):
    """Arquivo cycle_snapshot.json corrompido → 📈 cai pra 'Sem mudança
    relevante', relatório segue."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "cycle_snapshot.json").write_text(
        "{{not json", encoding="utf-8",
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(_result(), store, _now())
    assert "Sem mudança relevante" in body


def test_report_with_bottleneck_mentions_it(monkeypatch, tmp_path):
    """Quando há contador de bloqueio > 0, a leitura cita o gargalo."""
    _patch_config_data_dir(monkeypatch, tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build_message(
            _result(cabin_blocked=12, suspicious_blocked=3),
            store, _now(),
        )
    leitura = body.split("🧠 Leitura do ciclo")[1].split("\n\n")[0]
    assert "Gargalo principal" in leitura
    assert "12" in leitura
