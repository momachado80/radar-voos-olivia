from __future__ import annotations

from flight_mapper.detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    Decision,
)
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


def _drop_decision(**overrides) -> Decision:
    base = dict(
        alert=True,
        reason="queda de 14% vs média histórica",
        average=2483.0,
        drop_pct=0.14,
        criterion=CRITERION_AVERAGE_DROP,
    )
    base.update(overrides)
    return Decision(**base)


def _ceiling_decision(**overrides) -> Decision:
    base = dict(
        alert=True,
        reason="preço R$ 2350 <= teto R$ 2400",
        criterion=CRITERION_CEILING,
        threshold=2400.0,
    )
    base.update(overrides)
    return Decision(**base)


# ---------- alerta por queda vs média ----------

def test_format_alert_drop_includes_city_airport_and_source():
    text = format_alert(_quote(), _drop_decision())
    assert "São Paulo → Paris" in text
    assert "GRU → CDG" in text
    assert "(Europa)" in text
    assert "R$ 2.140" in text
    assert "média R$ 2.483" in text
    assert "queda 14%" in text
    assert "Travelpayouts (cache)" in text
    assert "Conferir busca" in text


def test_format_alert_drop_has_average_criterion_line():
    text = format_alert(_quote(), _drop_decision())
    assert "📉 Critério: queda histórica acima do limite" in text
    assert "🎯 Critério: preço abaixo do alvo" not in text


def test_format_alert_uses_brl_with_dot_separator():
    text = format_alert(_quote(price_brl=10000.0), _drop_decision(average=12000.0))
    assert "R$ 10.000" in text
    assert "R$ 12.000" in text
    # nunca a vírgula en-US como separador de milhar
    assert "R$ 10,000" not in text
    assert "R$ 12,000" not in text


# ---------- alerta por preço-alvo ----------

def test_format_alert_ceiling_has_target_criterion_line():
    text = format_alert(_quote(price_brl=2350.0), _ceiling_decision())
    assert "🎯 Critério: preço abaixo do alvo configurado para esta rota" in text
    assert "📉 Critério" not in text


def test_format_alert_ceiling_shows_price_and_ceiling():
    text = format_alert(_quote(price_brl=2350.0), _ceiling_decision())
    assert "R$ 2.350" in text
    assert "teto R$ 2.400" in text


def test_format_alert_ceiling_omits_average_block():
    text = format_alert(_quote(price_brl=2350.0), _ceiling_decision())
    assert "média R$" not in text
    assert "queda" not in text


# ---------- comportamentos comuns ----------

def test_format_alert_omits_source_line_when_missing():
    text = format_alert(_quote(source=None), _drop_decision())
    assert "🛒 Fonte" not in text
    assert "Conferir busca" in text


def test_format_alert_omits_link_when_missing():
    text = format_alert(_quote(deep_link=None), _drop_decision())
    assert "Conferir busca" not in text
    assert "Travelpayouts (cache)" in text


def test_format_alert_priority_has_flag():
    text = format_alert(_quote(), _drop_decision(), priority=True)
    assert "🔥" in text


def test_format_alert_non_priority_has_no_flag():
    text = format_alert(_quote(), _drop_decision(), priority=False)
    assert "🔥" not in text


def test_format_alert_one_way_omits_return_date():
    text = format_alert(_quote(return_date=None), _drop_decision())
    assert "📅 2026-06-15" in text
    assert "→ 2026" not in text


def test_format_alert_unknown_source_uses_raw_value():
    text = format_alert(_quote(source="amadeus"), _drop_decision())
    assert "🛒 Fonte: amadeus" in text


def test_format_alert_unknown_airport_omits_iata_duplicate_line():
    text = format_alert(
        _quote(route=Route("XYZ", "ABC", "Europa")),
        _drop_decision(),
    )
    # quando city_label == iata_label, não duplica a linha
    assert text.count("XYZ → ABC") == 1


def test_format_alert_avoids_misleading_open_offer_label():
    text = format_alert(_quote(), _drop_decision())
    assert "Abrir oferta" not in text
