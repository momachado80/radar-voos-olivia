from __future__ import annotations

from flight_mapper.airports import (
    airport_label,
    build_search_url,
    route_airport_label,
    route_city_label,
)
from flight_mapper.providers import TravelpayoutsProvider
from flight_mapper.regions import Route


def test_airport_label_known():
    assert airport_label("CDG") == "CDG Paris"
    assert airport_label("GRU") == "GRU São Paulo"
    assert airport_label("JFK") == "JFK Nova York"


def test_airport_label_unknown_falls_back_to_code():
    assert airport_label("XYZ") == "XYZ"
    assert airport_label("") == ""


def test_route_city_label_both_known():
    assert route_city_label("GRU", "CDG") == "São Paulo → Paris"
    assert route_city_label("CGH", "MIA") == "São Paulo → Miami"


def test_route_city_label_one_unknown_falls_back():
    assert route_city_label("GRU", "XYZ") == "São Paulo → XYZ"
    assert route_city_label("XYZ", "CDG") == "XYZ → Paris"
    assert route_city_label("XYZ", "ABC") == "XYZ → ABC"


def test_route_airport_label():
    assert route_airport_label("GRU", "CDG") == "GRU → CDG"
    assert route_airport_label("XYZ", "ABC") == "XYZ → ABC"


def test_build_search_url_without_dates():
    assert build_search_url("GRU", "CDG") == "https://www.aviasales.com/search/GRUCDG"


def test_build_search_url_one_way_with_date():
    assert (
        build_search_url("GRU", "LHR", departure="2026-06-15")
        == "https://www.aviasales.com/search/GRU1506LHR1"
    )


def test_build_search_url_round_trip_matches_provider():
    route = Route("GRU", "LHR", "Europa")
    expected = TravelpayoutsProvider._search_url(route, "2026-06-15", "2026-06-22")
    actual = build_search_url("GRU", "LHR", departure="2026-06-15", return_date="2026-06-22")
    assert actual == expected


def test_build_search_url_invalid_date_falls_back():
    assert (
        build_search_url("GRU", "LHR", departure="not-a-date")
        == "https://www.aviasales.com/search/GRULHR"
    )
