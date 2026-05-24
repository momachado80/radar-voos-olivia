"""PR #36 — Provider Readiness Pack.

Sem rede, sem Telegram. Smokes Amadeus/SerpApi rodam exclusivamente
com fixtures (--mock-file) e parsing puro.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flight_mapper.__main__ import main
from flight_mapper.config import Config
from flight_mapper.amadeus_client import (
    AmadeusAuthError,
    AmadeusClient,
    AmadeusError,
    parse_flight_offers,
    parse_offers_from_file,
)
from flight_mapper.provider_readiness import (
    audit_all,
    audit_amadeus,
    audit_kiwi,
    audit_serpapi,
    audit_travelpayouts,
    format_report,
    overall_recommendation,
)
from flight_mapper.regions import Cabin, TripType
from flight_mapper.serpapi_client import (
    KNOWN_BOOKING_FIELDS,
    SerpApiAuthError,
    SerpApiBookingOption,
    SerpApiClient,
    SerpApiError,
    _resolve_travel_class,
    audit_offer_fields,
    audit_trip_consistency,
    parse_booking_options,
    parse_booking_options_from_file,
    parse_search,
    parse_search_from_file,
    url_domain,
)
from flight_mapper.state import PriceStore


FIXTURE_AMADEUS = Path(__file__).parent / "fixtures" / "amadeus_business.json"
FIXTURE_SERPAPI = Path(__file__).parent / "fixtures" / "serpapi_google_flights.json"
FIXTURE_SERPAPI_BOOKING = (
    Path(__file__).parent / "fixtures" / "serpapi_booking_options.json"
)
FIXTURE_SERPAPI_MIXED = (
    Path(__file__).parent / "fixtures" / "serpapi_mixed_cabins.json"
)
FIXTURE_SERPAPI_ONLY_ECONOMY = (
    Path(__file__).parent / "fixtures" / "serpapi_only_economy.json"
)
FIXTURE_SERPAPI_AUDIT = (
    Path(__file__).parent / "fixtures" / "serpapi_offers_for_audit.json"
)
FIXTURE_SERPAPI_FIRST_HOP = (
    Path(__file__).parent / "fixtures" / "serpapi_first_hop_departure.json"
)
FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP = (
    Path(__file__).parent / "fixtures" / "serpapi_departure_followup.json"
)


def _workflows_with(secret_name: str, tmp_path: Path) -> Path:
    """Cria um workflows dir mínimo expondo apenas os secrets pedidos."""
    wf = tmp_path / "workflows"
    wf.mkdir()
    body_lines = ["name: x", "on: push", "jobs:", "  x:", "    runs-on: ubuntu-latest", "    steps:", "      - run: echo"]
    if secret_name:
        body_lines.append(f"    env:\n      {secret_name}: ${{{{ secrets.{secret_name} }}}}")
    (wf / "x.yml").write_text("\n".join(body_lines), encoding="utf-8")
    return wf


# ----------------- provider_readiness -----------------

def test_kiwi_audit_absent_recommends_configure(tmp_path: Path):
    wf = _workflows_with("KIWI_API_KEY", tmp_path)
    s = audit_kiwi(env={}, workflows_dir=wf, history={})
    assert s.configured is False
    assert s.workflow_exposes is True
    assert s.used_in_pipeline == "unused"
    assert s.history_count == 0
    assert "Configure `KIWI_API_KEY`" in s.recommendation


def test_kiwi_audit_present_but_no_history_flags_investigation(tmp_path: Path):
    wf = _workflows_with("KIWI_API_KEY", tmp_path)
    s = audit_kiwi(
        env={"KIWI_API_KEY": "x"}, workflows_dir=wf,
        history={"GRU-MIA-business": {"last_quote": {"source": "travelpayouts"}}},
    )
    assert s.configured is True
    assert s.history_count == 0
    assert s.used_in_pipeline == "primary"
    assert "401/403" in s.recommendation or "401/403" in "".join(s.notes)


def test_travelpayouts_audit_uses_field(tmp_path: Path):
    wf = _workflows_with("TRAVELPAYOUTS_TOKEN", tmp_path)
    hist = {
        "GRU-MIA-business": {"last_quote": {"source": "travelpayouts"}},
        "GRU-LHR-business": {"last_quote": {"source": "travelpayouts"}},
    }
    s = audit_travelpayouts(
        env={"TRAVELPAYOUTS_TOKEN": "x"}, workflows_dir=wf, history=hist,
    )
    assert s.configured is True
    assert s.history_count == 2
    # sem Kiwi → travelpayouts vira primary
    assert s.used_in_pipeline == "primary"


def test_amadeus_audit_pending_when_envs_missing(tmp_path: Path):
    wf = _workflows_with("KIWI_API_KEY", tmp_path)  # nada de amadeus
    s = audit_amadeus(env={}, workflows_dir=wf, history={})
    assert s.configured is False
    assert s.used_in_pipeline == "unused"
    assert "developers.amadeus.com" in s.recommendation
    assert "test env" in s.recommendation


def test_amadeus_audit_ready_when_both_envs_present(tmp_path: Path):
    wf = _workflows_with("AMADEUS_CLIENT_ID", tmp_path)
    # YAML cobre só ID; secret expose=False (precisa dos dois)
    s = audit_amadeus(
        env={"AMADEUS_CLIENT_ID": "x", "AMADEUS_CLIENT_SECRET": "y"},
        workflows_dir=wf, history={},
    )
    assert s.configured is True
    assert s.workflow_exposes is False  # falta o SECRET no YAML
    assert s.used_in_pipeline == "smoke_only"
    assert "amadeus-smoke" in s.recommendation


def test_serpapi_audit_pending_is_not_error(tmp_path: Path):
    s = audit_serpapi(env={}, workflows_dir=tmp_path, history={})
    assert s.configured is False
    assert s.used_in_pipeline == "unused"
    assert "Pendente" in s.recommendation


def test_format_report_does_not_reveal_secret_values(tmp_path: Path):
    wf = _workflows_with("KIWI_API_KEY", tmp_path)
    statuses = audit_all(
        env={
            "KIWI_API_KEY": "SECRET-KIWI-XYZ",
            "TRAVELPAYOUTS_TOKEN": "SECRET-TP-XYZ",
            "AMADEUS_CLIENT_ID": "SECRET-AM-ID",
            "AMADEUS_CLIENT_SECRET": "SECRET-AM-SK",
            "SERPAPI_API_KEY": "SECRET-SP",
        },
        workflows_dir=wf,
        history={"GRU-MIA-business": {"last_quote": {"source": "kiwi"}}},
    )
    txt = format_report(statuses)
    for secret_value in (
        "SECRET-KIWI-XYZ", "SECRET-TP-XYZ", "SECRET-AM-ID",
        "SECRET-AM-SK", "SECRET-SP",
    ):
        assert secret_value not in txt
    # mostra status sem valor
    assert "Kiwi (Tequila)" in txt
    assert "Amadeus (test env)" in txt
    assert "SerpApi (Google Flights)" in txt
    assert "Configurado: sim" in txt


def test_overall_recommendation_when_kiwi_active(tmp_path: Path):
    wf = _workflows_with("KIWI_API_KEY", tmp_path)
    st = audit_all(
        env={"KIWI_API_KEY": "x"}, workflows_dir=wf,
        history={"GRU-MIA-business": {"last_quote": {"source": "kiwi"}}},
    )
    assert "Kiwi ativo" in overall_recommendation(st)


def test_overall_recommendation_when_kiwi_configured_zero_history(tmp_path: Path):
    wf = _workflows_with("KIWI_API_KEY", tmp_path)
    st = audit_all(
        env={"KIWI_API_KEY": "x"}, workflows_dir=wf,
        history={"GRU-MIA-business": {"last_quote": {"source": "travelpayouts"}}},
    )
    rec = overall_recommendation(st)
    assert "Investigar logs do Actions" in rec


def test_overall_recommendation_when_amadeus_ready(tmp_path: Path):
    wf = _workflows_with("AMADEUS_CLIENT_ID", tmp_path)
    st = audit_all(
        env={"AMADEUS_CLIENT_ID": "x", "AMADEUS_CLIENT_SECRET": "y"},
        workflows_dir=wf, history={},
    )
    assert "Amadeus pronto" in overall_recommendation(st)


def test_overall_recommendation_when_nothing_configured(tmp_path: Path):
    st = audit_all(env={}, workflows_dir=tmp_path, history={})
    rec = overall_recommendation(st)
    assert "sinais brutos" in rec or "deal intelligence" in rec


# ----------------- Amadeus parsing (puro) -----------------

def test_amadeus_fixture_parses_business_confirmed():
    offers = parse_offers_from_file(str(FIXTURE_AMADEUS))
    assert len(offers) == 2
    o = offers[0]
    assert o.price_total == 1850.20
    assert o.currency == "USD"
    assert o.cabin is Cabin.BUSINESS
    assert o.cabin_confirmed is True
    assert o.cabin_raw == "BUSINESS"
    assert o.trip_type is TripType.ROUND_TRIP
    assert o.departure_date == "2026-09-10"
    assert o.return_date == "2026-09-17"
    assert o.carriers == ["LH"]


def test_amadeus_one_way_detected_from_itineraries():
    payload = json.loads(FIXTURE_AMADEUS.read_text(encoding="utf-8"))
    # remove segundo itinerary do primeiro offer → one_way
    payload["data"][0]["itineraries"] = payload["data"][0]["itineraries"][:1]
    payload["data"][0]["oneWay"] = True
    offers = parse_flight_offers(payload)
    assert offers[0].trip_type is TripType.ONE_WAY
    assert offers[0].return_date is None


def test_amadeus_mixed_cabin_is_not_confirmed():
    payload = json.loads(FIXTURE_AMADEUS.read_text(encoding="utf-8"))
    # quebra a cabine do segundo segmento
    payload["data"][0]["travelerPricings"][0]["fareDetailsBySegment"][1]["cabin"] = "ECONOMY"
    offers = parse_flight_offers(payload)
    o = offers[0]
    assert o.cabin is Cabin.UNKNOWN
    assert o.cabin_confirmed is False
    assert "MIXED" in o.cabin_raw


def test_amadeus_invalid_payload_raises():
    with pytest.raises(AmadeusError):
        parse_flight_offers("not a dict")  # type: ignore[arg-type]


def test_amadeus_empty_data_is_empty_list():
    assert parse_flight_offers({"data": []}) == []
    assert parse_flight_offers({"data": None}) == []  # type: ignore[arg-type]


def test_amadeus_client_constructor_validates_credentials():
    with pytest.raises(AmadeusAuthError):
        AmadeusClient("", "x")


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(req_payload: dict):
    return _FakeResp(json.dumps(req_payload).encode("utf-8"))


def test_amadeus_client_token_and_search(monkeypatch):
    import flight_mapper.amadeus_client as ac

    calls: list[str] = []
    fixture = json.loads(FIXTURE_AMADEUS.read_text(encoding="utf-8"))

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        calls.append(url)
        if "/v1/security/oauth2/token" in url:
            return _ok({"access_token": "TKN", "expires_in": 1799})
        return _ok(fixture)

    monkeypatch.setattr(ac, "urlopen", _fake_urlopen)
    client = AmadeusClient("id", "sk")
    offers = client.search_offers(
        origin="GRU", destination="MIA",
        departure_date="2026-09-10", return_date="2026-09-17",
        travel_class="BUSINESS",
    )
    assert len(offers) == 2
    assert offers[0].cabin_confirmed is True
    assert any("/v1/security/oauth2/token" in c for c in calls)
    assert any("/v2/shopping/flight-offers" in c for c in calls)


def test_amadeus_client_401_raises_auth_error(monkeypatch):
    import flight_mapper.amadeus_client as ac
    from urllib.error import HTTPError
    from io import BytesIO

    def _fake_urlopen(req, *a, **k):
        raise HTTPError(
            url=getattr(req, "full_url", ""),
            code=401, msg="Unauthorized", hdrs=None,
            fp=BytesIO(b'{"errors":[{"title":"Invalid credentials"}]}'),
        )

    monkeypatch.setattr(ac, "urlopen", _fake_urlopen)
    with pytest.raises(AmadeusAuthError):
        AmadeusClient("id", "sk").fetch_token()


# ----------------- SerpApi parsing (puro) -----------------

def test_serpapi_fixture_parses_offers():
    offers = parse_search_from_file(str(FIXTURE_SERPAPI))
    assert len(offers) == 2  # best + other
    best = offers[0]
    assert best.price == 1820.0
    assert best.currency == "USD"
    assert best.cabin is Cabin.BUSINESS
    assert best.trip_type is TripType.ROUND_TRIP
    assert best.booking_token == "BK_TOKEN_1"
    assert best.departure_date == "2026-09-10"
    assert best.return_date == "2026-09-17"
    assert "LATAM" in best.carriers


def test_serpapi_invalid_payload_raises():
    with pytest.raises(SerpApiError):
        parse_search("not dict")  # type: ignore[arg-type]


def test_serpapi_client_constructor_validates_key():
    with pytest.raises(SerpApiAuthError):
        SerpApiClient("")


def test_serpapi_client_search_uses_correct_url(monkeypatch):
    import flight_mapper.serpapi_client as sp
    fixture = json.loads(FIXTURE_SERPAPI.read_text(encoding="utf-8"))
    captured: dict = {}

    def _fake_urlopen(req, *a, **k):
        captured["url"] = getattr(req, "full_url", str(req))
        return _FakeResp(json.dumps(fixture).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    offers = SerpApiClient("KEY").search_google_flights(
        origin="GRU", destination="MIA",
        outbound_date="2026-09-10", return_date="2026-09-17",
        travel_class="business",
    )
    assert offers and offers[0].cabin is Cabin.BUSINESS
    assert "engine=google_flights" in captured["url"]
    assert "api_key=KEY" in captured["url"]
    assert "type=1" in captured["url"]  # round_trip
    # business → 3 (regressão do HTTP 400 "Unsupported '0' for travel class")
    assert "travel_class=3" in captured["url"]


# ---- _resolve_travel_class (cobertura completa do bug fix) ----

def test_resolve_travel_class_maps_business_to_3():
    assert _resolve_travel_class("business") == 3
    assert _resolve_travel_class("BUSINESS") == 3
    assert _resolve_travel_class("  Business  ") == 3


def test_resolve_travel_class_accepts_int_passthrough():
    assert _resolve_travel_class(3) == 3
    assert _resolve_travel_class(1) == 1
    assert _resolve_travel_class(4) == 4


def test_resolve_travel_class_other_cabins():
    assert _resolve_travel_class("economy") == 1
    assert _resolve_travel_class("premium_economy") == 2
    assert _resolve_travel_class("premium economy") == 2
    assert _resolve_travel_class("premiumeconomy") == 2
    assert _resolve_travel_class("first") == 4


def test_resolve_travel_class_rejects_invalid_string():
    with pytest.raises(SerpApiError):
        _resolve_travel_class("xyz")
    with pytest.raises(SerpApiError):
        _resolve_travel_class("")


def test_resolve_travel_class_rejects_invalid_int():
    with pytest.raises(SerpApiError):
        _resolve_travel_class(0)
    with pytest.raises(SerpApiError):
        _resolve_travel_class(5)
    with pytest.raises(SerpApiError):
        _resolve_travel_class(99)


def test_serpapi_client_403_raises_auth_error(monkeypatch):
    import flight_mapper.serpapi_client as sp
    from urllib.error import HTTPError
    from io import BytesIO

    def _fake_urlopen(req, *a, **k):
        raise HTTPError(
            url=getattr(req, "full_url", ""), code=403,
            msg="Forbidden", hdrs=None, fp=BytesIO(b"{}"),
        )

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    with pytest.raises(SerpApiAuthError):
        SerpApiClient("KEY").search_google_flights(
            origin="GRU", destination="MIA", outbound_date="2026-09-10",
        )


# ----------------- CLI (offline) -----------------

def _safe_config(monkeypatch, tmp_path: Path):
    fake = Config(
        telegram_bot_token=None, telegram_chat_id=None,
        travelpayouts_token=None, kiwi_api_key=None, data_dir=tmp_path,
    )
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: fake))


def test_cli_provider_readiness_smoke(tmp_path, monkeypatch, capsys):
    _safe_config(monkeypatch, tmp_path)
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AMADEUS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    rc = main(["provider-readiness"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "🔌 Provider readiness" in out
    assert "Kiwi (Tequila)" in out
    assert "Amadeus (test env)" in out
    assert "SerpApi (Google Flights)" in out
    assert "Recomendação geral" in out


def test_cli_amadeus_smoke_with_fixture(capsys):
    rc = main([
        "amadeus-smoke",
        "--route", "GRU-MIA",
        "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_AMADEUS),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Amadeus smoke (fixture)" in out
    assert "USD 1850.20" in out
    assert "cabin=business" in out  # já normalizado p/ Cabin enum
    assert "confirmed=True" in out


def test_cli_serpapi_smoke_with_fixture(capsys):
    rc = main([
        "serpapi-smoke",
        "--route", "GRU-MIA",
        "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SerpApi smoke (fixture)" in out
    assert "USD 1820.00" in out
    assert "booking_token=sim" in out


def test_cli_amadeus_smoke_without_mock_and_no_env_is_graceful(
    tmp_path, monkeypatch, capsys
):
    _safe_config(monkeypatch, tmp_path)
    monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AMADEUS_CLIENT_SECRET", raising=False)
    rc = main(["amadeus-smoke", "--route", "GRU-MIA"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ausentes" in out or "Use --mock-file" in out


def test_cli_serpapi_smoke_without_mock_and_no_env_is_graceful(
    tmp_path, monkeypatch, capsys
):
    _safe_config(monkeypatch, tmp_path)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    rc = main(["serpapi-smoke", "--route", "GRU-MIA"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ausente" in out


# ----------------- SerpApi booking options (PR #40) -----------------


def test_booking_options_fixture_parses_provider_and_url():
    options = parse_booking_options_from_file(str(FIXTURE_SERPAPI_BOOKING))
    assert len(options) == 3
    first = options[0]
    assert first.provider == "Latam Airlines"
    assert first.provider_raw == "Latam Airlines"
    assert first.booking_url == "https://www.latam.com/checkout?token=abc"
    assert first.has_post_data is True
    assert isinstance(first, SerpApiBookingOption)


def test_booking_options_fixture_parses_price_and_currency():
    options = parse_booking_options_from_file(str(FIXTURE_SERPAPI_BOOKING))
    assert options[0].price == 1820.0
    assert options[0].currency == "USD"
    assert options[1].price == 1855.0
    assert options[2].price == 1900.0


def test_booking_options_handles_missing_url_without_crash():
    options = parse_booking_options_from_file(str(FIXTURE_SERPAPI_BOOKING))
    third = options[2]
    assert third.provider_raw == "ProviderSemURL"
    assert third.booking_url is None
    assert third.has_post_data is False


def test_booking_options_invalid_payload_raises():
    with pytest.raises(SerpApiError):
        parse_booking_options("not dict")  # type: ignore[arg-type]


def test_booking_options_empty_payload_returns_empty_list():
    assert parse_booking_options({}) == []
    assert parse_booking_options({"booking_options": []}) == []


def test_fetch_booking_options_requires_token():
    client = SerpApiClient("KEY")
    with pytest.raises(SerpApiError):
        client.fetch_booking_options(
            booking_token="",
            departure_id="GRU", arrival_id="MIA",
            outbound_date="2026-09-10", return_date="2026-09-17",
        )


def test_fetch_booking_options_uses_correct_url(monkeypatch):
    import flight_mapper.serpapi_client as sp
    fixture = json.loads(FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8"))
    captured: dict = {}

    def _fake_urlopen(req, *a, **k):
        captured["url"] = getattr(req, "full_url", str(req))
        return _FakeResp(json.dumps(fixture).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    options = SerpApiClient("KEY").fetch_booking_options(
        booking_token="BK_TOKEN_1",
        departure_id="GRU", arrival_id="MIA",
        outbound_date="2026-09-10", return_date="2026-09-17",
        travel_class="business",
    )
    assert len(options) == 3
    assert "engine=google_flights" in captured["url"]
    assert "booking_token=BK_TOKEN_1" in captured["url"]
    assert "travel_class=3" in captured["url"]
    assert "type=1" in captured["url"]  # round_trip pelo return_date


def test_url_domain_extraction():
    assert url_domain("https://www.latam.com/x?y=1") == "www.latam.com"
    assert url_domain("https://gflights.kissandfly.com/book") == (
        "gflights.kissandfly.com"
    )
    assert url_domain(None) is None
    assert url_domain("") is None


# ----------------- audit_trip_consistency (PR #40) -----------------


def test_audit_trip_consistency_round_trip_round_payload_ok():
    payload = {
        "search_parameters": {"type": "1"},
        "best_flights": [{"type": "Round trip"}],
    }
    assert audit_trip_consistency(TripType.ROUND_TRIP, payload) is None


def test_audit_trip_consistency_round_request_but_one_way_offers():
    payload = {
        "search_parameters": {"type": 1},
        "best_flights": [{"type": "One way"}, {"type": "One way"}],
    }
    assert (
        audit_trip_consistency(TripType.ROUND_TRIP, payload)
        == "payload_trip_inconclusive"
    )


def test_audit_trip_consistency_sp_type_diverges_from_request():
    payload = {"search_parameters": {"type": "2"}}  # one_way
    assert (
        audit_trip_consistency(TripType.ROUND_TRIP, payload)
        == "payload_trip_inconclusive"
    )


def test_audit_trip_consistency_one_way_request_round_payload():
    payload = {"search_parameters": {"type": "1"}}
    assert (
        audit_trip_consistency(TripType.ONE_WAY, payload)
        == "payload_trip_inconclusive"
    )


def test_audit_trip_consistency_handles_int_type():
    """SerpApi pode devolver type como inteiro 1/2; helper precisa
    tratar sem TypeError."""
    payload = {"search_parameters": {"type": 1}}
    assert audit_trip_consistency(TripType.ROUND_TRIP, payload) is None


def test_audit_trip_consistency_no_signal_returns_none():
    """Payload sem `type` em search_parameters nem nos offers não dá
    base p/ divergência."""
    assert audit_trip_consistency(TripType.ROUND_TRIP, {}) is None


# ----------------- CLI: serpapi-booking-options (PR #40) -----------------


def test_cli_serpapi_booking_options_with_mock_file(capsys):
    rc = main([
        "serpapi-booking-options",
        "--mock-file", str(FIXTURE_SERPAPI_BOOKING),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "booking options (fixture)" in out
    assert "Latam Airlines" in out
    assert "www.latam.com" in out
    assert "sem URL clicável" in out  # 3a opção da fixture


def test_cli_serpapi_booking_options_requires_token_in_live_mode(
    tmp_path, monkeypatch, capsys
):
    _safe_config(monkeypatch, tmp_path)
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_KEY_NO_NETWORK")
    rc = main(["serpapi-booking-options"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "--booking-token" in out


def test_cli_serpapi_booking_options_without_env_is_graceful(
    tmp_path, monkeypatch, capsys
):
    _safe_config(monkeypatch, tmp_path)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    rc = main([
        "serpapi-booking-options",
        "--booking-token", "BK_TOKEN_1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ausente" in out


# ----------------- CLI: serpapi-smoke --fetch-booking-options (PR #40) -----


def test_cli_serpapi_smoke_with_fetch_flag_in_fixture_mode_is_noop(capsys):
    """Em modo fixture, o flag --fetch-booking-options NÃO faz rede —
    aponta para serpapi-booking-options."""
    rc = main([
        "serpapi-smoke",
        "--route", "GRU-MIA",
        "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI),
        "--fetch-booking-options",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fetch-booking-options ignorado em modo fixture" in out


def test_cli_serpapi_smoke_reports_request_type_and_payload_trip(capsys):
    rc = main([
        "serpapi-smoke",
        "--route", "GRU-MIA",
        "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "request trip: round_trip/type=1" in out
    assert "payload trip: round_trip" in out


def test_cli_serpapi_smoke_max_booking_options_default_is_one(monkeypatch):
    """Verifica via parser que o default de --max-booking-options é 1
    (não vai sair varrendo as 11 ofertas)."""
    from flight_mapper.__main__ import main as _main
    import argparse as _ap
    captured: dict = {}

    real_parser_init = _ap.ArgumentParser.parse_args

    def _intercept(self, argv=None):
        ns = real_parser_init(self, argv)
        captured["ns"] = ns
        return ns

    monkeypatch.setattr(_ap.ArgumentParser, "parse_args", _intercept)
    rc = _main([
        "serpapi-smoke",
        "--route", "GRU-MIA",
        "--mock-file", str(FIXTURE_SERPAPI),
    ])
    assert rc == 0
    ns = captured["ns"]
    assert ns.max_booking_options == 1
    assert ns.fetch_booking_options is False


def test_cli_serpapi_smoke_live_caps_booking_options_to_max(monkeypatch):
    """Mesmo com SERPAPI_API_KEY setado e 3 offers com booking_token,
    --max-booking-options=1 só dispara 1 fetch."""
    import flight_mapper.serpapi_client as sp
    from flight_mapper.__main__ import main as _main

    search_payload = json.loads(FIXTURE_SERPAPI.read_text(encoding="utf-8"))
    booking_payload = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    calls: dict = {"search": 0, "booking": 0}

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            calls["booking"] += 1
            return _FakeResp(json.dumps(booking_payload).encode("utf-8"))
        calls["search"] += 1
        return _FakeResp(json.dumps(search_payload).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_KEY_NO_NETWORK")
    rc = _main([
        "serpapi-smoke",
        "--route", "GRU-MIA",
        "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10",
        "--return-date", "2026-09-17",
        "--fetch-booking-options",
    ])
    assert rc == 0
    assert calls["search"] == 1
    # default --max-booking-options=1 → 1 fetch só, mesmo com 2 offers
    assert calls["booking"] == 1


# ----------------- PR #42: cabin-aware booking_token selector + log refinements -----------------


def _live_smoke_with_payload(monkeypatch, search_payload: dict,
                              booking_payload: dict | None = None):
    """Helper p/ rodar `serpapi-smoke` em modo live com payloads mockados.
    Não faz rede (urlopen monkeypatched)."""
    import flight_mapper.serpapi_client as sp
    calls: dict = {"search": 0, "booking": 0, "tokens": []}

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            calls["booking"] += 1
            # extrai token da URL (rastreável p/ asserts)
            for part in url.split("&"):
                if part.startswith("booking_token="):
                    calls["tokens"].append(part.split("=", 1)[1])
            payload_to_use = booking_payload or {"booking_options": []}
            return _FakeResp(json.dumps(payload_to_use).encode("utf-8"))
        calls["search"] += 1
        return _FakeResp(json.dumps(search_payload).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_KEY_NO_NETWORK")
    return calls


def test_smoke_business_skips_first_economy_offer(monkeypatch, capsys):
    """Bug observado no smoke real: 1ª oferta veio economy mesmo em
    busca business; expandiu booking_token errado. Agora o seletor
    pula a economy e expande o 1º business."""
    search = json.loads(FIXTURE_SERPAPI_MIXED.read_text(encoding="utf-8"))
    booking = json.loads(FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8"))
    calls = _live_smoke_with_payload(monkeypatch, search, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-booking-options",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # expandiu COPA (oferta #2), não BoA (oferta #1 economy)
    assert "expandindo booking_token da oferta #2" in out
    assert "cabin=business" in out
    assert "carriers=COPA" in out
    assert calls["booking"] == 1
    assert calls["tokens"] == ["BK_TOKEN_BIZ_2"]
    # token economy NÃO foi usado
    assert "BK_TOKEN_ECON_FIRST" not in calls["tokens"]


def test_smoke_business_with_only_economy_skips_expansion(
    monkeypatch, capsys
):
    """Se nenhuma oferta business tem booking_token, não expande nada
    e imprime mensagem honesta."""
    search = json.loads(
        FIXTURE_SERPAPI_ONLY_ECONOMY.read_text(encoding="utf-8")
    )
    calls = _live_smoke_with_payload(monkeypatch, search)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-booking-options",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "nenhuma oferta com cabine confirmada compatível "
        "para expandir booking_token"
    ) in out
    assert calls["booking"] == 0
    assert calls["tokens"] == []


def test_smoke_economy_search_picks_first_economy(monkeypatch, capsys):
    """Simetria: busca econômica deve expandir 1º economy, não business."""
    search = json.loads(FIXTURE_SERPAPI_MIXED.read_text(encoding="utf-8"))
    booking = json.loads(FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8"))
    calls = _live_smoke_with_payload(monkeypatch, search, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "one_way",
        "--cabin", "economy",
        "--departure", "2026-09-10",
        "--fetch-booking-options",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "expandindo booking_token da oferta #1" in out
    assert "cabin=economy" in out
    assert calls["tokens"] == ["BK_TOKEN_ECON_FIRST"]


def test_booking_options_post_marked_as_not_simple_hyperlink(capsys):
    """Booking option com booking_request.post_data deve ser rotulada
    como 'POST — não é hyperlink simples' (visível p/ humano e
    pesquisável no log)."""
    rc = main([
        "serpapi-booking-options",
        "--mock-file", str(FIXTURE_SERPAPI_BOOKING),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # 1ª opção da fixture: Latam com post_data
    assert "POST — não é hyperlink simples" in out


def test_booking_options_simple_url_marked_as_link_simples(capsys):
    """Booking option só com URL (sem post_data) deve ser rotulada
    como 'link simples'."""
    rc = main([
        "serpapi-booking-options",
        "--mock-file", str(FIXTURE_SERPAPI_BOOKING),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # 2ª opção da fixture: Kissandfly só URL (sem post_data)
    assert "link simples" in out
    assert "Kissandfly" in out


def test_smoke_log_trip_status_inconclusive_when_payload_diverges(capsys):
    """request=round_trip + payload one_way deve gerar bloco:
    request trip / payload trip / status: trip inconclusivo..."""
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI_MIXED),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "request trip: round_trip/type=1" in out
    assert "payload trip: one_way/type=2" in out
    assert (
        "status: trip inconclusivo, não integrar ao alerta ainda" in out
    )


def test_smoke_log_no_status_line_when_trip_consistent(capsys):
    """Quando request e payload concordam (round/round), a linha
    `status: trip inconclusivo` NÃO deve aparecer."""
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI),  # type=1, payload round_trip
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "request trip: round_trip/type=1" in out
    assert "payload trip: round_trip" in out
    assert "trip inconclusivo" not in out


def test_select_expansion_target_helper_business():
    from flight_mapper.__main__ import _select_expansion_target
    from flight_mapper.serpapi_client import parse_search_from_file
    offers = parse_search_from_file(str(FIXTURE_SERPAPI_MIXED))
    tgt = _select_expansion_target(offers, "business")
    assert tgt is not None
    assert tgt.cabin.value == "business"
    assert tgt.booking_token == "BK_TOKEN_BIZ_2"
    assert "COPA" in tgt.carriers


def test_select_expansion_target_helper_returns_none_when_no_match():
    from flight_mapper.__main__ import _select_expansion_target
    from flight_mapper.serpapi_client import parse_search_from_file
    offers = parse_search_from_file(str(FIXTURE_SERPAPI_ONLY_ECONOMY))
    assert _select_expansion_target(offers, "business") is None
    # mas economy bate
    assert _select_expansion_target(offers, "economy") is not None


# ----------------- PR #44: audit_offer_fields + --debug-booking-fields -----------------


def test_audit_detects_top_level_booking_token():
    """1ª oferta da fixture de auditoria tem booking_token top-level."""
    payload = json.loads(FIXTURE_SERPAPI_AUDIT.read_text(encoding="utf-8"))
    offer = payload["best_flights"][0]
    audit = audit_offer_fields(offer)
    assert "booking_token" in audit["top_level_keys"]
    assert audit["fields"]["booking_token"]["present"] is True
    assert audit["fields"]["booking_token"]["kind"] == "str"
    # Length presente, mas valor NUNCA aparece
    assert "length" in audit["fields"]["booking_token"]
    assert "value" not in audit["fields"]["booking_token"]
    assert "preview" not in audit["fields"]["booking_token"]


def test_audit_detects_nested_booking_request_inner_keys():
    """2ª oferta: booking_request é dict; auditoria mostra inner_keys."""
    payload = json.loads(FIXTURE_SERPAPI_AUDIT.read_text(encoding="utf-8"))
    offer = payload["best_flights"][1]
    audit = audit_offer_fields(offer)
    info = audit["fields"]["booking_request"]
    assert info["present"] is True
    assert info["kind"] == "dict"
    assert info["inner_keys"] == ["method", "post_data", "url"]
    # booking_token deve estar ausente nessa oferta
    assert audit["fields"]["booking_token"]["present"] is False
    # Sub-campos seguros extraídos do dict (PR #45):
    assert info["domain"] == "www.google.com"
    assert info["method"] == "POST"
    assert info["post_data_present"] is True
    # Defesa: conteúdo de post_data NUNCA aparece no audit
    serialized = json.dumps(audit)
    assert "secret_post_body" not in serialized
    assert "secret_value" not in serialized


def test_audit_url_only_domain_no_full_url():
    """3ª oferta tem url e link como URLs completas. Audit deve mostrar
    apenas o domínio — NUNCA a URL completa (defesa de log)."""
    payload = json.loads(FIXTURE_SERPAPI_AUDIT.read_text(encoding="utf-8"))
    offer = payload["best_flights"][2]
    audit = audit_offer_fields(offer)
    url_info = audit["fields"]["url"]
    link_info = audit["fields"]["link"]
    assert url_info == {"present": True, "kind": "url", "domain": "www.aa.com"}
    assert link_info == {
        "present": True, "kind": "url", "domain": "booking.aa.com",
    }
    # Garantia: nenhum payload sensível vazou
    serialized = json.dumps(audit)
    assert "secret_path_here" not in serialized
    assert "secret_cart_id" not in serialized
    assert "checkout" not in serialized
    assert "?ref=" not in serialized


def test_audit_absent_fields_are_marked_explicitly():
    """4ª oferta não tem nenhum campo de booking. Audit imprime
    `present: False` para todos os campos conhecidos."""
    payload = json.loads(FIXTURE_SERPAPI_AUDIT.read_text(encoding="utf-8"))
    offer = payload["best_flights"][3]
    audit = audit_offer_fields(offer)
    for field in KNOWN_BOOKING_FIELDS:
        assert audit["fields"][field] == {"present": False}, field


def test_audit_handles_non_dict_input():
    """Robustez: payload bruto inválido não pode crashar."""
    assert audit_offer_fields(None) == {"top_level_keys": [], "fields": {}}
    assert audit_offer_fields("string") == {"top_level_keys": [], "fields": {}}


def test_audit_handles_list_field():
    """Field como list deve mostrar `kind=list, len=N` e
    first_inner_keys se 1º item for dict."""
    offer = {"booking_options": [{"a": 1, "b": 2}, {"c": 3}]}
    audit = audit_offer_fields(offer)
    info = audit["fields"]["booking_options"]
    assert info["present"] is True
    assert info["kind"] == "list"
    assert info["len"] == 2
    assert info["first_inner_keys"] == ["a", "b"]


def test_audit_handles_list_field_first_not_dict():
    """Se o 1º item não é dict, first_inner_keys NÃO aparece."""
    offer = {"booking_options": ["a", "b", "c"]}
    audit = audit_offer_fields(offer)
    info = audit["fields"]["booking_options"]
    assert info["present"] is True
    assert info["kind"] == "list"
    assert info["len"] == 3
    assert "first_inner_keys" not in info


def test_audit_dict_extracts_url_domain_only():
    """Dict com sub-campo url: só domínio entra no audit, URL completa
    nunca."""
    offer = {
        "booking_request": {
            "url": "https://www.google.com/travel/clk/redirect?token=secret_xyz",
        },
    }
    audit = audit_offer_fields(offer)
    info = audit["fields"]["booking_request"]
    assert info["kind"] == "dict"
    assert info["domain"] == "www.google.com"
    # NUNCA URL completa nem path nem query
    serialized = json.dumps(audit)
    assert "secret_xyz" not in serialized
    assert "redirect" not in serialized
    assert "?token" not in serialized


def test_audit_dict_extracts_method():
    """Dict com sub-campo `method` (curto, não-sensível) é exposto."""
    offer = {"booking_request": {"method": "POST"}}
    audit = audit_offer_fields(offer)
    info = audit["fields"]["booking_request"]
    assert info["method"] == "POST"


def test_audit_dict_post_data_present_never_leaks_value():
    """Dict com `post_data`: audit só marca presença, NUNCA conteúdo."""
    offer = {
        "booking_request": {
            "post_data": "secret_form_body=secret_value&other=secret_param",
        },
    }
    audit = audit_offer_fields(offer)
    info = audit["fields"]["booking_request"]
    assert info["post_data_present"] is True
    serialized = json.dumps(audit)
    assert "secret_form_body" not in serialized
    assert "secret_value" not in serialized
    assert "secret_param" not in serialized
    # Apenas a flag booleana entra na saída
    assert "post_data" in info.get("inner_keys", [])


def test_audit_dict_post_data_empty_does_not_set_flag():
    """post_data vazio (string vazia / None) não marca presença."""
    audit_empty = audit_offer_fields({"booking_request": {"post_data": ""}})
    assert "post_data_present" not in audit_empty["fields"]["booking_request"]
    audit_none = audit_offer_fields({"booking_request": {"post_data": None}})
    assert "post_data_present" not in audit_none["fields"]["booking_request"]


def test_audit_never_leaks_token_value():
    """Defesa explícita: token longo nunca aparece no audit, em
    nenhuma representação (preview, value, slice)."""
    secret = "BK_TOKEN_SHOULD_NEVER_BE_LOGGED_xyz1234567890"
    offer = {
        "booking_token": secret,
        "departure_token": secret,
        "search_token": secret,
        "token": secret,
    }
    audit = audit_offer_fields(offer)
    serialized = json.dumps(audit)
    assert "SHOULD_NEVER_BE_LOGGED" not in serialized
    assert secret not in serialized
    for fname in ("booking_token", "departure_token", "search_token", "token"):
        assert audit["fields"][fname]["kind"] == "str"
        assert audit["fields"][fname]["length"] == len(secret)


def test_cli_serpapi_smoke_debug_booking_fields(capsys):
    """CLI: --debug-booking-fields imprime auditoria sem rede."""
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI_AUDIT),
        "--debug-booking-fields",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "debug-booking-fields: auditoria read-only" in out
    assert "oferta #1" in out
    # 1ª oferta: booking_token presente — só length
    assert "booking_token: type=str, length=" in out
    # 2ª oferta: booking_request dict + sub-campos
    assert "booking_request: type=dict, inner_keys=" in out
    assert "domínio=www.google.com" in out
    assert "method=POST" in out
    assert "post_data_presente=True" in out
    # 3ª oferta: url/link só domínio
    assert "url: domínio=www.aa.com" in out
    assert "link: domínio=booking.aa.com" in out
    # 4ª oferta: todos ausentes — usa o atalho consolidado
    assert "todos os campos de booking auditados: ausentes" in out
    # 5ª oferta: booking_options como list
    assert "booking_options: type=list, length=2" in out
    assert "first_inner_keys=" in out
    # Nenhum valor sensível leakado em nenhuma oferta
    assert "secret_path_here" not in out
    assert "secret_cart_id" not in out
    assert "secret_payload_here" not in out
    assert "secret_post_body" not in out
    # Defesa: nenhuma URL completa nem post_data raw no log
    assert "https://" not in out
    assert "?secret_path" not in out
    assert "param=value" not in out


def test_cli_debug_booking_fields_does_not_trigger_booking_options(
    monkeypatch, capsys
):
    """Em live mode, --debug-booking-fields sozinho NÃO chama
    fetch_booking_options — auditoria é independente da expansão."""
    import flight_mapper.serpapi_client as sp
    payload = json.loads(FIXTURE_SERPAPI_AUDIT.read_text(encoding="utf-8"))
    calls: dict = {"search": 0, "booking": 0}

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            calls["booking"] += 1
        else:
            calls["search"] += 1
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_KEY_NO_NETWORK")
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--debug-booking-fields",  # SOZINHO, sem --fetch-booking-options
    ])
    assert rc == 0
    assert calls["search"] == 1
    assert calls["booking"] == 0  # NÃO chamou booking options
    out = capsys.readouterr().out
    assert "debug-booking-fields" in out


def test_cli_no_debug_flag_no_audit_output(capsys):
    """Sem --debug-booking-fields, NÃO imprime bloco de auditoria."""
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI_AUDIT),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "debug-booking-fields" not in out
    assert "auditoria read-only" not in out


# ----------------- PR #46: departure_token follow-up (2º hop) -----------------


# Strings sentinela usadas pelas fixtures p/ detectar leak. Qualquer
# delas no stdout do CLI é falha imediata.
_LEAK_SENTINELS = (
    "DEP_TOKEN_THAT_MUST_NEVER_LEAK",
    "BK_TOKEN_THAT_MUST_NEVER_LEAK",
    "BR_secret_payload",
    "BR_secret_post_body",
    "BR_secret_param",
    "BR_secret_ref",
    "BR_secret_cart_value",
    "BR_secret_iberia_path",
)
_LEAK_URL_FRAGMENTS = (
    "https://",
    "?token=",
    "?ref=",
    "?cart=",
    "redirect",
    "checkout",
)


def _live_followup_smoke(monkeypatch, first_hop: dict, followup: dict):
    """Helper que injeta urlopen mockado: 1ª chamada = first_hop;
    chamadas com departure_token na URL = followup."""
    import flight_mapper.serpapi_client as sp
    calls: dict = {"search": 0, "followup": 0, "tokens": []}

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "departure_token=" in url:
            calls["followup"] += 1
            for part in url.split("&"):
                if part.startswith("departure_token="):
                    calls["tokens"].append(part.split("=", 1)[1])
            return _FakeResp(json.dumps(followup).encode("utf-8"))
        calls["search"] += 1
        return _FakeResp(json.dumps(first_hop).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_KEY_NO_NETWORK")
    return calls


def test_select_departure_followup_targets_filters_by_cabin():
    """1ª oferta business (LATAM) é elegível; economy (BoA) é pulada;
    business COPA é elegível em 2º; AA em 3º."""
    from flight_mapper.__main__ import _select_departure_followup_targets
    from flight_mapper.serpapi_client import parse_search_from_file
    offers = parse_search_from_file(str(FIXTURE_SERPAPI_FIRST_HOP))
    selected = _select_departure_followup_targets(offers, "business", 3)
    assert [o.carriers[0] for o in selected] == ["LATAM", "COPA", "American"]
    # economy NÃO entra
    assert all("Boliviana" not in o.carriers[0] for o in selected)


def test_select_departure_followup_targets_respects_max():
    from flight_mapper.__main__ import _select_departure_followup_targets
    from flight_mapper.serpapi_client import parse_search_from_file
    offers = parse_search_from_file(str(FIXTURE_SERPAPI_FIRST_HOP))
    assert len(_select_departure_followup_targets(offers, "business", 1)) == 1
    assert len(_select_departure_followup_targets(offers, "business", 2)) == 2
    # Cap a 3: mesmo pedindo 99, retorna no máximo 3
    assert len(_select_departure_followup_targets(offers, "business", 99)) == 3


def test_select_departure_followup_targets_empty_if_no_compat():
    """Se nenhuma oferta tem cabine compatível, retorna []."""
    from flight_mapper.__main__ import _select_departure_followup_targets
    from flight_mapper.serpapi_client import parse_search_from_file
    offers = parse_search_from_file(str(FIXTURE_SERPAPI_FIRST_HOP))
    assert _select_departure_followup_targets(offers, "first", 3) == []


def test_fetch_departure_followup_requires_token():
    client = SerpApiClient("KEY")
    with pytest.raises(SerpApiError):
        client.fetch_departure_followup(
            departure_token="",
            departure_id="GRU", arrival_id="MIA",
            outbound_date="2026-09-10", return_date="2026-09-17",
        )


def test_fetch_departure_followup_uses_correct_url(monkeypatch):
    """Chamada com departure_token: URL contém departure_token=...,
    type=1 (round-trip pelo return_date), travel_class=3."""
    import flight_mapper.serpapi_client as sp
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    captured: dict = {}

    def _fake_urlopen(req, *a, **k):
        captured["url"] = getattr(req, "full_url", str(req))
        return _FakeResp(json.dumps(followup).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    offers = SerpApiClient("KEY").fetch_departure_followup(
        departure_token="DEP_TKN_ABC", departure_id="GRU",
        arrival_id="MIA", outbound_date="2026-09-10",
        return_date="2026-09-17", travel_class="business",
    )
    assert len(offers) == 5  # 2 best + 3 other
    assert "engine=google_flights" in captured["url"]
    assert "departure_token=DEP_TKN_ABC" in captured["url"]
    assert "type=1" in captured["url"]
    assert "travel_class=3" in captured["url"]


def test_cli_smoke_departure_followup_outputs_block(monkeypatch, capsys):
    """Live mode com flag: bloco 🧭 aparece com return_offer audits."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    calls = _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup",
        "--max-departure-followups", "2",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Cabeçalho do bloco
    assert "🧭 departure_token follow-up" in out
    assert "2 offer(s) selecionada(s) (limite=2)" in out
    # 2 followups feitos (LATAM + COPA business; BoA economy pulado)
    assert calls["followup"] == 2
    # Auditoria do payload de volta: vários formatos cobertos
    assert "return_offer #1" in out
    assert "booking_token: type=str, length=" in out
    assert (
        "booking_request: type=dict, inner_keys=['method', 'post_data', 'url'], "
        "domínio=www.google.com, method=POST, post_data_presente=True"
    ) in out
    assert "url: domínio=www.aa.com" in out
    assert "link: domínio=booking.aa.com" in out
    assert "booking_options: type=list, length=2" in out
    assert "todos os campos de booking auditados: ausentes" in out


def test_cli_smoke_departure_followup_caps_to_one(monkeypatch, capsys):
    """Sem --max-departure-followups (default 1) só dispara 1 chamada
    mesmo havendo 3 business com departure_token."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    calls = _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup",
    ])
    assert rc == 0
    assert calls["followup"] == 1  # default cap = 1


def test_cli_smoke_departure_followup_flag_off_no_call(monkeypatch, capsys):
    """Sem --fetch-departure-token-followup, ZERO chamadas de 2º hop."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    calls = _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
    ])
    assert rc == 0
    assert calls["followup"] == 0
    out = capsys.readouterr().out
    assert "🧭 departure_token follow-up" not in out


def test_cli_smoke_departure_followup_only_compatible_cabin_makes_call(
    monkeypatch, capsys,
):
    """Pedido com --cabin first NÃO encontra business compat → 0 chamadas
    de 2º hop + mensagem honesta."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    calls = _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "first",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup",
        "--max-departure-followups", "3",
    ])
    assert rc == 0
    assert calls["followup"] == 0
    out = capsys.readouterr().out
    assert (
        "nenhuma oferta com cabine confirmada compatível E "
        "departure_token para 2º hop"
    ) in out


def test_cli_smoke_departure_followup_no_secret_leak(monkeypatch, capsys):
    """Defesa explícita: NENHUMA das sentinelas da fixture aparece no
    stdout completo, mesmo com follow-up ativo e max=3."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup",
        "--max-departure-followups", "3",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    for sentinel in _LEAK_SENTINELS:
        assert sentinel not in out, f"LEAK: {sentinel!r}"
    for fragment in _LEAK_URL_FRAGMENTS:
        assert fragment not in out, f"URL fragment leaked: {fragment!r}"


def test_cli_smoke_departure_followup_in_fixture_mode_is_noop(capsys):
    """Em modo fixture, --fetch-departure-token-followup é no-op (2º
    hop exige live)."""
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI_FIRST_HOP),
        "--fetch-departure-token-followup",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "--fetch-departure-token-followup ignorado em modo fixture"
    ) in out


# ----------------- PR #47: early-return regression -----------------


def test_followup_runs_when_no_booking_token_target_found(
    monkeypatch, capsys,
):
    """Regressão do run #9: 1º hop só tem departure_token (sem
    booking_token). Antes do fix, `if target is None: return 0` no
    bloco `fetch_options` matava o fluxo antes do `fetch_followup`.
    Agora:
    1. linha 'nenhuma oferta ... para expandir booking_token' aparece;
    2. logo depois, o bloco '🧭 departure_token follow-up' também roda.
    """
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    calls = _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        # AMBOS os flags ativos — caso real do workflow #9:
        "--fetch-booking-options", "--max-booking-options", "3",
        "--fetch-departure-token-followup",
        "--max-departure-followups", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # 1) diagnóstico do expansor ainda aparece
    assert (
        "nenhuma oferta com cabine confirmada compatível para "
        "expandir booking_token"
    ) in out
    # 2) follow-up roda DEPOIS — fluxo não foi cortado
    assert "🧭 departure_token follow-up" in out
    assert "return_offer #1" in out
    # 3) o expansor NÃO chamou booking_options (nenhum candidato);
    #    o follow-up SIM (1 chamada)
    assert calls["search"] == 1
    assert calls["followup"] == 1
    # 4) defesa: nenhum leak
    for sentinel in _LEAK_SENTINELS:
        assert sentinel not in out, f"LEAK: {sentinel!r}"
    for fragment in _LEAK_URL_FRAGMENTS:
        assert fragment not in out, f"URL fragment leaked: {fragment!r}"


def test_no_followup_when_flag_off_and_no_booking_token(
    monkeypatch, capsys,
):
    """Comportamento simétrico: SEM --fetch-departure-token-followup,
    mesmo cenário (1º hop sem booking_token) termina sem chamar o
    2º hop — só imprime o diagnóstico do expansor. Garante que o
    fix não introduziu chamadas indevidas."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    calls = _live_followup_smoke(monkeypatch, first, followup)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-booking-options", "--max-booking-options", "3",
        # SEM --fetch-departure-token-followup
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "nenhuma oferta com cabine confirmada compatível para "
        "expandir booking_token"
    ) in out
    assert "🧭 departure_token follow-up" not in out
    assert calls["search"] == 1
    assert calls["followup"] == 0


# ----------------- PR #48: 3º hop expand_return_booking_token -----------------


# Sentinelas extras vindas de tests/fixtures/serpapi_booking_options.json
# (booking_options fixture do PR #40). NUNCA podem aparecer no stdout
# nem em modo 3-hop.
_LEAK_BOOKING_OPTIONS_SENTINELS = (
    "?token=abc",       # booking_request.url query
    "?ref=xyz",         # gflights.kissandfly.com URL query
    "param=value",      # post_data raw
)


def _live_three_hop_smoke(
    monkeypatch, first_hop: dict, followup: dict, booking_options: dict,
):
    """Helper que injeta urlopen mockado para 3 hops:
    - URL sem token       → first_hop
    - URL c/ departure_token → followup
    - URL c/ booking_token   → booking_options
    """
    import flight_mapper.serpapi_client as sp
    calls: dict = {
        "search": 0, "followup": 0, "booking": 0, "tokens": [],
    }

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "booking_token=" in url:
            calls["booking"] += 1
            for part in url.split("&"):
                if part.startswith("booking_token="):
                    calls["tokens"].append(("booking", part.split("=", 1)[1]))
            return _FakeResp(json.dumps(booking_options).encode("utf-8"))
        if "departure_token=" in url:
            calls["followup"] += 1
            for part in url.split("&"):
                if part.startswith("departure_token="):
                    calls["tokens"].append(("departure", part.split("=", 1)[1]))
            return _FakeResp(json.dumps(followup).encode("utf-8"))
        calls["search"] += 1
        return _FakeResp(json.dumps(first_hop).encode("utf-8"))

    monkeypatch.setattr(sp, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SERPAPI_API_KEY", "FAKE_KEY_NO_NETWORK")
    return calls


def test_expand_return_booking_token_runs_when_both_flags_active(
    monkeypatch, capsys,
):
    """3-hop completo: search → followup → booking_options do return_offer."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    booking = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    calls = _live_three_hop_smoke(monkeypatch, first, followup, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup", "--max-departure-followups", "1",
        "--expand-return-booking-token",
        "--max-return-booking-expansions", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # 3 hops aconteceram
    assert calls["search"] == 1
    assert calls["followup"] == 1
    assert calls["booking"] == 1
    # Bloco do 3º hop visível e bem formado
    assert "🔗 return booking_token expansion" in out
    assert "[followup #1 → return_offer #1]" in out
    assert "cabin=business" in out
    assert "(expansão 1/1)" in out
    # Output de booking_options usa o printer existente (formato sanitizado)
    assert "Latam Airlines | USD 1820.00 | domínio=www.latam.com" in out
    assert "POST — não é hyperlink simples" in out
    assert "Kissandfly | USD 1855.00 | domínio=gflights.kissandfly.com" in out
    assert "ProviderSemURL | USD 1900.00 | sem URL clicável" in out


def test_expand_return_booking_token_skipped_when_flag_off(
    monkeypatch, capsys,
):
    """Sem --expand-return-booking-token: 0 chamadas de 3º hop, mesmo
    com followup ativo."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    booking = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    calls = _live_three_hop_smoke(monkeypatch, first, followup, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup", "--max-departure-followups", "1",
        # SEM --expand-return-booking-token
    ])
    assert rc == 0
    assert calls["followup"] == 1
    assert calls["booking"] == 0
    out = capsys.readouterr().out
    assert "🔗 return booking_token expansion" not in out


def test_expand_return_booking_token_skipped_without_followup(
    monkeypatch, capsys,
):
    """Sem --fetch-departure-token-followup: 0 chamadas de 3º hop,
    mesmo com --expand-return-booking-token ativo (gate na cadeia)."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    booking = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    calls = _live_three_hop_smoke(monkeypatch, first, followup, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--expand-return-booking-token",
        "--max-return-booking-expansions", "1",
    ])
    assert rc == 0
    assert calls["followup"] == 0
    assert calls["booking"] == 0
    out = capsys.readouterr().out
    assert "🔗 return booking_token expansion" not in out


def test_expand_return_booking_token_skipped_when_no_compat_in_return(
    monkeypatch, capsys,
):
    """Pedido --cabin first: nenhum departure_token compatível →
    helper já bloqueia o 2º hop, e o 3º não roda. Mas se subimos via
    2º hop (cabin business) E return_offers não têm booking_token
    compatível, o 3º hop imprime diagnóstico honesto."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    booking = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    # Followup payload onde TODAS as return_offers são economy → nenhuma
    # bate com --cabin business + booking_token.
    followup_no_compat = {
        "search_parameters": {"type": "1", "currency": "USD"},
        "best_flights": [{
            "type": "Round trip", "price": 1100,
            "flights": [{"travel_class": "Economy", "airline": "BoA"}],
            "booking_token": "EconOnly_BK_token_zzzzzzzzzzz",
        }],
    }
    calls = _live_three_hop_smoke(
        monkeypatch, first, followup_no_compat, booking,
    )
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup", "--max-departure-followups", "1",
        "--expand-return-booking-token",
        "--max-return-booking-expansions", "1",
    ])
    assert rc == 0
    assert calls["followup"] == 1
    assert calls["booking"] == 0  # nenhum business compat → 0 expansões
    out = capsys.readouterr().out
    assert (
        "nenhuma return_offer com cabine compatível E booking_token "
        "p/ expandir"
    ) in out


def test_expand_return_booking_token_caps_at_max(monkeypatch, capsys):
    """Com 2 followups (cada um produzindo 1 return_offer business
    com booking_token), --max-return-booking-expansions=1 limita
    TOTAL a 1 chamada de booking_options (não 2)."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    booking = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    calls = _live_three_hop_smoke(monkeypatch, first, followup, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup", "--max-departure-followups", "2",
        "--expand-return-booking-token",
        "--max-return-booking-expansions", "1",
    ])
    assert rc == 0
    assert calls["followup"] == 2
    assert calls["booking"] == 1  # cap TOTAL = 1, não 1 por followup


def test_expand_return_booking_token_no_secret_leak(monkeypatch, capsys):
    """Defesa: nenhuma sentinela dos 3 payloads aparece no stdout.
    Cobertura inclui as sentinelas da fixture de booking_options
    (que está agora consumida via 3º hop)."""
    first = json.loads(FIXTURE_SERPAPI_FIRST_HOP.read_text(encoding="utf-8"))
    followup = json.loads(
        FIXTURE_SERPAPI_DEPARTURE_FOLLOWUP.read_text(encoding="utf-8")
    )
    booking = json.loads(
        FIXTURE_SERPAPI_BOOKING.read_text(encoding="utf-8")
    )
    _live_three_hop_smoke(monkeypatch, first, followup, booking)
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--departure", "2026-09-10", "--return-date", "2026-09-17",
        "--fetch-departure-token-followup", "--max-departure-followups", "1",
        "--expand-return-booking-token",
        "--max-return-booking-expansions", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Sentinelas dos hops 1 + 2
    for sentinel in _LEAK_SENTINELS:
        assert sentinel not in out, f"LEAK hop1/2: {sentinel!r}"
    # Sentinelas extras do hop 3 (booking_options fixture)
    for sentinel in _LEAK_BOOKING_OPTIONS_SENTINELS:
        assert sentinel not in out, f"LEAK hop3 (booking_options): {sentinel!r}"
    # Nenhum fragmento de URL completa
    for fragment in _LEAK_URL_FRAGMENTS:
        assert fragment not in out, f"URL fragment leaked: {fragment!r}"


def test_expand_return_booking_token_in_fixture_mode_is_noop(capsys):
    """Em fixture mode o flag é no-op honesto (3º hop precisa live)."""
    rc = main([
        "serpapi-smoke", "--route", "GRU-MIA", "--trip", "round_trip",
        "--cabin", "business",
        "--mock-file", str(FIXTURE_SERPAPI_FIRST_HOP),
        "--expand-return-booking-token",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "--expand-return-booking-token ignorado em modo fixture"
    ) in out


def test_departure_followup_not_consumed_by_pipeline_core():
    """2º hop é só smoke CLI; NÃO pode aparecer em monitor/providers/
    notifier/state."""
    for mod in ("monitor.py", "providers.py", "notifier.py", "state.py"):
        src = (Path("flight_mapper") / mod).read_text(encoding="utf-8")
        assert "fetch_departure_followup" not in src, mod
        assert "_select_departure_followup_targets" not in src, mod
        assert "_print_departure_followup_block" not in src, mod


# ----------------- Garantias gerais -----------------

def test_no_provider_module_imported_in_pipeline_core(tmp_path):
    """Amadeus/SerpApi NÃO devem ter sido referenciados em monitor.py,
    providers.py, notifier.py — só são smoke/auxiliar."""
    for mod in ("monitor.py", "providers.py", "notifier.py"):
        src = (Path("flight_mapper") / mod).read_text(encoding="utf-8")
        assert "amadeus_client" not in src, mod
        assert "serpapi_client" not in src, mod


def test_booking_options_not_consumed_by_pipeline_core():
    """Booking options PR #40 — read-only. Não pode aparecer em
    monitor/providers/notifier/state."""
    for mod in ("monitor.py", "providers.py", "notifier.py", "state.py"):
        src = (Path("flight_mapper") / mod).read_text(encoding="utf-8")
        assert "fetch_booking_options" not in src, mod
        assert "parse_booking_options" not in src, mod
        assert "SerpApiBookingOption" not in src, mod
