"""Observabilidade: notas/logs do hot-scan trip-aware.

Sem rede, sem Telegram. Só formatação da nota — decisões inalteradas.
"""

from __future__ import annotations

from pathlib import Path

from flight_mapper.monitor import Monitor
from flight_mapper.providers import Quote
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore

_RT = Route("GRU", "LHR", "Europa")  # round_trip
_OW = Route("GRU", "MIA", "EUA", TripType.ONE_WAY)  # one_way


class _Notifier:
    def __init__(self):
        self.alerts = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append(quote)
        return True

    def send(self, text):  # pragma: no cover
        return True


def _kiwi_business(route, price):
    return Quote(
        route=route,
        price_brl=price,
        deep_link=f"https://www.kiwi.com/deep/{route.origin}-{route.destination}",
        departure_date="2026-09-10",
        return_date="2026-09-17" if route.trip_type == TripType.ROUND_TRIP else None,
        source="kiwi",
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
        trip_type=route.trip_type,
    )


def test_round_trip_note_contains_ida_e_volta(tmp_path: Path):
    class _P:
        def quote(self, route):
            return _kiwi_business(route, 1900.0)  # <= good GRU-LHR (2000)

    notifier = _Notifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_P(), notifier=notifier, store=store, confirm_alerts=False,
    ).run_once([_RT])

    joined = "\n".join(result.notes)
    assert "GRU→LHR [ida e volta]:" in joined
    assert "[somente ida]" not in joined
    # decisão preservada: rota plausível business confirmada ainda alerta
    assert result.alerts_sent == 1


def test_one_way_note_contains_somente_ida(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _Tp:  # Travelpayouts-like: cabin unknown → bloqueado
        def quote(self, route):
            return Quote(
                route=route, price_brl=8000.0, deep_link=None,
                departure_date="2026-09-10", return_date=None,
                source="travelpayouts", amount=1454.0, currency="USD",
                amount_brl_estimated=8000.0, fx_rate=5.5,
                cabin=Cabin.UNKNOWN, cabin_confirmed=False,
                trip_type=TripType.ONE_WAY,
            )

    notifier = _Notifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_Tp(), notifier=notifier, store=store, confirm_alerts=False,
    ).run_once([_OW])

    joined = "\n".join(result.notes)
    assert "GRU→MIA [somente ida]: alerta bloqueado: cabine não confirmada" in joined
    # decisão preservada: cabine não confirmada continua bloqueando
    assert result.cabin_blocked == 1
    assert notifier.alerts == []


def test_notes_disambiguate_same_route_diff_trip(tmp_path: Path, monkeypatch):
    """Mesma rota GRU-MIA em RT e OW gera notas distinguíveis."""
    monkeypatch.setenv("USD_BRL_RATE", "5.50")
    rt = Route("GRU", "MIA", "EUA")  # round_trip
    ow = Route("GRU", "MIA", "EUA", TripType.ONE_WAY)

    class _Tp:
        def quote(self, route):
            return Quote(
                route=route, price_brl=8000.0, deep_link=None,
                departure_date="2026-09-10",
                return_date="2026-09-17" if route.trip_type == TripType.ROUND_TRIP else None,
                source="travelpayouts", amount=1454.0, currency="USD",
                amount_brl_estimated=8000.0, fx_rate=5.5,
                cabin=Cabin.UNKNOWN, cabin_confirmed=False,
                trip_type=route.trip_type,
            )

    notifier = _Notifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_Tp(), notifier=notifier, store=store, confirm_alerts=False,
    ).run_once([rt, ow])

    joined = "\n".join(result.notes)
    assert "GRU→MIA [ida e volta]:" in joined
    assert "GRU→MIA [somente ida]:" in joined


def test_canonical_key_not_consumed_in_monitor():
    src = Path(
        __import__("flight_mapper.monitor", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert "canonical_key" not in src
    assert "get_history" not in src
    assert "resolve_history_key" not in src
    assert "ensure_canonical_seed" not in src
