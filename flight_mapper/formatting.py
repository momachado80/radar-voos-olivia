"""Helpers de formatação compartilhados entre alertas e relatórios."""

from __future__ import annotations

from datetime import datetime


SOURCE_LABELS: dict[str, str] = {
    "travelpayouts": "Travelpayouts (cache)",
    "kiwi": "Kiwi",
    "mock": "Mock",
    # Composto: preço detectado no radar Travelpayouts, link comercial via Kiwi
    "travelpayouts+kiwi": "Travelpayouts + Kiwi",
}


def format_brl(value: float) -> str:
    """Formata `1207.0` -> `R$ 1.207` (separador de milhar BR)."""
    raw = f"{value:,.0f}"
    return f"R$ {raw.replace(',', '.')}"


def format_usd(value: float) -> str:
    """Formata `2079.0` -> `US$ 2,079` (separador de milhar US)."""
    return f"US$ {value:,.0f}"


def _fmt_rate(rate: float) -> str:
    """`5.4` -> `5,40` (vírgula decimal BR)."""
    return f"{rate:.2f}".replace(".", ",")


def format_price(
    amount: float,
    currency: str,
    amount_brl_estimated: float | None,
    fx_rate: float | None = None,
) -> str:
    """Exibe o preço sem nunca mostrar `R$` sem certeza.

    - BRL confirmado: `R$ 1.207`.
    - USD: `US$ 2,079 (≈ R$ 11.227 · câmbio USD_BRL_RATE=5,40)`. O valor
      em USD é o número primário; o BRL aparece só como estimativa
      rotulada com o câmbio usado.
    - Moeda não comprovada: nunca em R$.
    """
    cur = (currency or "").strip().upper()
    if cur == "BRL":
        return format_brl(amount)
    if cur == "USD":
        usd = format_usd(amount)
        if amount_brl_estimated is not None:
            tail = f"≈ {format_brl(amount_brl_estimated)}"
            if fx_rate is not None:
                tail += f" · câmbio USD_BRL_RATE={_fmt_rate(fx_rate)}"
            else:
                tail += " — conversão estimada"
            return f"{usd} ({tail})"
        return f"{usd} (conversão BRL indisponível — USD_BRL_RATE ausente)"
    return f"{amount:,.0f} {cur or '?'} (moeda não confirmada)"


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
