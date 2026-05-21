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
    SerpApiClient,
    SerpApiError,
    parse_search,
    parse_search_from_file,
)
from flight_mapper.state import PriceStore


FIXTURE_AMADEUS = Path(__file__).parent / "fixtures" / "amadeus_business.json"
FIXTURE_SERPAPI = Path(__file__).parent / "fixtures" / "serpapi_google_flights.json"


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


# ----------------- Garantias gerais -----------------

def test_no_provider_module_imported_in_pipeline_core(tmp_path):
    """Amadeus/SerpApi NÃO devem ter sido referenciados em monitor.py,
    providers.py, notifier.py — só são smoke/auxiliar."""
    for mod in ("monitor.py", "providers.py", "notifier.py"):
        src = (Path("flight_mapper") / mod).read_text(encoding="utf-8")
        assert "amadeus_client" not in src, mod
        assert "serpapi_client" not in src, mod
