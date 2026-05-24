"""Helpers puros para classificar acionabilidade de booking options
e transformar isso em decisão operacional do Radar.

NUNCA chama rede. NUNCA toca PriceStore. NUNCA envia Telegram. NUNCA
imprime URL completa, token, post_data ou query string. Trabalha
apenas sobre `SerpApiBookingOption` já parseado (domínio + presença
de post_data + provider).

Camadas:
1. `BookingActionability` — 8 categorias para a NATUREZA do link.
2. `OperationalDecision` — 5 estados decisórios derivados da combinação
   de cabine, banda de preço, actionability, baseline e moeda.
3. `compute_decision(DecisionInputs) -> (OperationalDecision, reason)`
   — pura, totalmente testável, sem efeitos colaterais.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .serpapi_client import SerpApiBookingOption, url_domain


class BookingActionability(str, Enum):
    """Natureza do conjunto de booking_options para UM booking_token.
    Categorias mutuamente exclusivas; sempre cabe em exatamente uma."""

    AIRLINE_SIMPLE_LINK = "airline_simple_link"
    OTA_SIMPLE_LINK = "ota_simple_link"
    MIXED_SIMPLE_AND_POST = "mixed_simple_and_post"
    GOOGLE_POST_ONLY = "google_post_only"
    NO_CLICKABLE_URL = "no_clickable_url"
    EMPTY_BOOKING_OPTIONS = "empty_booking_options"
    ERROR = "error"
    UNKNOWN = "unknown"


class OperationalDecision(str, Enum):
    """Decisão operacional final do Radar sobre o que fazer com o sinal."""

    CONFIRMED_ACTIONABLE = "confirmed_actionable"
    CONFIRMED_MANUAL_CHECK = "confirmed_manual_check"
    POSSIBLE_ECONOMY = "possible_economy"
    WATCH_ONLY = "watch_only"
    RAW_SIGNAL = "raw_signal"
    BLOCKED = "blocked"


# Domínios reconhecidos como cia aérea direta. Lista intencionalmente
# pequena e conservadora — qualquer domínio fora dela vira "OTA" (ou
# UNKNOWN se for google.* — tratado separadamente).
_AIRLINE_DOMAIN_HINTS: tuple[str, ...] = (
    "latam.com",
    "copaair.com",
    "aa.com",
    "americanairlines.com",
    "iberia.com",
    "avianca.com",
    "united.com",
    "delta.com",
    "lufthansa.com",
    "airfrance.com",
    "klm.com",
    "ba.com",
    "britishairways.com",
    "emirates.com",
    "qatarairways.com",
    "ana.co.jp",
    "jal.co.jp",
    "singaporeair.com",
    "tap.pt",
    "flytap.com",
    "azul.com.br",
    "voegol.com.br",
)


def _is_airline_domain(host: str | None) -> bool:
    if not host:
        return False
    h = host.lower()
    return any(h == d or h.endswith("." + d) for d in _AIRLINE_DOMAIN_HINTS)


def _is_google_domain(host: str | None) -> bool:
    if not host:
        return False
    h = host.lower()
    return h == "google.com" or h.endswith(".google.com")


def classify_actionability(
    options: list[SerpApiBookingOption] | None,
    *,
    error: bool = False,
) -> BookingActionability:
    """Classifica a lista de booking options.

    Regras (decidem na ordem):
    - `error=True`             → ERROR (caller indicou falha de fetch)
    - `options is None`        → UNKNOWN (não tentou ainda)
    - lista vazia              → EMPTY_BOOKING_OPTIONS
    - nenhuma opção com URL    → NO_CLICKABLE_URL
    - simples + POST coexistem → MIXED_SIMPLE_AND_POST
    - só simples airline       → AIRLINE_SIMPLE_LINK
    - só simples OTA           → OTA_SIMPLE_LINK
    - só simples desconhecido  → UNKNOWN (não classifica como acionável)
    - só POST + domínio Google → GOOGLE_POST_ONLY
    - só POST não-Google       → UNKNOWN

    Pura. Não consome URL completa nem post_data — só `booking_url`
    (para extrair domínio via `url_domain`) e `has_post_data` (bool).
    """
    if error:
        return BookingActionability.ERROR
    if options is None:
        return BookingActionability.UNKNOWN
    if len(options) == 0:
        return BookingActionability.EMPTY_BOOKING_OPTIONS

    has_simple = False
    has_post = False
    airline_simple = False
    ota_simple = False
    google_post = False
    non_google_post = False

    for opt in options:
        if not opt.booking_url:
            continue
        host = url_domain(opt.booking_url)
        if opt.has_post_data:
            has_post = True
            if _is_google_domain(host):
                google_post = True
            else:
                non_google_post = True
        else:
            has_simple = True
            if _is_airline_domain(host):
                airline_simple = True
            elif _is_google_domain(host):
                # simples + google sozinho é estranho; trata como OTA
                # (não-airline) para fins de classificação.
                ota_simple = True
            else:
                ota_simple = True

    if not has_simple and not has_post:
        return BookingActionability.NO_CLICKABLE_URL
    if has_simple and has_post:
        return BookingActionability.MIXED_SIMPLE_AND_POST
    if has_simple:
        if airline_simple:
            return BookingActionability.AIRLINE_SIMPLE_LINK
        if ota_simple:
            return BookingActionability.OTA_SIMPLE_LINK
        return BookingActionability.UNKNOWN
    # has_post == True, has_simple == False
    if google_post and not non_google_post:
        return BookingActionability.GOOGLE_POST_ONLY
    return BookingActionability.UNKNOWN


@dataclass(frozen=True)
class DecisionInputs:
    """Entradas puras para `compute_decision`. Todos os campos vêm de
    inferências já feitas pelo pipeline existente (Cabin enum,
    deal_intelligence, sanity, history_stats) — este módulo não
    re-calcula nada."""

    cabin_confirmed: bool
    price_grade: str          # 'forte' | 'boa' | 'ignorar' | 'none'
    actionability: BookingActionability | None
    baseline_weak: bool
    suspicious: bool
    currency_known: bool


def compute_decision(
    inputs: DecisionInputs,
) -> tuple[OperationalDecision, str]:
    """Transforma sinais em decisão operacional + reason code.

    Ordem de avaliação (gates duros primeiro):
    1. BLOCKED se preço suspeito ou moeda desconhecida.
    2. Cabine confirmada + preço bom (forte/boa):
       - airline_simple OR ota_simple OR mixed_simple_and_post
                                                → CONFIRMED_ACTIONABLE
       - google_post_only                       → CONFIRMED_MANUAL_CHECK
       - empty/no_link/error/unknown/None       → CONFIRMED_MANUAL_CHECK
    3. Cabine confirmada + preço NÃO bom:
       - baseline_weak                          → WATCH_ONLY
       - caso contrário                         → WATCH_ONLY
    4. Cabine NÃO confirmada:
       - baseline_weak                          → WATCH_ONLY
       - preço forte/boa                        → POSSIBLE_ECONOMY
       - caso contrário                         → RAW_SIGNAL

    Pura. Sem rede, sem PriceStore, sem Telegram. `reason` é um código
    interno (snake_case) para logs/relatórios técnicos — NÃO destinado
    a virar texto direto no Telegram humano.
    """
    if inputs.suspicious:
        return OperationalDecision.BLOCKED, "suspicious_price"
    if not inputs.currency_known:
        return OperationalDecision.BLOCKED, "currency_unknown"

    price_good = inputs.price_grade in ("forte", "boa")

    if inputs.cabin_confirmed:
        if price_good:
            act = inputs.actionability
            if act in (
                BookingActionability.AIRLINE_SIMPLE_LINK,
                BookingActionability.OTA_SIMPLE_LINK,
                BookingActionability.MIXED_SIMPLE_AND_POST,
            ):
                return (
                    OperationalDecision.CONFIRMED_ACTIONABLE,
                    f"cabin_confirmed_{act.value}",
                )
            if act == BookingActionability.GOOGLE_POST_ONLY:
                return (
                    OperationalDecision.CONFIRMED_MANUAL_CHECK,
                    "cabin_confirmed_google_post_only",
                )
            # NO_CLICKABLE_URL / EMPTY / ERROR / UNKNOWN / None
            return (
                OperationalDecision.CONFIRMED_MANUAL_CHECK,
                "cabin_confirmed_no_usable_link",
            )
        # Cabine confirmada mas preço fraco
        if inputs.baseline_weak:
            return OperationalDecision.WATCH_ONLY, "baseline_weak"
        return OperationalDecision.WATCH_ONLY, "cabin_confirmed_price_neutral"

    # Cabine NÃO confirmada
    if inputs.baseline_weak:
        return OperationalDecision.WATCH_ONLY, "baseline_weak"
    if price_good:
        return OperationalDecision.POSSIBLE_ECONOMY, "no_cabin_price_graded"
    return OperationalDecision.RAW_SIGNAL, "no_cabin_no_grade"


# Frases humanas (PT) para uso em status.py / Telegram. Evita expor
# termos técnicos no relatório do usuário. `reason_code` técnico
# continua disponível para logs.
_HUMAN_PHRASES_BASELINE_WEAK: tuple[str, ...] = (
    "A fonte vem repetindo valores muito parecidos.",
    "Ainda não há variação suficiente para confirmar promoção real.",
    "Preço forte, mas sem sinal claro de movimento real.",
)


def humanize_baseline_weak() -> str:
    """Retorna a frase humana canônica usada no relatório do Telegram
    para sinalizar baseline fraco (em vez do termo técnico
    "cache repetitivo"). Determinístico — sempre devolve a 1ª frase.
    Caller pode escolher outra das alternativas em
    `_HUMAN_PHRASES_BASELINE_WEAK` se quiser variar."""
    return _HUMAN_PHRASES_BASELINE_WEAK[0]


def humanize_decision(decision: OperationalDecision) -> str:
    """Mapeamento decisão → rótulo amigável no Telegram."""
    return {
        OperationalDecision.CONFIRMED_ACTIONABLE: "Executiva confirmada",
        OperationalDecision.CONFIRMED_MANUAL_CHECK: (
            "Oportunidade para verificação manual"
        ),
        OperationalDecision.POSSIBLE_ECONOMY: "Econômica possível",
        OperationalDecision.WATCH_ONLY: "Sinal em observação",
        OperationalDecision.RAW_SIGNAL: "Sinal bruto de preço",
        OperationalDecision.BLOCKED: "Bloqueado por segurança",
    }[decision]


def manual_check_hint(actionability: BookingActionability | None) -> str:
    """Texto humano para acompanhar `confirmed_manual_check`.
    Apenas usa categoria de actionability — nunca URL completa
    nem post_data."""
    if actionability == BookingActionability.GOOGLE_POST_ONLY:
        return (
            "Booking encontrado, mas sem link simples. "
            "Ação sugerida: verificar manualmente no Google Flights "
            "ou na companhia."
        )
    if actionability == BookingActionability.NO_CLICKABLE_URL:
        return (
            "Booking sem URL clicável aproveitável. "
            "Ação sugerida: verificar manualmente na companhia."
        )
    if actionability == BookingActionability.EMPTY_BOOKING_OPTIONS:
        return (
            "Token de booking existe, mas sem opções utilizáveis. "
            "Ação sugerida: verificar manualmente."
        )
    return (
        "Cabine confirmada mas sem link acionável. "
        "Ação sugerida: verificar manualmente."
    )
