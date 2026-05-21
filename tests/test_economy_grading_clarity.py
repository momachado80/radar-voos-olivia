"""Testes da semântica clarificada do Deal Intelligence (issues
relatadas no Telegram): banda USD dirige classificação; histórico
fraco não vira desconto enganoso; `ignorar` fora da seção de promoções.

Sem rede, sem Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flight_mapper.deal_intelligence import (
    DEAL_GOOD,
    DEAL_IGNORE,
    DEAL_VERY_STRONG,
    evaluate_deal,
    history_stats,
)
from flight_mapper.monitor import MonitorResult
from flight_mapper.regions import TripType
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message


_NOW = datetime(2026, 5, 21, 17, 0, tzinfo=timezone.utc)


def _seed_signal(
    store, key, dest, brl, *, trip="one_way", prices=None, usd=None,
):
    h = store.get(key)
    for p in (prices or []):
        h.push(round(p, 2))
    h.push(brl)
    h.last_quote = {
        "origin": "GRU", "destination": dest,
        "departure_date": "2026-09-10",
        "return_date": "2026-09-17" if trip == "round_trip" else None,
        "source": "travelpayouts", "currency": "USD",
        "amount": usd if usd is not None else round(brl / 5.5, 2),
        "amount_brl_estimated": brl, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False, "deep_link": None,
    }


def _result():
    return MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[])


# 1. US$ 221 EUA one_way → muito forte, NÃO boa
def test_us221_eua_one_way_is_very_strong_not_good():
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=221.0, brl_amount=1216.0,
        prices=[1216.0] * 10,  # repetitivo — não importa para banda
    )
    assert ev.deal == DEAL_VERY_STRONG, (
        "US$221 está abaixo do piso forte (250). Classificação correta "
        "é 'muito forte'; não pode ser rebaixada para 'boa'."
    )
    assert ev.region_band == "forte"
    assert "piso muito forte" in ev.reason


# 2. US$ 348 Europa one_way → muito forte (< 350 piso forte)
def test_us348_europa_one_way_is_very_strong():
    ev = evaluate_deal(
        destination="CDG", trip_type=TripType.ONE_WAY,
        usd_amount=348.0, brl_amount=1914.0,
        prices=[1914.0] * 10,
    )
    assert ev.deal == DEAL_VERY_STRONG
    assert ev.region == "Europa"
    assert ev.region_band == "forte"


# 3. Item com classificação "ignorar" NÃO entra na seção de promoções
def test_ignore_items_do_not_enter_economy_section(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # US$ 450 EUA one_way (boa floor 350, strong floor 250) → acima de
    # ambos → DEAL_IGNORE. brl=2475 ainda está em [1000, 2500), então
    # _economy_plausible=True; sem o filtro novo, entraria como
    # "ignorar" na seção. Com o filtro, vai para sinais brutos.
    _seed_signal(
        store, "GRU-MIA-one_way-business", "MIA", 2475.0, usd=450.0,
        prices=[2475.0] * 5,
    )
    body = _build_message(_result(), store, _NOW)

    eco = body.split("💸 Possíveis promoções de econômica")[1].split("🛡️")[0]
    raw = body.split("📡 Sinais brutos de preço")[1].split("💸")[0]

    # NUNCA dentro da seção de promoções
    assert "Classificação: ignorar" not in eco
    assert "São Paulo → Miami" not in eco
    # E não há outra promoção legítima → placeholder
    assert "• Nenhum sinal compatível com econômica promocional agora." in eco
    # Aparece em sinais brutos
    assert "São Paulo → Miami" in raw


# 4. Histórico repetitivo NÃO vira leitura enganosa de desconto real
def test_repetitive_history_does_not_show_misleading_zero_discount(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # Cache Travelpayouts repete o MESMO preço várias vezes → mediana =
    # preço atual → desconto = 0%. Sob a nova semântica, marcamos como
    # histórico interno fraco e NÃO publicamos "0% vs mediana".
    _seed_signal(
        store, "GRU-MIA-one_way-business", "MIA", 1216.0, usd=221.0,
        prices=[1216.0] * 12,
    )
    body = _build_message(_result(), store, _NOW)
    eco = body.split("💸 Possíveis promoções de econômica")[1].split("🛡️")[0]

    # Mensagem honesta no lugar do desconto enganoso
    assert "histórico interno ainda fraco" in eco.lower()
    # NÃO mostra "0% vs mediana"
    assert "0% vs mediana" not in eco
    # Histórico marca a baixa variação
    assert "variação muito baixa" in eco
    # E ainda assim a classificação correta aparece (US$221 < 250 forte)
    assert "Classificação: muito forte" in eco


def test_history_stats_marks_baseline_weak_on_repetition():
    s = history_stats([1216.0] * 12)
    assert s.sufficient is True
    assert s.baseline_weak is True
    s2 = history_stats(
        [3000, 3100, 3200, 3300, 3400, 3500, 3600, 3700, 3800, 3900]
    )
    assert s2.sufficient is True
    assert s2.baseline_weak is False


def test_history_stats_marks_baseline_weak_on_insufficient():
    s = history_stats([1500.0, 1600.0, 1700.0])
    assert s.sufficient is False
    assert s.baseline_weak is True


# 5. Sinais brutos continuam aparecendo (regressão)
def test_raw_signals_still_appear_for_above_band_items(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # USD 450 EUA one_way → ignorar → vai para raw section
    _seed_signal(
        store, "GRU-MIA-one_way-business", "MIA", 2475.0, usd=450.0,
    )
    body = _build_message(_result(), store, _NOW)
    raw = body.split("📡 Sinais brutos de preço")[1].split("💸")[0]
    assert "São Paulo → Miami" in raw
    # Sinais brutos mantêm Fonte/Cabine
    assert "Cabine: não confirmada" in raw
    assert "Fonte: Travelpayouts" in raw


# 6. Aviso fixo preservado
def test_economy_warning_still_present(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _seed_signal(
        store, "GRU-FCO-one_way-business", "FCO", 1320.0, usd=240.0,
        prices=[1500.0, 1600.0, 1700.0, 1800.0, 1900.0,
                2000.0, 2100.0, 2200.0, 2300.0, 2400.0],
    )
    body = _build_message(_result(), store, _NOW)
    assert (
        "⚠️ Cabine não confirmada. Classificado como possível econômica, "
        "não executiva." in body
    )
