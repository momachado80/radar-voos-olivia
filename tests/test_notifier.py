from __future__ import annotations

from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Route


def _quote(**overrides) -> Quote:
    base = dict(
        route=Route("GRU", "CDG", "Europa"),
        price_brl=2140.0,
        deep_link="https://www.aviasales.com/search/GRU1506CDG22061",
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="travelpayouts",
    )
    base.update(overrides)
    return Quote(**base)


def test_format_alert_includes_city_airport_and_source():
    text = format_alert(_quote(), average=2483.0, drop_pct=0.14)
    assert "São Paulo → Paris" in text
    assert "GRU → CDG" in text
    assert "(Europa)" in text
    assert "R$ 2,140" in text
    assert "média R$ 2,483" in text
    assert "queda 14%" in text
    assert "Travelpayouts (cache)" in text
    assert "Conferir busca" in text
    assert "https://www.aviasales.com/search/GRU1506CDG22061" in text


def test_format_alert_omits_source_line_when_missing():
    text = format_alert(_quote(source=None), average=2483.0, drop_pct=0.14)
    assert "🛒 Fonte" not in text
    assert "Conferir busca" in text  # link ainda presente


def test_format_alert_omits_link_when_missing():
    text = format_alert(_quote(deep_link=None), average=2483.0, drop_pct=0.14)
    assert "Conferir busca" not in text
    assert "Travelpayouts (cache)" in text  # source ainda presente


def test_format_alert_priority_has_flag():
    text = format_alert(_quote(), average=2483.0, drop_pct=0.14, priority=True)
    assert "🔥" in text


def test_format_alert_non_priority_has_no_flag():
    text = format_alert(_quote(), average=2483.0, drop_pct=0.14, priority=False)
    assert "🔥" not in text


def test_format_alert_one_way_omits_return_date():
    text = format_alert(_quote(return_date=None), average=2483.0, drop_pct=0.14)
    assert "📅 2026-06-15" in text
    assert "→ 2026" not in text  # sem flecha de volta de data


def test_format_alert_unknown_source_uses_raw_value():
    text = format_alert(_quote(source="amadeus"), average=2483.0, drop_pct=0.14)
    assert "🛒 Fonte: amadeus" in text


def test_format_alert_avoids_misleading_open_offer_label():
    text = format_alert(_quote(), average=2483.0, drop_pct=0.14)
    assert "Abrir oferta" not in text
