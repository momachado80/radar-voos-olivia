"""PR C — validação honesta de cabin/trip_type no provider + gate de
cabine no Monitor.

Sem rede (urlopen monkeypatchado), sem Telegram (notifier fake/None).
"""

from __future__ import annotations

import json
from pathlib import Path

import flight_mapper.providers as providers
from flight_mapper.monitor import Monitor
from flight_mapper.providers import (
    KiwiTequilaProvider,
    Quote,
    TravelpayoutsProvider,
)
from flight_mapper.regions import Cabin, Route, TripType
from flight_mapper.state import PriceStore

_ROUTE = Route("GRU", "MIA", "EUA")  # route.cabin == BUSINESS (default)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, payload: dict):
    monkeypatch.setattr(
        providers,
        "urlopen",
        lambda *a, **k: _FakeResponse(payload),
    )


class _CaptureNotifier:
    def __init__(self):
        self.alerts: list = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append(quote)
        return True

    def send(self, text):  # pragma: no cover
        return True


# ---------- 1. Travelpayouts sem campo de cabine → unknown/unconfirmed ----------

def test_travelpayouts_unknown_cabin_when_no_reliable_field(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "success": True,
            "data": [
                {
                    "price": 232,
                    "departure_at": "2026-06-15T10:00:00Z",
                    "return_at": "2026-06-22T10:00:00Z",
                }
            ],
        },
    )
    monkeypatch.setenv("USD_BRL_RATE", "5.50")
    q = TravelpayoutsProvider(token="x").quote(_ROUTE)
    assert q is not None
    assert q.cabin is Cabin.UNKNOWN
    assert q.cabin_confirmed is False
    assert q.currency == "USD"


# ---------- 2. Travelpayouts com retorno → round_trip ----------

def test_travelpayouts_round_trip_when_return_present(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "success": True,
            "data": [
                {
                    "price": 900,
                    "departure_at": "2026-06-15T10:00:00Z",
                    "return_at": "2026-06-22T10:00:00Z",
                }
            ],
        },
    )
    monkeypatch.setenv("USD_BRL_RATE", "5.50")
    q = TravelpayoutsProvider(token="x").quote(_ROUTE)
    assert q.trip_type is TripType.ROUND_TRIP
    assert q.return_date == "2026-06-22"


# ---------- 3. Travelpayouts sem retorno → one_way ----------

def test_travelpayouts_one_way_when_no_return(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "success": True,
            "data": [
                {
                    "price": 600,
                    "departure_at": "2026-06-15T10:00:00Z",
                    "return_at": None,
                }
            ],
        },
    )
    monkeypatch.setenv("USD_BRL_RATE", "5.50")
    q = TravelpayoutsProvider(token="x").quote(_ROUTE)
    assert q.trip_type is TripType.ONE_WAY
    assert q.return_date is None


# ---------- 5. Kiwi: selected_cabins=C no servidor → business confirmado ----------

def test_kiwi_confirms_business_via_server_side_cabin_filter(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "data": [
                {
                    "price": 5000,
                    "deep_link": "https://www.kiwi.com/deep/GRU-MIA",
                    "local_departure": "2026-06-15T10:00:00Z",
                    "route": [{"local_departure": "2026-06-22T10:00:00Z"}],
                }
            ]
        },
    )
    q = KiwiTequilaProvider(api_key="x").quote(_ROUTE)
    assert q.cabin is Cabin.BUSINESS
    assert q.cabin_confirmed is True
    assert q.trip_type is TripType.ROUND_TRIP


# ---------- 4/5. Monitor bloqueia quando cabine não confirmada ----------

def test_monitor_blocks_alert_when_cabin_unconfirmed(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _UnconfirmedProvider:
        def quote(self, route):
            return Quote(
                route=route,
                price_brl=900.0,
                deep_link="https://www.kiwi.com/deep/GRU-MIA-2026-06-15",
                departure_date="2026-06-15",
                return_date="2026-06-22",
                source="travelpayouts",
                cabin=Cabin.UNKNOWN,
                cabin_confirmed=False,
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=_UnconfirmedProvider(), notifier=notifier, store=store,
        confirm_alerts=False,
    )
    result = monitor.run_once([_ROUTE])

    assert result.cabin_blocked == 1
    assert result.alerts_sent == 0
    # 6. notifier NÃO foi chamado
    assert notifier.alerts == []
    # 5. nota clara
    assert any(
        "alerta bloqueado: cabine não confirmada" in n for n in result.notes
    )
    # histórico preservado (continuidade da série, mesmo bloqueado)
    assert store.get(_ROUTE.key).prices == [900.0]


# ---------- 7. cabin=business + cabin_confirmed=True ainda alerta ----------

def test_confirmed_business_still_alerts(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _ConfirmedProvider:
        def quote(self, route):
            return Quote(
                route=route,
                price_brl=900.0,
                deep_link="https://www.kiwi.com/deep/GRU-MIA-2026-06-15",
                departure_date="2026-06-15",
                return_date="2026-06-22",
                source="kiwi",
                cabin=Cabin.BUSINESS,
                cabin_confirmed=True,
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=_ConfirmedProvider(), notifier=notifier, store=store,
        confirm_alerts=False,
    )
    result = monitor.run_once([_ROUTE])

    assert result.cabin_blocked == 0
    assert result.alerts_sent == 1
    assert len(notifier.alerts) == 1


# ---------- 8. Kiwi mantém comportamento (confirma business com segurança) ----------

def test_kiwi_one_way_when_no_return_leg(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        {
            "data": [
                {
                    "price": 4000,
                    "deep_link": "https://www.kiwi.com/deep/GRU-MIA",
                    "local_departure": "2026-06-15T10:00:00Z",
                    "route": [{}],
                }
            ]
        },
    )
    q = KiwiTequilaProvider(api_key="x").quote(_ROUTE)
    assert q.cabin is Cabin.BUSINESS
    assert q.cabin_confirmed is True
    assert q.trip_type is TripType.ONE_WAY


# ---------- 9/10. Route.key legado e canonical_key não consumido ----------

def test_route_key_legacy_and_canonical_not_consumed():
    assert _ROUTE.key == "GRU-MIA-business"
    assert _ROUTE.canonical_key == "GRU-MIA-round_trip-business"
    import flight_mapper.monitor as monitor_mod
    src = Path(monitor_mod.__file__).read_text(encoding="utf-8")
    assert "canonical_key" not in src
    assert "get_history" not in src
    assert "resolve_history_key" not in src


# ---------- _quote_to_dict reflete cabine real ----------

def test_quote_to_dict_records_real_cabin(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _UnconfirmedProvider:
        def quote(self, route):
            return Quote(
                route=route, price_brl=900.0, deep_link=None,
                departure_date="2026-06-15", return_date="2026-06-22",
                source="travelpayouts",
                cabin=Cabin.UNKNOWN, cabin_confirmed=False,
            )

    store = PriceStore(tmp_path / "h.json")
    Monitor(
        provider=_UnconfirmedProvider(), notifier=None, store=store,
        confirm_alerts=False,
    ).run_once([_ROUTE])
    lq = store.get(_ROUTE.key).last_quote
    assert lq["cabin"] == "unknown"
    assert lq["cabin_confirmed"] is False
    assert lq["trip_type"] == "round_trip"
