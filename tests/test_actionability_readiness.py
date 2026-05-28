"""Testes do PR #61 — spike de actionability por provider.

Cobre os 4 providers atuais do repo + regras de decisão + zero leak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flight_mapper.actionability_readiness import (
    DECISION_CANDIDATE,
    DECISION_INSUFFICIENT,
    DECISION_NOT_SUITABLE,
    DECISION_VALIDATOR_ONLY,
    ActionabilityReport,
    apply_decision,
    format_actionability_report,
    load_and_parse,
    parse_amadeus_for_actionability,
    parse_kiwi_for_actionability,
    parse_serpapi_for_actionability,
    parse_travelpayouts_for_actionability,
)
from flight_mapper.__main__ import main


FIX = Path(__file__).parent / "fixtures"


# ----------------- apply_decision (regras puras) -----------------


def test_decision_candidate_for_integration():
    assert apply_decision(
        cabin_confirmed=True, actionable_url=True, has_price=True,
    ) == DECISION_CANDIDATE


def test_decision_validator_only_when_no_link():
    assert apply_decision(
        cabin_confirmed=True, actionable_url=False, has_price=True,
    ) == DECISION_VALIDATOR_ONLY
    # Mesmo sem preço, cabine confirmada SEM link continua validator_only
    assert apply_decision(
        cabin_confirmed=True, actionable_url=False, has_price=False,
    ) == DECISION_VALIDATOR_ONLY


def test_decision_insufficient_when_link_no_cabin():
    assert apply_decision(
        cabin_confirmed=False, actionable_url=True, has_price=True,
    ) == DECISION_INSUFFICIENT


def test_decision_not_suitable_when_nothing():
    assert apply_decision(
        cabin_confirmed=False, actionable_url=False, has_price=False,
    ) == DECISION_NOT_SUITABLE


# ----------------- Amadeus -----------------


def test_amadeus_fixture_returns_validator_only():
    """Amadeus business fixture: cabin OK + price OK, NO deep_link
    → validator_only."""
    from flight_mapper.amadeus_client import parse_offers_from_file
    offers = parse_offers_from_file(str(FIX / "amadeus_business.json"))
    report = parse_amadeus_for_actionability(offers, route="GRU-MIA")
    assert report.provider == "amadeus"
    assert report.cabin_confirmed is True
    assert report.price_amount is not None
    assert report.actionable_url is False
    assert report.booking_flow == "amadeus_pricing_required"
    assert report.decision == DECISION_VALIDATOR_ONLY
    # blockers cita Pricing/Orders explicitamente
    assert any(
        "amadeus_pricing_orders_api" in b for b in report.blockers
    )


def test_amadeus_empty_payload_not_suitable():
    report = parse_amadeus_for_actionability([], route="GRU-MIA")
    assert report.decision == DECISION_NOT_SUITABLE
    assert "empty_payload" in report.blockers


# ----------------- Kiwi -----------------


def test_kiwi_fixture_is_candidate_for_integration():
    """Kiwi com deep_link + cabin business → único candidato real."""
    payload = json.loads(
        (FIX / "kiwi_tequila_business_gru_mia.json").read_text(encoding="utf-8")
    )
    report = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.provider == "kiwi"
    assert report.cabin_confirmed is True
    assert report.actionable_url is True
    assert report.booking_flow == "deep_link"
    assert report.booking_domain == "www.kiwi.com"
    assert report.decision == DECISION_CANDIDATE
    assert report.blockers == ()


def test_kiwi_empty_payload_not_suitable():
    report = parse_kiwi_for_actionability(
        {"data": []}, route="GRU-MIA", requested_cabin="business",
    )
    assert report.decision == DECISION_NOT_SUITABLE
    assert "empty_payload" in report.blockers


def test_kiwi_without_deep_link_falls_to_validator_only():
    """Edge case: cabin OK mas deep_link ausente do payload."""
    payload = {
        "currency": "BRL",
        "data": [{
            "id": "no-link", "price": 9000,
            "local_departure": "2026-09-10T22:30:00.000Z",
            "local_arrival": "2026-09-11T07:55:00.000Z",
            "airlines": ["LA"],
        }],
    }
    report = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert report.cabin_confirmed is True
    assert report.actionable_url is False
    assert report.decision == DECISION_VALIDATOR_ONLY
    assert "deep_link_absent" in report.blockers


# ----------------- SerpApi -----------------


def test_serpapi_search_only_no_booking_returns_validator_only():
    """Search devolve cabin+price; sem booking_options p/ classificar
    actionability → validator_only (não eleva)."""
    from flight_mapper.serpapi_client import parse_search_from_file
    offers = parse_search_from_file(str(FIX / "serpapi_google_flights.json"))
    report = parse_serpapi_for_actionability(
        offers, route="GRU-MIA", requested_cabin="business",
        booking_options=None,
    )
    assert report.provider == "serpapi"
    assert report.cabin_confirmed is True
    assert report.actionable_url is False
    assert report.decision == DECISION_VALIDATOR_ONLY
    assert "booking_options_not_provided" in report.blockers


def test_serpapi_google_post_only_is_validator_only():
    """Booking google_post_only NÃO é hyperlink → validator_only."""
    from flight_mapper.serpapi_client import (
        parse_booking_options_from_file, parse_search_from_file,
    )
    offers = parse_search_from_file(str(FIX / "serpapi_google_flights.json"))
    booking = parse_booking_options_from_file(
        str(FIX / "serpapi_booking_google_post_only.json")
    )
    report = parse_serpapi_for_actionability(
        offers, route="GRU-MIA", requested_cabin="business",
        booking_options=booking,
    )
    assert report.cabin_confirmed is True
    assert report.actionable_url is False
    assert report.booking_flow == "google_post"
    assert report.booking_domain == "www.google.com"
    assert "booking_google_post_only" in report.blockers
    assert report.decision == DECISION_VALIDATOR_ONLY


def test_serpapi_mixed_simple_and_post_is_candidate():
    """Booking com pelo menos um link simples → candidato."""
    from flight_mapper.serpapi_client import (
        parse_booking_options_from_file, parse_search_from_file,
    )
    offers = parse_search_from_file(str(FIX / "serpapi_google_flights.json"))
    booking = parse_booking_options_from_file(
        str(FIX / "serpapi_booking_options.json")
    )
    report = parse_serpapi_for_actionability(
        offers, route="GRU-MIA", requested_cabin="business",
        booking_options=booking,
    )
    assert report.cabin_confirmed is True
    assert report.actionable_url is True
    assert report.booking_flow == "deep_link"
    assert report.decision == DECISION_CANDIDATE


def test_serpapi_empty_offers_not_suitable():
    report = parse_serpapi_for_actionability(
        [], route="GRU-MIA", requested_cabin="business",
    )
    assert report.decision == DECISION_NOT_SUITABLE
    assert "empty_payload" in report.blockers


# ----------------- Travelpayouts -----------------


def test_travelpayouts_fixture_is_not_suitable():
    """Cache não confirma cabine nem dá link comercial confiável."""
    payload = json.loads(
        (FIX / "travelpayouts_cache_no_cabin.json").read_text(encoding="utf-8")
    )
    report = parse_travelpayouts_for_actionability(payload, route="GRU-MIA")
    assert report.provider == "travelpayouts"
    assert report.cabin_confirmed is False
    assert report.actionable_url is False
    assert report.decision == DECISION_NOT_SUITABLE
    assert "no_cabin_confirmation_from_provider" in report.blockers
    assert "no_actionable_deep_link" in report.blockers


def test_travelpayouts_empty_payload_not_suitable():
    report = parse_travelpayouts_for_actionability(
        {"data": None}, route="GRU-MIA",
    )
    assert report.decision == DECISION_NOT_SUITABLE


# ----------------- format_actionability_report (determinístico) -----------------


def test_format_is_deterministic_for_kiwi():
    """Saída chave=valor estável — fixture idêntica → output idêntico."""
    payload = json.loads(
        (FIX / "kiwi_tequila_business_gru_mia.json").read_text(encoding="utf-8")
    )
    r = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    out1 = format_actionability_report(r)
    out2 = format_actionability_report(r)
    assert out1 == out2
    # Linhas obrigatórias presentes
    for key in (
        "provider:", "route:", "outbound_date:", "return_date:",
        "cabin_confirmed:", "price_amount:", "price_currency:",
        "airlines:", "actionable_url:", "booking_flow:",
        "booking_domain:", "blockers:", "decision:",
    ):
        assert key in out1


def test_format_uses_yes_no_for_booleans():
    payload = json.loads(
        (FIX / "kiwi_tequila_business_gru_mia.json").read_text(encoding="utf-8")
    )
    r = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    out = format_actionability_report(r)
    assert "cabin_confirmed: yes" in out
    assert "actionable_url:  yes" in out


# ----------------- load_and_parse (CLI helper) -----------------


def test_load_and_parse_kiwi():
    r = load_and_parse(
        "kiwi",
        FIX / "kiwi_tequila_business_gru_mia.json",
        route="GRU-MIA", requested_cabin="business",
    )
    assert r.decision == DECISION_CANDIDATE


def test_load_and_parse_amadeus():
    r = load_and_parse(
        "amadeus",
        FIX / "amadeus_business.json",
        route="GRU-MIA", requested_cabin="business",
    )
    assert r.decision == DECISION_VALIDATOR_ONLY


def test_load_and_parse_serpapi_with_booking():
    r = load_and_parse(
        "serpapi",
        FIX / "serpapi_google_flights.json",
        route="GRU-MIA", requested_cabin="business",
        booking_options_path=FIX / "serpapi_booking_google_post_only.json",
    )
    assert r.decision == DECISION_VALIDATOR_ONLY
    assert r.booking_flow == "google_post"


def test_load_and_parse_unknown_provider_raises():
    with pytest.raises(ValueError):
        load_and_parse(
            "duffel",  # não implementado
            FIX / "amadeus_business.json",
            route="GRU-MIA",
        )


# ----------------- CLI integration -----------------


def test_cli_actionability_amadeus(capsys):
    rc = main([
        "provider-readiness", "--provider", "amadeus",
        "--mock-file", str(FIX / "amadeus_business.json"),
        "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "provider:        amadeus" in out
    assert "cabin_confirmed: yes" in out
    assert "decision:        validator_only" in out


def test_cli_actionability_kiwi(capsys):
    rc = main([
        "provider-readiness", "--provider", "kiwi",
        "--mock-file", str(FIX / "kiwi_tequila_business_gru_mia.json"),
        "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "decision:        candidate_for_integration" in out
    assert "booking_flow:    deep_link" in out
    assert "booking_domain:  www.kiwi.com" in out


def test_cli_actionability_serpapi_with_booking(capsys):
    rc = main([
        "provider-readiness", "--provider", "serpapi",
        "--mock-file", str(FIX / "serpapi_google_flights.json"),
        "--booking-options-file",
        str(FIX / "serpapi_booking_google_post_only.json"),
        "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "decision:        validator_only" in out
    assert "booking_flow:    google_post" in out


def test_cli_actionability_travelpayouts(capsys):
    rc = main([
        "provider-readiness", "--provider", "travelpayouts",
        "--mock-file", str(FIX / "travelpayouts_cache_no_cabin.json"),
        "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "decision:        not_suitable" in out


def test_cli_actionability_requires_mock_file(capsys):
    rc = main([
        "provider-readiness", "--provider", "amadeus",
        "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--mock-file" in err


def test_cli_legacy_audit_mode_still_works(capsys):
    """Sem --provider, comportamento original (audit) é preservado."""
    rc = main(["provider-readiness"])
    assert rc == 0
    out = capsys.readouterr().out
    # Audit usa cabeçalho "🔌 Provider readiness"
    assert "Provider readiness" in out


# ----------------- zero leak -----------------


def test_amadeus_report_no_leak():
    """Output do parser Amadeus NÃO contém URL/token/post_data."""
    from flight_mapper.amadeus_client import parse_offers_from_file
    offers = parse_offers_from_file(str(FIX / "amadeus_business.json"))
    out = format_actionability_report(
        parse_amadeus_for_actionability(offers, route="GRU-MIA")
    )
    for needle in (
        "http://", "https://", "?token=", "?ref=", "?cart=",
        "post_data",
    ):
        assert needle not in out, f"LEAK amadeus: {needle!r}"


def test_serpapi_report_no_leak_with_google_post_fixture():
    """Fixture do google_post tem `spike_fixture_xyz` no token e
    `spike_fixture_post_body` no post_data. NENHUM pode aparecer no
    output do parser."""
    from flight_mapper.serpapi_client import (
        parse_booking_options_from_file, parse_search_from_file,
    )
    offers = parse_search_from_file(str(FIX / "serpapi_google_flights.json"))
    booking = parse_booking_options_from_file(
        str(FIX / "serpapi_booking_google_post_only.json")
    )
    r = parse_serpapi_for_actionability(
        offers, route="GRU-MIA", requested_cabin="business",
        booking_options=booking,
    )
    out = format_actionability_report(r)
    # Sentinelas explícitas da fixture
    assert "spike_fixture_xyz" not in out
    assert "spike_fixture_post_body" not in out
    # Fragmentos genéricos
    for needle in (
        "http://", "https://", "?token=", "?ref=", "?cart=",
        "post_data",
    ):
        assert needle not in out, f"LEAK serpapi: {needle!r}"


def test_kiwi_report_only_exposes_domain():
    """Apesar da URL do deep_link conter query string, o output só
    deve mostrar o domínio."""
    payload = {
        "currency": "BRL",
        "data": [{
            "id": "leak-test", "price": 9000,
            "local_departure": "2026-09-10T22:30:00.000Z",
            "local_arrival": "2026-09-11T07:55:00.000Z",
            "airlines": ["LA"],
            "deep_link": "https://www.kiwi.com/deep?token=spike_kiwi_secret_xyz",
        }],
    }
    r = parse_kiwi_for_actionability(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    out = format_actionability_report(r)
    assert "spike_kiwi_secret_xyz" not in out
    assert "?token=" not in out
    assert "https://" not in out
    assert "www.kiwi.com" in out  # apenas o domínio
