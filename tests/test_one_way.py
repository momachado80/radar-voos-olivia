"""PR F1 — one-way business hot routes.

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
from flight_mapper.thresholds import (
    HOT_ROUTE_KEYS,
    hot_routes,
    levels_for,
    one_way_hot_routes,
    scaled_levels,
)

_RT = Route("GRU", "MIA", "EUA")  # round_trip default
_OW = Route("GRU", "MIA", "EUA", TripType.ONE_WAY)


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, payload, captured: dict):
    def _fake(req, *a, **k):
        captured["url"] = getattr(req, "full_url", req)
        return _FakeResp(payload)

    monkeypatch.setattr(providers, "urlopen", _fake)


class _CaptureNotifier:
    def __init__(self):
        self.alerts = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append(quote)
        return True

    def send(self, text):  # pragma: no cover
        return True


# ---------- chaves ----------

def test_one_way_route_key():
    assert _OW.key == "GRU-MIA-one_way-business"


def test_round_trip_route_key_preserved():
    assert _RT.key == "GRU-MIA-business"
    # não consome canonical_key
    assert _OW.canonical_key == "GRU-MIA-one_way-business"  # propriedade PR A
    assert _RT.canonical_key == "GRU-MIA-round_trip-business"


# ---------- thresholds ----------

def test_levels_for_one_way_returns_approved_values():
    assert levels_for("GRU-MIA-one_way-business") == {
        "excellent_brl": 700, "good_brl": 1000
    }
    assert levels_for("GRU-JFK-one_way-business") == {
        "excellent_brl": 900, "good_brl": 1300
    }
    assert levels_for("GRU-LHR-one_way-business") == {
        "excellent_brl": 1100, "good_brl": 1500
    }
    # round_trip inalterado
    assert levels_for("GRU-MIA-business") == {
        "excellent_brl": 1100, "good_brl": 1300
    }


def test_scaled_levels_converts_one_way_by_rate():
    lv = levels_for("GRU-MIA-one_way-business")
    scaled = scaled_levels(lv, 5.5)
    assert scaled == {"excellent_brl": round(700 * 5.5, 2),
                      "good_brl": round(1000 * 5.5, 2)}
    assert scaled_levels(lv, None) is None


# ---------- PriceStore separa históricos ----------

def test_pricestore_separates_round_trip_and_one_way(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    store.get(_RT.key).push(8000.0)
    store.get(_OW.key).push(4500.0)
    assert store.get("GRU-MIA-business").prices == [8000.0]
    assert store.get("GRU-MIA-one_way-business").prices == [4500.0]
    assert _RT.key != _OW.key


# ---------- hot routes ----------

def test_one_way_hot_routes_has_ten_one_way():
    ow = one_way_hot_routes()
    assert len(ow) == 10
    assert all(r.trip_type is TripType.ONE_WAY for r in ow)
    assert all(r.key.endswith("-one_way-business") for r in ow)
    dests = {r.destination for r in ow}
    assert dests == {
        "MIA", "JFK", "LAX", "SFO", "LHR",
        "CDG", "LIS", "MAD", "AMS", "FCO",
    }


def test_round_trip_hot_routes_unchanged():
    rt = hot_routes()
    assert all(r.trip_type is TripType.ROUND_TRIP for r in rt)
    assert all(r.key in HOT_ROUTE_KEYS for r in rt)
    assert all(r.key.count("-") == 2 for r in rt)  # O-D-business


# ---------- provider one-way ----------

def test_travelpayouts_one_way_param_and_quote(monkeypatch):
    cap: dict = {}
    _patch_urlopen(
        monkeypatch,
        {"success": True, "data": [
            {"price": 800, "departure_at": "2026-09-10T10:00:00Z",
             "return_at": None}
        ]},
        cap,
    )
    monkeypatch.setenv("USD_BRL_RATE", "5.50")
    q = TravelpayoutsProvider(token="x").quote(_OW)
    assert "one_way=true" in cap["url"]
    assert q.trip_type is TripType.ONE_WAY
    assert q.return_date is None
    assert q.cabin is Cabin.UNKNOWN
    assert q.cabin_confirmed is False


def test_kiwi_one_way_business_confirmed(monkeypatch):
    cap: dict = {}
    _patch_urlopen(
        monkeypatch,
        {"data": [
            {"price": 5200, "deep_link": "https://www.kiwi.com/deep/GRU-MIA",
             "local_departure": "2026-09-10T10:00:00Z", "route": [{}]}
        ]},
        cap,
    )
    q = KiwiTequilaProvider(api_key="x").quote(_OW)
    assert "flight_type=oneway" in cap["url"]
    assert "nights_in_dst" not in cap["url"]
    assert q.trip_type is TripType.ONE_WAY
    assert q.cabin is Cabin.BUSINESS
    assert q.cabin_confirmed is True


# ---------- monitor integração ----------

def test_monitor_one_way_kiwi_plausible_alerts_somente_ida(monkeypatch, tmp_path):
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _Confirmed:
        def quote(self, route):
            # USD business confirmado (fonte futura), one_way. Câmbio 5.5:
            # one_way GRU-MIA good = 1000 USD → R$ 5.500; excellent = 700
            # → R$ 3.850. amount_brl_estimated=4500 ⇒ entre excellent e
            # good (alerta "good"), acima do piso de sanidade (R$ 2.500).
            return Quote(
                route=route,
                price_brl=4500.0,
                deep_link="https://www.kiwi.com/deep/GRU-MIA-2026-09-10",
                departure_date="2026-09-10",
                return_date=None,
                source="kiwi",
                amount=818.0,
                currency="USD",
                amount_brl_estimated=4500.0,
                fx_rate=5.5,
                cabin=Cabin.BUSINESS,
                cabin_confirmed=True,
                trip_type=TripType.ONE_WAY,
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_Confirmed(), notifier=notifier, store=store,
        confirm_alerts=False,
    ).run_once([_OW])

    assert result.alerts_sent == 1
    from flight_mapper.notifier import format_alert
    from flight_mapper.detector import Decision, CRITERION_CEILING, LEVEL_GOOD
    body = format_alert(
        notifier.alerts[0],
        Decision(alert=True, reason="", criterion=CRITERION_CEILING,
                 threshold=5500.0, level=LEVEL_GOOD, score=70),
    )
    assert "(somente ida)" in body
    assert "→ 2026" not in body
    # histórico no namespace one-way (preço normalizado p/ BRL)
    assert store.get("GRU-MIA-one_way-business").prices == [4500.0]
    assert store.get("GRU-MIA-business").prices == []


def test_one_way_business_below_sanity_floor_blocked(monkeypatch, tmp_path):
    """USD one_way business < piso R$2.500 (PR D) → bloqueado."""
    monkeypatch.setenv("USD_BRL_RATE", "5.50")

    class _Usd:
        def quote(self, route):
            return Quote(
                route=route, price_brl=1100.0,
                deep_link="https://www.kiwi.com/deep/GRU-MIA",
                departure_date="2026-09-10", return_date=None,
                source="travelpayouts", amount=200.0, currency="USD",
                amount_brl_estimated=1100.0, fx_rate=5.5,
                cabin=Cabin.BUSINESS, cabin_confirmed=True,
                trip_type=TripType.ONE_WAY,
            )

    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=_Usd(), notifier=notifier, store=store,
        confirm_alerts=False,
    ).run_once([_OW])

    assert result.suspicious_blocked == 1
    assert result.alerts_sent == 0
    assert notifier.alerts == []


def test_travelpayouts_one_way_blocked_by_cabin_gate(monkeypatch, tmp_path):
    """Travelpayouts one_way segue cabin=unknown → gate de cabine bloqueia."""
    monkeypatch.setenv("USD_BRL_RATE", "5.50")
    cap: dict = {}
    _patch_urlopen(
        monkeypatch,
        {"success": True, "data": [
            {"price": 1200, "departure_at": "2026-09-10T10:00:00Z",
             "return_at": None}
        ]},
        cap,
    )
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    result = Monitor(
        provider=TravelpayoutsProvider(token="x"), notifier=notifier,
        store=store, confirm_alerts=False,
    ).run_once([_OW])

    assert result.cabin_blocked == 1
    assert result.alerts_sent == 0
    assert notifier.alerts == []


# ---------- robustez de parsers ----------

def test_split_key_parsers_tolerate_four_parts():
    from flight_mapper.diagnostics import _split_key
    from flight_mapper.status import _split_route_key
    assert _split_key("GRU-MIA-one_way-business") == ("GRU", "MIA")
    assert _split_route_key("GRU-MIA-one_way-business") == ("GRU", "MIA")


# ---------- canonical_key não consumido ----------

def test_canonical_key_not_consumed_in_pipeline():
    import flight_mapper.monitor as m
    import flight_mapper.providers as p
    import flight_mapper.thresholds as t
    for mod in (m, p, t):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "canonical_key" not in src
        assert "get_history" not in src
        assert "resolve_history_key" not in src
        assert "ensure_canonical_seed" not in src
