"""Testes específicos do PR #51 — política operacional aplicada
ao relatório diário do Telegram.

Cobre:
1. Cada seção decisória presente em estado vazio com texto honesto.
2. business + link clicável → 🟢 Executiva confirmada.
3. business + sem link clicável → 🟡 Verificação manual com dica.
4. preço bom sem cabine confirmada → 💸 Econômica possível
   (nunca 🟢 nem 🟡).
5. Histórico repetitivo → linguagem humana (sem "cache repetitivo").
6. Defesa de leak: relatório nunca contém URL completa, token,
   query string ou post_data.
7. Bloqueios por cabine continuam visíveis no painel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

import pytest

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message


def _now() -> datetime:
    return datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


def _empty_result() -> MonitorResult:
    return MonitorResult(scanned=0, quotes_received=0, alerts_sent=0, notes=[])


def _build(store: PriceStore, result: MonitorResult | None = None) -> str:
    return _build_message(result or _empty_result(), store, _now())


# ----------------- 1. seções decisórias presentes -----------------


def test_empty_store_shows_all_decisional_sections():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build(store)
    # 6 seções da política operacional
    assert "🟢 Executiva confirmada" in body
    assert "🟡 Verificação manual" in body
    assert "💸 Econômica possível" in body
    assert "👀 Sinais em observação" in body
    assert "🛡️ Bloqueios de segurança" in body
    assert "🧭 Status das fontes" in body


def test_empty_actionable_section_uses_honest_text():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build(store)
    actionable = body.split("🟢 Executiva confirmada")[1].split("🟡")[0]
    assert "Nenhuma executiva confirmada agora" in actionable


def test_empty_manual_check_section_uses_honest_text():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        body = _build(store)
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    assert "Nenhuma oferta confirmada sem link agora" in manual


# ----------------- 2. business + link → 🟢 -----------------


def _kiwi_actionable(store, key, origin, dest, brl):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10", "return_date": "2026-09-17",
        "source": "kiwi", "currency": "BRL",
        "amount": brl, "amount_brl_estimated": brl,
        "cabin": "business", "cabin_confirmed": True,
        "trip_type": "round_trip", "actionable_url": True,
        "deep_link": f"https://www.kiwi.com/deep/{origin}-{dest}-2026-09-10",
    }


def test_business_with_actionable_link_lands_in_executiva_confirmada():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _kiwi_actionable(store, "GRU-LHR-business", "GRU", "LHR", 9500.0)
        body = _build(store)
    actionable = body.split("🟢 Executiva confirmada")[1].split("🟡")[0]
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    assert "São Paulo → Londres" in actionable
    assert "São Paulo → Londres" not in manual
    # Hyperlink renderizado
    assert 'href="https://www.kiwi.com/deep/' in actionable
    # Score só aparece na 🟢
    assert "Score médio (executiva confirmada)" in body


# ----------------- 3. business + sem link → 🟡 -----------------


def _kiwi_no_link(store, key, origin, dest, brl):
    """Kiwi confirmou cabine mas deep_link veio inválido/não-acionável.
    Reproduz o caso real: cabine validada por Kiwi/SerpApi, mas a
    rota não tem URL clicável simples (ex.: SerpApi google_post_only)."""
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10", "return_date": "2026-09-17",
        "source": "kiwi", "currency": "BRL",
        "amount": brl, "amount_brl_estimated": brl,
        "cabin": "business", "cabin_confirmed": True,
        "trip_type": "round_trip", "actionable_url": False,
        "deep_link": None,
    }


def test_business_without_link_lands_in_verificacao_manual():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _kiwi_no_link(store, "GRU-MIA-business", "GRU", "MIA", 8000.0)
        body = _build(store)
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    actionable = body.split("🟢 Executiva confirmada")[1].split("🟡")[0]
    assert "São Paulo → Miami" in manual
    assert "São Paulo → Miami" not in actionable
    # Texto humano de orientação ao usuário
    assert (
        "Booking encontrado, mas sem link simples. "
        "Ação sugerida: verificar manualmente no Google Flights "
        "ou na companhia."
    ) in manual


def test_manual_check_section_uses_safe_language_no_url_or_post():
    """A dica de verificação manual NUNCA pode conter URL, query string
    ou texto técnico tipo post_data."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _kiwi_no_link(store, "GRU-MIA-business", "GRU", "MIA", 8000.0)
        body = _build(store)
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    forbidden = (
        "post_data", "?token=", "?ref=", "?cart=",
        "http://", "https://", "POST ", "GET ",
    )
    for needle in forbidden:
        assert needle not in manual, f"LEAK na 🟡: {needle!r}"


# ----------------- 4. preço bom sem cabine → 💸 -----------------


def _tp_unknown_cabin(store, key, origin, dest, usd, brl):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": origin, "destination": dest,
        "departure_date": "2026-09-10", "return_date": None,
        "source": "travelpayouts", "currency": "USD",
        "amount": usd, "amount_brl_estimated": brl, "fx_rate": 5.5,
        "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": "one_way", "actionable_url": False,
        "deep_link": None,
    }


def test_unconfirmed_good_price_lands_in_economica_possivel_not_executiva():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # USD 212 < piso forte EUA one_way (250) → "muito_forte"
        _tp_unknown_cabin(
            store, "GRU-MIA-one_way-business", "GRU", "MIA", 212.0, 1166.0,
        )
        body = _build(store)
    eco = body.split("💸 Econômica possível")[1].split("👀")[0]
    actionable = body.split("🟢 Executiva confirmada")[1].split("🟡")[0]
    manual = body.split("🟡 Verificação manual")[1].split("💸")[0]
    assert "São Paulo → Miami" in eco
    assert "São Paulo → Miami" not in actionable
    assert "São Paulo → Miami" not in manual
    # Rotulação obrigatória — nunca como executiva
    assert "Cabine: não confirmada" in eco


# ----------------- 5. linguagem humana no baseline_weak -----------------


def test_repetitive_history_uses_human_phrase_not_cache_repetitivo():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # 12 preços idênticos → baseline_weak + sufficient (n>=10)
        h = store.get("GRU-MIA-economy")
        for _ in range(12):
            h.push(1200.0)
        h.last_quote = {
            "origin": "GRU", "destination": "MIA",
            "departure_date": "2026-09-10", "return_date": None,
            "source": "travelpayouts", "currency": "USD",
            "amount": 220.0,
            "amount_brl_estimated": 1200.0, "fx_rate": 5.5,
            "cabin": "unknown", "cabin_confirmed": False,
            "trip_type": "one_way", "actionable_url": False,
            "deep_link": None,
        }
        body = _build(store)
    # termo técnico bloqueado
    assert "cache repetitivo" not in body
    # uma das frases humanas aprovadas aparece
    body_lower = body.lower()
    assert (
        "a fonte vem repetindo valores muito parecidos" in body_lower
        or "ainda não há variação suficiente para confirmar promoção" in body_lower
        or "preço forte, mas sem sinal claro de movimento real" in body_lower
    )


# ----------------- 6. defesa global de leak -----------------


def test_full_report_no_token_or_full_url_leak():
    """O relatório inteiro (com múltiplas rotas e cenários) NUNCA
    contém token bruto, post_data, query string sensível ou texto
    interno tipo "BR_secret"."""
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        _kiwi_actionable(store, "GRU-LHR-business", "GRU", "LHR", 9500.0)
        _kiwi_no_link(store, "GRU-MIA-business", "GRU", "MIA", 8000.0)
        _tp_unknown_cabin(
            store, "GRU-ORD-one_way-business", "GRU", "ORD", 220.0, 1210.0,
        )
        body = _build(store)
    forbidden = (
        "post_data",
        "BR_secret",
        "DEP_TOKEN_THAT_MUST_NEVER_LEAK",
        "BK_TOKEN_THAT_MUST_NEVER_LEAK",
        "?token=",
        "?ref=",
        "?cart=",
        "secret_payload",
        "secret_post_body",
        "secret_path",
        "secret_cart",
    )
    for needle in forbidden:
        assert needle not in body, f"LEAK no relatório: {needle!r}"


# ----------------- 7. bloqueios por cabine seguem visíveis -----------------


def test_cabin_blocked_counter_appears_in_security_block():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        result = MonitorResult(
            scanned=10, quotes_received=8, alerts_sent=0, notes=[],
            cabin_blocked=3,
            suspicious_blocked=1,
            currency_blocked=2,
        )
        body = _build(store, result)
    security = body.split("🛡️ Bloqueios de segurança")[1].split("🧭")[0]
    assert "cabine não confirmada: 3" in security
    assert "preço economicamente suspeito: 1" in security
    assert "câmbio ausente" in security
    # Painel decisório também segue intacto
    assert "📊 Ciclo recente" in body


# ----------------- 8. legacy omitidos -----------------


def test_legacy_unproven_currency_still_omitted_with_counter():
    with tempfile.TemporaryDirectory() as tmp:
        store = PriceStore(Path(tmp) / "h.json")
        # Rota legada: sem `currency` em last_quote
        h = store.get("GRU-AAA-business")
        h.push(2000.0)
        h.last_quote = {
            "origin": "GRU", "destination": "AAA",
            "amount": 2000.0,
            # `currency` ausente de propósito
            "cabin": "business", "cabin_confirmed": True,
            "trip_type": "round_trip", "source": "kiwi",
        }
        body = _build(store)
    # Rota legada NÃO aparece em nenhuma seção decisória
    assert "AAA" not in body
    # Contador de omitidas aparece no painel
    assert "Entradas legadas sem moeda comprovada (omitidas):" in body
