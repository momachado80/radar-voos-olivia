from __future__ import annotations

import json
from pathlib import Path

from flight_mapper.state import PriceStore, RouteHistory


def test_loads_legacy_json_without_last_quote(tmp_path: Path):
    """JSON antigo (sem last_quote) carrega normalmente; last_quote=None."""
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({
            "GRU-MIA-business": {
                "prices": [1234.0, 1207.0],
                "last_alert_at": None,
                "last_alert_price": None,
            }
        }),
        encoding="utf-8",
    )
    store = PriceStore(path)
    hist = store.get("GRU-MIA-business")
    assert hist.prices == [1234.0, 1207.0]
    assert hist.last_alert_at is None
    assert hist.last_alert_price is None
    assert hist.last_quote is None


def test_round_trip_saves_and_loads_last_quote(tmp_path: Path):
    path = tmp_path / "h.json"
    store = PriceStore(path)
    history = store.get("GRU-CDG-business")
    history.push(2483.0)
    history.last_quote = {
        "price_brl": 2483.0,
        "origin": "GRU",
        "destination": "CDG",
        "departure_date": "2026-06-15",
        "return_date": "2026-06-22",
        "source": "travelpayouts",
        "deep_link": "https://search.aviasales.com/flights/?origin_iata=GRU&destination_iata=CDG&depart_date=2026-06-15&trip_class=1",
        "detected_at": "2026-05-12T17:30:00+00:00",
        "actionable_url": True,
        "cabin": "business",
        "provider_note": None,
    }
    store.save()

    reopened = PriceStore(path)
    reloaded = reopened.get("GRU-CDG-business")
    assert reloaded.prices == [2483.0]
    assert reloaded.last_quote is not None
    assert reloaded.last_quote["price_brl"] == 2483.0
    assert reloaded.last_quote["origin"] == "GRU"
    assert reloaded.last_quote["destination"] == "CDG"
    assert reloaded.last_quote["actionable_url"] is True


def test_route_history_default_last_quote_is_none():
    h = RouteHistory()
    assert h.last_quote is None


def test_loads_json_with_partial_last_quote_field(tmp_path: Path):
    """Aceitar last_quote como dict mesmo com campos faltando."""
    path = tmp_path / "h.json"
    path.write_text(
        json.dumps({
            "GRU-CDG-business": {
                "prices": [2483.0],
                "last_alert_at": None,
                "last_alert_price": None,
                "last_quote": {
                    "price_brl": 2483.0,
                    "origin": "GRU",
                    "destination": "CDG",
                },
            }
        }),
        encoding="utf-8",
    )
    store = PriceStore(path)
    lq = store.get("GRU-CDG-business").last_quote
    assert lq is not None
    assert lq["origin"] == "GRU"
    assert lq.get("deep_link") is None
