"""Testes do módulo de links auxiliares de pesquisa."""

from __future__ import annotations

from flight_mapper.auxiliary_links import (
    build_auxiliary_search_links,
    build_google_flights_query_url,
    build_google_search_url,
    build_kayak_search_url,
)
from flight_mapper.providers import Quote
from flight_mapper.regions import Route


_ROUTE = Route("GRU", "CDG", "Europa")


def _quote(return_date: str | None = "2026-06-16") -> Quote:
    return Quote(
        route=_ROUTE,
        price_brl=1500.0,
        deep_link=None,
        departure_date="2026-06-09",
        return_date=return_date,
        source="manual_purchase",
    )


# ---------- Google Search (fallback genérico) ----------

def test_google_search_url_contains_route_date_and_class():
    url = build_google_search_url(_ROUTE, "2026-06-09", "2026-06-16")
    assert url.startswith("https://www.google.com/search?q=")
    lowered = url.lower()
    assert "gru" in lowered
    assert "cdg" in lowered
    assert "2026-06-09" in url
    assert "2026-06-16" in url
    assert "business" in lowered


def test_google_search_url_one_way_omits_return():
    url = build_google_search_url(_ROUTE, "2026-06-09", None)
    assert "2026-06-09" in url
    # data de volta padrão NÃO aparece
    assert "2026-06-16" not in url
    assert "business" in url.lower()


# ---------- Google Flights ----------

def test_google_flights_url_contains_route_date_and_class():
    url = build_google_flights_query_url(_ROUTE, "2026-06-09", "2026-06-16")
    assert url.startswith("https://www.google.com/travel/flights?q=")
    lowered = url.lower()
    assert "gru" in lowered
    assert "cdg" in lowered
    assert "2026-06-09" in url
    assert "2026-06-16" in url
    assert "business" in lowered


def test_google_flights_url_one_way():
    url = build_google_flights_query_url(_ROUTE, "2026-06-09", None)
    assert "2026-06-09" in url
    assert "return" not in url.lower()


# ---------- Kayak ----------

def test_kayak_url_has_stable_path_with_business_segment():
    url = build_kayak_search_url(_ROUTE, "2026-06-09", "2026-06-16")
    assert url == "https://www.kayak.com/flights/GRU-CDG/2026-06-09/2026-06-16/business"


def test_kayak_url_one_way():
    url = build_kayak_search_url(_ROUTE, "2026-06-09", None)
    assert url == "https://www.kayak.com/flights/GRU-CDG/2026-06-09/business"


# ---------- Lineup completo ----------

def test_build_auxiliary_search_links_order_and_labels():
    links = build_auxiliary_search_links(_quote())
    assert [label for label, _ in links] == [
        "Pesquisar no Google",
        "Pesquisar no Google Flights",
        "Pesquisar no Kayak",
    ]


def test_build_auxiliary_search_links_never_emits_aviasales():
    """Garantia dura: nenhum link auxiliar pode conter aviasales (em qualquer forma)."""
    for return_date in (None, "2026-06-16"):
        links = build_auxiliary_search_links(_quote(return_date=return_date))
        for _, url in links:
            lowered = url.lower()
            assert "aviasales" not in lowered
            assert "search.aviasales.com" not in lowered
            assert "aviasales.ru" not in lowered


def test_build_auxiliary_search_links_pure_no_network(monkeypatch):
    """Helpers são puros: nenhuma chamada a urlopen é permitida."""
    import urllib.request

    def _no_network(*a, **k):  # pragma: no cover - defensive
        raise AssertionError("auxiliary_links não deve usar rede")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    links = build_auxiliary_search_links(_quote())
    assert len(links) == 3


def test_build_auxiliary_search_links_all_urls_carry_route_date_class():
    """Os 3 URLs do lineup carregam origem, destino, data de ida e business."""
    quote = _quote(return_date="2026-06-16")
    for _, url in build_auxiliary_search_links(quote):
        lowered = url.lower()
        assert "gru" in lowered
        assert "cdg" in lowered
        assert "2026-06-09" in url
        assert "business" in lowered
