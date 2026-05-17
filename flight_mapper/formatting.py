"""Helpers de formatação compartilhados entre alertas e relatórios."""

from __future__ import annotations

from datetime import datetime

from .regions import Cabin, TripType


def cabin_search_term(cabin: Cabin) -> str:
    """Termo de classe para URLs de busca auxiliar.

    economy → "economy"; business/unknown → "business". `unknown` cai em
    "business" de forma defensiva: em produção o alerta manual só ocorre
    com cabine business confirmada (o gate de cabine bloqueia unknown
    antes), então este fallback nunca aparece no caminho real.
    """
    return "economy" if cabin == Cabin.ECONOMY else "business"


def cabin_label_pt(cabin: Cabin, cabin_confirmed: bool) -> str:
    """Rótulo PT da classe para a linha de pesquisa manual.

    business confirmado → "executiva"; economy confirmado → "econômica";
    qualquer outro (unknown / não confirmado) → "cabine não confirmada".
    """
    if cabin_confirmed and cabin == Cabin.BUSINESS:
        return "executiva"
    if cabin_confirmed and cabin == Cabin.ECONOMY:
        return "econômica"
    return "cabine não confirmada"


def trip_label_pt(trip_type: TripType) -> str:
    """Rótulo PT do tipo de viagem para o título da mensagem."""
    if trip_type == TripType.ONE_WAY:
        return "somente ida"
    if trip_type == TripType.OPEN_JAW_CANDIDATE:
        return "multicidade candidata"
    return "ida e volta"


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
    """Formata `1878.0` -> `US$ 1.878` (separador de milhar BR p/ leitura)."""
    raw = f"{value:,.0f}"
    return f"US$ {raw.replace(',', '.')}"


def format_rate(rate: float) -> str:
    """`5.5` -> `5.50` (ponto decimal, como no env USD_BRL_RATE)."""
    return f"{rate:.2f}"


def format_fx_line(fx_rate: float | None) -> str | None:
    """Linha de câmbio do alerta: `Câmbio usado: USD_BRL_RATE=5.50`."""
    if fx_rate is None:
        return None
    return f"Câmbio usado: USD_BRL_RATE={format_rate(fx_rate)}"


def format_price(
    amount: float,
    currency: str,
    amount_brl_estimated: float | None,
    fx_rate: float | None = None,
) -> str:
    """Exibe o preço sem nunca mostrar `R$` sem certeza.

    - BRL confirmado: `R$ 1.207`.
    - USD: `US$ 1.878 ≈ R$ 10.329` (valor USD primário; BRL só como
      estimativa). O câmbio usado vai em linha própria via
      `format_fx_line` (Regra 6).
    - Moeda não comprovada: nunca em R$.
    """
    cur = (currency or "").strip().upper()
    if cur == "BRL":
        return format_brl(amount)
    if cur == "USD":
        usd = format_usd(amount)
        if amount_brl_estimated is not None:
            return f"{usd} ≈ {format_brl(amount_brl_estimated)}"
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
