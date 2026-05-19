"""PR: painel de confiança SEMPRE renderizado no relatório diário.

Sem rede, sem Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import _build_message

_NOW = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)


def _q(store, key, o, d, brl, *, trip="round_trip", currency="USD"):
    h = store.get(key)
    h.push(brl)
    h.last_quote = {
        "origin": o, "destination": d,
        "departure_date": "2026-09-10",
        "return_date": "2026-09-17" if trip == "round_trip" else None,
        "source": "travelpayouts", "currency": currency,
        "amount": round(brl / 5.5, 2), "amount_brl_estimated": brl,
        "fx_rate": 5.5, "cabin": "unknown", "cabin_confirmed": False,
        "trip_type": trip, "actionable_url": False, "deep_link": None,
    }


def _r(**kw):
    base = dict(scanned=20, quotes_received=10, alerts_sent=0, notes=[])
    base.update(kw)
    return MonitorResult(**base)


# 1+2+3+4 — painel completo sempre, sem fallback antigo
def test_panel_always_full_even_zero_quotes():
    store = PriceStore.__new__(PriceStore)
    store.path = Path("/tmp/none.json")
    store._data = {}
    body = _build_message(_r(quotes_received=0), store, _NOW)
    for sec in (
        "📌 Oportunidades confirmadas",
        "📡 Sinais brutos de preço",
        "💸 Possíveis promoções de econômica",
        "🛡️ Alertas bloqueados por segurança",
        "🧭 Status das fontes",
    ):
        assert sec in body
    # fallback/estrutura antiga eliminados
    assert "💸 Top 3 sinais brutos de menor preço" not in body
    assert "📡 Observação" not in body
    assert "Retornou 0 cotações" not in body


# 5 — dedupe / diferenciação por trip
def test_raw_signals_dedupe_and_trip_differentiated(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    # GRU-MIA round_trip e one_way, mesmo preço/fonte/cabine → não some
    # um do outro; ficam diferenciados pelo [trip]. >= piso business
    # round_trip (4000) e >= piso business one_way (2500) → ambos raw.
    _q(store, "GRU-MIA-business", "GRU", "MIA", 5000.0, trip="round_trip")
    _q(store, "GRU-MIA-one_way-business", "GRU", "MIA", 5000.0, trip="one_way")
    # duplicata EXATA (mesma assinatura) é colapsada
    _q(store, "GRU-MIA-business-dup", "GRU", "MIA", 5000.0, trip="round_trip")
    body = _build_message(_r(), store, _NOW)

    raw = body.split("📡 Sinais brutos de preço")[1].split("💸")[0]
    assert "[ida e volta]" in raw
    assert "[somente ida]" in raw
    # a duplicata exata round_trip não gera 3ª entrada
    assert raw.count("[ida e volta]") == 1
    assert raw.count("[somente ida]") == 1


# 6 — moeda não confirmada não entra no top3 principal
def test_unproven_currency_omitted(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-JFK-business")
    h.push(1919.0)  # sem last_quote → moeda não comprovada
    body = _build_message(_r(), store, _NOW)
    assert "R$ 1.919" not in body
    assert "moeda não confirmada" not in body
    assert "Entradas legadas sem moeda comprovada (omitidas): 1" in body
    assert "• Nenhum sinal bruto de preço no momento." in body


# 7 — fechamento correto
def test_closing_uses_sem_oportunidade_confirmada(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    body = _build_message(_r(quotes_received=0), store, _NOW)
    assert "Sem oportunidade confirmada agora." in body
    assert "Sem oportunidade dentro dos critérios de alerta agora." not in body
