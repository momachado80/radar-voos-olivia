"""PR E — mensagens/links cientes de cabin e trip_type.

Sem rede, sem Telegram (format_alert é puro).
"""

from __future__ import annotations

from flight_mapper.auxiliary_links import build_auxiliary_search_links
from flight_mapper.detector import CRITERION_CEILING, LEVEL_GOOD, Decision
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType

_ROUTE = Route("GRU", "CDG", "Europa")


def _q(**ov) -> Quote:
    base = dict(
        route=_ROUTE,
        price_brl=9000.0,
        deep_link="https://www.kiwi.com/deep/GRU-CDG-2026-06-09",
        departure_date="2026-06-09",
        return_date="2026-06-16",
        source="kiwi",
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP,
    )
    base.update(ov)
    return Quote(**base)


_DEC = Decision(
    alert=True, reason="", criterion=CRITERION_CEILING,
    threshold=11000.0, level=LEVEL_GOOD, score=80,
)


# ---------- 1. business confirmado ----------

def test_business_confirmed_title():
    body = format_alert(_q(), _DEC)
    assert "Business em promoção" in body
    assert "(ida e volta)" in body


# ---------- 2. economy confirmado ----------

def test_economy_confirmed_title():
    body = format_alert(_q(cabin=Cabin.ECONOMY), _DEC)
    assert "Econômica em promoção" in body
    assert "Business em promoção" not in body


# ---------- 3. cabin unknown nunca usa "Business" ----------

def test_unknown_cabin_never_business():
    body = format_alert(
        _q(cabin=Cabin.UNKNOWN, cabin_confirmed=False), _DEC
    )
    assert "Business em promoção" not in body
    assert "Econômica em promoção" not in body
    assert "⚠️ Cabine não confirmada — verificar" in body


def test_confirmed_flag_false_is_not_business():
    body = format_alert(_q(cabin=Cabin.BUSINESS, cabin_confirmed=False), _DEC)
    assert "Business em promoção" not in body
    assert "Cabine não confirmada" in body


# ---------- 4. round_trip data com seta ----------

def test_round_trip_dates_arrow():
    body = format_alert(_q(), _DEC)
    assert "📅 2026-06-09 → 2026-06-16" in body


# ---------- 5. one_way só data de ida ----------

def test_one_way_dates_no_arrow():
    body = format_alert(
        _q(trip_type=TripType.ONE_WAY, return_date=None), _DEC
    )
    assert "📅 2026-06-09" in body
    assert "→ 2026" not in body
    assert "(somente ida)" in body


def test_one_way_with_stray_return_date_still_no_arrow():
    # trip one_way mas return_date presente (não deveria) → sem seta
    body = format_alert(
        _q(trip_type=TripType.ONE_WAY, return_date="2026-06-16"), _DEC
    )
    assert "→ 2026-06-16" not in body


# ---------- 6/7. manual fallback one-way economy ----------

def test_manual_fallback_one_way_economy():
    body = format_alert(
        _q(
            source="manual_purchase",
            cabin=Cabin.ECONOMY,
            trip_type=TripType.ONE_WAY,
            return_date=None,
            deep_link=None,
        ),
        _DEC,
    )
    assert "Pesquise manualmente: GRU → CDG, 2026-06-09, econômica." in body
    assert "→ 2026" not in body
    assert "Econômica em promoção" in body
    assert "Links auxiliares de pesquisa, não oferta confirmada." in body


def test_manual_fallback_round_trip_business_uses_executiva():
    body = format_alert(_q(source="manual_purchase", deep_link=None), _DEC)
    assert (
        "Pesquise manualmente: GRU → CDG, 2026-06-09 → 2026-06-16, executiva."
        in body
    )


def test_manual_fallback_unknown_uses_cabine_nao_confirmada():
    body = format_alert(
        _q(source="manual_purchase", deep_link=None,
           cabin=Cabin.UNKNOWN, cabin_confirmed=False),
        _DEC,
    )
    assert "cabine não confirmada." in body


# ---------- 8/9. links auxiliares por cabine ----------

def test_aux_links_business_terms():
    q = _q(source="manual_purchase", deep_link=None)
    urls = [u for _, u in build_auxiliary_search_links(q)]
    for u in urls:
        assert "business" in u.lower()
        assert "economy" not in u.lower()


def test_aux_links_economy_terms():
    q = _q(source="manual_purchase", deep_link=None, cabin=Cabin.ECONOMY)
    urls = [u for _, u in build_auxiliary_search_links(q)]
    for u in urls:
        assert "economy" in u.lower()
        assert "business" not in u.lower()


def test_aux_links_one_way_omits_return():
    q = _q(
        source="manual_purchase", deep_link=None,
        trip_type=TripType.ONE_WAY, return_date="2026-06-16",
    )
    for _, u in build_auxiliary_search_links(q):
        assert "2026-06-16" not in u


# ---------- 10. alerta Kiwi com link não tem links auxiliares ----------

def test_kiwi_alert_no_auxiliary_links():
    body = format_alert(_q(), _DEC)  # source=kiwi, deep_link acionável
    assert "🔎 <a href=" in body
    assert "Conferir busca" in body
    assert "Pesquisar no Google" not in body
    assert "Pesquisar no Kayak" not in body


# ---------- 11. Aviasales sempre ausente ----------

def test_aviasales_never_present():
    for src, dl in (
        ("kiwi", "https://www.kiwi.com/deep/GRU-CDG-2026-06-09"),
        ("manual_purchase", None),
    ):
        body = format_alert(_q(source=src, deep_link=dl), _DEC)
        assert "aviasales" not in body.lower()


# ---------- 12. canonical_key não consumido no pipeline ----------

def test_canonical_key_not_consumed():
    import flight_mapper.notifier as n
    import flight_mapper.auxiliary_links as a
    import flight_mapper.formatting as f
    from pathlib import Path
    for mod in (n, a, f):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "canonical_key" not in src
        assert "get_history" not in src
        assert "resolve_history_key" not in src
