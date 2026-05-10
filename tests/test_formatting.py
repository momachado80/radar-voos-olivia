from __future__ import annotations

import sys
from datetime import datetime, timezone

from flight_mapper.formatting import format_brl, format_detection_time, format_source


def test_format_brl_thousands_separator_is_dot():
    assert format_brl(1207.0) == "R$ 1.207"
    assert format_brl(2483.0) == "R$ 2.483"
    assert format_brl(10000.0) == "R$ 10.000"


def test_format_brl_rounds_to_integer():
    assert format_brl(2140.49) == "R$ 2.140"
    assert format_brl(2140.51) == "R$ 2.141"


def test_format_brl_zero_and_small():
    assert format_brl(0.0) == "R$ 0"
    assert format_brl(99.0) == "R$ 99"


def test_format_source_known_values():
    assert format_source("travelpayouts") == "Travelpayouts (cache)"
    assert format_source("kiwi") == "Kiwi"
    assert format_source("mock") == "Mock"


def test_format_source_none_returns_none():
    assert format_source(None) is None
    assert format_source("") is None


def test_format_source_unknown_returns_raw():
    assert format_source("amadeus") == "amadeus"


def test_format_detection_time_returns_brt_or_utc():
    """10:43 UTC = 07:43 BRT (UTC-3, sem horário de verão).

    Aceita ambos os formatos porque o ambiente pode não ter tzdata.
    """
    now = datetime(2026, 5, 10, 10, 43, tzinfo=timezone.utc)
    out = format_detection_time(now)
    assert out in {"10/05 07:43 BRT", "10/05 10:43 UTC"}


def test_format_detection_time_falls_back_when_zoneinfo_missing(monkeypatch):
    """Simula ausência de zoneinfo: deve cair para UTC sem crash."""
    # Bloquear o import de zoneinfo dentro da função
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "zoneinfo":
            raise ImportError("zoneinfo blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    # também limpar cache do módulo já importado, se houver
    monkeypatch.setitem(sys.modules, "zoneinfo", None)

    now = datetime(2026, 5, 10, 10, 43, tzinfo=timezone.utc)
    out = format_detection_time(now)
    assert out == "10/05 10:43 UTC"
