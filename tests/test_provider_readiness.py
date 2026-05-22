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
    SerpApiAuthError,
    SerpApiBookingOption,
    SerpApiClient,
    SerpApiError,
    _resolve_travel_class,
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
