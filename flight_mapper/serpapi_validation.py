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

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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


# Default conservador para o plano gratuito SerpApi (~100 queries/mês).
# Reserva margem de segurança: 90 queries/mês = 30 validações/mês (cada
# validação custa até 3 queries — ver `ESTIMATED_QUERIES_PER_VALIDATION`).
# Pode ser ajustado pelo workflow via env SERPAPI_VALIDATION_MONTHLY_BUDGET
# sem precisar rebuild de código.
DEFAULT_MONTHLY_BUDGET = 90

# Custo conservador (queries SerpApi) por validação tentada. Cobre tanto
# o caminho one-way (2 queries: search + booking_options) quanto
# round-trip (3 queries: search + departure_followup + booking_options).
# Sempre reservamos o pior caso para nunca estourar o orçamento.
ESTIMATED_QUERIES_PER_VALIDATION = 3


@dataclass(frozen=True)
class SerpApiValidationConfig:
    """Configuração do validador a partir de env vars."""

    enabled: bool = False
    max_per_cycle: int = 1
    monthly_budget: int = DEFAULT_MONTHLY_BUDGET
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
        raw_budget = str(
            e.get(
                "SERPAPI_VALIDATION_MONTHLY_BUDGET",
                str(DEFAULT_MONTHLY_BUDGET),
            ) or str(DEFAULT_MONTHLY_BUDGET)
        )
        try:
            b = int(raw_budget)
        except (TypeError, ValueError):
            b = DEFAULT_MONTHLY_BUDGET
        # Cap inferior 0 (=desliga validação); cap superior 10000 cobre
        # planos pagos generosos sem permitir typo cataclísmico.
        monthly_budget = max(0, min(b, 10000))
        api_key = e.get("SERPAPI_API_KEY") or None
        return cls(
            enabled=enabled,
            max_per_cycle=max_per_cycle,
            monthly_budget=monthly_budget,
            api_key=api_key,
        )


@dataclass(frozen=True)
class SerpApiValidationBudget:
    """Snapshot MENSAL do orçamento. PERSISTÊNCIA MÍNIMA: apenas
    mês UTC + contador de queries estimadas consumidas. NUNCA armazena
    token, URL, post_data, payload, carriers, preço, rota ou qualquer
    dado sensível — schema fechado em (month_utc, count).
    """

    month_utc: str  # "YYYY-MM" (UTC)
    count: int      # queries SerpApi estimadas consumidas neste mês

    @classmethod
    def load(cls, path: Path | None) -> "SerpApiValidationBudget":
        """Carrega do disco. Se arquivo ausente / inválido / com schema
        diferente do esperado, retorna budget zero do mês atual (UTC).

        Defensivo: NÃO propaga JSON malformado, campo extra ou schema
        antigo (date_utc) — ignora silenciosamente e devolve budget
        novo. Garante que arquivo corrompido nunca quebra o relatório.
        """
        this_month = datetime.now(timezone.utc).strftime("%Y-%m")
        if path is None or not path.exists():
            return cls(month_utc=this_month, count=0)
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return cls(month_utc=this_month, count=0)
        if not isinstance(raw, dict):
            return cls(month_utc=this_month, count=0)
        # Detecta schema antigo do PR #54 ({date_utc, count}) sem
        # `month_utc` → migração silenciosa para schema novo, descartando
        # o `count` legado (a contagem diária não traduz pra mensal).
        if "month_utc" not in raw:
            return cls(month_utc=this_month, count=0)
        month_utc = str(raw["month_utc"] or this_month)
        try:
            count = max(0, int(raw.get("count") or 0))
        except (TypeError, ValueError):
            count = 0
        # Schema-strict: ignoramos qualquer chave extra (incluindo
        # date_utc legado) que tenha sido escrita por engano.
        return cls(month_utc=month_utc, count=count)

    def save(self, path: Path | None) -> None:
        """Grava o snapshot mínimo. Schema fechado garante zero leak.

        NUNCA grava token, URL, post_data, payload, carriers, preço ou
        rota. Se `path` for None, no-op silencioso (útil em testes /
        fixture mode)."""
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"month_utc": self.month_utc, "count": self.count},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            # Defensivo — falha de I/O não pode derrubar o ciclo.
            pass

    def reset_if_new_month(
        self, this_month_utc: str,
    ) -> "SerpApiValidationBudget":
        """Se mudou o mês UTC, devolve budget zerado. Caso contrário,
        devolve `self` (imutável — não há side effect)."""
        if self.month_utc != this_month_utc:
            return SerpApiValidationBudget(
                month_utc=this_month_utc, count=0,
            )
        return self

    def add_queries(self, n: int) -> "SerpApiValidationBudget":
        """Incrementa o contador pelo custo estimado em queries.
        Imutável."""
        return SerpApiValidationBudget(
            month_utc=self.month_utc,
            count=self.count + max(0, int(n)),
        )

    def remaining(self, monthly_budget: int) -> int:
        return max(0, monthly_budget - self.count)


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
    # PR #60: cabine business é UMA evidência; preço diferente do
    # sinal original significa que SerpApi achou OUTRA executiva, não
    # validou a tarifa do Travelpayouts. `price_compatible=False` impede
    # elevação para 🟡 (vide compute_decision em status.py).
    price_compatible: bool = False


@dataclass(frozen=True)
class SerpApiValidationSummary:
    """Snapshot SANITIZADO da validação SerpApi em UM ciclo, p/ render
    no bloco 🧭 Status das fontes do Telegram.

    NUNCA contém:
    - token bruto;
    - URL completa nem query string;
    - post_data nem payload;
    - raw response nem raw exception;
    - rota, preço, carriers de candidatos individuais.

    Apenas contadores agregados + booleanos derivados de env/budget.
    """

    enabled: bool
    api_key_present: bool
    monthly_budget: int
    monthly_used: int                 # = budget.count antes do ciclo
    candidates_considered: int        # tamanho do pool filtrado
    validations_attempted: int        # quantas chamadas SerpApi rodaram
    elevated_to_manual_check: int     # quantos viraram 🟡 (cabin OK + preço OK)
    # PR #60: cabin business confirmada MAS preço SerpApi diverge muito
    # do sinal original — NÃO eleva, apenas informa.
    price_mismatched: int = 0
    skipped_reason: str | None = None  # reason code interno (snake_case)


def humanize_validation_summary(
    summary: SerpApiValidationSummary,
) -> str:
    """Frase humana p/ aparecer no 🧭 Status das fontes. NUNCA
    contém token, URL, payload, post_data, query string nem rota."""
    if not summary.enabled:
        return "SerpApi: validação desativada."
    if not summary.api_key_present:
        return (
            "SerpApi: configurada, mas sem chave disponível nos "
            "Actions Secrets."
        )
    used = max(0, summary.monthly_used)
    budget = max(0, summary.monthly_budget)
    # Sem budget OU sem cobertura sequer para 1 validação (3 queries) →
    # tratamos como esgotado.
    if budget <= 0 or (budget - used) < ESTIMATED_QUERIES_PER_VALIDATION:
        return (
            f"SerpApi: orçamento mensal esgotado ({used}/{budget} "
            "queries usadas); validação pausada até a virada do "
            "mês UTC."
        )
    prefix = f"SerpApi: ativa; {used}/{budget} queries usadas no mês."
    elevated = max(0, summary.elevated_to_manual_check)
    attempted = max(0, summary.validations_attempted)
    considered = max(0, summary.candidates_considered)
    mismatched = max(0, getattr(summary, "price_mismatched", 0))
    if elevated > 0:
        plural = "" if elevated == 1 else "s"
        return (
            f"{prefix} {elevated} candidato{plural} validado{plural} "
            "e movido(s) para Verificação manual."
        )
    if mismatched > 0:
        # PR #60: SerpApi encontrou cabine business mas em preço
        # diferente do sinal Travelpayouts original. NÃO eleva — só
        # informa. Mensagem honesta evita induzir o usuário.
        plural = "" if mismatched == 1 else "s"
        return (
            f"{prefix} SerpApi encontrou executiva em {mismatched} "
            f"rota{plural}, mas em preço diferente do sinal original — "
            "não confirmou a tarifa."
        )
    if attempted > 0:
        plural = "" if attempted == 1 else "s"
        return (
            f"{prefix} Validação tentou {attempted} candidato{plural} "
            "neste ciclo, mas não confirmou executiva."
        )
    if considered == 0:
        return f"{prefix} Nenhum candidato forte elegível neste ciclo."
    return (
        f"{prefix} Tentativa não conclusiva neste ciclo. O radar "
        "seguiu sem promover o sinal."
    )


# Reason codes — todos snake_case, nunca contêm payload sensível.
RC_DISABLED = "validation_disabled"
RC_NO_API_KEY = "no_api_key"
RC_OVER_QUOTA_CAP = "over_cycle_cap"
RC_MONTHLY_BUDGET_EXHAUSTED = "monthly_budget_exhausted"
RC_SEARCH_FAILED = "search_failed"
RC_NO_DEPARTURE_TARGET = "no_departure_token_candidate"
RC_FOLLOWUP_FAILED = "departure_followup_failed"
RC_NO_BOOKING_TOKEN_IN_RETURN = "no_booking_token_in_return_offers"
RC_NO_BOOKING_TOKEN_IN_SEARCH = "no_booking_token_in_search"
RC_BOOKING_OPTIONS_FAILED = "booking_options_failed"
RC_CABIN_NOT_CONFIRMED = "cabin_not_confirmed"
RC_PRICE_MISMATCH = "price_mismatch_with_signal"
RC_VALIDATION_OK = "validation_ok"


# PR #60: tolerância de preço entre o sinal Travelpayouts e o preço
# que a SerpApi devolveu para a oferta business. Sem isso, US$ 208
# Travelpayouts + US$ 1137 SerpApi (5.4×) era "validado" como
# executiva, induzindo o usuário a achar que a tarifa original tinha
# sido confirmada como business.
PRICE_COMPATIBILITY_RATIO = 1.25     # SerpApi ≤ expected × 1.25
PRICE_COMPATIBILITY_ABS_USD = 100.0  # OU |delta| ≤ USD 100


def price_is_compatible(
    expected_usd: float | None,
    serpapi_usd: float | None,
) -> bool:
    """Compara preço esperado (sinal Travelpayouts) com preço SerpApi
    da oferta business. Compatível se SerpApi ≤ expected×1.25 OU
    |delta| ≤ USD 100. Conservador: ambos os valores são obrigatórios;
    None em qualquer um → incompatível.

    Exemplos:
    - 208 vs 1137 → False (5.4×, |Δ|=929)
    - 1000 vs 1070 → True  (1.07×)
    - 1000 vs 1300 → False (1.3×, |Δ|=300)
    - 1000 vs 1099 → True  (1.099×)
    - 1000 vs 950  → True  (0.95×)
    - None vs anything → False
    """
    if expected_usd is None or serpapi_usd is None:
        return False
    try:
        exp = float(expected_usd)
        sp = float(serpapi_usd)
    except (TypeError, ValueError):
        return False
    if exp <= 0:
        return False
    if sp <= exp * PRICE_COMPATIBILITY_RATIO:
        return True
    if abs(sp - exp) <= PRICE_COMPATIBILITY_ABS_USD:
        return True
    return False


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

    # PR #60: compatibilidade de preço. Cabine business é evidência;
    # preço diferente do sinal original significa que SerpApi achou
    # OUTRA executiva, não validou a tarifa do Travelpayouts. Só
    # eleva para 🟡 quando cabine + preço batem com o sinal original.
    compatible = (
        price_is_compatible(candidate.expected_usd, price_usd)
        if cabin_confirmed else False
    )

    # Decisão sugerida — NUNCA CONFIRMED_ACTIONABLE via validação SerpApi.
    # SerpApi não vira fonte de link de compra (princípio do PR #52).
    if not cabin_confirmed:
        suggested = OperationalDecision.RAW_SIGNAL
        rc_main = RC_CABIN_NOT_CONFIRMED
    elif not compatible:
        # PR #60: cabine business confirmada mas preço diverge muito do
        # sinal original. NÃO eleva para 🟡 — o relatório usa o resultado
        # como nota informativa no bloco original (💸/👀).
        suggested = OperationalDecision.RAW_SIGNAL
        rc_main = RC_PRICE_MISMATCH
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
        # + preço bate, mas booking sem caminho útil → ainda manual_check
        # (cabine confirmada + preço compatível já é informação valiosa).
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
        price_compatible=compatible,
    )


def validate_cycle_candidates(
    candidates: list[SerpApiValidationCandidate],
    config: SerpApiValidationConfig,
    client_factory=None,
    budget_path: Path | None = None,
) -> dict[str, SerpApiValidationResult]:
    """Orquestrador top-level. Devolve {route_key: SerpApiValidationResult}.

    Gates duros (em ordem):
    1. `config.enabled` obrigatório;
    2. `SERPAPI_API_KEY` obrigatório (sem ele, retorna dict vazio);
    3. `max_per_cycle` aplicado (cap intra-ciclo);
    4. **Orçamento mensal** persistido (cap inter-mês):
       - `budget_path` aponta para `data/serpapi_validation_budget.json`
         (arquivo mínimo: só `{month_utc, count}`, nunca payload);
       - reset automático em virada de mês UTC;
       - `count` rastreia queries SerpApi estimadas consumidas no mês;
       - antes de cada validação, verifica
         `remaining >= ESTIMATED_QUERIES_PER_VALIDATION`;
       - sem orçamento → retorna dict vazio (sinal permanece sem
         elevar; ciclo continua normal).

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
    if config.monthly_budget <= 0:
        # Budget zero = validação desligada explicitamente.
        return {}

    # Orçamento mensal (cap inter-ciclo, reset na virada do mês UTC).
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    budget = SerpApiValidationBudget.load(budget_path).reset_if_new_month(
        this_month,
    )
    remaining = budget.remaining(config.monthly_budget)
    if remaining < ESTIMATED_QUERIES_PER_VALIDATION:
        # Cota mensal esgotada (sem cobertura sequer para 1 validação).
        # Registra estado e sai sem chamar SerpApi.
        budget.save(budget_path)
        return {}

    if client_factory is None:
        client_factory = lambda key: SerpApiClient(key)

    try:
        client = client_factory(config.api_key)
    except Exception:
        return {}

    out: dict[str, SerpApiValidationResult] = {}
    for cand in candidates[: config.max_per_cycle]:
        # Re-check do budget dentro do loop — múltiplos candidatos podem
        # esgotar a cota dentro do mesmo ciclo.
        if budget.remaining(config.monthly_budget) < ESTIMATED_QUERIES_PER_VALIDATION:
            break
        try:
            res = validate_with_serpapi(cand, client)
        except Exception:
            res = _empty_result(cand.key, RC_SEARCH_FAILED)
        out[cand.key] = res
        # Incrementa pelo CUSTO ESTIMADO — pior caso (3 queries) cobre
        # tanto one-way (2 hops) quanto round-trip (3 hops). Sempre
        # conservador p/ não estourar o budget.
        budget = budget.add_queries(ESTIMATED_QUERIES_PER_VALIDATION)

    # Persistência final — uma escrita por ciclo (não por candidato).
    budget.save(budget_path)
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


def humanize_price_mismatch_note(
    result: SerpApiValidationResult,
    expected_usd: float | None,
) -> str:
    """PR #60: nota informativa quando SerpApi encontra cabine business
    mas em preço diferente do sinal original (incompatível). O
    candidato permanece em 💸/👀 — esta linha NÃO sugere ação de
    compra. NUNCA inclui token, URL, post_data.
    """
    sp = result.price_usd
    if sp is not None and expected_usd is not None:
        return (
            f"SerpApi encontrou executiva na rota por ~USD "
            f"{sp:.0f}, mas não confirmou a tarifa original de "
            f"US$ {expected_usd:.0f}."
        )
    if sp is not None:
        return (
            f"SerpApi encontrou executiva na rota por ~USD "
            f"{sp:.0f}, mas não confirmou a tarifa original do sinal."
        )
    return (
        "SerpApi encontrou cabine business na rota, mas em preço "
        "diferente do sinal original."
    )
