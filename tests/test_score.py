from __future__ import annotations

from flight_mapper.score import (
    SCORE_BAND_EXCELLENT,
    SCORE_BAND_GOOD,
    SCORE_BAND_LOW,
    SCORE_BAND_OBSERVE,
    compute_opportunity_score,
    score_band,
)
from flight_mapper.state import RouteHistory


# ---------- bands ----------

def test_score_band_excellent_at_90_or_above():
    assert score_band(100) == SCORE_BAND_EXCELLENT
    assert score_band(90) == SCORE_BAND_EXCELLENT


def test_score_band_good_between_75_and_89():
    assert score_band(89) == SCORE_BAND_GOOD
    assert score_band(75) == SCORE_BAND_GOOD


def test_score_band_observe_between_60_and_74():
    assert score_band(74) == SCORE_BAND_OBSERVE
    assert score_band(60) == SCORE_BAND_OBSERVE


def test_score_band_low_below_60():
    assert score_band(59) == SCORE_BAND_LOW
    assert score_band(0) == SCORE_BAND_LOW


def test_score_band_none_returns_low():
    assert score_band(None) == SCORE_BAND_LOW


# ---------- compute_opportunity_score ----------

def test_compute_score_excellent_full_score():
    """price excelente + boa distância + queda + hot + actionable + confirmed → topo."""
    history = RouteHistory(prices=[3000.0, 3000.0, 3000.0])
    levels = {"excellent_brl": 2400, "good_brl": 2800}
    score = compute_opportunity_score(
        price_brl=2000.0,  # bem abaixo de excellent
        levels=levels,
        history=history,
        actionable_url=True,
        confirmed=True,
        is_hot_route=True,
    )
    # 30 (excelente) + 20 (price/good = 0.71 ≤ 0.85) + 20 (drop 33%) + 10 + 10 + 10 = 100
    assert score == 100


def test_compute_score_good_typical_case():
    """Preço bem perto do good_brl, sem queda histórica, hot, actionable, confirmed."""
    levels = {"excellent_brl": 2400, "good_brl": 2800}
    score = compute_opportunity_score(
        price_brl=2750.0,  # 0.98 do good (level=good)
        levels=levels,
        history=None,
        actionable_url=True,
        confirmed=True,
        is_hot_route=True,
    )
    # 20 (good) + 10 (price/good ~0.98) + 0 (sem history) + 10 + 10 + 10 = 60
    assert score == 60


def test_compute_score_zero_without_anything():
    score = compute_opportunity_score(
        price_brl=5000.0,
        levels=None,
        history=None,
        actionable_url=False,
        confirmed=False,
        is_hot_route=False,
    )
    assert score == 0


def test_compute_score_capped_at_100():
    """Soma teórica > 100 deve travar em 100."""
    history = RouteHistory(prices=[100000.0])
    levels = {"excellent_brl": 1000, "good_brl": 2000}
    score = compute_opportunity_score(
        price_brl=500.0,
        levels=levels,
        history=history,
        actionable_url=True,
        confirmed=True,
        is_hot_route=True,
    )
    assert score == 100


def test_compute_score_handles_missing_excellent_brl():
    """Levels com excellent_brl=None (camada legada) ainda pontua via good_brl."""
    levels = {"excellent_brl": None, "good_brl": 2000}
    score = compute_opportunity_score(
        price_brl=1900.0,
        levels=levels,
        history=None,
        actionable_url=True,
        confirmed=False,
        is_hot_route=False,
    )
    # 20 (good) + 10 (price/good=0.95) + 10 (actionable) = 40
    assert score == 40


def test_compute_score_is_informational_not_filter():
    """Score baixo coexiste com flags positivas; não bloqueia decisão."""
    score = compute_opportunity_score(
        price_brl=10000.0,  # bem acima do good
        levels={"excellent_brl": 2000, "good_brl": 3000},
        history=None,
        actionable_url=True,
        confirmed=True,
        is_hot_route=True,
    )
    # 0 nível + 0 distance + 0 drop + 10 + 10 + 10 = 30
    assert score == 30
    assert score_band(score) == SCORE_BAND_LOW
