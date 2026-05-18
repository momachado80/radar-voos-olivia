"""Relatório: separa oportunidades confirmadas de sinais brutos.

Travelpayouts/cabine não confirmada não pode ser rotulado como
Executiva/Business/oportunidade confirmada. Sem rede, sem Telegram.
"""

from __future__ import annotations

from pathlib import Path

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import StatusState, maybe_send_status


class _Stub:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return True

    def send_alert(self, *a, **k):  # pragma: no cover
        return True


def _result():
    return MonitorResult(scanned=20, quotes_received=10, alerts_sent=0, notes=[])


def _send(store, tmp_path):
    n = _Stub()
    maybe_send_status(
        result=_result(), store=store, state=StatusState(),
        notifier=n, state_path=tmp_path / "s.json",
    )
    return n.sent[0]


def _tp_unknown(store, key, origin, dest, usd, brl):
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


def _kiwi_confirmed(store, key, origin, dest, brl):
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


# 1 + 2 + 4
def test_unknown_cabin_is_raw_signal_not_executiva(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _tp_unknown(store, "GRU-MIA-one_way-business", "GRU", "MIA", 212.0, 1166.0)
    store.save()
    body = _send(store, tmp_path)

    # aparece como sinal bruto, com US$ ≈ R$ e "cabine não confirmada"
    assert "💸 Top 3 sinais brutos de menor preço" in body
    assert "US$ 212 ≈ R$ 1.166 — cabine não confirmada" in body
    # nunca rotulado como executiva/business/confirmada
    assert "Executiva" not in body
    assert "Business" not in body
    assert "Melhor oportunidade confirmada" not in body
    # seção de confirmadas existe, porém vazia
    assert "📌 Oportunidades confirmadas" in body
    assert "• Nenhuma oportunidade confirmada agora." in body


# 3 + 5
def test_confirmed_business_appears_as_confirmed(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _kiwi_confirmed(store, "GRU-CDG-business", "GRU", "CDG", 9000.0)
    store.save()
    body = _send(store, tmp_path)

    assert "📌 Oportunidades confirmadas" in body
    conf = body.split("📌 Oportunidades confirmadas")[1].split("💸")[0]
    assert "São Paulo → Paris (GRU → CDG)" in conf
    assert "Executiva" in conf
    assert "Conferir busca" in conf
    assert "• Nenhuma oportunidade confirmada agora." not in conf


# 6
def test_raw_signals_not_under_old_watchlist_heading(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _tp_unknown(store, "GRU-MAD-one_way-business", "GRU", "MAD", 388.0, 2134.0)
    store.save()
    body = _send(store, tmp_path)

    assert "📌 Melhores oportunidades monitoradas" not in body
    assert "São Paulo → Madri (GRU → MAD)" in body
    raw = body.split("💸 Top 3 sinais brutos de menor preço")[1]
    assert "cabine não confirmada" in raw


# mistura: confirmada + bruto coexistindo, cada um na sua seção
def test_confirmed_and_raw_coexist_in_separate_sections(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _kiwi_confirmed(store, "GRU-LHR-business", "GRU", "LHR", 9500.0)
    _tp_unknown(store, "GRU-MIA-one_way-business", "GRU", "MIA", 212.0, 1166.0)
    store.save()
    body = _send(store, tmp_path)

    conf = body.split("📌 Oportunidades confirmadas")[1].split("💸")[0]
    raw = body.split("💸 Top 3 sinais brutos de menor preço")[1]
    assert "São Paulo → Londres (GRU → LHR)" in conf and "Executiva" in conf
    assert "São Paulo → Miami (GRU → MIA)" in raw
    assert "cabine não confirmada" in raw
    assert "📡 Observação" in body


# preço suspeito + cabine confirmada (futuro USD) NÃO entra em confirmadas
def test_confirmed_but_suspicious_price_is_not_confirmed(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-MIA-one_way-business")
    h.push(1276.0)  # US$232 ≈ R$1.276 < piso one_way business R$2.500
    h.last_quote = {
        "origin": "GRU", "destination": "MIA",
        "departure_date": "2026-09-10", "return_date": None,
        "source": "kiwi", "currency": "USD",
        "amount": 232.0, "amount_brl_estimated": 1276.0, "fx_rate": 5.5,
        "cabin": "business", "cabin_confirmed": True,
        "trip_type": "one_way", "actionable_url": True,
        "deep_link": "https://www.kiwi.com/deep/GRU-MIA",
    }
    store.save()
    body = _send(store, tmp_path)

    assert "• Nenhuma oportunidade confirmada agora." in body
    assert "cabine não confirmada" in body  # cai em sinais brutos


# 7
def test_aviasales_never_present(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _tp_unknown(store, "GRU-MIA-one_way-business", "GRU", "MIA", 212.0, 1166.0)
    _kiwi_confirmed(store, "GRU-CDG-business", "GRU", "CDG", 9000.0)
    store.save()
    body = _send(store, tmp_path)
    assert "aviasales" not in body.lower()
    assert "search.aviasales.com" not in body.lower()
