from __future__ import annotations

import urllib.request
from pathlib import Path

from flight_mapper.__main__ import main
from flight_mapper.config import Config
from flight_mapper.thresholds import (
    HOT_ONE_WAY_ROUTE_KEYS,
    HOT_ROUTE_KEYS,
    hot_routes,
    one_way_hot_routes,
)


def _install_safe_config(monkeypatch, tmp_path: Path):
    """Substitui Config.from_env por uma instância apontando para tmp_path."""
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
    def _no_network(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("hot-scan deve rodar sem rede em testes")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)


def test_hot_scan_returns_zero_and_emits_log(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)

    rc = main(["hot-scan", "--mock"])

    assert rc == 0
    out = capsys.readouterr().out
    # PR F1: hot-scan agora compõe round_trip + one_way hot routes.
    expected_n = len(hot_routes()) + len(one_way_hot_routes())
    assert expected_n > 0
    assert len(one_way_hot_routes()) == 10
    assert f"hot-scan scanned={expected_n}" in out
    # MockProvider sempre devolve cotação, então quotes == scanned
    assert f"quotes={expected_n}" in out
    assert "alerts=" in out


def test_hot_scan_uses_only_hot_routes(tmp_path, monkeypatch, capsys):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)

    rc = main(["hot-scan", "--mock"])
    assert rc == 0

    out = capsys.readouterr().out
    # Notas trip-aware: "  GRU→MIA [ida e volta]: ..." /
    # "  GRU→MIA [somente ida]: ...". Reconstrói a chave conforme o trip.
    note_lines = [line for line in out.splitlines() if line.startswith("  ")]
    seen_keys = set()
    seen_tags = set()
    for line in note_lines:
        origin = line.strip().split("→")[0]
        rest = line.split("→")[1].split(":")[0].strip()  # "MIA [ida e volta]"
        dest, _, tag = rest.partition(" [")
        tag = tag.rstrip("]")
        seen_tags.add(tag)
        if tag == "somente ida":
            seen_keys.add(f"{origin}-{dest}-one_way-business")
        else:
            seen_keys.add(f"{origin}-{dest}-business")
    # notas carregam o trip explícito
    assert seen_tags == {"ida e volta", "somente ida"}
    # todas as chaves nas notas são rotas quentes (RT ∪ one-way)
    assert seen_keys.issubset(set(HOT_ROUTE_KEYS) | set(HOT_ONE_WAY_ROUTE_KEYS))


def test_hot_scan_persists_history_in_configured_data_dir(tmp_path, monkeypatch):
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)

    rc = main(["hot-scan", "--mock"])
    assert rc == 0

    # MockProvider em hot-scan grava histórico
    history_path = tmp_path / "price_history.json"
    assert history_path.exists()
    # nada criado fora do tmp
    assert (tmp_path / "cycle.json").exists() is False  # hot-scan usa run_once, não cycle


def test_hot_scan_does_not_send_telegram(tmp_path, monkeypatch, capsys):
    """Sem TELEGRAM_BOT_TOKEN/CHAT_ID na config, notifier é None — zero envios."""
    _install_safe_config(monkeypatch, tmp_path)
    _block_network(monkeypatch)

    sent = []
    real_send = type("Telegram", (), {"send": lambda self, t: sent.append(t) or True})
    monkeypatch.setattr(
        "flight_mapper.__main__._make_notifier",
        lambda config: None,
    )

    rc = main(["hot-scan", "--mock"])
    assert rc == 0
    assert sent == []
