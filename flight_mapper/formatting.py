"""Helpers de formatação compartilhados entre alertas e relatórios."""

from __future__ import annotations


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
