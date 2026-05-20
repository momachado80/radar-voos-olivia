"""Compactação de sinais brutos quando homogêneos.

Sem rede, sem Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message

_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _tp(store, key, o, d, brl, *, trip="one_way", source="travelpayouts"):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": o, "destination": d,
        "departure_date": "2026-09-10",
        "return_date": "2026-09-17" if trip == "round_trip" else None,
        "source": source, "currency": "USD",
        "amount": round(brl / 5.5, 2), "amount_brl_estimated": brl,
        "fx_rate": 5.5, "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False, "deep_link": None,
    }


def _result():
    return MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[])


def _raw_section(body: str) -> str:
    return body.split("📡 Sinais brutos de preço")[1].split("💸")[0]


# 1+2+3+4 — homogêneos: cabeçalho único, sem "Executiva", [trip] por item
def test_homogeneous_sources_compact_with_single_header(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # 3 sinais brutos one_way >= piso business one_way (2500) → raw
    _tp(store, "GRU-FCO-business", "GRU", "FCO", 2612.0, trip="one_way")
    _tp(store, "GRU-CDG-business", "GRU", "CDG", 2618.0, trip="one_way")
    _tp(store, "GRU-SFO-business", "GRU", "SFO", 2750.0, trip="one_way")
    body = _build_message(_result(), store, _NOW)
    raw = _raw_section(body)

    # 1) painel diz que cabine não é confirmada
    assert "Cabine: não confirmada" in raw
    # 2) zero "Executiva"/"Business" em sinais brutos
    assert "Executiva" not in raw
    assert "Business" not in raw
    # 3) "Interpretação:" aparece UMA única vez (compactado no cabeçalho)
    assert raw.count("Interpretação:") == 1
    assert raw.count("Fonte: Travelpayouts") == 1
    assert raw.count("Cabine: não confirmada") == 1
    assert (
        "Interpretação: podem ser econômica promocional ou tarifa "
        "sem classe comprovada." in raw
    )
    # 4) [trip] preservado em cada item
    assert raw.count("[somente ida]") == 3
    # 3 linhas numeradas
    assert "1. São Paulo → Roma (GRU → FCO)" in raw
    assert "2. São Paulo → Paris (GRU → CDG)" in raw
    assert "3. São Paulo → São Francisco (GRU → SFO)" in raw


def test_mixed_trip_still_compact_when_same_source(tmp_path: Path):
    """Mesmo round_trip + one_way da mesma fonte → ainda compacta;
    [trip] no item diferencia."""
    store = PriceStore(tmp_path / "h.json")
    _tp(store, "GRU-FCO-business", "GRU", "FCO", 4500.0, trip="round_trip")
    _tp(store, "GRU-CDG-one_way-business", "GRU", "CDG", 2800.0, trip="one_way")
    body = _build_message(_result(), store, _NOW)
    raw = _raw_section(body)
    assert raw.count("Fonte: Travelpayouts") == 1
    assert raw.count("Interpretação:") == 1
    assert "[ida e volta]" in raw
    assert "[somente ida]" in raw


def test_mixed_sources_falls_back_to_multiline(tmp_path: Path):
    """Fontes diferentes → fallback multilinha por item (mantém Fonte
    correta em cada bloco)."""
    store = PriceStore(tmp_path / "h.json")
    _tp(store, "GRU-FCO-business", "GRU", "FCO", 4500.0, source="travelpayouts")
    _tp(store, "GRU-CDG-business", "GRU", "CDG", 4600.0, source="mock")
    body = _build_message(_result(), store, _NOW)
    raw = _raw_section(body)
    # 2 fontes distintas no rodapé Fonte: por item
    assert "Fonte: Travelpayouts" in raw
    assert "Fonte: Mock" in raw
    # cabeçalho compacto único NÃO foi usado: "Interpretação" aparece >=2 vezes
    assert raw.count("Interpretação:") >= 2


def test_empty_raw_section_keeps_placeholder(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    body = _build_message(_result(), store, _NOW)
    assert "• Nenhum sinal bruto de preço no momento." in body
