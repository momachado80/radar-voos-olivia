"""PR B — compatibilidade legacy/canonical no PriceStore.

Garante migração não-destrutiva e zero mudança de comportamento:
pipeline continua usando get(route.key) com Route.key legado.
"""

from __future__ import annotations

import json
from pathlib import Path

from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore, RouteHistory
from flight_mapper.thresholds import levels_for


_ROUTE = Route("GRU", "MIA", "EUA")  # defaults: round_trip / business


def test_legacy_key_and_canonical_key_shapes():
    assert _ROUTE.legacy_key == "GRU-MIA-business"
    assert _ROUTE.canonical_key == "GRU-MIA-round_trip-business"
    # Route.key ainda legado (não mudou neste PR)
    assert _ROUTE.key == "GRU-MIA-business"


def test_reads_history_by_legacy_key(tmp_path: Path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({"GRU-MIA-business": {"prices": [1234.0, 1207.0]}}),
        encoding="utf-8",
    )
    store = PriceStore(path)
    # só legacy existe → resolve para legacy
    assert store.resolve_history_key(_ROUTE) == "GRU-MIA-business"
    hist = store.get_history(_ROUTE)
    assert hist.prices == [1234.0, 1207.0]


def test_reads_history_by_canonical_key(tmp_path: Path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({"GRU-MIA-round_trip-business": {"prices": [999.0]}}),
        encoding="utf-8",
    )
    store = PriceStore(path)
    assert store.resolve_history_key(_ROUTE) == "GRU-MIA-round_trip-business"
    assert store.get_history(_ROUTE).prices == [999.0]


def test_canonical_takes_priority_when_both_exist(tmp_path: Path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps(
            {
                "GRU-MIA-business": {"prices": [1000.0]},
                "GRU-MIA-round_trip-business": {"prices": [2000.0, 2100.0]},
            }
        ),
        encoding="utf-8",
    )
    store = PriceStore(path)
    assert store.resolve_history_key(_ROUTE) == "GRU-MIA-round_trip-business"
    assert store.get_history(_ROUTE).prices == [2000.0, 2100.0]


def test_legacy_history_still_compatible_via_string_get(tmp_path: Path):
    """Caminho de produção (Monitor usa store.get(route.key)) inalterado."""
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({"GRU-MIA-business": {"prices": [1234.0, 1207.0]}}),
        encoding="utf-8",
    )
    store = PriceStore(path)
    hist = store.get(_ROUTE.key)  # _ROUTE.key == "GRU-MIA-business"
    assert hist.prices == [1234.0, 1207.0]


def test_ensure_canonical_seed_copies_without_duplicating(tmp_path: Path):
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps(
            {
                "GRU-MIA-business": {
                    "prices": [1234.0, 1207.0],
                    "last_alert_at": "2026-05-10T00:00:00+00:00",
                    "last_alert_price": 1207.0,
                    "last_quote": {"origin": "GRU", "destination": "MIA"},
                }
            }
        ),
        encoding="utf-8",
    )
    store = PriceStore(path)

    assert store.ensure_canonical_seed(_ROUTE) is True
    legacy = store.get("GRU-MIA-business")
    canonical = store.get("GRU-MIA-round_trip-business")

    # cópia fiel
    assert canonical.prices == [1234.0, 1207.0]
    assert canonical.last_alert_price == 1207.0
    assert canonical.last_quote == {"origin": "GRU", "destination": "MIA"}
    # legacy preservado, não apagado
    assert legacy.prices == [1234.0, 1207.0]
    # sem aliasing: mutar canonical não afeta legacy
    canonical.push(1100.0)
    assert legacy.prices == [1234.0, 1207.0]
    # idempotente: segunda chamada não duplica nem re-semeia
    assert store.ensure_canonical_seed(_ROUTE) is False
    assert store.get("GRU-MIA-round_trip-business").prices == [1234.0, 1207.0, 1100.0]


def test_ensure_canonical_seed_noop_when_no_legacy(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")  # store vazio
    assert store.ensure_canonical_seed(_ROUTE) is False
    assert "GRU-MIA-round_trip-business" not in list(store.keys())


def test_ensure_canonical_seed_does_not_write_disk(tmp_path: Path):
    path = tmp_path / "h.json"
    original = json.dumps({"GRU-MIA-business": {"prices": [1234.0]}})
    path.write_text(original, encoding="utf-8")
    store = PriceStore(path)
    store.ensure_canonical_seed(_ROUTE)
    # leitura/seed não persiste; arquivo intacto até save() explícito
    assert path.read_text(encoding="utf-8") == original


def test_canonical_key_does_not_change_thresholds():
    # thresholds continuam casando pela chave legada
    assert levels_for(_ROUTE.key) == {"excellent_brl": 1100, "good_brl": 1300}
    # a chave canônica NÃO tem threshold (ainda não consumida)
    assert levels_for(_ROUTE.canonical_key) is None


def test_old_price_history_json_loads_without_error(tmp_path: Path):
    """JSON antigo (chaves legadas, sem last_quote) carrega sem erro."""
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps(
            {
                "CGH-AMS-business": {"prices": [2739.0, 2738.0]},
                "GRU-MIA-business": {
                    "prices": [1207.0],
                    "last_alert_at": None,
                    "last_alert_price": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = PriceStore(path)
    assert store.get("CGH-AMS-business").prices == [2739.0, 2738.0]
    assert store.get("GRU-MIA-business").prices == [1207.0]


def test_route_history_clone_is_independent():
    src = RouteHistory(prices=[1.0, 2.0], last_quote={"a": 1})
    cp = src.clone()
    cp.push(3.0)
    cp.last_quote["a"] = 99
    assert src.prices == [1.0, 2.0]
    assert src.last_quote == {"a": 1}


def test_non_default_route_canonical_is_distinct(tmp_path: Path):
    """Rota com trip/cabin não-default tem canonical próprio distinto.

    Documenta a semântica atual da prioridade (canonical→legacy→canonical):
    como `legacy_key` é sempre `origin-dest-business` (PR A), uma rota
    one_way/economy SEM canonical próprio cai no legacy business existente.
    Inofensivo neste PR: `resolve_history_key`/`get_history` NÃO estão
    ligados ao pipeline (Route.key segue legado). O refino dessa borda
    (não reaproveitar legacy business p/ economy) fica para a fase que
    de fato consumir canonical.
    """
    r = Route("GRU", "MIA", "EUA", TripType.ONE_WAY, Cabin.ECONOMY)
    assert r.canonical_key == "GRU-MIA-one_way-economy"
    assert r.legacy_key == "GRU-MIA-business"
    store = PriceStore(tmp_path / "h.json")
    # canonical próprio presente → tem prioridade
    store.get("GRU-MIA-one_way-economy").push(320.0)
    assert store.resolve_history_key(r) == "GRU-MIA-one_way-economy"
    # sem canonical próprio, com legacy business presente → cai no legacy
    r2 = Route("GRU", "JFK", "EUA", TripType.ONE_WAY, Cabin.ECONOMY)
    store.get("GRU-JFK-business").push(1500.0)
    assert store.resolve_history_key(r2) == "GRU-JFK-business"
