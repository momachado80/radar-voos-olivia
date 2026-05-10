from __future__ import annotations

from flight_mapper.formatting import format_brl, format_source


def test_format_brl_thousands_separator_is_dot():
    assert format_brl(1207.0) == "R$ 1.207"
    assert format_brl(2483.0) == "R$ 2.483"
    assert format_brl(10000.0) == "R$ 10.000"


def test_format_brl_rounds_to_integer():
    assert format_brl(2140.49) == "R$ 2.140"
    assert format_brl(2140.51) == "R$ 2.141"


def test_format_brl_zero_and_small():
    assert format_brl(0.0) == "R$ 0"
    assert format_brl(99.0) == "R$ 99"


def test_format_source_known_values():
    assert format_source("travelpayouts") == "Travelpayouts (cache)"
    assert format_source("kiwi") == "Kiwi"
    assert format_source("mock") == "Mock"


def test_format_source_none_returns_none():
    assert format_source(None) is None
    assert format_source("") is None


def test_format_source_unknown_returns_raw():
    assert format_source("amadeus") == "amadeus"
