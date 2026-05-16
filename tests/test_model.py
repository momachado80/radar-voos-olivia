"""PR A — modelo trip_type/cabin. Garante zero mudança de comportamento:
`.key`, PRIORITY/all_routes e Quote legado permanecem idênticos.
"""

from flight_mapper.providers import Quote
from flight_mapper.regions import (
    Cabin,
    Route,
    TripType,
    all_routes,
    is_priority,
)


def test_enum_values():
    assert TripType.ONE_WAY.value == "one_way"
    assert TripType.ROUND_TRIP.value == "round_trip"
    assert TripType.OPEN_JAW_CANDIDATE.value == "open_jaw_candidate"
    assert Cabin.ECONOMY.value == "economy"
    assert Cabin.BUSINESS.value == "business"
    assert Cabin.UNKNOWN.value == "unknown"
    # mixin str: comparação com string crua funciona
    assert TripType.ROUND_TRIP == "round_trip"
    assert Cabin.BUSINESS == "business"


def test_route_defaults_preserve_behavior():
    r = Route("GRU", "MIA", "EUA")
    assert r.trip_type is TripType.ROUND_TRIP
    assert r.cabin is Cabin.BUSINESS
    # .key NÃO mudou — continua no formato legado
    assert r.key == "GRU-MIA-business"
    assert r.legacy_key == "GRU-MIA-business"


def test_canonical_key_is_new_format_and_unused():
    r = Route("GRU", "MIA", "EUA")
    assert r.canonical_key == "GRU-MIA-round_trip-business"
    r2 = Route("GRU", "MIA", "EUA", TripType.ONE_WAY, Cabin.ECONOMY)
    assert r2.canonical_key == "GRU-MIA-one_way-economy"
    # legacy_key ignora trip/cabin (estável)
    assert r2.legacy_key == "GRU-MIA-business"


def test_all_routes_and_priority_unchanged():
    routes = all_routes()
    assert all(r.key.endswith("-business") for r in routes)
    assert is_priority(Route("GRU", "SFO", "EUA")) is True
    assert is_priority(Route("GRU", "MIA", "EUA")) is False


def test_route_equality_with_defaults():
    assert Route("GRU", "CDG", "Europa") == Route("GRU", "CDG", "Europa")
    assert Route("GRU", "CDG", "Europa") != Route(
        "GRU", "CDG", "Europa", TripType.ONE_WAY
    )


def test_quote_new_fields_default_safe():
    q = Quote(
        route=Route("GRU", "CDG", "Europa"),
        price_brl=5000.0,
        deep_link=None,
        departure_date="2026-06-01",
        return_date="2026-06-08",
    )
    assert q.cabin is Cabin.UNKNOWN
    assert q.trip_type is TripType.ROUND_TRIP
    assert q.cabin_confirmed is False
    assert q.suspicious is False
    # caminho legado BRL preservado (__post_init__)
    assert q.amount == 5000.0
    assert q.currency == "BRL"
    assert q.amount_brl_estimated == 5000.0
