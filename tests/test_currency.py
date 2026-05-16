"""Testes do módulo de correção/conversão de moeda."""

from __future__ import annotations

from flight_mapper.currency import (
    CURRENCY_BRL,
    CURRENCY_USD,
    USD_BRL_RATE_ENV,
    get_usd_brl_rate,
    to_brl,
)


# ---------- get_usd_brl_rate ----------

def test_rate_absent_returns_none():
    assert get_usd_brl_rate(env={}) is None


def test_rate_empty_string_returns_none():
    assert get_usd_brl_rate(env={USD_BRL_RATE_ENV: "  "}) is None


def test_rate_non_numeric_returns_none():
    assert get_usd_brl_rate(env={USD_BRL_RATE_ENV: "abc"}) is None


def test_rate_out_of_sanity_band_returns_none():
    assert get_usd_brl_rate(env={USD_BRL_RATE_ENV: "0.5"}) is None
    assert get_usd_brl_rate(env={USD_BRL_RATE_ENV: "99"}) is None


def test_rate_valid_parsed():
    assert get_usd_brl_rate(env={USD_BRL_RATE_ENV: "5.4"}) == 5.4


def test_rate_reads_process_env(monkeypatch):
    monkeypatch.setenv(USD_BRL_RATE_ENV, "5.25")
    assert get_usd_brl_rate() == 5.25


def test_rate_no_network(monkeypatch):
    """get_usd_brl_rate é puro: jamais abre socket."""
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sem rede")),
    )
    assert get_usd_brl_rate(env={USD_BRL_RATE_ENV: "5.4"}) == 5.4


# ---------- to_brl ----------

def test_brl_is_identity():
    assert to_brl(1500.0, CURRENCY_BRL, None) == 1500.0
    assert to_brl(1500.0, "brl", 5.4) == 1500.0


def test_usd_converted_with_rate():
    assert to_brl(2079.0, CURRENCY_USD, 5.4) == round(2079.0 * 5.4, 2)


def test_usd_without_rate_returns_none():
    assert to_brl(2079.0, CURRENCY_USD, None) is None


def test_unknown_currency_returns_none():
    assert to_brl(2079.0, "EUR", 5.4) is None
    assert to_brl(2079.0, "", 5.4) is None
