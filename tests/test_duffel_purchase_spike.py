"""Testes do PR #70 — spike read-only do caminho de compra Duffel.

Cobre os requisitos do goal:
1. CLI recusa sem --test-mode.
2. CLI recusa sem DUFFEL_ACCESS_TOKEN em modo real (live-test).
3. Parser extrai campos de order-flow de fixture sanitizada.
4. Parser detecta campos obrigatórios ausentes.
5. Saída nunca loga token/offer_id/order_id/payment_id/PII/URL/payload cru.
6. Nenhum workflow de produção alterado.
7. Sem integração Telegram.
8. Sem chamada /air/orders (a menos que dry-run seguro — não existe no Duffel).
9. Sem dry-run → comando sai com segurança e documenta o bloqueio.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from flight_mapper.__main__ import main
from flight_mapper.duffel_purchase_spike import (
    DUFFEL_HAS_ORDER_DRY_RUN,
    SAFE_NEXT_ORDER_PII,
    format_purchase_path_report,
    parse_duffel_purchase_path,
)


FIX = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


# ----------------- 1 & 2. CLI gates -----------------


def test_cli_refuses_without_test_mode():
    code, out, err = _run([
        "duffel-purchase-spike", "--route", "GRU-MIA", "--cabin", "business",
    ])
    assert code == 2
    assert "--test-mode" in err
    assert out == ""


def test_cli_refuses_without_token_in_real_mode(monkeypatch):
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    code, out, err = _run([
        "duffel-purchase-spike", "--route", "GRU-MIA", "--test-mode",
    ])
    assert code == 2
    assert "DUFFEL_ACCESS_TOKEN" in err
    assert out == ""


def test_cli_refuses_live_token(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_should_be_blocked")
    code, out, err = _run([
        "duffel-purchase-spike", "--route", "GRU-MIA", "--test-mode",
    ])
    assert code == 2
    assert "LIVE" in err or "live" in err
    # NUNCA ecoa o token.
    assert "duffel_live_should_be_blocked" not in err
    assert out == ""


def test_cli_refuses_non_test_token(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "sometoken_without_prefix")
    code, out, err = _run([
        "duffel-purchase-spike", "--route", "GRU-MIA", "--test-mode",
    ])
    assert code == 2
    assert "sometoken_without_prefix" not in err


# ----------------- 3. parser extracts order-flow fields -----------------


def test_parser_extracts_required_order_fields():
    payload = json.loads(
        (FIX / "duffel_order_fields_present.json").read_text(encoding="utf-8")
    )
    r = parse_duffel_purchase_path(
        payload, route="GRU-MIA", requested_cabin="business",
        trip_type="one_way", environment="test",
    )
    assert r.provider == "duffel" and r.environment == "test"
    assert r.offer_found is True
    assert r.cabin_confirmed is True
    assert r.has_offer_id is True
    assert r.has_passenger_ids is True
    assert r.price_amount == 964.30
    assert r.price_currency == "EUR"
    assert r.airline == "AF"
    # Duffel: sem dry-run, exige PII, sem recuperação por dashboard.
    assert r.dry_run_available == "no"
    assert r.order_creation_requires_passenger_data == "yes"
    assert r.dashboard_recovery == "no"
    assert r.safe_next_step == SAFE_NEXT_ORDER_PII


# ----------------- 4. parser detects missing fields -----------------


def test_parser_detects_missing_required_fields():
    payload = json.loads(
        (FIX / "duffel_order_fields_missing.json").read_text(encoding="utf-8")
    )
    r = parse_duffel_purchase_path(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    assert r.offer_found is True
    assert r.has_offer_id is False
    assert r.has_passenger_ids is False
    assert "offer_missing_offer_id" in r.blockers
    assert "offer_missing_passenger_ids" in r.blockers


def test_parser_no_offer_is_safe_no_path():
    from flight_mapper.duffel_purchase_spike import SAFE_NEXT_NO_PATH
    r = parse_duffel_purchase_path(
        {"data": {"offers": []}, "_blocker": "http_429"},
        route="GRU-MIA", requested_cabin="business",
    )
    assert r.offer_found is False
    assert r.safe_next_step == SAFE_NEXT_NO_PATH
    assert "no_offer_in_test_search" in r.blockers
    assert "live_http_429" in r.blockers


# ----------------- 8 & 9. no dry-run; never /air/orders -----------------


def test_no_dry_run_endpoint_documented():
    # Fato do contrato Duffel: sem validação/dry-run de ordem.
    assert DUFFEL_HAS_ORDER_DRY_RUN is False
    payload = json.loads(
        (FIX / "duffel_order_fields_present.json").read_text(encoding="utf-8")
    )
    r = parse_duffel_purchase_path(payload, route="GRU-MIA")
    assert r.dry_run_available == "no"
    assert "no_validation_only_order_endpoint" in r.blockers


def test_spike_module_never_builds_orders_or_payment_url():
    src = (REPO / "flight_mapper" / "duffel_purchase_spike.py").read_text(
        encoding="utf-8"
    )
    # Nunca constrói URL de criação de ordem/pagamento.
    assert "api.duffel.com/air/orders" not in src
    assert "api.duffel.com/air/payments" not in src
    # E não importa/chama um cliente de orders.
    assert "create_order" not in src and "create_payment" not in src


def test_cli_end_to_end_test_mode_uses_offer_requests_only(monkeypatch):
    """Modo live-test mockado: o spike só faz Offer Request (POST
    /air/offer_requests), nunca /air/orders, e imprime o relatório."""
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_fake_xyz")
    sample = json.loads(
        (FIX / "duffel_order_fields_present.json").read_text(encoding="utf-8")
    )
    captured: dict = {}

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        assert "duffel_test_fake_xyz" not in req.full_url
        return _Resp(json.dumps(sample).encode("utf-8"))

    import flight_mapper.actionability_readiness as ar
    real = ar.duffel_live_search
    monkeypatch.setattr(
        ar, "duffel_live_search",
        lambda **kw: real(**{**kw, "urlopen_impl": fake_urlopen}),
    )

    code, out, err = _run([
        "duffel-purchase-spike", "--route", "GRU-MIA", "--cabin", "business",
        "--trip", "one_way", "--test-mode", "--departure", "2026-09-10",
    ])
    assert code == 0
    assert "offer_requests" in captured["url"]
    assert "orders" not in captured["url"] and "payments" not in captured["url"]
    assert captured["method"] == "POST"
    assert "safe_next_step:" in out
    assert "B) order_api_requires_sensitive_data" in out


# ----------------- 5. no leak -----------------


def test_output_never_leaks_sensitive(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_sentineltok")
    # Fixture com sentinelas de offer_id/passenger_id.
    payload = json.loads(
        (FIX / "duffel_order_fields_present.json").read_text(encoding="utf-8")
    )
    r = parse_duffel_purchase_path(
        payload, route="GRU-MIA", requested_cabin="business",
    )
    out = format_purchase_path_report(r)
    for sentinel in (
        "off_test_fields_present",   # offer_id
        "pas_test_present_1",        # passenger_id
        "duffel_test_sentineltok",   # token
        "api.duffel.com", "https://", "Bearer",
        "payment_requirements", "total_amount", "cabin_class",
        "order_id", "payment_id",
    ):
        assert sentinel not in out, f"LEAK: {sentinel!r}"


def test_report_schema_has_no_sensitive_fields():
    from dataclasses import fields
    from flight_mapper.duffel_purchase_spike import PurchasePathReport
    names = {f.name for f in fields(PurchasePathReport)}
    forbidden = {
        "offer_id", "order_id", "payment_id", "token", "access_token",
        "url", "deep_link", "payload", "passenger", "passengers",
        "given_name", "family_name", "born_on",
    }
    assert not (names & forbidden)


# ----------------- 6 & 7. no workflow / no Telegram -----------------


def test_spike_does_not_touch_production_workflow():
    wf = (REPO / ".github" / "workflows" / "flight-mapper.yml").read_text(
        encoding="utf-8"
    )
    # O workflow de produção NÃO referencia o spike.
    assert "duffel-purchase-spike" not in wf


def test_spike_module_has_no_telegram_integration():
    src = (REPO / "flight_mapper" / "duffel_purchase_spike.py").read_text(
        encoding="utf-8"
    )
    # Ignora docstrings/comentários (que podem citar "sem Telegram"): só
    # importa que NÃO há import/uso real do canal Telegram.
    assert "import" not in src or "notifier" not in src
    for needle in ("TelegramNotifier", "send_alert", "from .notifier"):
        assert needle not in src
