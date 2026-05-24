"""Testes do módulo `booking_actionability` (PR #50).

Cobre:
1. classify_actionability nos 8 estados.
2. compute_decision nos 6 estados (incluindo gates de bloqueio).
3. Frase humana de baseline_weak no relatório do Telegram
   (sem "cache repetitivo").
4. Defesa de zero leak no relatório (sem URL completa, query string,
   post_data ou token).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import pytest

from flight_mapper.booking_actionability import (
    BookingActionability,
    DecisionInputs,
    OperationalDecision,
    classify_actionability,
    compute_decision,
    humanize_baseline_weak,
    humanize_decision,
    manual_check_hint,
)
from flight_mapper.serpapi_client import SerpApiBookingOption


# ----------------- factory helpers (sem rede) -----------------


def _opt(provider: str, url: str | None, post: bool = False) -> SerpApiBookingOption:
    return SerpApiBookingOption(
        provider=provider,
        provider_raw=provider,
        price=1820.0,
        currency="USD",
        booking_url=url,
        has_post_data=post,
        raw=None,
    )


# ----------------- classify_actionability -----------------


def test_actionability_airline_simple_link():
    # Latam sem POST + American sem POST → airline_simple_link
    opts = [
        _opt("LATAM", "https://www.latam.com/checkout?token=abc"),
        _opt("American", "https://www.aa.com/booking?ref=xyz"),
    ]
    assert classify_actionability(opts) is (
        BookingActionability.AIRLINE_SIMPLE_LINK
    )


def test_actionability_ota_simple_link():
    # Kissandfly sem POST → OTA
    opts = [
        _opt("Kissandfly", "https://gflights.kissandfly.com/book?id=1"),
        _opt("Trip", "https://trip.com/checkout?cart=zzz"),
    ]
    assert classify_actionability(opts) is (
        BookingActionability.OTA_SIMPLE_LINK
    )


def test_actionability_mixed_simple_and_post():
    # Latam simples + Google POST
    opts = [
        _opt("LATAM", "https://www.latam.com/checkout?token=abc"),
        _opt(
            "Google", "https://www.google.com/travel/clk?token=abc", post=True,
        ),
    ]
    assert classify_actionability(opts) is (
        BookingActionability.MIXED_SIMPLE_AND_POST
    )


def test_actionability_google_post_only():
    # Run #11 real: tudo veio Google POST
    opts = [
        _opt(
            "BoA",
            "https://www.google.com/travel/clk?token=secret_payload",
            post=True,
        ),
        _opt(
            "Google",
            "https://www.google.com/travel/clk?token=another",
            post=True,
        ),
    ]
    assert classify_actionability(opts) is (
        BookingActionability.GOOGLE_POST_ONLY
    )


def test_actionability_empty_booking_options():
    assert classify_actionability([]) is (
        BookingActionability.EMPTY_BOOKING_OPTIONS
    )


def test_actionability_no_clickable_url():
    # Options sem booking_url em nenhuma
    opts = [
        _opt("ProviderSemURL", None),
        _opt("Outro", None),
    ]
    assert classify_actionability(opts) is (
        BookingActionability.NO_CLICKABLE_URL
    )


def test_actionability_error():
    assert classify_actionability([], error=True) is (
        BookingActionability.ERROR
    )
    assert classify_actionability(None, error=True) is (
        BookingActionability.ERROR
    )


def test_actionability_unknown_when_none():
    """Caller que ainda não tentou expandir passa None."""
    assert classify_actionability(None) is BookingActionability.UNKNOWN


def test_actionability_unknown_for_simple_link_unrecognized_domain():
    """Simples mas domínio não reconhecido (não airline E não Google)
    cai como OTA, não UNKNOWN — política de _is_airline_domain
    intencionalmente conservadora."""
    opts = [_opt("Random", "https://booking.example.com/buy?id=1")]
    assert classify_actionability(opts) is (
        BookingActionability.OTA_SIMPLE_LINK
    )


def test_actionability_unknown_for_post_unrecognized_domain():
    """POST em domínio não-Google: classifica como UNKNOWN
    (não dá pra prometer que vira link funcional)."""
    opts = [_opt("Random", "https://random.com/api/book", post=True)]
    assert classify_actionability(opts) is BookingActionability.UNKNOWN


# ----------------- compute_decision -----------------


def _inputs(**overrides) -> DecisionInputs:
    base = dict(
        cabin_confirmed=True,
        price_grade="forte",
        actionability=BookingActionability.AIRLINE_SIMPLE_LINK,
        baseline_weak=False,
        suspicious=False,
        currency_known=True,
    )
    base.update(overrides)
    return DecisionInputs(**base)


def test_decision_confirmed_actionable_airline():
    """Cabine business confirmada + preço forte + link de airline."""
    decision, reason = compute_decision(_inputs())
    assert decision is OperationalDecision.CONFIRMED_ACTIONABLE
    assert "airline" in reason


def test_decision_confirmed_actionable_ota():
    """OTA também conta como acionável — link clicável simples basta."""
    decision, reason = compute_decision(
        _inputs(actionability=BookingActionability.OTA_SIMPLE_LINK),
    )
    assert decision is OperationalDecision.CONFIRMED_ACTIONABLE
    assert "ota" in reason


def test_decision_confirmed_actionable_mixed():
    decision, _ = compute_decision(
        _inputs(actionability=BookingActionability.MIXED_SIMPLE_AND_POST),
    )
    assert decision is OperationalDecision.CONFIRMED_ACTIONABLE


def test_decision_confirmed_manual_check_google_post_only():
    """Run #11 real: Google POST only → manual check, não actionable."""
    decision, reason = compute_decision(
        _inputs(actionability=BookingActionability.GOOGLE_POST_ONLY),
    )
    assert decision is OperationalDecision.CONFIRMED_MANUAL_CHECK
    assert "google_post" in reason


def test_decision_confirmed_manual_check_when_no_link():
    """Cabine + preço bom mas sem URL aproveitável → manual check."""
    for act in (
        BookingActionability.NO_CLICKABLE_URL,
        BookingActionability.EMPTY_BOOKING_OPTIONS,
        BookingActionability.ERROR,
        BookingActionability.UNKNOWN,
        None,
    ):
        decision, _ = compute_decision(_inputs(actionability=act))
        assert decision is OperationalDecision.CONFIRMED_MANUAL_CHECK, act


def test_decision_possible_economy_no_cabin():
    """Sem cabine confirmada + preço bom → econômica possível."""
    decision, reason = compute_decision(
        _inputs(cabin_confirmed=False, price_grade="boa", actionability=None),
    )
    assert decision is OperationalDecision.POSSIBLE_ECONOMY
    assert reason == "no_cabin_price_graded"


def test_decision_raw_signal_no_cabin_no_grade():
    """Sem cabine + sem grading bom → sinal bruto."""
    decision, reason = compute_decision(
        _inputs(
            cabin_confirmed=False, price_grade="none", actionability=None,
        ),
    )
    assert decision is OperationalDecision.RAW_SIGNAL
    assert reason == "no_cabin_no_grade"


def test_decision_watch_only_baseline_weak_with_cabin():
    """Cabine confirmada mas preço fraco + baseline_weak → watch."""
    decision, reason = compute_decision(
        _inputs(
            price_grade="ignorar",
            actionability=None,
            baseline_weak=True,
        ),
    )
    assert decision is OperationalDecision.WATCH_ONLY


def test_decision_watch_only_baseline_weak_without_cabin():
    """Sem cabine + baseline fraco → watch, NÃO promove a possível
    econômica mesmo se price_grade='forte'."""
    decision, reason = compute_decision(
        _inputs(
            cabin_confirmed=False,
            price_grade="forte",
            actionability=None,
            baseline_weak=True,
        ),
    )
    assert decision is OperationalDecision.WATCH_ONLY
    assert reason == "baseline_weak"


def test_decision_blocked_suspicious():
    """Preço suspeito sempre bloqueia, mesmo com cabine confirmada."""
    decision, reason = compute_decision(_inputs(suspicious=True))
    assert decision is OperationalDecision.BLOCKED
    assert reason == "suspicious_price"


def test_decision_blocked_currency_unknown():
    """Moeda desconhecida bloqueia."""
    decision, reason = compute_decision(_inputs(currency_known=False))
    assert decision is OperationalDecision.BLOCKED
    assert reason == "currency_unknown"


# ----------------- humanize helpers (sem leak) -----------------


def test_humanize_baseline_weak_returns_user_friendly_phrase():
    """Frase humana NÃO pode conter o termo técnico 'cache repetitivo'."""
    phrase = humanize_baseline_weak()
    assert "cache repetitivo" not in phrase.lower()
    assert "fonte" in phrase.lower() or "valores" in phrase.lower()


def test_humanize_decision_covers_all_states():
    """Cada estado decisório tem rótulo humano em português."""
    for d in OperationalDecision:
        label = humanize_decision(d)
        assert isinstance(label, str)
        assert len(label) > 0
        # nenhum rótulo deve expor termo técnico
        assert "_" not in label, f"{d}: {label!r}"


def test_manual_check_hint_google_post_only():
    msg = manual_check_hint(BookingActionability.GOOGLE_POST_ONLY)
    assert "verificar manualmente" in msg.lower()
    assert "google" in msg.lower() or "companhia" in msg.lower()
    # Sem URL nem post_data
    assert "http" not in msg.lower()
    assert "post_data" not in msg.lower()


def test_manual_check_hint_no_clickable_url():
    msg = manual_check_hint(BookingActionability.NO_CLICKABLE_URL)
    assert "verificar manualmente" in msg.lower()


# ----------------- status.py: relatório humano sem leak -----------------


def test_status_report_uses_human_phrase_not_cache_repetitivo():
    """O relatório do Telegram (build_message) NÃO pode usar o termo
    técnico 'cache repetitivo' no texto humano. Deve usar uma das
    frases aprovadas — começando por 'a fonte vem repetindo valores'."""
    # Lazy imports p/ não puxar deps de PriceStore se não rodar este teste
    from flight_mapper.state import PriceStore
    from flight_mapper.status import _build_message
    from flight_mapper.monitor import MonitorResult

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # Rota economy USA com preço bom (banda USD < piso forte) e
        # 12 valores repetidos → baseline_weak=True
        key = "GRU-MIA-economy"
        h = store.get(key)
        for _ in range(12):
            h.push(1200.0)
        # last_quote para o sinal aparecer no Possíveis promoções
        h.last_quote = {
            "price_brl": 1200.0,
            "amount": 220.0,        # USD < 250 forte EUA one_way
            "currency": "USD",
            "amount_brl_estimated": 1200.0,
            "fx_rate": 5.5,
            "origin": "GRU",
            "destination": "MIA",
            "departure_date": "2026-09-10",
            "return_date": None,
            "source": "travelpayouts",
            "cabin": "unknown",
            "cabin_confirmed": False,
            "trip_type": "one_way",
            "deep_link": None,
            "detected_at": "2026-05-12T14:00:00+00:00",
            "actionable_url": False,
            "provider_note": None,
        }
        result = MonitorResult(
            scanned=1, quotes_received=1, alerts_sent=0, notes=[],
        )
        now = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
        message = _build_message(result, store, now)

    # Defesa principal: termo técnico não aparece no texto humano
    assert "cache repetitivo" not in message
    # Frase humana aprovada aparece
    assert "a fonte vem repetindo valores muito parecidos" in message.lower()
    # E a marcação "variação muito baixa" continua (deal_intelligence
    # context — não conflita com a frase humana).
    assert "variação muito baixa" in message


def test_status_report_no_url_token_or_post_data_leaks():
    """O relatório NUNCA imprime URL completa, post_data ou token
    bruto — mesmo quando o histórico tem deep_link gravado."""
    from flight_mapper.state import PriceStore
    from flight_mapper.status import _build_message
    from flight_mapper.monitor import MonitorResult

    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        key = "GRU-LHR-business"
        h = store.get(key)
        for p in [2300, 2350, 2320, 2280, 2290, 2310, 2330, 2300, 2340, 2310]:
            h.push(float(p))
        # Deep link curto e seguro (Kiwi-like). O texto do relatório
        # pode renderizar o link como `actionable_url=True`, mas nunca
        # imprimir post_data ou token bruto literal.
        h.last_quote = {
            "price_brl": 2300.0,
            "amount": 418.0,
            "currency": "USD",
            "amount_brl_estimated": 2300.0,
            "fx_rate": 5.5,
            "origin": "GRU",
            "destination": "LHR",
            "departure_date": "2026-09-10",
            "return_date": "2026-09-17",
            "source": "kiwi",
            "cabin": "business",
            "cabin_confirmed": True,
            "trip_type": "round_trip",
            "deep_link": (
                "https://www.kiwi.com/deep/GRU-LHR-2026-09-10-2026-09-17"
            ),
            "detected_at": "2026-05-12T14:00:00+00:00",
            "actionable_url": True,
            "provider_note": None,
        }
        result = MonitorResult(
            scanned=1, quotes_received=1, alerts_sent=0, notes=[],
        )
        now = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
        message = _build_message(result, store, now)

    # Defesas explícitas — nada disso pode aparecer no texto do Telegram
    forbidden = [
        "post_data",
        "BR_secret",       # sentinelas dos PRs 44/46/48
        "DEP_TOKEN_THAT_MUST_NEVER_LEAK",
        "BK_TOKEN_THAT_MUST_NEVER_LEAK",
        "?token=",
        "?ref=",
        "?cart=",
        "secret_payload",
        "secret_post_body",
    ]
    for needle in forbidden:
        assert needle not in message, f"LEAK no relatório: {needle!r}"


# ----------------- garantias gerais -----------------


def test_booking_actionability_not_consumed_by_pipeline_core():
    """booking_actionability é só decisão/relatório. NÃO pode ser
    consumido pelo motor (monitor/providers/notifier/detector)."""
    for mod in ("monitor.py", "providers.py", "notifier.py", "detector.py"):
        src = (Path("flight_mapper") / mod).read_text(encoding="utf-8")
        assert "booking_actionability" not in src, mod
        assert "BookingActionability" not in src, mod
        assert "OperationalDecision" not in src, mod
        assert "compute_decision" not in src, mod
