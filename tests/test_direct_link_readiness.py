"""Testes do PR #72 — spike de provedores de direct booking / deep_link.

Regra do goal: "No deep_link = not suitable". Só deep_link de OFERTA EXATA
vira candidate_for_integration; busca genérica / sem link / order_flow são
not_suitable. Saída sanitizada (sem token/query secret/URL completa).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from flight_mapper.__main__ import main
from flight_mapper.direct_link_readiness import (
    DECISION_BLOCKED_COMMERCIAL,
    DECISION_CANDIDATE,
    DECISION_NOT_SUITABLE,
    DL_BOOKING_FLOW,
    DL_EXACT_OFFER,
    DL_GENERIC_SEARCH,
    DL_NONE,
    classify_deep_link,
    format_direct_link_report,
    parse_direct_link_offer,
)


FIX = Path(__file__).parent / "fixtures"


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


# ----------------- classify_deep_link unit -----------------


def test_classify_exact_offer_kiwi():
    assert classify_deep_link(
        "https://www.kiwi.com/deep?flightsId=abc&token=x"
    ) == DL_EXACT_OFFER


def test_classify_generic_search_hosts():
    assert classify_deep_link(
        "https://www.google.com/travel/flights/search?tfs=x"
    ) == DL_GENERIC_SEARCH
    assert classify_deep_link(
        "https://www.kayak.com/flights/GRU-LHR"
    ) == DL_GENERIC_SEARCH
    assert classify_deep_link(
        "https://www.skyscanner.com/transport/flights/gru/lhr/"
    ) == DL_GENERIC_SEARCH


def test_classify_generic_by_search_path():
    # Host desconhecido mas path de busca → genérico.
    assert classify_deep_link(
        "https://example-ota.com/search?o=GRU&d=LHR"
    ) == DL_GENERIC_SEARCH


def test_classify_none_for_missing_or_non_http():
    assert classify_deep_link(None) == DL_NONE
    assert classify_deep_link("") == DL_NONE
    assert classify_deep_link("ftp://x/y") == DL_NONE
    assert classify_deep_link("not a url") == DL_NONE


def test_classify_booking_flow_overrides_url():
    # order_flow (Duffel) nunca é clicável, mesmo com URL.
    assert classify_deep_link(
        "https://api.duffel.com/air/offers/off_1", booking_flow=True,
    ) == DL_BOOKING_FLOW


# ----------------- 1. exact_offer → candidate -----------------


def test_exact_offer_is_candidate():
    payload = json.loads((FIX / "directlink_kiwi_exact.json").read_text())
    r = parse_direct_link_offer(
        payload["offer"], provider="kiwi", route="GRU-LHR",
    )
    assert r.deep_link_type == DL_EXACT_OFFER
    assert r.deep_link_available is True
    assert r.decision == DECISION_CANDIDATE
    assert r.deep_link_domain == "www.kiwi.com"
    assert r.cabin_available is True and r.price_available is True
    assert r.blockers == ()


# ----------------- 2. generic search → not_suitable -----------------


def test_generic_search_is_not_suitable():
    payload = json.loads((FIX / "directlink_generic_search.json").read_text())
    r = parse_direct_link_offer(
        payload["offer"], provider="google", route="GRU-LHR",
    )
    assert r.deep_link_type == DL_GENERIC_SEARCH
    assert r.deep_link_available is False
    assert r.decision == DECISION_NOT_SUITABLE
    assert "only_generic_search_url" in r.blockers


# ----------------- 3. no link → not_suitable -----------------


def test_no_link_is_not_suitable():
    payload = json.loads((FIX / "directlink_no_link.json").read_text())
    r = parse_direct_link_offer(
        payload["offer"], provider="latam", route="GRU-LHR",
    )
    assert r.deep_link_type == DL_NONE
    assert r.decision == DECISION_NOT_SUITABLE
    assert "no_deep_link" in r.blockers


# ----------------- 4. order_flow only → not_suitable -----------------


def test_order_flow_is_not_suitable_for_this_goal():
    payload = json.loads((FIX / "directlink_duffel_order_flow.json").read_text())
    r = parse_direct_link_offer(
        payload["offer"], provider="duffel", route="GRU-LHR",
    )
    assert r.deep_link_type == DL_BOOKING_FLOW
    assert r.deep_link_available is False
    assert r.decision == DECISION_NOT_SUITABLE
    assert "order_flow_not_clickable_checkout" in r.blockers


# ----------------- blocked_commercially -----------------


def test_commercial_block_overrides_decision():
    r = parse_direct_link_offer(
        {
            "deep_link": "https://www.kiwi.com/deep?flightsId=abc",
            "price": 100, "cabin_class": "economy", "commercial_blocked": True,
        },
        provider="someapi", route="GRU-LHR",
    )
    assert r.decision == DECISION_BLOCKED_COMMERCIAL
    assert "requires_manual_commercial_approval" in r.blockers


# ----------------- 5. output sanitization -----------------


def test_output_sanitizes_tokens_and_query_secrets():
    payload = json.loads((FIX / "directlink_kiwi_exact.json").read_text())
    r = parse_direct_link_offer(
        payload["offer"], provider="kiwi", route="GRU-LHR",
    )
    out = format_direct_link_report(r)
    for sentinel in (
        "SECRET_KIWI_TOKEN_XYZ", "token=", "flightsId", "https://", "?",
    ):
        assert sentinel not in out, f"LEAK: {sentinel!r}"
    # Só o domínio aparece.
    assert "www.kiwi.com" in out


def test_report_schema_has_no_url_or_token_field():
    from dataclasses import fields
    from flight_mapper.direct_link_readiness import DirectLinkReport
    names = {f.name for f in fields(DirectLinkReport)}
    forbidden = {"url", "deep_link", "token", "query", "booking_url", "payload"}
    assert not (names & forbidden)


# ----------------- CLI -----------------


def test_cli_refuses_without_mock_file():
    code, out, err = _run([
        "direct-link-readiness", "--provider", "kiwi", "--route", "GRU-LHR",
    ])
    assert code == 2
    assert "--mock-file" in err
    assert out == ""


def test_cli_exact_offer_end_to_end():
    code, out, err = _run([
        "direct-link-readiness", "--provider", "kiwi", "--route", "GRU-LHR",
        "--trip", "round_trip", "--cabin", "economy",
        "--mock-file", str(FIX / "directlink_kiwi_exact.json"),
    ])
    assert code == 0
    assert "decision:             candidate_for_integration" in out
    assert "deep_link_type:       exact_offer" in out
    # CLI também não vaza secret.
    assert "SECRET_KIWI_TOKEN_XYZ" not in out


def test_cli_generic_search_end_to_end():
    code, out, err = _run([
        "direct-link-readiness", "--provider", "google", "--route", "GRU-LHR",
        "--mock-file", str(FIX / "directlink_generic_search.json"),
    ])
    assert code == 0
    assert "decision:             not_suitable" in out
    assert "deep_link_type:       generic_search" in out


# ----------------- no production touch -----------------


def test_spike_module_no_telegram_or_network():
    src = (
        Path(__file__).resolve().parents[1]
        / "flight_mapper" / "direct_link_readiness.py"
    ).read_text(encoding="utf-8")
    for needle in ("TelegramNotifier", "send_alert", "urlopen", "requests."):
        assert needle not in src
