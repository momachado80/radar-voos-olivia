"""Testes do módulo de links auxiliares de pesquisa."""

from __future__ import annotations

from flight_mapper.auxiliary_links import (
    build_auxiliary_links,
    build_expedia_search_url,
    build_google_flights_search_url,
    build_kayak_search_url,
)
from flight_mapper.providers import Quote
from flight_mapper.regions import Route


_ROUTE = Route("GRU", "JFK", "EUA")


def _quote(return_date: str | None = "2026-06-26") -> Quote:
    return Quote(
        route=_ROUTE,
        price_brl=1500.0,
        deep_link=None,
        departure_date="2026-06-19",
        return_date=return_date,
        source="manual_purchase",
    )


def test_google_flights_url_contains_route_date_and_class():
    url = build_google_flights_search_url(_ROUTE, "2026-06-19", "2026-06-26")
    assert url.startswith("https://www.google.com/travel/flights?q=")
    lowered = url.lower()
    assert "gru" in lowered
    assert "jfk" in lowered
    assert "2026-06-19" in url
    assert "2026-06-26" in url
    assert "business" in lowered


def test_google_flights_url_one_way_omits_return():
    url = build_google_flights_search_url(_ROUTE, "2026-06-19", None)
    assert "2026-06-19" in url
    assert "return" not in url.lower()


def test_kayak_url_has_stable_path_with_business_segment():
    url = build_kayak_search_url(_ROUTE, "2026-06-19", "2026-06-26")
    assert url == "https://www.kayak.com/flights/GRU-JFK/2026-06-19/2026-06-26/business"


def test_kayak_url_one_way():
    url = build_kayak_search_url(_ROUTE, "2026-06-19", None)
    assert url == "https://www.kayak.com/flights/GRU-JFK/2026-06-19/business"


def test_expedia_url_uses_google_site_search():
    url = build_expedia_search_url(_ROUTE, "2026-06-19", "2026-06-26")
    assert url.startswith("https://www.google.com/search?q=")
    lowered = url.lower()
    assert "site%3aexpedia.com" in lowered
    assert "gru" in lowered
    assert "jfk" in lowered
    assert "2026-06-19" in url
    assert "business" in lowered


def test_build_auxiliary_links_order_and_labels():
    links = build_auxiliary_links(_quote())
    assert [label for label, _ in links] == [
        "Pesquisar no Google Flights",
        "Pesquisar no Kayak",
        "Pesquisar na Expedia",
    ]


def test_build_auxiliary_links_never_emits_aviasales():
    """Garantia dura: nenhuma URL auxiliar pode conter aviasales."""
    for return_date in (None, "2026-06-26"):
        links = build_auxiliary_links(_quote(return_date=return_date))
        for _, url in links:
            assert "aviasales" not in url.lower()


def test_build_auxiliary_links_pure_no_network(monkeypatch):
    """Helpers são puros: nenhuma chamada a urlopen é permitida."""
    import urllib.request

    def _no_network(*a, **k):  # pragma: no cover - defensive
        raise AssertionError("auxiliary_links não deve usar rede")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    links = build_auxiliary_links(_quote())
    assert len(links) == 3
