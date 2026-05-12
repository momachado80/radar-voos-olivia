from __future__ import annotations

from pathlib import Path

from flight_mapper.state import PriceStore
from flight_mapper.watchlists import WATCHLISTS, Watchlist, best_per_watchlist


def test_watchlists_non_empty_and_have_labels():
    assert len(WATCHLISTS) >= 3
    for wl in WATCHLISTS:
        assert isinstance(wl, Watchlist)
        assert wl.name
        assert wl.label
        assert wl.route_keys


def test_watchlist_labels_use_executiva_terminology():
    labels = {wl.label for wl in WATCHLISTS}
    assert "Europa Executiva" in labels
    assert "EUA Executiva" in labels
    assert "Ásia/Oriente Médio Executiva" in labels


def test_watchlist_route_keys_follow_business_pattern():
    for wl in WATCHLISTS:
        for key in wl.route_keys:
            parts = key.split("-")
            assert len(parts) == 3
            assert parts[2] == "business"


def test_best_per_watchlist_picks_lowest_price(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # Europa: LHR mais barata
    store.get("GRU-LHR-business").push(1800.0)
    store.get("GRU-CDG-business").push(2483.0)
    # EUA: MIA mais barata
    store.get("GRU-MIA-business").push(1207.0)
    store.get("GRU-ORD-business").push(1631.0)
    # Ásia: DXB mais barata
    store.get("GRU-DXB-business").push(2798.0)
    store.get("GRU-NRT-business").push(3999.0)

    out = best_per_watchlist(store)
    by_label = {wl.label: (key, price) for wl, key, price in out}

    assert by_label["Europa Executiva"] == ("GRU-LHR-business", 1800.0)
    assert by_label["EUA Executiva"] == ("GRU-MIA-business", 1207.0)
    assert by_label["Ásia/Oriente Médio Executiva"] == ("GRU-DXB-business", 2798.0)


def test_best_per_watchlist_skips_empty_watchlists(tmp_path: Path):
    """Sem histórico em nenhuma rota Europa → Europa não aparece no retorno."""
    store = PriceStore(tmp_path / "h.json")
    store.get("GRU-MIA-business").push(1207.0)

    out = best_per_watchlist(store)
    labels = {wl.label for wl, _, _ in out}
    assert "EUA Executiva" in labels
    assert "Europa Executiva" not in labels  # sem rota Europa povoada


def test_best_per_watchlist_ordered_by_priority(tmp_path: Path):
    """Resultado segue priority crescente (Europa=0, EUA=1, Ásia=2)."""
    store = PriceStore(tmp_path / "h.json")
    store.get("GRU-LHR-business").push(1800.0)
    store.get("GRU-MIA-business").push(1207.0)
    store.get("GRU-DXB-business").push(2798.0)

    out = best_per_watchlist(store)
    priorities = [wl.priority for wl, _, _ in out]
    assert priorities == sorted(priorities)
