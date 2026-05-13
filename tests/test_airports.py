from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from flight_mapper.airports import (
    airport_label,
    build_search_url,
    is_actionable_url,
    route_airport_label,
    route_city_label,
)


# ---------- airport / route labels (regressão) ----------

def test_airport_label_known():
    assert airport_label("CDG") == "CDG Paris"
    assert airport_label("GRU") == "GRU São Paulo"
    assert airport_label("JFK") == "JFK Nova York"


def test_airport_label_unknown_falls_back_to_code():
    assert airport_label("XYZ") == "XYZ"
    assert airport_label("") == ""


def test_route_city_label_both_known():
    assert route_city_label("GRU", "CDG") == "São Paulo → Paris"


def test_route_city_label_one_unknown_falls_back():
    assert route_city_label("GRU", "XYZ") == "São Paulo → XYZ"
    assert route_city_label("XYZ", "ABC") == "XYZ → ABC"


def test_route_airport_label():
    assert route_airport_label("GRU", "CDG") == "GRU → CDG"


# ---------- build_search_url: nova URL parametrizada ----------

def test_build_search_url_round_trip_parametrized():
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")
    assert url is not None
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "search.aviasales.com"
    assert parsed.path == "/flights/"
    qs = parse_qs(parsed.query)
    assert qs["origin_iata"] == ["GRU"]
    assert qs["destination_iata"] == ["MIA"]
    assert qs["depart_date"] == ["2026-06-15"]
    assert qs["return_date"] == ["2026-06-22"]
    assert qs["adults"] == ["1"]
    assert qs["children"] == ["0"]
    assert qs["infants"] == ["0"]
    assert qs["trip_class"] == ["1"]
    assert qs["currency"] == ["usd"]
    assert qs["locale"] == ["en-us"]
    assert qs["marker_locale"] == ["en-us"]


def test_build_search_url_one_way_omits_return_date():
    url = build_search_url("GRU", "LHR", "2026-07-10")
    assert url is not None
    qs = parse_qs(urlparse(url).query)
    assert qs["depart_date"] == ["2026-07-10"]
    assert "return_date" not in qs
    assert qs["trip_class"] == ["1"]


def test_build_search_url_returns_none_without_departure():
    assert build_search_url("GRU", "MIA") is None
    assert build_search_url("GRU", "MIA", departure="") is None
    assert build_search_url("GRU", "MIA", departure=None) is None


def test_build_search_url_returns_none_for_invalid_date():
    assert build_search_url("GRU", "MIA", "not-a-date") is None
    assert build_search_url("GRU", "MIA", "2026-06-15", "garbage") is None


def test_build_search_url_returns_none_without_origin_or_destination():
    assert build_search_url("", "MIA", "2026-06-15") is None
    assert build_search_url("GRU", "", "2026-06-15") is None


def test_build_search_url_does_not_use_legacy_path_pattern():
    """Padrão antigo /search/GRUMIA não pode mais ser produzido."""
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")
    assert url is not None
    assert "/search/" not in url
    assert "GRUMIA" not in url


# ---------- is_actionable_url ----------

def test_is_actionable_url_accepts_parameterized_aviasales():
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")
    assert is_actionable_url(url) is True


def test_is_actionable_url_rejects_legacy_pattern():
    assert is_actionable_url("https://www.aviasales.com/search/GRUMIA") is False
    assert is_actionable_url("https://www.aviasales.com/search/GRU1506MIA22061") is False


def test_is_actionable_url_rejects_none_and_empty():
    assert is_actionable_url(None) is False
    assert is_actionable_url("") is False


def test_is_actionable_url_rejects_unknown_domain():
    assert is_actionable_url("https://example.com/GRU-MIA") is False
    assert is_actionable_url("https://random.site/foo") is False


def test_is_actionable_url_accepts_kiwi_deep_link():
    assert is_actionable_url("https://www.kiwi.com/deep/abc123") is True


def test_is_actionable_url_rejects_aviasales_without_required_params():
    """search.aviasales.com sem trip_class ou outros params exigidos não passa."""
    assert is_actionable_url("https://search.aviasales.com/flights/?origin_iata=GRU") is False


def test_is_actionable_url_rejects_non_http_scheme():
    assert is_actionable_url("ftp://search.aviasales.com/flights/") is False
    assert is_actionable_url("javascript:alert(1)") is False


# ---------- Comercialmente útil: locale e domínios russos ----------

def test_build_search_url_default_locale_is_en_us():
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")
    assert url is not None
    qs = parse_qs(urlparse(url).query)
    assert qs["locale"] == ["en-us"]
    assert qs["marker_locale"] == ["en-us"]


def test_build_search_url_default_currency_is_usd():
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")
    assert url is not None
    qs = parse_qs(urlparse(url).query)
    assert qs["currency"] == ["usd"]


def test_build_search_url_accepts_custom_locale_and_currency():
    url = build_search_url(
        "GRU", "MIA", "2026-06-15", "2026-06-22",
        locale="en-gb", currency="eur",
    )
    assert url is not None
    qs = parse_qs(urlparse(url).query)
    assert qs["locale"] == ["en-gb"]
    assert qs["currency"] == ["eur"]


def test_is_actionable_url_rejects_russian_tld():
    assert is_actionable_url("https://aviasales.ru/flights/?origin_iata=GRU&destination_iata=MIA&depart_date=2026-06-15&trip_class=1&locale=ru") is False
    assert is_actionable_url("https://foo.ru/bar") is False


def test_is_actionable_url_rejects_russian_subdomain():
    assert is_actionable_url("https://ru.aviasales.com/flights/?origin_iata=GRU&destination_iata=MIA") is False


def test_is_actionable_url_rejects_aviasales_without_locale():
    """URL antiga sem locale → rejeitada (cairia em russo no Aviasales)."""
    url = (
        "https://search.aviasales.com/flights/?origin_iata=GRU"
        "&destination_iata=MIA&depart_date=2026-06-15&return_date=2026-06-22"
        "&adults=1&children=0&infants=0&trip_class=1&currency=brl"
    )
    assert is_actionable_url(url) is False


def test_is_actionable_url_accepts_aviasales_with_locale():
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")
    assert is_actionable_url(url) is True
    qs = parse_qs(urlparse(url).query)
    assert "locale" in qs


def test_is_actionable_url_rejects_old_locale_en():
    """locale=en não passa mais — defaults migraram para en-us."""
    url = (
        "https://search.aviasales.com/flights/?origin_iata=GRU"
        "&destination_iata=MIA&depart_date=2026-06-15&return_date=2026-06-22"
        "&adults=1&children=0&infants=0&trip_class=1&currency=usd"
        "&locale=en&marker_locale=en"
    )
    assert is_actionable_url(url) is False


def test_is_actionable_url_rejects_currency_brl():
    """currency=brl não passa mais — defaults migraram para usd."""
    url = build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22", currency="brl")
    assert is_actionable_url(url) is False


def test_is_actionable_url_rejects_locale_ru_even_on_correct_host():
    url = (
        "https://search.aviasales.com/flights/?origin_iata=GRU"
        "&destination_iata=MIA&depart_date=2026-06-15"
        "&adults=1&children=0&infants=0&trip_class=1&currency=usd"
        "&locale=ru&marker_locale=ru"
    )
    assert is_actionable_url(url) is False
