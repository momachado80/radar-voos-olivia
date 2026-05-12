from __future__ import annotations

from flight_mapper.thresholds import (
    ABSOLUTE_CEILING_BRL,
    HOT_ROUTE_KEYS,
    ceiling_for,
    hot_routes,
)


def test_dict_is_non_empty():
    assert len(ABSOLUTE_CEILING_BRL) >= 5


def test_all_values_are_positive():
    assert all(v > 0 for v in ABSOLUTE_CEILING_BRL.values())


def test_all_keys_follow_business_pattern():
    for key in ABSOLUTE_CEILING_BRL.keys():
        parts = key.split("-")
        assert len(parts) == 3
        assert parts[2] == "business"
        assert len(parts[0]) == 3
        assert len(parts[1]) == 3


def test_ceiling_for_known_route():
    """ceiling_for prefere good_brl do ROUTE_THRESHOLDS quando definido."""
    from flight_mapper.thresholds import ROUTE_THRESHOLDS
    assert ceiling_for("GRU-CDG-business") == ROUTE_THRESHOLDS["GRU-CDG-business"]["good_brl"]


def test_ceiling_for_unknown_route_returns_none():
    assert ceiling_for("XYZ-ABC-business") is None
    assert ceiling_for("") is None


def test_levels_for_known_route_returns_dict():
    from flight_mapper.thresholds import levels_for
    lvl = levels_for("GRU-CDG-business")
    assert lvl is not None
    assert lvl["excellent_brl"] == 2400
    assert lvl["good_brl"] == 2800


def test_levels_for_unknown_route_returns_none():
    from flight_mapper.thresholds import levels_for
    assert levels_for("XYZ-ABC-business") is None


def test_levels_for_legacy_compat():
    """Rota só em ABSOLUTE_CEILING_BRL devolve excellent_brl=None + good_brl=ceiling."""
    from flight_mapper.thresholds import ABSOLUTE_CEILING_BRL, ROUTE_THRESHOLDS, levels_for
    # Cria rota fake só em ABSOLUTE_CEILING_BRL para testar compat
    ABSOLUTE_CEILING_BRL["TEST-LEGACY-business"] = 1234.0
    try:
        assert "TEST-LEGACY-business" not in ROUTE_THRESHOLDS
        lvl = levels_for("TEST-LEGACY-business")
        assert lvl == {"excellent_brl": None, "good_brl": 1234.0}
    finally:
        del ABSOLUTE_CEILING_BRL["TEST-LEGACY-business"]


def test_priority_routes_have_ceiling():
    # As 4 rotas prioritárias devem ter teto, senão o produto não rende valor.
    for key in ("GRU-SFO-business", "GRU-JFK-business", "GRU-LHR-business", "GRU-CDG-business"):
        assert ceiling_for(key) is not None, f"missing ceiling for {key}"


def test_hot_route_keys_non_empty():
    assert len(HOT_ROUTE_KEYS) > 0


def test_every_hot_route_has_ceiling():
    for key in HOT_ROUTE_KEYS:
        assert ceiling_for(key) is not None, f"hot route {key} has no ceiling"


def test_hot_routes_returns_route_objects_with_matching_keys():
    routes = hot_routes()
    keys = {r.key for r in routes}
    assert keys == set(HOT_ROUTE_KEYS)
    # cada Route tem region populada (pega de all_routes)
    for r in routes:
        assert r.region in {"Europa", "EUA", "Ásia"}
