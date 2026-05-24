"""Testes do Deal Intelligence (PR #35).

Funções puras, sem rede, sem Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flight_mapper.__main__ import main
from flight_mapper.config import Config
from flight_mapper.deal_intelligence import (
    DEAL_GOOD,
    DEAL_IGNORE,
    DEAL_VERY_STRONG,
    ECONOMY_BANDS_USD,
    evaluate_deal,
    history_stats,
    region_for_destination,
    usd_band,
)
from flight_mapper.monitor import MonitorResult
from flight_mapper.regions import TripType
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message, explain_deals


_NOW = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)


# ---------- mapeamento de região ----------

def test_region_for_destination():
    assert region_for_destination("JFK") == "EUA"
    assert region_for_destination("MIA") == "EUA"
    assert region_for_destination("LHR") == "Europa"
    assert region_for_destination("DXB") == "Ásia"
    assert region_for_destination("DOH") == "Ásia"
    assert region_for_destination("XYZ") is None


# ---------- bandas USD ----------

def test_economy_bands_match_spec():
    assert ECONOMY_BANDS_USD[("EUA", TripType.ONE_WAY)] == (250.0, 350.0)
    assert ECONOMY_BANDS_USD[("Europa", TripType.ONE_WAY)] == (350.0, 500.0)
    assert ECONOMY_BANDS_USD[("Ásia", TripType.ONE_WAY)] == (500.0, 700.0)
    assert ECONOMY_BANDS_USD[("EUA", TripType.ROUND_TRIP)] == (450.0, 650.0)
    assert ECONOMY_BANDS_USD[("Europa", TripType.ROUND_TRIP)] == (600.0, 850.0)
    assert ECONOMY_BANDS_USD[("Ásia", TripType.ROUND_TRIP)] == (800.0, 1100.0)


def test_usd_band_eua_one_way():
    assert usd_band(220.0, "EUA", TripType.ONE_WAY) == "forte"
    assert usd_band(300.0, "EUA", TripType.ONE_WAY) == "boa"
    assert usd_band(400.0, "EUA", TripType.ONE_WAY) is None


def test_usd_band_europa_round_trip():
    assert usd_band(550.0, "Europa", TripType.ROUND_TRIP) == "forte"
    assert usd_band(800.0, "Europa", TripType.ROUND_TRIP) == "boa"
    assert usd_band(900.0, "Europa", TripType.ROUND_TRIP) is None


def test_usd_band_unknown_region_or_amount():
    assert usd_band(100.0, None, TripType.ONE_WAY) is None
    assert usd_band(None, "EUA", TripType.ONE_WAY) is None


# ---------- estatísticas de histórico ----------

def test_history_stats_empty():
    s = history_stats([])
    assert s.n == 0
    assert s.sufficient is False
    assert s.median_brl is None
    assert s.p25_brl is None


def test_history_stats_insufficient():
    s = history_stats([1000.0, 1100.0, 1200.0])
    assert s.n == 3
    assert s.sufficient is False
    assert s.median_brl == 1100.0


def test_history_stats_sufficient_and_recent():
    prices = [3000.0, 3100.0, 3200.0, 3300.0, 3400.0,
              3500.0, 3600.0, 3700.0, 3800.0, 3900.0]
    s = history_stats(prices)
    assert s.n == 10
    assert s.sufficient is True
    assert s.median_brl == 3450.0
    # p25 interpolado em sorted: posição 0.25*9=2.25 → 3200 + 0.25*(3300-3200)=3225
    assert s.p25_brl == 3225.0
    # mínimo recente = janela [-10:] = a lista toda → 3000
    assert s.min_recent_brl == 3000.0


# ---------- evaluate_deal: cenários por banda × histórico ----------

def test_evaluate_strong_band_with_strong_discount():
    # EUA one_way: forte < 250. Histórico mediana ~3200, atual 2200 → 31%
    prices = [3000, 3100, 3200, 3300, 3400, 3500, 3600, 3700, 3800, 3900]
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=200.0, brl_amount=2200.0, prices=prices,
    )
    assert ev.deal == DEAL_VERY_STRONG
    assert ev.region == "EUA"
    assert ev.region_band == "forte"
    assert ev.discount_pct is not None and ev.discount_pct >= 0.25


def test_evaluate_strong_band_no_history_promotes_to_very_strong():
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=180.0, brl_amount=990.0, prices=[1000.0, 1100.0],
    )
    assert ev.deal == DEAL_VERY_STRONG
    assert ev.region_band == "forte"
    assert ev.history.sufficient is False
    assert ev.discount_pct is None


def test_evaluate_strong_band_with_small_discount_still_very_strong():
    # USD forte (200 < 250) com mediana próxima do preço atual.
    # Banda USD dirige a classificação: NÃO rebaixa por desconto baixo.
    prices = [2200, 2250, 2300, 2350, 2400, 2450, 2500, 2550, 2600, 2650]
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=200.0, brl_amount=2200.0, prices=prices,
    )
    assert ev.deal == DEAL_VERY_STRONG
    # motivo coerente: só fala do piso, não menciona desconto p/ rebaixar
    assert "piso muito forte" in ev.reason
    assert "insuficiente" not in ev.reason


def test_evaluate_good_band_with_good_discount():
    # EUA one_way: boa entre 250-350; histórico cai 12%
    prices = [3500] * 10  # repetitivo → baseline_weak True → discount None
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=300.0, brl_amount=3080.0, prices=prices,
    )
    assert ev.deal == DEAL_GOOD


def test_evaluate_good_band_always_good_no_downgrade():
    # USD na faixa boa (250-350); histórico variado e sem desconto.
    # Sob a nova semântica, banda boa → DEAL_GOOD (não existe mais
    # downgrade p/ "observar"). Histórico variado garante baseline OK.
    prices = [3000, 3000, 3010, 3020, 3010, 3000, 2990, 3010, 3020, 3000]
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=300.0, brl_amount=2980.0, prices=prices,
    )
    assert ev.deal == DEAL_GOOD


def test_evaluate_above_bands_is_ignore():
    ev = evaluate_deal(
        destination="MIA", trip_type=TripType.ONE_WAY,
        usd_amount=400.0, brl_amount=2200.0, prices=[],
    )
    assert ev.deal == DEAL_IGNORE


def test_evaluate_unknown_destination_is_ignore():
    ev = evaluate_deal(
        destination="XYZ", trip_type=TripType.ONE_WAY,
        usd_amount=100.0, brl_amount=550.0, prices=[],
    )
    assert ev.deal == DEAL_IGNORE
    assert ev.region is None


# ---------- integração no relatório (status) ----------

def _seed(store, key, dest, brl, *, trip="one_way", source="travelpayouts"):
    h = store.get(key)
    # popular histórico p/ habilitar mediana confiável
    for p in [brl * 1.4, brl * 1.45, brl * 1.5, brl * 1.5, brl * 1.5,
              brl * 1.55, brl * 1.6, brl * 1.6, brl * 1.65, brl * 1.7]:
        h.push(round(p, 2))
    h.push(brl)
    h.last_quote = {
        "origin": "GRU", "destination": dest,
        "departure_date": "2026-09-10",
        "return_date": "2026-09-17" if trip == "round_trip" else None,
        "source": source, "currency": "USD",
        "amount": round(brl / 5.5, 2), "amount_brl_estimated": brl,
        "fx_rate": 5.5, "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False, "deep_link": None,
    }


def test_economy_block_shows_classification_history_and_discount(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # GRU-FCO one_way: Europa one_way bandas 350/500 USD.
    # brl=1320 (≈USD 240 < 350 forte). Histórico (BRL ~1800-2200) → desconto >25%
    _seed(store, "GRU-FCO-one_way-business", "FCO", 1320.0)
    body = _build_message(
        MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[]),
        store, _NOW,
    )
    eco = body.split("💸 Econômica possível")[1].split("👀")[0]

    assert "São Paulo → Roma (GRU → FCO)" in eco
    assert "Classificação: muito forte (Europa/one_way)" in eco
    assert "Histórico interno: mediana R$" in eco
    assert "Desconto estimado:" in eco
    assert "% vs mediana" in eco
    assert "Interpretação: preço compatível com econômica promocional" in eco
    # aviso fixo do bloco
    assert "⚠️ Cabine não confirmada. Classificado como possível econômica, não executiva." in body
    # sem linguagem enganosa
    for banned in ("Executiva", "Business", "excelente", "bom", "Score"):
        assert banned not in eco


def test_economy_block_history_insufficient_shows_so(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-MIA-one_way-business")
    # poucos preços → histórico insuficiente
    h.push(1300.0)
    h.last_quote = {
        "origin": "GRU", "destination": "MIA",
        "departure_date": "2026-09-10", "return_date": None,
        "source": "travelpayouts", "currency": "USD",
        "amount": 236.0, "amount_brl_estimated": 1300.0, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": "one_way", "actionable_url": False, "deep_link": None,
    }
    body = _build_message(
        MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[]),
        store, _NOW,
    )
    eco = body.split("💸 Econômica possível")[1].split("👀")[0]
    assert "Histórico interno: insuficiente" in eco
    assert "Desconto: histórico insuficiente" in eco
    # Banda USD forte sem histórico ainda promove a "muito forte"
    assert "Classificação: muito forte" in eco


# ---------- explain_deals (função e CLI) ----------

def test_explain_deals_text(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _seed(store, "GRU-FCO-one_way-business", "FCO", 1320.0)
    txt = explain_deals(store)
    assert "💸 Top sinais de econômica" in txt
    assert "São Paulo → Roma" in txt
    assert "muito forte" in txt
    assert "Desconto:" in txt
    assert "Motivo:" in txt
    assert "Cabine não confirmada" in txt
    assert "Executiva" not in txt


def test_explain_deals_empty(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # entrada legada sem moeda comprovada → não classificável
    store.get("GRU-MIA-business").push(1900.0)
    txt = explain_deals(store)
    assert "Nenhum sinal com moeda comprovada" in txt


def _safe_config(monkeypatch, tmp_path):
    fake = Config(
        telegram_bot_token=None, telegram_chat_id=None,
        travelpayouts_token=None, kiwi_api_key=None, data_dir=tmp_path,
    )
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: fake))


def test_cli_explain_deals(tmp_path, monkeypatch, capsys):
    _safe_config(monkeypatch, tmp_path)
    cfg = Config.from_env()
    store = PriceStore(cfg.history_path)
    _seed(store, "GRU-FCO-one_way-business", "FCO", 1320.0)
    store.save()

    rc = main(["explain-deals"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "💸 Top sinais de econômica" in out
    assert "muito forte" in out
    assert "Cabine não confirmada" in out


def test_cli_explain_deals_empty_history(tmp_path, monkeypatch, capsys):
    _safe_config(monkeypatch, tmp_path)
    rc = main(["explain-deals"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() != ""  # mensagem de histórico vazio
