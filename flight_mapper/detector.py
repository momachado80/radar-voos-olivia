"""Decide se um preço atual deve disparar alerta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .state import RouteHistory
from .thresholds import ceiling_for


MIN_SAMPLES = 5
DROP_THRESHOLD = 0.25
DEDUPE_WINDOW_HOURS = 24
PRIORITY_DROP_THRESHOLD = 0.15
PRIORITY_DEDUPE_HOURS = 12

# Dedupe inteligente: dentro da janela, só liberamos novo alerta se o
# preço melhorou pelo menos o suficiente para compensar ruído de cache.
MIN_REALERT_IMPROVEMENT_BRL = 200.0
MIN_REALERT_IMPROVEMENT_PCT = 0.05


CRITERION_AVERAGE_DROP = "average_drop"
CRITERION_CEILING = "ceiling"


@dataclass
class Decision:
    alert: bool
    reason: str
    average: float | None = None
    drop_pct: float | None = None
    criterion: str = CRITERION_AVERAGE_DROP
    threshold: float | None = None


def _within_dedupe(
    history: RouteHistory,
    current_price: float,
    now: datetime,
    hours: int,
) -> bool:
    """True se um alerta recente deve suprimir um novo, dentro da janela.

    Considera melhoria mínima: preço só libera novo alerta se cair
    pelo menos `MIN_REALERT_IMPROVEMENT_BRL` em valor absoluto OU
    `MIN_REALERT_IMPROVEMENT_PCT` em proporção. Evita spam de cache
    em rota estável abaixo do teto.
    """
    if not history.last_alert_at or history.last_alert_price is None:
        return False
    last = datetime.fromisoformat(history.last_alert_at)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now - last >= timedelta(hours=hours):
        return False
    last_price = history.last_alert_price
    improvement_brl = last_price - current_price
    if improvement_brl <= 0:
        return True
    improvement_pct = improvement_brl / last_price if last_price > 0 else 0.0
    if (
        improvement_brl >= MIN_REALERT_IMPROVEMENT_BRL
        or improvement_pct >= MIN_REALERT_IMPROVEMENT_PCT
    ):
        return False
    return True


def evaluate(
    history: RouteHistory,
    current_price: float,
    now: datetime | None = None,
    *,
    priority: bool = False,
) -> Decision:
    now = now or datetime.now(timezone.utc)
    samples = len(history.prices)
    threshold = PRIORITY_DROP_THRESHOLD if priority else DROP_THRESHOLD
    dedupe_hours = PRIORITY_DEDUPE_HOURS if priority else DEDUPE_WINDOW_HOURS

    if samples < MIN_SAMPLES:
        return Decision(
            alert=False,
            reason=f"acumulando histórico ({samples}/{MIN_SAMPLES})",
            average=history.average,
            drop_pct=None,
            criterion=CRITERION_AVERAGE_DROP,
        )

    average = history.average
    assert average is not None
    drop_pct = (average - current_price) / average

    if drop_pct < threshold:
        return Decision(
            alert=False,
            reason=f"queda {drop_pct:.1%} < limite {threshold:.0%}",
            average=average,
            drop_pct=drop_pct,
            criterion=CRITERION_AVERAGE_DROP,
        )

    if _within_dedupe(history, current_price, now, dedupe_hours):
        return Decision(
            alert=False,
            reason=f"alerta repetido dentro de {dedupe_hours}h sem nova queda",
            average=average,
            drop_pct=drop_pct,
            criterion=CRITERION_AVERAGE_DROP,
        )

    return Decision(
        alert=True,
        reason=f"queda de {drop_pct:.1%} vs média histórica",
        average=average,
        drop_pct=drop_pct,
        criterion=CRITERION_AVERAGE_DROP,
    )


def evaluate_ceiling(
    history: RouteHistory,
    current_price: float,
    route_key: str,
    now: datetime | None = None,
    *,
    priority: bool = False,
) -> Decision:
    """Alerta quando o preço atual está abaixo do teto absoluto configurado."""
    now = now or datetime.now(timezone.utc)
    threshold = ceiling_for(route_key)
    if threshold is None:
        return Decision(
            alert=False,
            reason="sem teto configurado",
            criterion=CRITERION_CEILING,
        )

    if current_price > threshold:
        return Decision(
            alert=False,
            reason=f"preço R$ {current_price:.0f} > teto R$ {threshold:.0f}",
            criterion=CRITERION_CEILING,
            threshold=threshold,
        )

    dedupe_hours = PRIORITY_DEDUPE_HOURS if priority else DEDUPE_WINDOW_HOURS
    if _within_dedupe(history, current_price, now, dedupe_hours):
        return Decision(
            alert=False,
            reason=f"alerta repetido dentro de {dedupe_hours}h sem nova queda",
            criterion=CRITERION_CEILING,
            threshold=threshold,
        )

    return Decision(
        alert=True,
        reason=f"preço R$ {current_price:.0f} <= teto R$ {threshold:.0f}",
        criterion=CRITERION_CEILING,
        threshold=threshold,
    )
