"""PR D — regras de sanidade econômica (piso de preço suspeito).

Escopo: só cotações não-BRL-nativas/USD. Sem rede, sem Telegram.
"""

from __future__ import annotations

from pathlib import Path

from flight_mapper.monitor import Monitor
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.sanity import (
    SUSPICIOUS_FLOOR_BRL,
    is_suspicious_price,
    suspicious_reason,
)
from flight_mapper.state import PriceStore

_ROUTE = Route("GRU", "MIA", "EUA")


def _usd_quote(brl: float, *, trip=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS,
               suspicious=False) -> Quote:
    return Quote(
        route=_ROUTE,
        price_brl=brl,
        deep_link="https://www.kiwi.com/deep/GRU-MIA-2026-06-15",
        departure_date="2026-06-15",
        return_date="2026-06-22" if trip == TripType.ROUND_TRIP else None,
        source="travelpayouts",
        amount=brl / 5.5,
        currency="USD",
        amount_brl_estimated=brl,
        fx_rate=5.5,
        cabin=cabin,
        cabin_confirmed=True,
        trip_type=trip,
        suspicious=suspicious,
    )


class _CaptureNotifier:
    def __init__(self):
        self.alerts: list = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append(quote)
        return True

    def send(self, text):  # pragma: no cover
        return True


# ---------- pisos configurados ----------

def test_floor_constants():
    assert SUSPICIOUS_FLOOR_BRL[(TripType.ROUND_TRIP, Cabin.BUSINESS)] == 4000.0
    assert SUSPICIOUS_FLOOR_BRL[(TripType.ONE_WAY, Cabin.BUSINESS)] == 2500.0
    assert SUSPICIOUS_FLOOR_BRL[(TripType.ROUND_TRIP, Cabin.ECONOMY)] == 1800.0
    assert SUSPICIOUS_FLOOR_BRL[(TripType.ONE_WAY, Cabin.ECONOMY)] == 1000.0


# ---------- 1. business RT abaixo do piso ----------

def test_business_round_trip_below_floor_is_suspicious():
    q = _usd_quote(1276.0, trip=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS)
    assert is_suspicious_price(_ROUTE, q, 1276.0) is True
    assert "abaixo do piso" in suspicious_reason(_ROUTE, q, 1276.0)


# ---------- 2. business OW abaixo do piso ----------

def test_business_one_way_below_floor_is_suspicious():
    q = _usd_quote(2000.0, trip=TripType.ONE_WAY, cabin=Cabin.BUSINESS)
    assert is_suspicious_price(_ROUTE, q, 2000.0) is True


# ---------- 3. economy (ainda não usado) coberto por unidade ----------

def test_economy_floors_unit_covered():
    rt = _usd_quote(1500.0, trip=TripType.ROUND_TRIP, cabin=Cabin.ECONOMY)
    assert is_suspicious_price(_ROUTE, rt, 1500.0) is True   # < 1800
    ow = _usd_quote(900.0, trip=TripType.ONE_WAY, cabin=Cabin.ECONOMY)
    assert is_suspicious_price(_ROUTE, ow, 900.0) is True    # < 1000
    ok = _usd_quote(2500.0, trip=TripType.ROUND_TRIP, cabin=Cabin.ECONOMY)
    assert is_suspicious_price(_ROUTE, ok, 2500.0) is False  # > 1800


# ---------- 4. acima do piso não é suspeito ----------

def test_price_above_floor_not_suspicious():
    q = _usd_quote(6050.0, trip=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS)
    assert is_suspicious_price(_ROUTE, q, 6050.0) is False
    assert suspicious_reason(_ROUTE, q, 6050.0) is None


# ---------- escopo: BRL-nativo nunca aplica piso ----------

def test_brl_native_never_suspicious_by_floor():
    brl = Quote(
        route=_ROUTE, price_brl=1500.0, deep_link=None,
        departure_date="2026-06-15", return_date="2026-06-22",
        source="kiwi", cabin=Cabin.BUSINESS, cabin_confirmed=True,
    )  # currency default BRL
    assert is_suspicious_price(_ROUTE, brl, 1500.0) is False


# ---------- 7. quote.suspicious=True bloqueia mesmo passando piso ----------

def test_provider_flagged_suspicious_blocks_even_above_floor():
    q = _usd_quote(9000.0, suspicious=True)
    assert is_suspicious_price(_ROUTE, q, 9000.0) is True
    # vale também para BRL-nativo
    brl = Quote(
        route=_ROUTE, price_brl=9000.0, deep_link=None,
        departure_date="2026-06-15", return_date="2026-06-22",
        source="kiwi", cabin=Cabin.BUSINESS, cabin_confirmed=True,
        suspicious=True,
    )
    assert is_suspicious_price(_ROUTE, brl, 9000.0) is True


# ---------- 5/6. Monitor bloqueia suspeito e não chama notifier ----------

def test_monitor_blocks_suspicious_and_skips_notifier(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _P:
        def quote(self, route):
            return _usd_quote(1276.0)

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_P(), notifier=notifier, store=store, confirm_alerts=False,
    ).run_once([_ROUTE])

    assert result.suspicious_blocked == 1
    assert result.alerts_sent == 0
    assert notifier.alerts == []
    assert any(
        "alerta bloqueado: preço economicamente suspeito" in n
        for n in result.notes
    )
    # histórico preservado
    assert store.get(_ROUTE.key).prices == [1276.0]


# ---------- 8. cabine unknown continua bloqueada pelo gate de cabine ----------

def test_cabin_gate_still_blocks_unknown_without_conflict(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _P:
        def quote(self, route):
            return Quote(
                route=route, price_brl=9000.0, deep_link=None,
                departure_date="2026-06-15", return_date="2026-06-22",
                source="travelpayouts", amount=1636.0, currency="USD",
                amount_brl_estimated=9000.0, fx_rate=5.5,
                cabin=Cabin.UNKNOWN, cabin_confirmed=False,
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_P(), notifier=notifier, store=store, confirm_alerts=False,
    ).run_once([_ROUTE])

    # cabine bloqueia ANTES do gate de sanidade — sem conflito
    assert result.cabin_blocked == 1
    assert result.suspicious_blocked == 0
    assert result.alerts_sent == 0
    assert notifier.alerts == []


# ---------- 9. Kiwi/business confirmado plausível continua alertando ----------

def test_confirmed_plausible_business_still_alerts(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _P:
        def quote(self, route):
            # USD acima do piso (R$ 5.000 > R$ 4.000) e <= EXCELENTE
            # escalado de GRU-MIA (1100 USD * 5.5 = R$ 6.050) → alerta.
            return _usd_quote(5000.0)

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_P(), notifier=notifier, store=store, confirm_alerts=False,
    ).run_once([_ROUTE])

    assert result.suspicious_blocked == 0
    assert result.cabin_blocked == 0
    assert result.alerts_sent == 1
    assert len(notifier.alerts) == 1


# ---------- 10. canonical_key não consumido no pipeline ----------

def test_canonical_key_not_consumed_in_pipeline():
    import flight_mapper.monitor as m
    import flight_mapper.sanity as s
    for mod in (m, s):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "canonical_key" not in src
        assert "get_history" not in src
        assert "resolve_history_key" not in src
