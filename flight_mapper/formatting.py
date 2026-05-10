"""Helpers de formatação compartilhados entre alertas e relatórios."""

from __future__ import annotations

from datetime import datetime


SOURCE_LABELS: dict[str, str] = {
    "travelpayouts": "Travelpayouts (cache)",
    "kiwi": "Kiwi",
    "mock": "Mock",
}


def format_brl(value: float) -> str:
    """Formata `1207.0` -> `R$ 1.207` (separador de milhar BR)."""
    raw = f"{value:,.0f}"
    return f"R$ {raw.replace(',', '.')}"


def format_source(source: str | None) -> str | None:
    """Mapeia o source do provider para label humano. None => None (chamador omite linha)."""
    if not source:
        return None
    return SOURCE_LABELS.get(source, source)


def format_detection_time(now: datetime) -> str:
    """`10/05 07:43 BRT` quando zoneinfo disponível; fallback `10/05 10:43 UTC`."""
    try:
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo("America/Sao_Paulo"))
        return local.strftime("%d/%m %H:%M BRT")
    except Exception:
        return now.strftime("%d/%m %H:%M UTC")
