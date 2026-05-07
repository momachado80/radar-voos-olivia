"""Decide se um preço atual deve disparar alerta."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .state import RouteHistory


MIN_SAMPLES = 5
DROP_THRESHOLD = 0.25
DEDUPE_WINDOW_HOURS = 24
PRIORITY_DROP_THRESHOLD = 0.15
PRIORITY_DEDUPE_HOURS = 12


@dataclass
class Decision:
    alert: bool
    reason: str
    average: float | None
    drop_pct: float | None


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
        )

    if history.last_alert_at and history.last_alert_price is not None:
        last = datetime.fromisoformat(history.last_alert_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        within_dedupe = now - last < timedelta(hours=dedupe_hours)
        if within_dedupe and current_price >= history.last_alert_price:
            return Decision(
                alert=False,
                reason=f"alerta repetido dentro de {dedupe_hours}h sem nova queda",
                average=average,
                drop_pct=drop_pct,
            )

    return Decision(
        alert=True,
        reason=f"queda de {drop_pct:.1%} vs média histórica",
        average=average,
        drop_pct=drop_pct,
    )
