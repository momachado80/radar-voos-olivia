"""Testes do PR #84 — número do voo Duffel surfaceado no alerta e na URL.

A Duffel devolve `marketing_carrier_flight_number` em cada segmento. Antes
do PR, esse dado era jogado fora. Agora:
1. O parser (`parse_duffel_for_actionability`) extrai os voos do OUTBOUND
   slice e devolve `flight_numbers=("AF447", "KL1234")`.
2. O `Quote` propaga; o `DuffelProvider.quote_for_dates` repassa.
3. A linha 🛫 do alerta vira "Companhia: Air France — voo AF447" (ou
   "voos AF447 → KL1234" pra conexão). Sem voo, mantém PR #83.
4. A query do Google Flights apende o(s) voo(s) — Google interpreta e cai
   no voo exato em vez de "todos os voos da rota".

Cobre também: degradação silenciosa (segmento sem flight_number), return
slice é ignorado de propósito (basta o voo de ida na busca), nenhuma
informação sensível é exposta (voo é público).
"""

from __future__ import annotations

from urllib.parse import unquote_plus

from flight_mapper.actionability_readiness import parse_duffel_for_actionability
from flight_mapper.auxiliary_links import build_google_flights_query_url
from flight_mapper.google_flights_link import duffel_google_flights_url
from flight_mapper.notifier import format_alert
from flight_mapper.detector import Decision, CRITERION_CEILING, LEVEL_GOOD
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType


# ----------------- helpers de fixture inline -----------------


def _offer(*segments, return_segments=None, airline_owner="AF",
           total_amount="4000", total_currency="EUR"):
    slices = [{"segments": list(segments)}]
    if return_segments is not None:
        slices.append({"segments": list(return_segments)})
    return {"data": {"offers": [{
        "id": "off_xxx",
        "total_amount": total_amount, "total_currency": total_currency,
        "owner": {"iata_code": airline_owner},
        "slices": slices,
    }]}}


def _seg(*, carrier="AF", flight_number="447", cabin="business",
         departing_at="2026-09-02T22:30:00"):
    seg = {
        "departing_at": departing_at,
        "marketing_carrier": {"iata_code": carrier},
        "passengers": [{"cabin_class": cabin}],
    }
    if flight_number is not None:
        seg["marketing_carrier_flight_number"] = flight_number
    return seg


# ----------------- 1. parser extrai os voos -----------------


def test_parser_extracts_single_flight_number_for_direct_segment():
    r = parse_duffel_for_actionability(
        _offer(_seg(carrier="AF", flight_number="447")),
        route="GRU-CDG", requested_cabin="business",
    )
    assert r.flight_numbers == ("AF447",)


def test_parser_extracts_multiple_flight_numbers_for_connection():
    """Conexão: cada segmento vira um voo separado, na ordem do roteiro."""
    r = parse_duffel_for_actionability(
        _offer(
            _seg(carrier="AF", flight_number="447"),
            _seg(carrier="KL", flight_number="1234",
                 departing_at="2026-09-03T08:30:00"),
        ),
        route="GRU-CDG", requested_cabin="business",
    )
    assert r.flight_numbers == ("AF447", "KL1234")


def test_parser_skips_segments_without_flight_number():
    """Segmento sem `marketing_carrier_flight_number` é pulado — não vira
    "AF" / "AFNone" / placeholder. Degrada silenciosamente."""
    r = parse_duffel_for_actionability(
        _offer(
            _seg(carrier="AF", flight_number="447"),
            _seg(carrier="KL", flight_number=None,
                 departing_at="2026-09-03T08:30:00"),
        ),
        route="GRU-CDG", requested_cabin="business",
    )
    assert r.flight_numbers == ("AF447",)


def test_parser_returns_empty_tuple_when_no_flight_numbers_anywhere():
    """Payload antigo (sem o campo) ⇒ tupla vazia, sem crash."""
    r = parse_duffel_for_actionability(
        _offer(_seg(carrier="LA", flight_number=None)),
        route="GRU-MIA", requested_cabin="business",
    )
    assert r.flight_numbers == ()


def test_parser_ignores_return_slice_flight_numbers():
    """Só voos do OUTBOUND — o filtro de cabine cobre os dois sentidos na
    busca. Voos de volta na query só poluiriam o resultado."""
    r = parse_duffel_for_actionability(
        _offer(
            _seg(carrier="AF", flight_number="447"),
            return_segments=[_seg(
                carrier="AF", flight_number="448",
                departing_at="2026-09-12T10:00:00",
            )],
        ),
        route="GRU-CDG", requested_cabin="business",
    )
    assert r.flight_numbers == ("AF447",)
    assert "AF448" not in r.flight_numbers


# ----------------- 2. Quote propaga -----------------


def test_quote_flight_numbers_defaults_to_empty_tuple():
    """Compat: Quotes legados (sem o campo) seguem funcionando."""
    q = Quote(
        route=Route("GRU", "LHR", "Europa"),
        price_brl=1000.0, deep_link=None,
        departure_date="2026-09-02", return_date=None,
    )
    assert q.flight_numbers == ()


def test_quote_accepts_flight_numbers_tuple():
    q = Quote(
        route=Route("GRU", "LHR", "Europa"),
        price_brl=1000.0, deep_link=None,
        departure_date="2026-09-02", return_date=None,
        flight_numbers=("BA248", "BA9000"),
    )
    assert q.flight_numbers == ("BA248", "BA9000")


# ----------------- 3. URL builder apende o voo -----------------


def test_url_appends_flight_number_when_provided():
    route = Route("GRU", "CDG", "Europa")
    url = build_google_flights_query_url(
        route, "2026-09-02", "2026-09-12",
        airline_iata="AF", flight_numbers=("AF447",),
    )
    q = unquote_plus(url.split("q=", 1)[1])
    # Cia + voo aparecem após a cabine, na ordem cia→voo.
    assert q.endswith("on Air France AF447")


def test_url_appends_multiple_flight_numbers_in_order():
    route = Route("GRU", "CDG", "Europa")
    url = build_google_flights_query_url(
        route, "2026-09-02", "2026-09-12",
        airline_iata="AF", flight_numbers=("AF447", "KL1234"),
    )
    q = unquote_plus(url.split("q=", 1)[1])
    assert "AF447 KL1234" in q


def test_url_skips_flight_numbers_when_none_or_empty():
    route = Route("GRU", "CDG", "Europa")
    a = build_google_flights_query_url(
        route, "2026-09-02", "2026-09-12", airline_iata="AF",
    )
    b = build_google_flights_query_url(
        route, "2026-09-02", "2026-09-12", airline_iata="AF",
        flight_numbers=(),
    )
    c = build_google_flights_query_url(
        route, "2026-09-02", "2026-09-12", airline_iata="AF",
        flight_numbers=None,
    )
    assert unquote_plus(a) == unquote_plus(b) == unquote_plus(c)


# ----------------- 4. notifier mostra voo -----------------


def _quote(flight_numbers=()):
    return Quote(
        route=Route("GRU", "CDG", "Europa",
                    trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS),
        price_brl=12000.0, deep_link=None,
        departure_date="2026-09-02", return_date="2026-09-12",
        source="duffel", amount=2000.0, currency="EUR",
        amount_brl_estimated=12000.0, fx_rate=6.0,
        cabin=Cabin.BUSINESS, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline="AF",
        flight_numbers=flight_numbers,
    )


def _decision():
    return Decision(
        alert=True, reason="x", criterion=CRITERION_CEILING,
        threshold=14000.0, level=LEVEL_GOOD, score=85,
    )


def test_alert_shows_flight_number_when_known_single():
    msg = format_alert(_quote(flight_numbers=("AF447",)), _decision())
    assert "🛫 Companhia: Air France — voo AF447" in msg
    # NÃO repete o IATA entre parênteses (o voo já o contém).
    assert "Air France (AF)" not in msg


def test_alert_shows_flight_numbers_for_connection():
    msg = format_alert(
        _quote(flight_numbers=("AF447", "KL1234")), _decision(),
    )
    assert "🛫 Companhia: Air France — voos AF447 → KL1234" in msg


def test_alert_keeps_label_with_iata_when_no_flight_number():
    """Sem voo extraído ⇒ formato PR #83 preservado."""
    msg = format_alert(_quote(flight_numbers=()), _decision())
    assert "🛫 Companhia: Air France (AF)" in msg


# ----------------- 5. integração end-to-end (Duffel → URL) -----------------


def test_duffel_url_includes_flight_number_when_quote_carries_it():
    q = _quote(flight_numbers=("AF447",))
    url = duffel_google_flights_url(q)
    assert url is not None
    text = unquote_plus(url.split("q=", 1)[1])
    assert text.endswith("on Air France AF447")


def test_duffel_url_omits_flight_number_when_quote_empty():
    q = _quote(flight_numbers=())
    url = duffel_google_flights_url(q)
    text = unquote_plus(url.split("q=", 1)[1])
    # URL não tem flight_number, só o filtro de cia do PR #83.
    assert text.endswith("on Air France")


# ----------------- 6. sanitização: voo é público, sem dado sensível -----------------


def test_url_with_flight_number_never_contains_sensitive_fields():
    """Voo é informação pública (boarding pass etc.). A URL ainda não pode
    ter offer_id/preço/token/passageiro."""
    q = _quote(flight_numbers=("AF447",))
    url = duffel_google_flights_url(q)
    for sensitive in ("off_", "token", "passenger", "payload",
                      "2000", "12000", "EUR"):
        assert sensitive not in url, f"URL não pode conter {sensitive!r}: {url}"
