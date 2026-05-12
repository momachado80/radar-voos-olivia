"""Opportunity score: 0-100, informativo, calibrado para discussão.

NÃO usado como filtro de alerta neste pacote. Decisão de envio continua
sendo: ceiling (excellent/good) + detector legado + segunda checagem +
link acionável. O score adiciona contexto para calibrarmos os
thresholds depois de observação real.
"""

from __future__ import annotations

from .state import RouteHistory


# Bandas (constantes, usadas em testes e helpers de relatório)
SCORE_BAND_EXCELLENT = "EXCELENTE"   # >= 90
SCORE_BAND_GOOD = "BOM"              # >= 75
SCORE_BAND_OBSERVE = "OBSERVAR"      # >= 60
SCORE_BAND_LOW = "BAIXO"             # < 60


def score_band(score: int | None) -> str:
    if score is None:
        return SCORE_BAND_LOW
    if score >= 90:
        return SCORE_BAND_EXCELLENT
    if score >= 75:
        return SCORE_BAND_GOOD
    if score >= 60:
        return SCORE_BAND_OBSERVE
    return SCORE_BAND_LOW


def compute_opportunity_score(
    price_brl: float,
    levels: dict | None,
    history: RouteHistory | None = None,
    *,
    actionable_url: bool = False,
    confirmed: bool = False,
    is_hot_route: bool = False,
) -> int:
    """Retorna score 0-100.

    Critérios:
    - Nível (excellent/good): até 30 pts
    - Distância para good_brl: até 20 pts
    - Queda vs média histórica: até 20 pts
    - Rota quente: 10 pts
    - Link acionável: 10 pts
    - Confirmado pela 2ª checagem: 10 pts
    """
    score = 0

    excellent_brl = levels.get("excellent_brl") if levels else None
    good_brl = levels.get("good_brl") if levels else None

    # Nível (max 30)
    if excellent_brl is not None and price_brl <= excellent_brl:
        score += 30
    elif good_brl is not None and price_brl <= good_brl:
        score += 20

    # Distância para o alvo (max 20)
    if good_brl is not None and good_brl > 0:
        ratio = price_brl / good_brl
        if ratio <= 0.85:
            score += 20
        elif ratio <= 0.92:
            score += 15
        elif ratio <= 1.0:
            score += 10

    # Queda vs média histórica (max 20)
    if history is not None and history.average is not None and history.average > 0:
        drop_pct = (history.average - price_brl) / history.average
        if drop_pct >= 0.25:
            score += 20
        elif drop_pct >= 0.15:
            score += 15
        elif drop_pct >= 0.05:
            score += 10

    if is_hot_route:
        score += 10

    if actionable_url:
        score += 10

    if confirmed:
        score += 10

    return min(score, 100)
