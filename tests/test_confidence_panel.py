"""Painel de confiança: seções novas + CLI explain-status.

Sem rede, sem Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flight_mapper.__main__ import main
from flight_mapper.config import Config
from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message, explain_status

_NOW = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)


def _tp(store, key, o, d, usd, brl, trip="one_way"):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": o, "destination": d,
        "departure_date": "2026-09-10",
        "return_date": "2026-09-17" if trip == "round_trip" else None,
        "source": "travelpayouts", "currency": "USD",
        "amount": usd, "amount_brl_estimated": brl, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False, "deep_link": None,
    }


def _safe_config(monkeypatch, tmp_path):
    fake = Config(
        telegram_bot_token=None, telegram_chat_id=None,
        travelpayouts_token=None, kiwi_api_key=None, data_dir=tmp_path,
    )
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: fake))
    return fake


# ---- security counters + no-alert reason ----

def test_security_block_shows_counters(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _tp(store, "GRU-FCO-business", "GRU", "FCO", 900.0, 4950.0, "round_trip")
    result = MonitorResult(
        scanned=20, quotes_received=10, alerts_sent=0, notes=[],
        cabin_blocked=12, suspicious_blocked=3, currency_blocked=1,
        non_actionable_links_skipped=2,
    )
    body = _build_message(result, store, _NOW)

    assert "🛡️ Bloqueios de segurança" in body
    sec = body.split("🛡️ Bloqueios de segurança")[1].split("🧭")[0]
    assert "cabine não confirmada: 12" in sec
    assert "preço economicamente suspeito: 3" in sec
    assert "câmbio ausente/ inválido: 1" in sec
    assert "link comercial indisponível: 2" in sec
    # contadores no resumo do ciclo
    assert "• Bloqueados por cabine: 12" in body
    assert "• Bloqueados por preço suspeito: 3" in body
    # motivo de ausência de alerta explicado
    assert "Sem alerta confirmado:" in body
    assert "nenhuma cabine confirmada (12 bloqueada(s))" in body


def test_security_block_elegant_fallback_when_zero(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _tp(store, "GRU-FCO-business", "GRU", "FCO", 900.0, 4950.0, "round_trip")
    result = MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[])
    body = _build_message(result, store, _NOW)
    assert "• Nenhum bloqueio de segurança neste ciclo." in body


def test_economy_section_separates_from_raw(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # one_way R$1.166 → econômica plausível (1000 ≤ 1166 < 2500)
    _tp(store, "GRU-MIA-one_way-business", "GRU", "MIA", 212.0, 1166.0, "one_way")
    # round_trip R$ 9.000 → acima do piso business (4000) → sinal bruto puro
    _tp(store, "GRU-CDG-business", "GRU", "CDG", 1636.0, 9000.0, "round_trip")
    result = MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[])
    body = _build_message(result, store, _NOW)

    eco = body.split("💸 Econômica possível")[1].split("👀")[0]
    raw = body.split("👀 Sinais em observação")[1].split("🛡️")[0]
    assert "São Paulo → Miami (GRU → MIA)" in eco
    assert "São Paulo → Paris (GRU → CDG)" in raw
    # sem linguagem enganosa em nenhuma das duas
    for section in (eco, raw):
        for banned in ("Executiva", "Business", "excelente", "bom", "Score"):
            assert banned not in section


# ---- explain_status (função pura) ----

def test_explain_status_text(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _tp(store, "GRU-MIA-one_way-business", "GRU", "MIA", 221.0, 1216.0)
    txt = explain_status(store, _NOW)
    assert "🧭 Resumo das fontes" in txt
    assert "❓ Por que não há alerta confirmado" in txt
    assert "Nenhuma rota tem cotação com cabine confirmada" in txt
    assert "📡 Melhores sinais brutos" in txt
    assert "São Paulo → Miami (GRU → MIA)" in txt
    assert "🚧 Próximos gargalos para alerta confirmado" in txt
    assert "KIWI_API_KEY" in txt
    # nada de linguagem enganosa
    assert "Executiva" not in txt
    assert "oportunidade confirmada" not in txt


# ---- CLI explain-status (read-only, sem rede/Telegram) ----

def test_cli_explain_status(tmp_path, monkeypatch, capsys):
    _safe_config(monkeypatch, tmp_path)
    cfg = Config.from_env()
    store = PriceStore(cfg.history_path)
    _tp(store, "GRU-MIA-one_way-business", "GRU", "MIA", 221.0, 1216.0)
    store.save()

    rc = main(["explain-status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "🧭 Resumo das fontes" in out
    assert "🚧 Próximos gargalos para alerta confirmado" in out


def test_cli_explain_status_empty_history(tmp_path, monkeypatch, capsys):
    _safe_config(monkeypatch, tmp_path)
    rc = main(["explain-status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() != ""  # mensagem de histórico vazio, sem crash
