"""Tests para o Calibration & Diagnostics Pack."""

from __future__ import annotations

import csv
import urllib.request
from pathlib import Path

import pytest

from flight_mapper import diagnostics
from flight_mapper.__main__ import main
from flight_mapper.airports import build_search_url
from flight_mapper.config import Config
from flight_mapper.state import PriceStore


# ---------- percentile ----------

def test_percentile_empty_returns_none():
    assert diagnostics.percentile([], 10) is None
    assert diagnostics.percentile([], 50) is None


def test_percentile_single_value():
    assert diagnostics.percentile([42.0], 10) == 42.0
    assert diagnostics.percentile([42.0], 50) == 42.0
    assert diagnostics.percentile([42.0], 90) == 42.0


def test_percentile_multiple_values():
    """Percentil por índice arredondado (round-half-even do Python)."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    # p10 → idx = round(0.10 * 9) = 1 → 20.0
    assert diagnostics.percentile(values, 10) == 20.0
    # p50 → idx = round(0.50 * 9) = round(4.5) = 4 (round half to even) → 50.0
    assert diagnostics.percentile(values, 50) == 50.0
    # p25 → idx = round(0.25 * 9) = round(2.25) = 2 → 30.0
    assert diagnostics.percentile(values, 25) == 30.0
    # p90 → idx = round(0.90 * 9) = round(8.1) = 8 → 90.0
    assert diagnostics.percentile(values, 90) == 90.0


def test_percentile_invalid_p_raises():
    with pytest.raises(ValueError):
        diagnostics.percentile([1.0, 2.0], -10)
    with pytest.raises(ValueError):
        diagnostics.percentile([1.0, 2.0], 150)


# ---------- stats_for ----------

def test_stats_for_route_with_history(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-LHR-business")
    for p in [1900.0, 1850.0, 1800.0, 1850.0, 1900.0]:
        history.push(p)
    stats = diagnostics.stats_for("GRU-LHR-business", history)
    assert stats.samples == 5
    assert stats.latest == 1900.0
    assert stats.min_price == 1800.0
    assert stats.avg == pytest.approx(1860.0)
    assert stats.p10 == 1800.0
    assert stats.route_label == "São Paulo → Londres (GRU → LHR)"
    assert stats.is_hot is True
    assert stats.last_quote_present is False
    assert stats.last_quote_actionable is False
    assert stats.watchlist_label == "Europa Executiva"


def test_stats_for_route_with_actionable_last_quote(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-MIA-business")
    history.push(1207.0)
    history.last_quote = {
        "price_brl": 1207.0,
        "origin": "GRU",
        "destination": "MIA",
        "departure_date": "2026-06-15",
        "return_date": "2026-06-22",
        "source": "travelpayouts",
        "deep_link": build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22"),
        "detected_at": "2026-05-12T17:00:00+00:00",
        "actionable_url": True,
        "cabin": "business",
        "provider_note": None,
    }
    stats = diagnostics.stats_for("GRU-MIA-business", history)
    assert stats.last_quote_present is True
    assert stats.last_quote_actionable is True
    assert "search.aviasales.com/flights/" in stats.deep_link
    assert stats.source == "travelpayouts"


def test_stats_for_route_without_history(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    stats = diagnostics.stats_for("GRU-MIA-business", store.get("GRU-MIA-business"))
    assert stats.samples == 0
    assert stats.latest is None
    assert stats.min_price is None
    assert stats.avg is None
    assert stats.p10 is None
    assert stats.last_quote_present is False


# ---------- suggest_thresholds ----------

def test_suggest_thresholds_with_enough_samples(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-LHR-business")
    for p in [1700.0, 1750.0, 1800.0, 1850.0, 1900.0, 1950.0, 2000.0]:
        history.push(p)
    stats = diagnostics.stats_for("GRU-LHR-business", history)
    suggested_e, suggested_g = diagnostics.suggest_thresholds(stats)
    assert suggested_e is not None
    assert suggested_g is not None
    # Arredondados para múltiplos de 50
    assert suggested_e % 50 == 0
    assert suggested_g % 50 == 0


def test_suggest_thresholds_insufficient_samples(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-LHR-business")
    history.push(1800.0)
    history.push(1850.0)
    stats = diagnostics.stats_for("GRU-LHR-business", history)
    e, g = diagnostics.suggest_thresholds(stats)
    assert e is None
    assert g is None


# ---------- simulate_alerts ----------

def test_simulate_alerts_current_factor(tmp_path: Path):
    """Preço atual abaixo do good_brl → alerta no cenário current."""
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-LHR-business")
    h.push(1600.0)  # abaixo de excellent_brl=1700 → conta como excellent
    stats = [diagnostics.stats_for("GRU-LHR-business", h)]
    result = diagnostics.simulate_alerts(stats, factor=1.0)
    assert result["excellent"] == 1
    assert result["total"] == 1


def test_simulate_alerts_stricter_factor(tmp_path: Path):
    """Stricter -10%: alvo menor que latest → não dispara."""
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-LHR-business")
    h.push(1900.0)  # entre excellent=1700 e good=2000; stricter -10% baixa para 1530/1800
    stats = [diagnostics.stats_for("GRU-LHR-business", h)]
    result = diagnostics.simulate_alerts(stats, factor=0.9)
    assert result["total"] == 0


def test_simulate_alerts_use_p10(tmp_path: Path):
    """use_p10 ignora o teto e usa p10 do histórico da rota."""
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-LHR-business")
    for p in [1500.0, 1600.0, 1700.0, 1800.0, 1900.0]:
        h.push(p)
    stats = [diagnostics.stats_for("GRU-LHR-business", h)]
    # latest=1900, p10 ~ 1500 — 1900 > 1500, não dispara
    result = diagnostics.simulate_alerts(stats, use_p10=True)
    assert result["total"] == 0


def test_simulate_alerts_skips_routes_without_thresholds(tmp_path: Path):
    """Rota sem teto configurado: pulada e contada em skipped_no_threshold."""
    store = PriceStore(tmp_path / "h.json")
    h = store.get("XYZ-ABC-business")  # sem teto
    h.push(500.0)
    stats = [diagnostics.stats_for("XYZ-ABC-business", h)]
    result = diagnostics.simulate_alerts(stats, factor=1.0)
    assert result["total"] == 0
    assert result["skipped_no_threshold"] == 1


# ---------- audit_links ----------

def test_audit_links_detects_legacy_url(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-LHR-business")
    h.push(1900.0)
    h.last_quote = {
        "price_brl": 1900.0,
        "origin": "GRU",
        "destination": "LHR",
        "departure_date": "2026-06-15",
        "return_date": None,
        "source": "travelpayouts",
        "deep_link": "https://www.aviasales.com/search/GRULHR",  # padrão antigo
        "detected_at": "...",
        "actionable_url": False,
        "cabin": "business",
        "provider_note": None,
    }
    stats = [diagnostics.stats_for("GRU-LHR-business", h)]
    audit = diagnostics.audit_links(stats)
    assert len(audit["legacy_urls"]) == 1
    assert audit["legacy_urls"][0].key == "GRU-LHR-business"
    assert audit["non_actionable"] == 1
    assert audit["actionable"] == 0


def test_audit_links_detects_non_actionable_without_legacy(tmp_path: Path):
    """example.com — não acionável, mas não é URL antiga do aviasales."""
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-LHR-business")
    h.push(1900.0)
    h.last_quote = {
        "price_brl": 1900.0,
        "origin": "GRU",
        "destination": "LHR",
        "departure_date": "2026-06-15",
        "return_date": None,
        "source": "mock",
        "deep_link": "https://example.com/GRU-LHR",
        "detected_at": "...",
        "actionable_url": False,
        "cabin": "business",
        "provider_note": None,
    }
    stats = [diagnostics.stats_for("GRU-LHR-business", h)]
    audit = diagnostics.audit_links(stats)
    assert audit["non_actionable"] == 1
    assert audit["actionable"] == 0
    assert len(audit["legacy_urls"]) == 0


def test_audit_links_clean_when_all_actionable(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-MIA-business")
    h.push(1200.0)
    h.last_quote = {
        "price_brl": 1200.0,
        "origin": "GRU",
        "destination": "MIA",
        "departure_date": "2026-06-15",
        "return_date": "2026-06-22",
        "source": "travelpayouts",
        "deep_link": build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22"),
        "detected_at": "...",
        "actionable_url": True,
        "cabin": "business",
        "provider_note": None,
    }
    stats = [diagnostics.stats_for("GRU-MIA-business", h)]
    audit = diagnostics.audit_links(stats)
    assert audit["actionable"] == 1
    assert audit["non_actionable"] == 0
    assert audit["legacy_urls"] == []


# ---------- export_csv ----------

def test_export_csv_writes_expected_columns_and_rows(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    for key, prices in [("GRU-LHR-business", [1800.0, 1900.0]), ("GRU-MIA-business", [1200.0])]:
        h = store.get(key)
        for p in prices:
            h.push(p)
    stats = diagnostics.all_stats(store)

    csv_path = tmp_path / "out.csv"
    n = diagnostics.export_csv(stats, csv_path)
    assert n == 2
    assert csv_path.exists()

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    header = rows[0]
    assert header == diagnostics.CSV_COLUMNS
    assert len(header) == 15
    # Linhas têm o mesmo número de colunas
    for row in rows[1:]:
        assert len(row) == 15


# ---------- helpers CLI ----------

def _install_safe_config(monkeypatch, tmp_path: Path):
    fake = Config(
        telegram_bot_token=None,
        telegram_chat_id=None,
        travelpayouts_token=None,
        kiwi_api_key=None,
        data_dir=tmp_path,
    )
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: fake))
    return fake


def _block_network(monkeypatch):
    def _no_net(*a, **k):
        raise AssertionError("comando não deve usar rede")
    monkeypatch.setattr(urllib.request, "urlopen", _no_net)


def _block_telegram(monkeypatch):
    import flight_mapper.notifier as notifier_mod
    class _Forbidden(notifier_mod.TelegramNotifier):
        def __init__(self, *a, **k):
            raise AssertionError("TelegramNotifier não deve ser instanciado")
    monkeypatch.setattr(notifier_mod, "TelegramNotifier", _Forbidden)
    import flight_mapper.__main__ as main_mod
    monkeypatch.setattr(main_mod, "TelegramNotifier", _Forbidden)


def _seed_store(tmp_path: Path) -> Path:
    history_path = tmp_path / "price_history.json"
    store = PriceStore(history_path)
    for p in [1800.0, 1850.0, 1900.0, 1850.0, 1880.0]:
        store.get("GRU-LHR-business").push(p)
    for p in [1200.0, 1207.0]:
        store.get("GRU-MIA-business").push(p)
    store.get("GRU-MIA-business").last_quote = {
        "price_brl": 1207.0,
        "origin": "GRU",
        "destination": "MIA",
        "departure_date": "2026-06-15",
        "return_date": "2026-06-22",
        "source": "travelpayouts",
        "deep_link": build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22"),
        "detected_at": "2026-05-12T17:00:00+00:00",
        "actionable_url": True,
        "cabin": "business",
        "provider_note": None,
    }
    store.save()
    return history_path


# ---------- CLI tests ----------

def test_calibrate_routes_runs_with_data(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    rc = main(["calibrate-routes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ROTA" in out
    assert "GRU-LHR-business" in out
    assert "SUGGEST_EXC" in out


def test_calibrate_routes_runs_without_data(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)

    rc = main(["calibrate-routes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Sem dados suficientes" in out


def test_simulate_thresholds_uses_required_labels(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    rc = main(["simulate-thresholds"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "current" in out
    assert "stricter -10%" in out
    assert "looser +10%" in out
    assert "p10 cutoff" in out
    assert "p25 cutoff" in out


def test_rank_routes_uses_rank_score_label(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    rc = main(["rank-routes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rank_score" in out  # não confunde com opportunity score
    # disclaimer presente
    assert "separado do opportunity score" in out


def test_provider_health_uses_correct_title(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    rc = main(["provider-health"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cobertura histórica de cotações" in out
    assert "não consulta o provider em tempo real" in out


def test_audit_links_runs_clean(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    rc = main(["audit-links"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total last_quote" in out
    assert "URLs antigas" in out


def test_export_history_without_out_shows_message(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    rc = main(["export-history"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Use --out" in out
    # nenhum arquivo criado por default
    assert not (tmp_path / "history.csv").exists()


def test_export_history_writes_csv_when_out_given(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)
    out_path = tmp_path / "history.csv"

    rc = main(["export-history", "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "route_key" in text
    assert "GRU-LHR-business" in text


def test_no_network_during_any_diagnostic_command(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)
    for cmd in [
        ["calibrate-routes"],
        ["simulate-thresholds"],
        ["rank-routes"],
        ["provider-health"],
        ["audit-links"],
        ["export-history", "--out", str(tmp_path / "x.csv")],
    ]:
        rc = main(cmd)
        assert rc == 0, f"command {cmd} failed"


def test_diagnostic_commands_do_not_modify_data_dir(tmp_path, monkeypatch):
    """Nenhum comando deve criar/alterar arquivos em data_dir além do CSV explícito."""
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)
    _block_telegram(monkeypatch)
    _seed_store(tmp_path)

    snapshot = sorted((tmp_path / "price_history.json").read_text())
    for cmd in [
        ["calibrate-routes"],
        ["simulate-thresholds"],
        ["rank-routes"],
        ["provider-health"],
        ["audit-links"],
    ]:
        main(cmd)

    after = sorted((tmp_path / "price_history.json").read_text())
    assert snapshot == after
    # nada além do que foi seedado deve ter sido criado em tmp_path
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["price_history.json"]
