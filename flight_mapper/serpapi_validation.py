"""Integração read-only opcional do SerpApi como validador de cabine/
preço/booking actionability para candidatos de sinal bruto.

PRINCÍPIO: SerpApi NÃO vira fonte de link de compra. O melhor estado
operacional que uma validação pode sugerir é `CONFIRMED_MANUAL_CHECK`
(🟡 Verificação manual), nunca `CONFIRMED_ACTIONABLE` (🟢 Executiva
confirmada). Isso porque os booking_options vêm tipicamente como
google.com POST → não é hyperlink simples → exige verificação manual.

Gates duros:
- Default DESLIGADO (`SERPAPI_VALIDATION_ENABLED` ausente/false → no-op).
- Sem `SERPAPI_API_KEY` → falha silenciosa, nunca exceção propaga.
- Limite por ciclo (`SERPAPI_VALIDATION_MAX_PER_CYCLE`, default 1,
  cap 1..3).
- Nunca executa POST, nunca abre URL, nunca grava token em data/*.
- Resultado sanitizado: NUNCA contém token bruto, URL completa, query
  string ou post_data — só estrutura.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .booking_actionability import (
    BookingActionability,
    OperationalDecision,
    classify_actionability,
)
from .serpapi_client import (
    SerpApiAuthError,
    SerpApiClient,
    SerpApiError,
    SerpApiOffer,
)


@dataclass(frozen=True)
class SerpApiValidationConfig:
    """Configuração do validador a partir de env vars."""

    enabled: bool = False
    max_per_cycle: int = 1
    api_key: str | None = None

    @classmethod
    def from_env(
        cls, env: dict | None = None,
    ) -> "SerpApiValidationConfig":
        e = env if env is not None else os.environ
        raw_enabled = str(e.get("SERPAPI_VALIDATION_ENABLED", "")).strip().lower()
        enabled = raw_enabled in ("true", "yes", "y", "1")
        raw_max = str(e.get("SERPAPI_VALIDATION_MAX_PER_CYCLE", "1") or "1")
        try:
            n = int(raw_max)
        except (TypeError, ValueError):
            n = 1
        max_per_cycle = max(1, min(n, 3))
        api_key = e.get("SERPAPI_API_KEY") or None
        return cls(
            enabled=enabled,
            max_per_cycle=max_per_cycle,
            api_key=api_key,
        )


@dataclass(frozen=True)
class SerpApiValidationCandidate:
    """Snapshot read-only de um candidato a validação. NUNCA contém
    token nem URL — apenas dados de rota usados como input para o
    SerpApi search."""

    key: str
    origin: str
    destination: str
    outbound_date: str
    return_date: str | None
    travel_class: str  # "business" | "economy"
    expected_usd: float | None  # opcional, para sanity


@dataclass(frozen=True)
class SerpApiValidationResult:
    """Resultado sanitizado de UMA validação SerpApi.

    NUNCA contém:
    - booking_token bruto (apenas presença/length via campos derivados);
    - departure_token bruto;
    - URL completa (apenas domínio quando aplicável, dentro de
      `actionability`);
    - post_data (apenas presença via `actionability`);
    - query string sensível.
    """

    key: str
    provider: str  # sempre "serpapi"
    cabin_confirmed: bool
    price_usd: float | None
    price_brl: float | None
    carriers: tuple[str, ...]
    actionability: BookingActionability
    suggested_decision: OperationalDecision
    reason_codes: tuple[str, ...]


# Reason codes — todos snake_case, nunca contêm payload sensível.
RC_DISABLED = "validation_disabled"
RC_NO_API_KEY = "no_api_key"
RC_OVER_QUOTA_CAP = "over_cycle_cap"
RC_SEARCH_FAILED = "search_failed"
RC_NO_DEPARTURE_TARGET = "no_departure_token_candidate"
RC_FOLLOWUP_FAILED = "departure_followup_failed"
RC_NO_BOOKING_TOKEN_IN_RETURN = "no_booking_token_in_return_offers"
RC_NO_BOOKING_TOKEN_IN_SEARCH = "no_booking_token_in_search"
RC_BOOKING_OPTIONS_FAILED = "booking_options_failed"
RC_CABIN_NOT_CONFIRMED = "cabin_not_confirmed"
RC_VALIDATION_OK = "validation_ok"


def _select_offer_with_token(
    offers: list[SerpApiOffer],
    target_cabin: str,
    *,
    require: str,  # "booking_token" | "departure_token"
) -> SerpApiOffer | None:
    """Retorna o 1º offer compatível com a cabine pedida E com o token
    requerido presente. Sem isso, None."""
    for off in offers:
        if target_cabin and off.cabin.value != target_cabin:
            continue
        if require == "booking_token" and not off.booking_token:
            continue
        if require == "departure_token" and not off.departure_token:
            continue
        return off
    return None


def _empty_result(
    key: str, *reason_codes: str,
) -> SerpApiValidationResult:
    return SerpApiValidationResult(
        key=key,
        provider="serpapi",
        cabin_confirmed=False,
        price_usd=None,
        price_brl=None,
        carriers=(),
        actionability=BookingActionability.UNKNOWN,
        suggested_decision=OperationalDecision.RAW_SIGNAL,
        reason_codes=reason_codes,
    )


def validate_with_serpapi(
    candidate: SerpApiValidationCandidate,
    client: SerpApiClient,
) -> SerpApiValidationResult:
    """Roda o fluxo SerpApi (1-3 hops conforme one_way / round_trip)
    e devolve resultado sanitizado.

    Read-only. Nunca abre URL, nunca executa POST, nunca grava em
    data/*. Falha silenciosa em qualquer erro de rede ou parse —
    retorna result com `reason_codes` indicando o ponto da falha.

    Cost por candidate:
    - one_way:     2 queries (search + booking_options).
    - round_trip:  3 queries (search + departure_followup + booking_options).
    """
    is_round_trip = bool(candidate.return_date)
    target_cabin = (candidate.travel_class or "").strip().lower()

    # Hop 1: search
    try:
        offers = client.search_google_flights(
            origin=candidate.origin,
            destination=candidate.destination,
            outbound_date=candidate.outbound_date,
            return_date=candidate.return_date,
            travel_class=candidate.travel_class,
        )
    except (SerpApiAuthError, SerpApiError, Exception):
        # Defesa ampla — qualquer falha em qualquer hop é silenciosa.
        return _empty_result(candidate.key, RC_SEARCH_FAILED)

    if not offers:
        return _empty_result(candidate.key, RC_SEARCH_FAILED)

    # Para round_trip, hop 2 (departure_followup) traz o booking_token.
    # Para one_way, hop 1 já traz booking_token no offer.
    if is_round_trip:
        dep_target = _select_offer_with_token(
            offers, target_cabin, require="departure_token",
        )
        if dep_target is None or not dep_target.departure_token:
            return _empty_result(candidate.key, RC_NO_DEPARTURE_TARGET)
        try:
            return_offers = client.fetch_departure_followup(
                departure_token=dep_target.departure_token,
                departure_id=candidate.origin,
                arrival_id=candidate.destination,
                outbound_date=candidate.outbound_date,
                return_date=candidate.return_date,
                travel_class=candidate.travel_class,
            )
        except (SerpApiAuthError, SerpApiError, Exception):
            return _empty_result(candidate.key, RC_FOLLOWUP_FAILED)
        booking_target = _select_offer_with_token(
            return_offers, target_cabin, require="booking_token",
        )
        if booking_target is None or not booking_target.booking_token:
            return _empty_result(
                candidate.key, RC_NO_BOOKING_TOKEN_IN_RETURN,
            )
    else:
        booking_target = _select_offer_with_token(
            offers, target_cabin, require="booking_token",
        )
        if booking_target is None or not booking_target.booking_token:
            return _empty_result(
                candidate.key, RC_NO_BOOKING_TOKEN_IN_SEARCH,
            )

    cabin_confirmed = booking_target.cabin.value == target_cabin
    price_usd = booking_target.price
    carriers = tuple(booking_target.carriers)

    # Hop final: booking_options
    try:
        options = client.fetch_booking_options(
            booking_token=booking_target.booking_token,
            departure_id=candidate.origin,
            arrival_id=candidate.destination,
            outbound_date=candidate.outbound_date,
            return_date=candidate.return_date,
            travel_class=candidate.travel_class,
        )
    except (SerpApiAuthError, SerpApiError, Exception):
        # Sem booking_options não dá pra classificar actionability,
        # mas já temos cabin/price/carriers confirmados.
        actionability = BookingActionability.ERROR
        reason_extra = (RC_BOOKING_OPTIONS_FAILED,)
        options = None
    else:
        actionability = classify_actionability(options)
        reason_extra = ()

    # Decisão sugerida — NUNCA CONFIRMED_ACTIONABLE via validação SerpApi.
    # SerpApi não vira fonte de link de compra (princípio do PR).
    if not cabin_confirmed:
        suggested = OperationalDecision.RAW_SIGNAL
        rc_main = RC_CABIN_NOT_CONFIRMED
    elif actionability in (
        BookingActionability.AIRLINE_SIMPLE_LINK,
        BookingActionability.OTA_SIMPLE_LINK,
        BookingActionability.MIXED_SIMPLE_AND_POST,
        BookingActionability.GOOGLE_POST_ONLY,
    ):
        # Mesmo se actionability for airline_simple_link, mantemos
        # MANUAL_CHECK — SerpApi não promove para 🟢.
        suggested = OperationalDecision.CONFIRMED_MANUAL_CHECK
        rc_main = RC_VALIDATION_OK
    else:
        # NO_CLICKABLE_URL / EMPTY / ERROR / UNKNOWN: cabine confirmada
        # mas booking sem caminho útil → ainda manual_check (cabine
        # confirmada já é informação valiosa).
        suggested = OperationalDecision.CONFIRMED_MANUAL_CHECK
        rc_main = RC_VALIDATION_OK

    return SerpApiValidationResult(
        key=candidate.key,
        provider="serpapi",
        cabin_confirmed=cabin_confirmed,
        price_usd=price_usd,
        price_brl=None,  # conversão fica para o consumidor
        carriers=carriers,
        actionability=actionability,
        suggested_decision=suggested,
        reason_codes=(rc_main,) + reason_extra,
    )


def validate_cycle_candidates(
    candidates: list[SerpApiValidationCandidate],
    config: SerpApiValidationConfig,
    client_factory=None,
) -> dict[str, SerpApiValidationResult]:
    """Orquestrador top-level. Devolve {route_key: SerpApiValidationResult}.

    Aplica os gates duros antes de criar o client:
    - config.enabled obrigatório;
    - SERPAPI_API_KEY obrigatório (sem ele, retorna dict vazio);
    - max_per_cycle aplicado (descarta candidatos além do limite).

    `client_factory` é opcional p/ injeção em testes — recebe `api_key`
    e devolve um `SerpApiClient`. Default constrói o real.

    NUNCA propaga exceção. Falhas individuais geram resultados com
    reason_codes; nada quebra o pipeline.
    """
    if not config.enabled:
        return {}
    if not config.api_key:
        # Mesmo sem chave, devolvemos dict vazio — gracefully no-op.
        return {}
    if not candidates:
        return {}

    if client_factory is None:
        client_factory = lambda key: SerpApiClient(key)

    try:
        client = client_factory(config.api_key)
    except Exception:
        return {}

    out: dict[str, SerpApiValidationResult] = {}
    for cand in candidates[: config.max_per_cycle]:
        try:
            res = validate_with_serpapi(cand, client)
        except Exception:
            res = _empty_result(cand.key, RC_SEARCH_FAILED)
        out[cand.key] = res
    return out


def humanize_validation_note(result: SerpApiValidationResult) -> str:
    """Frase humana p/ aparecer no relatório do Telegram quando uma
    validação eleva um sinal para 🟡 Verificação manual.

    NUNCA inclui token, URL completa nem post_data.
    """
    if not result.cabin_confirmed:
        return (
            "SerpApi consultou a rota mas não confirmou cabine business."
        )
    parts = ["Validado por SerpApi: cabine business"]
    if result.carriers:
        parts.append(f"carriers={','.join(result.carriers)}")
    if result.price_usd is not None:
        parts.append(f"preço SerpApi ~ USD {result.price_usd:.0f}")
    act = result.actionability
    if act == BookingActionability.GOOGLE_POST_ONLY:
        parts.append(
            "booking google_post_only (não é hyperlink simples)"
        )
    elif act in (
        BookingActionability.AIRLINE_SIMPLE_LINK,
        BookingActionability.OTA_SIMPLE_LINK,
        BookingActionability.MIXED_SIMPLE_AND_POST,
    ):
        parts.append(
            f"booking actionability={act.value}"
        )
    elif act == BookingActionability.NO_CLICKABLE_URL:
        parts.append("booking sem URL clicável")
    elif act == BookingActionability.ERROR:
        parts.append("booking_options falhou ao expandir")
    return (
        ", ".join(parts)
        + ". Ação sugerida: verificar manualmente no Google Flights "
        "ou na companhia."
    )
