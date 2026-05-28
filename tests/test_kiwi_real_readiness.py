"""Testes do PR #62 — modo --real do `provider-readiness` para Kiwi
Tequila + workflow `kiwi-readiness-smoke.yml`.

Cobre as gates explícitas:
- --real só aceito p/ --provider kiwi
- KIWI_API_KEY obrigatório
- --real sem KIWI_API_KEY → mensagem clara, exit 2
- payload sanitizado (NUNCA URL completa nem header apikey)
- workflow é manual-only com inputs limitados
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from flight_mapper.__main__ import main
from flight_mapper.actionability_readiness import (
    DECISION_CANDIDATE,
    DECISION_NOT_SUITABLE,
    KIWI_TEQUILA_URL,
    format_actionability_report,
    kiwi_live_search,
    parse_kiwi_for_actionability,
)


# ----------------- gates: --real e KIWI_API_KEY -----------------


def test_real_refused_without_real_flag(monkeypatch, capsys):
    """--provider kiwi sem --real e sem --mock-file → mensagem clara."""
    monkeypatch.delenv("KIWI_API_KEY", raising=False)
    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--mock-file" in err
    assert "--real" in err


def test_real_refused_for_non_kiwi_provider(monkeypatch, capsys):
    """--real só funciona p/ Kiwi ou Duffel (PR #63). Amadeus/SerpApi/
    Travelpayouts são rejeitados claramente (sem chamar a rede do
    provider errado)."""
    monkeypatch.setenv("KIWI_API_KEY", "FAKE")
    rc = main([
        "provider-readiness", "--provider", "amadeus",
        "--route", "GRU-MIA", "--cabin", "business", "--real",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "kiwi" in err.lower() and "duffel" in err.lower()
    assert "--real" in err


def test_real_missing_kiwi_api_key_handled(monkeypatch, capsys):
    """Sem KIWI_API_KEY → mensagem clara, exit 2, ZERO rede."""
    monkeypatch.delenv("KIWI_API_KEY", raising=False)

    def _no_urlopen(req, *a, **k):
        raise AssertionError("urlopen NÃO deveria ser chamado sem KIWI_API_KEY")

    # Patch nas duas localizações usadas pelo módulo + builtin
    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", _no_urlopen)

    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--route", "GRU-MIA", "--cabin", "business", "--real",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "KIWI_API_KEY ausente" in err


def test_real_invalid_route_handled(monkeypatch, capsys):
    """Rota inválida (sem hífen) → mensagem clara, exit 2."""
    monkeypatch.setenv("KIWI_API_KEY", "FAKE")
    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--route", "INVALIDO", "--cabin", "business", "--real",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "rota inválida" in err


# ----------------- kiwi_live_search: URL building + sanitização -----------------


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self) -> bytes:
        return self._body


def test_kiwi_live_search_one_way_builds_correct_url():
    """URL contém selected_cabins=C, flight_type=oneway, fly_from/to,
    curr=BRL. Header `apikey` separado (não na URL)."""
    captured: dict = {}

    def _fake_urlopen(req, *a, **k):
        captured["url"] = getattr(req, "full_url", str(req))
        captured["headers"] = dict(req.header_items())
        return _FakeResp(json.dumps({"data": [], "currency": "BRL"}).encode())

    payload = kiwi_live_search(
        api_key="TEST_KEY",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date=date(2026, 9, 10),
        urlopen_impl=_fake_urlopen,
    )
    url = captured["url"]
    assert url.startswith(KIWI_TEQUILA_URL)
    assert "fly_from=GRU" in url
    assert "fly_to=MIA" in url
    assert "selected_cabins=C" in url
    assert "flight_type=oneway" in url
    assert "curr=BRL" in url
    assert "date_from=10%2F09%2F2026" in url  # DD/MM/YYYY URL-encoded
    # Header `apikey` presente, NÃO na URL.
    assert any(
        k.lower() == "apikey" and v == "TEST_KEY"
        for k, v in captured["headers"].items()
    )
    assert "apikey=" not in url  # NUNCA na URL
    assert payload == {"data": [], "currency": "BRL"}


def test_kiwi_live_search_round_trip_builds_correct_url():
    """round_trip omite flight_type e adiciona nights_in_dst_from/to."""
    captured: dict = {}

    def _fake_urlopen(req, *a, **k):
        captured["url"] = getattr(req, "full_url", str(req))
        return _FakeResp(b'{"data":[]}')

    kiwi_live_search(
        api_key="TEST_KEY",
        origin="GRU", destination="LHR",
        trip_type="round_trip",
        outbound_date=date(2026, 9, 10),
        return_date=date(2026, 9, 17),
        urlopen_impl=_fake_urlopen,
    )
    url = captured["url"]
    assert "nights_in_dst_from=7" in url
    assert "nights_in_dst_to=7" in url
    assert "flight_type=" not in url


def test_kiwi_live_search_http_error_returns_blocker():
    """HTTPError → payload com `_blocker` indicando código, sem
    propagar exceção."""
    from urllib.error import HTTPError
    from io import BytesIO

    def _fake_urlopen(req, *a, **k):
        raise HTTPError(
            url="x", code=429, msg="Rate limit",
            hdrs=None, fp=BytesIO(b""),
        )

    payload = kiwi_live_search(
        api_key="TEST_KEY",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date=date(2026, 9, 10),
        urlopen_impl=_fake_urlopen,
    )
    assert payload == {"data": [], "_blocker": "http_429"}


def test_kiwi_live_search_network_error_returns_blocker():
    """URLError → `_blocker=network_error`."""
    from urllib.error import URLError

    def _fake_urlopen(req, *a, **k):
        raise URLError("connection refused")

    payload = kiwi_live_search(
        api_key="TEST_KEY",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date=date(2026, 9, 10),
        urlopen_impl=_fake_urlopen,
    )
    assert payload["_blocker"] == "network_error"
    assert payload["data"] == []


def test_kiwi_live_search_invalid_json_returns_blocker():
    def _fake_urlopen(req, *a, **k):
        return _FakeResp(b"not-json{{{")

    payload = kiwi_live_search(
        api_key="TEST_KEY",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date=date(2026, 9, 10),
        urlopen_impl=_fake_urlopen,
    )
    assert payload["_blocker"] == "invalid_json_response"


# ----------------- parser propagates _blocker -----------------


def test_parser_propagates_live_blocker_when_empty():
    """payload com _blocker (rede falhou) → blockers do report contém
    `live_<reason>`."""
    payload = {"data": [], "_blocker": "http_429"}
    report = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.decision == DECISION_NOT_SUITABLE
    assert "empty_payload" in report.blockers
    assert "live_http_429" in report.blockers


def test_parser_real_payload_with_deep_link_is_candidate():
    """Payload simulando resposta real Kiwi com deep_link + cabin C
    → candidate_for_integration."""
    payload = {
        "currency": "BRL",
        "data": [{
            "id": "real-sim", "price": 7800,
            "local_departure": "2026-09-10T22:30:00.000Z",
            "local_arrival": "2026-09-11T07:55:00.000Z",
            "airlines": ["LA"],
            "deep_link": "https://www.kiwi.com/deep?token=sentinel_real_xyz&affilid=spike",
            "route": [{"local_departure": "2026-09-10T22:30:00.000Z"}],
        }],
    }
    report = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.cabin_confirmed is True
    assert report.actionable_url is True
    assert report.booking_domain == "www.kiwi.com"
    assert report.decision == DECISION_CANDIDATE
    # Saída sanitizada: URL completa não aparece.
    out = format_actionability_report(report)
    assert "https://" not in out
    assert "sentinel_real_xyz" not in out
    assert "affilid=" not in out
    assert "?token=" not in out


def test_parser_rejects_non_business_cabin():
    """requested_cabin != business → cabin_confirmed=False, blocker."""
    payload = {
        "currency": "BRL",
        "data": [{
            "id": "x", "price": 100,
            "local_departure": "2026-09-10T22:30:00.000Z",
            "local_arrival": "2026-09-11T07:55:00.000Z",
            "airlines": ["LA"],
            "deep_link": "https://www.kiwi.com/deep?x=1",
        }],
    }
    report = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="economy",
    )
    assert report.cabin_confirmed is False
    assert "requested_cabin_not_business" in report.blockers


# ----------------- CLI integration: end-to-end --real -----------------


def test_cli_real_kiwi_end_to_end(monkeypatch, capsys):
    """--real ativo: chama Kiwi (mockado), parser produz report,
    output sanitizado."""
    monkeypatch.setenv("KIWI_API_KEY", "TEST_KEY_NO_NETWORK")

    fake_payload = {
        "currency": "BRL",
        "data": [{
            "id": "live-1", "price": 8500,
            "local_departure": "2026-09-10T22:30:00.000Z",
            "local_arrival": "2026-09-11T07:55:00.000Z",
            "airlines": ["AA", "LA"],
            "deep_link": "https://www.kiwi.com/deep/full?secret=spike_cli_xyz",
            "route": [{"local_departure": "2026-09-10T22:30:00.000Z"}],
        }],
    }

    def _fake_urlopen(req, *a, **k):
        return _FakeResp(json.dumps(fake_payload).encode())

    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", _fake_urlopen)

    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--route", "GRU-MIA", "--cabin", "business",
        "--trip", "one_way",
        "--departure", "2026-09-10",
        "--real",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "provider:        kiwi" in out
    assert "route:           GRU-MIA" in out
    assert "trip_type:       one_way" in out
    assert "cabin_confirmed: yes" in out
    assert "actionable_url:  yes" in out
    assert "booking_domain:  www.kiwi.com" in out
    assert "decision:        candidate_for_integration" in out
    # Sanitização: SECRETOS e URL nunca aparecem.
    assert "https://" not in out
    assert "spike_cli_xyz" not in out
    assert "?secret=" not in out


def test_cli_real_kiwi_default_dates_used(monkeypatch, capsys):
    """Sem --departure → usa hoje+90d. Sem --return-date → não inclui."""
    monkeypatch.setenv("KIWI_API_KEY", "TEST_KEY")
    captured_dates: dict = {}

    def _fake_urlopen(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        # Extrai date_from da URL (URL-encoded DD/MM/YYYY)
        for part in url.split("&"):
            if part.startswith("date_from="):
                captured_dates["date_from"] = part.split("=", 1)[1]
        return _FakeResp(b'{"data":[]}')

    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", _fake_urlopen)

    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--route", "GRU-MIA", "--cabin", "business",
        "--trip", "one_way",
        "--real",
    ])
    assert rc == 0
    # date_from existe — data exata varia, então só verificamos o formato.
    df = captured_dates.get("date_from", "")
    # DD%2FMM%2FYYYY
    assert len(df) == 14  # "DD%2FMM%2FYYYY"
    assert df[2:5] == "%2F"


def test_cli_real_kiwi_http_error_shows_blocker(monkeypatch, capsys):
    """HTTP 429 do Kiwi → report com decision=not_suitable + blocker
    live_http_429. CLI NÃO crasha."""
    monkeypatch.setenv("KIWI_API_KEY", "TEST_KEY")

    from urllib.error import HTTPError
    from io import BytesIO

    def _fake_urlopen(req, *a, **k):
        raise HTTPError(
            url="x", code=429, msg="Rate limit",
            hdrs=None, fp=BytesIO(b""),
        )

    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", _fake_urlopen)

    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--route", "GRU-MIA", "--cabin", "business",
        "--trip", "one_way", "--real",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "decision:        not_suitable" in out
    assert "live_http_429" in out
    # Nenhum traceback / detalhe interno
    assert "Traceback" not in out


# ----------------- workflow YAML structural -----------------


def test_workflow_is_manual_only_no_schedule():
    """kiwi-readiness-smoke.yml NÃO pode ter cron/push/PR triggers."""
    import yaml
    path = Path(".github/workflows/kiwi-readiness-smoke.yml")
    assert path.exists()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    on = doc.get(True) or doc.get("on") or {}
    assert "workflow_dispatch" in on
    assert "schedule" not in on
    assert "push" not in on
    assert "pull_request" not in on


def test_workflow_uses_only_read_permissions():
    import yaml
    doc = yaml.safe_load(
        Path(".github/workflows/kiwi-readiness-smoke.yml")
        .read_text(encoding="utf-8")
    )
    perms = doc.get("permissions") or {}
    assert perms.get("contents") == "read"
    for k, v in perms.items():
        assert v != "write", f"permission '{k}' write não permitida"


def test_workflow_does_not_expose_telegram_or_serpapi():
    """O workflow Kiwi NÃO pode pedir/usar TELEGRAM_* nem SERPAPI_*."""
    raw = Path(
        ".github/workflows/kiwi-readiness-smoke.yml"
    ).read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" not in raw
    assert "TELEGRAM_CHAT_ID" not in raw
    assert "SERPAPI_API_KEY" not in raw
    assert "AMADEUS_" not in raw
    assert "TRAVELPAYOUTS_" not in raw


def test_workflow_does_not_write_or_commit_data():
    raw = Path(
        ".github/workflows/kiwi-readiness-smoke.yml"
    ).read_text(encoding="utf-8")
    assert "git add" not in raw
    assert "git commit" not in raw
    assert "git push" not in raw


def test_workflow_step_uses_only_kiwi_api_key_secret():
    """Step `Kiwi Tequila live readiness` deve receber APENAS
    KIWI_API_KEY como secret. Outros envs são inputs (não secretos)."""
    import yaml
    doc = yaml.safe_load(
        Path(".github/workflows/kiwi-readiness-smoke.yml")
        .read_text(encoding="utf-8")
    )
    step = next(
        s for s in doc["jobs"]["smoke"]["steps"]
        if "Kiwi" in (s.get("name") or "")
    )
    env = step.get("env") or {}
    # KIWI_API_KEY presente como secret
    assert env.get("KIWI_API_KEY") == "${{ secrets.KIWI_API_KEY }}"
    # Nenhum outro secrets.* exceto KIWI
    for k, v in env.items():
        if k == "KIWI_API_KEY":
            continue
        assert "secrets." not in str(v), (
            f"env {k}={v!r}: não pode trazer outro secret"
        )


def test_workflow_inputs_limit_routes_to_5():
    """Input `route` deve ser type=choice com no máximo 5 opções
    (as 5 rotas do goal)."""
    import yaml
    doc = yaml.safe_load(
        Path(".github/workflows/kiwi-readiness-smoke.yml")
        .read_text(encoding="utf-8")
    )
    on = doc.get(True) or doc.get("on") or {}
    inputs = (on.get("workflow_dispatch") or {}).get("inputs") or {}
    route_input = inputs.get("route") or {}
    assert route_input.get("type") == "choice"
    options = route_input.get("options") or []
    assert len(options) <= 5
    # Rotas do goal:
    for r in ("GRU-MIA", "GRU-JFK", "GRU-LIS", "GRU-MAD", "GRU-LHR"):
        assert r in options, f"rota {r} ausente no choice"


def test_workflow_runs_only_provider_readiness_kiwi_real():
    """Step shell DEVE chamar `provider-readiness --provider kiwi --real`
    e NÃO chamar qualquer outro command (serpapi-smoke, etc.)."""
    raw = Path(
        ".github/workflows/kiwi-readiness-smoke.yml"
    ).read_text(encoding="utf-8")
    assert "python -m flight_mapper provider-readiness" in raw
    assert "--provider" in raw
    assert "--real" in raw
    assert "--cabin business" in raw
    # Não deve usar outros commands do CLI
    assert "serpapi-smoke" not in raw
    assert "amadeus-smoke" not in raw
    assert "serpapi-booking-options" not in raw
    assert "cycle" not in raw.replace("cancel-in-progress", "")
    assert "hot-scan" not in raw
