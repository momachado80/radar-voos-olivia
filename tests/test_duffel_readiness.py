"""Testes do PR #63 — spike Duffel como provider transacional.

Cobre parser de fixtures, classificação de decision (candidate /
validator_only / not_suitable conforme regras do goal), modo --real
gated em DUFFEL_ACCESS_TOKEN, saída sanitizada e estrutura do workflow.
Sem rede real, sem secrets, sem Telegram, sem booking.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from flight_mapper.actionability_readiness import (
    DECISION_CANDIDATE,
    DECISION_NOT_SUITABLE,
    DECISION_VALIDATOR_ONLY,
    DUFFEL_API_URL,
    duffel_live_search,
    format_actionability_report,
    load_and_parse,
    parse_duffel_for_actionability,
)
from flight_mapper.__main__ import main


FIX = Path(__file__).parent / "fixtures"


# ----------------- parser sobre fixtures -----------------


def test_duffel_business_fixture_classifies_candidate():
    payload = json.loads(
        (FIX / "duffel_business_gru_mia.json").read_text(encoding="utf-8")
    )
    report = parse_duffel_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.provider == "duffel"
    assert report.cabin_confirmed is True
    assert report.price_amount == 4321.50
    assert report.price_currency == "USD"
    assert report.airlines == ("LA",)
    assert report.actionable_url is False
    assert report.booking_flow == "order_flow"
    assert report.booking_domain is None
    assert report.decision == DECISION_CANDIDATE
    assert report.outbound_date == "2026-09-10"
    assert report.trip_type == "one_way"


def test_duffel_economy_fixture_rejected_as_not_suitable():
    payload = json.loads(
        (FIX / "duffel_economy_gru_mia.json").read_text(encoding="utf-8")
    )
    report = parse_duffel_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.cabin_confirmed is False
    assert report.price_amount == 598.20
    assert "cabin_mismatch_or_absent" in report.blockers
    assert report.decision == DECISION_NOT_SUITABLE


def test_duffel_empty_payload_not_suitable():
    payload = json.loads(
        (FIX / "duffel_empty.json").read_text(encoding="utf-8")
    )
    report = parse_duffel_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.cabin_confirmed is False
    assert report.price_amount is None
    assert report.booking_flow == "none"
    assert report.actionable_url is False
    assert report.decision == DECISION_NOT_SUITABLE
    assert "empty_payload" in report.blockers


def test_duffel_validator_only_when_cabin_but_no_price():
    payload = {
        "data": {
            "offers": [
                {
                    "id": "off_no_price",
                    "owner": {"iata_code": "BA"},
                    "total_amount": None,
                    "total_currency": "USD",
                    "slices": [
                        {
                            "segments": [
                                {
                                    "departing_at": "2026-09-10T22:30:00",
                                    "marketing_carrier": {"iata_code": "BA"},
                                    "passengers": [
                                        {"cabin_class": "business"}
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }
    report = parse_duffel_for_actionability(
        payload, route="GRU-LHR", requested_cabin="business",
    )
    assert report.cabin_confirmed is True
    assert report.price_amount is None
    assert "price_absent" in report.blockers
    assert report.decision == DECISION_VALIDATOR_ONLY


def test_duffel_round_trip_inferred_from_slices():
    payload = {
        "data": {
            "offers": [
                {
                    "id": "off_rt",
                    "owner": {"iata_code": "LA"},
                    "total_amount": "5500.00",
                    "total_currency": "USD",
                    "slices": [
                        {
                            "segments": [
                                {
                                    "departing_at": "2026-09-10T22:30:00",
                                    "marketing_carrier": {"iata_code": "LA"},
                                    "passengers": [{"cabin_class": "business"}],
                                }
                            ]
                        },
                        {
                            "segments": [
                                {
                                    "departing_at": "2026-09-17T10:00:00",
                                    "marketing_carrier": {"iata_code": "LA"},
                                    "passengers": [{"cabin_class": "business"}],
                                }
                            ]
                        },
                    ],
                }
            ]
        }
    }
    report = parse_duffel_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.trip_type == "round_trip"
    assert report.outbound_date == "2026-09-10"
    assert report.return_date == "2026-09-17"
    assert report.decision == DECISION_CANDIDATE


def test_duffel_first_cabin_partial_mixed_not_confirmed():
    # 1º segmento com 2 passageiros, um business + um economy → conservador.
    payload = {
        "data": {
            "offers": [
                {
                    "id": "off_mixed",
                    "owner": {"iata_code": "AA"},
                    "total_amount": "999",
                    "total_currency": "USD",
                    "slices": [
                        {
                            "segments": [
                                {
                                    "departing_at": "2026-09-10T22:30:00",
                                    "marketing_carrier": {"iata_code": "AA"},
                                    "passengers": [
                                        {"cabin_class": "business"},
                                        {"cabin_class": "economy"},
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }
    report = parse_duffel_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.cabin_confirmed is False
    assert report.decision == DECISION_NOT_SUITABLE


def test_duffel_supports_data_as_list_shape():
    # Forma alternativa: /air/offers (list) ao invés de offer_request.
    payload = {
        "data": [
            {
                "id": "off_list_shape",
                "owner": {"iata_code": "AF"},
                "total_amount": "3210.00",
                "total_currency": "EUR",
                "slices": [
                    {
                        "segments": [
                            {
                                "departing_at": "2026-09-10T22:30:00",
                                "marketing_carrier": {"iata_code": "AF"},
                                "passengers": [{"cabin_class": "business"}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    report = parse_duffel_for_actionability(
        payload, route="GRU-CDG", requested_cabin="business",
    )
    assert report.decision == DECISION_CANDIDATE
    assert report.price_currency == "EUR"


def test_duffel_live_blocker_propagated_to_report():
    payload = {"data": {"offers": []}, "_blocker": "http_429"}
    report = parse_duffel_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.decision == DECISION_NOT_SUITABLE
    assert "live_http_429" in report.blockers


# ----------------- duffel_live_search (mocked) -----------------


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_duffel_live_search_one_way_posts_correct_body():
    captured: dict = {}

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(b'{"data":{"offers":[]}}')

    duffel_live_search(
        access_token="sekrit_test_token_xyz",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date="2026-09-10",
        cabin_class="business",
        urlopen_impl=fake_urlopen,
    )
    assert DUFFEL_API_URL in captured["url"]
    # Token vai NO HEADER, não na URL.
    assert "sekrit_test_token_xyz" not in captured["url"]
    auth_values = [v for k, v in captured["headers"].items() if k.lower() == "authorization"]
    assert auth_values and "Bearer sekrit_test_token_xyz" in auth_values[0]
    # Versão fixada e content-type.
    assert any(k.lower() == "duffel-version" for k in captured["headers"])
    assert captured["body"]["data"]["cabin_class"] == "business"
    slices = captured["body"]["data"]["slices"]
    assert len(slices) == 1
    assert slices[0] == {
        "origin": "GRU", "destination": "MIA",
        "departure_date": "2026-09-10",
    }
    assert captured["body"]["data"]["passengers"] == [{"type": "adult"}]


def test_duffel_live_search_round_trip_appends_return_slice():
    captured: dict = {}

    def fake_urlopen(req, timeout=20):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(b'{"data":{"offers":[]}}')

    duffel_live_search(
        access_token="t",
        origin="GRU", destination="LHR",
        trip_type="round_trip",
        outbound_date="2026-09-10",
        return_date="2026-09-20",
        urlopen_impl=fake_urlopen,
    )
    slices = captured["body"]["data"]["slices"]
    assert len(slices) == 2
    assert slices[1] == {
        "origin": "LHR", "destination": "GRU",
        "departure_date": "2026-09-20",
    }


def test_duffel_live_search_http_error_returns_blocker():
    from urllib.error import HTTPError

    def boom(req, timeout=20):
        raise HTTPError(
            url="x", code=429, msg="too many", hdrs=None, fp=None,
        )

    result = duffel_live_search(
        access_token="t",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date="2026-09-10",
        urlopen_impl=boom,
    )
    assert result == {"data": {"offers": []}, "_blocker": "http_429"}


def test_duffel_live_search_url_error_returns_blocker():
    from urllib.error import URLError

    def boom(req, timeout=20):
        raise URLError("dns down")

    result = duffel_live_search(
        access_token="t",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date="2026-09-10",
        urlopen_impl=boom,
    )
    assert result == {"data": {"offers": []}, "_blocker": "network_error"}


def test_duffel_live_search_invalid_json_returns_blocker():
    def fake(req, timeout=20):
        return _FakeResp(b"not json{{{")

    result = duffel_live_search(
        access_token="t",
        origin="GRU", destination="MIA",
        trip_type="one_way",
        outbound_date="2026-09-10",
        urlopen_impl=fake,
    )
    assert result == {"data": {"offers": []}, "_blocker": "invalid_json_response"}


# ----------------- CLI gates -----------------


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out_buf.getvalue(), err_buf.getvalue()


def test_cli_real_duffel_refused_without_token(monkeypatch):
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    code, out, err = _run_cli([
        "provider-readiness", "--provider", "duffel",
        "--route", "GRU-MIA", "--cabin", "business", "--real",
    ])
    assert code == 2
    assert "DUFFEL_ACCESS_TOKEN" in err
    assert out == ""


def test_cli_real_refused_for_provider_outside_kiwi_duffel(monkeypatch):
    code, out, err = _run_cli([
        "provider-readiness", "--provider", "amadeus",
        "--route", "GRU-MIA", "--cabin", "business", "--real",
    ])
    assert code == 2
    assert "kiwi" in err.lower() and "duffel" in err.lower()


def test_cli_duffel_mock_fixture_business_prints_candidate():
    code, out, err = _run_cli([
        "provider-readiness", "--provider", "duffel",
        "--route", "GRU-MIA", "--cabin", "business",
        "--mock-file", str(FIX / "duffel_business_gru_mia.json"),
    ])
    assert code == 0
    assert "decision:        candidate_for_integration" in out
    assert "booking_flow:    order_flow" in out
    # Sanitização: payload NUNCA aparece na saída (ids/passenger).
    assert "off_fixture_business_001" not in out
    assert "pas_fixture_001" not in out
    assert "Premium Business" not in out


def test_cli_duffel_real_end_to_end_with_mocked_urlopen(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "sentinel_token_qwerty")
    sample = json.loads(
        (FIX / "duffel_business_gru_mia.json").read_text(encoding="utf-8")
    )

    def fake_urlopen(req, timeout=20):
        assert "sentinel_token_qwerty" not in req.full_url
        return _FakeResp(json.dumps(sample).encode("utf-8"))

    import flight_mapper.actionability_readiness as mod
    monkeypatch.setattr(
        mod, "duffel_live_search",
        lambda **kw: mod.duffel_live_search.__wrapped__(  # type: ignore[attr-defined]
            **kw, urlopen_impl=fake_urlopen,
        ) if hasattr(mod.duffel_live_search, "__wrapped__") else json.loads(
            json.dumps(sample)
        ),
    )

    code, out, err = _run_cli([
        "provider-readiness", "--provider", "duffel",
        "--route", "GRU-MIA", "--cabin", "business", "--real",
        "--departure", "2026-09-10",
    ])
    assert code == 0
    assert "decision:        candidate_for_integration" in out
    # Token nunca vaza na saída.
    assert "sentinel_token_qwerty" not in out
    assert "sentinel_token_qwerty" not in err
    # Payload sanitizado: nenhum order/offer id aparece.
    assert "off_fixture_business_001" not in out


def test_cli_duffel_real_handles_http_429_cleanly(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "tok")
    from urllib.error import HTTPError
    import flight_mapper.actionability_readiness as mod

    def boom_payload(**kw):
        return {"data": {"offers": []}, "_blocker": "http_429"}

    monkeypatch.setattr(mod, "duffel_live_search", boom_payload)
    code, out, err = _run_cli([
        "provider-readiness", "--provider", "duffel",
        "--route", "GRU-MIA", "--cabin", "business", "--real",
    ])
    assert code == 0
    assert "decision:        not_suitable" in out
    assert "live_http_429" in out


def test_cli_existing_amadeus_mock_still_works():
    # Regressão: load_and_parse aceita providers existentes via choices.
    import flight_mapper.actionability_readiness as mod
    # Usa fixture já existente do spike PR #61.
    amadeus_fix = FIX / "amadeus_business.json"
    if not amadeus_fix.exists():
        pytest.skip("fixture do spike Amadeus não presente")
    code, out, err = _run_cli([
        "provider-readiness", "--provider", "amadeus",
        "--route", "GRU-MIA", "--cabin", "business",
        "--mock-file", str(amadeus_fix),
    ])
    assert code == 0
    assert "provider:        amadeus" in out


# ----------------- load_and_parse provider gate -----------------


def test_load_and_parse_supports_duffel_provider():
    report = load_and_parse(
        "duffel",
        FIX / "duffel_business_gru_mia.json",
        route="GRU-MIA", requested_cabin="business",
    )
    assert report.provider == "duffel"
    assert report.decision == DECISION_CANDIDATE


# ----------------- workflow estrutural -----------------


WF = (
    Path(__file__).resolve().parents[1]
    / ".github" / "workflows" / "duffel-readiness-smoke.yml"
)


def test_duffel_workflow_is_manual_only():
    txt = WF.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in txt
    assert "schedule:" not in txt
    assert "pull_request" not in txt
    assert "push:" not in txt


def test_duffel_workflow_has_read_only_permissions():
    txt = WF.read_text(encoding="utf-8")
    assert "permissions:" in txt
    assert "contents: read" in txt
    assert "contents: write" not in txt


def test_duffel_workflow_does_not_leak_other_secrets():
    txt = WF.read_text(encoding="utf-8")
    forbidden = [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "SERPAPI_API_KEY", "AMADEUS_CLIENT_ID",
        "AMADEUS_CLIENT_SECRET", "KIWI_API_KEY",
    ]
    for token in forbidden:
        assert token not in txt, f"workflow não pode referenciar {token}"


def test_duffel_workflow_consumes_only_duffel_secret():
    txt = WF.read_text(encoding="utf-8")
    assert "DUFFEL_ACCESS_TOKEN" in txt
    # Único secret referenciado.
    assert txt.count("${{ secrets.") == 1


def test_duffel_workflow_does_not_commit_or_push():
    txt = WF.read_text(encoding="utf-8")
    for forbidden in ("git commit", "git push", "git add"):
        assert forbidden not in txt


def test_duffel_workflow_caps_route_choices_at_five():
    txt = WF.read_text(encoding="utf-8")
    # Lista de rotas é fechada (choice). Pegamos a 1ª seção `route:`
    # e contamos quantas opções `- "XXX-YYY"` aparecem antes da próxima
    # entrada de input (`trip:`).
    start = txt.index("route:")
    end = txt.index("trip:", start)
    section = txt[start:end]
    # Linhas tipo `- "AAA-BBB"`.
    import re
    options = re.findall(r'- "[A-Z]{3}-[A-Z]{3}"', section)
    assert 1 <= len(options) <= 5, f"esperado <=5 rotas, achou {len(options)}"


def test_duffel_workflow_runs_only_provider_readiness_command():
    txt = WF.read_text(encoding="utf-8")
    # Deve invocar somente provider-readiness --provider duffel --real.
    assert "provider-readiness" in txt
    assert "--provider duffel" in txt
    assert "--real" in txt
    # Não deve EXECUTAR nada do motor de produção / booking. Comentários
    # explicativos podem mencionar essas palavras — checamos apenas
    # linhas não-comentário (linha começa com `#` após strip → ignora).
    executable_lines = [
        ln for ln in txt.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    executable_body = "\n".join(executable_lines)
    for forbidden in (
        "python -m flight_mapper monitor",
        "python -m flight_mapper detector",
        "cmd_cycle",
        "/air/orders",
    ):
        assert forbidden not in executable_body, (
            f"workflow não pode executar {forbidden}"
        )
