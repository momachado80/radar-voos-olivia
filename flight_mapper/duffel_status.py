"""Resumo SANITIZADO do pass Duffel para o relatório diário (PR #65).

Espelha o padrão de observabilidade do SerpApi (`SerpApiValidationSummary`):
um objeto pequeno, derivado do ciclo, que o 🧭 Status das fontes consome
para renderizar UMA linha humana.

Invariante de segurança: este objeto e a frase derivada NUNCA contêm
offer_id, token, URL, request body, order_id nem dado de passageiro —
apenas contadores e um código de resultado canônico (snake_case).
"""

from __future__ import annotations

from dataclasses import dataclass


# Códigos de resultado canônicos (estáveis p/ parsing externo / testes).
DUFFEL_DISABLED = "disabled"
DUFFEL_ALERT_SENT = "alert_sent"
DUFFEL_SEND_FAILED = "send_failed"
DUFFEL_BLOCKED_FX = "blocked_fx"
DUFFEL_ABOVE_THRESHOLD = "above_threshold"
DUFFEL_BLOCKED_CABIN = "blocked_cabin"
DUFFEL_BLOCKED_SUSPICIOUS = "blocked_suspicious"
DUFFEL_NO_OFFER = "no_offer"


@dataclass(frozen=True)
class DuffelStatusSummary:
    """Snapshot sanitizado do pass Duffel de um ciclo.

    `enabled`: provider instanciado (flag ligada + token presente) E cap > 0.
    `requests`: nº de Offer Requests feitos neste ciclo (cap conservador).
    `confirmed_alerts`: nº de alertas 🟢 enviados.
    `outcome`: código canônico do resultado dominante (ver DUFFEL_* acima).
    """

    enabled: bool
    requests: int
    confirmed_alerts: int
    outcome: str


def humanize_duffel_status(summary: DuffelStatusSummary) -> str:
    """Frase pt-BR p/ a linha do 🧭. NUNCA expõe dado sensível.

    Cobre os 6 estados do goal:
    - inativa (token ausente / flag off);
    - oferta confirmada enviada como alerta;
    - bloqueada por câmbio EUR→BRL ausente;
    - preço acima do teto;
    - sem oferta confirmada (+ contagem de consultas);
    - bloqueios de cabine / preço suspeito (estados seguros adicionais).
    """
    if not summary.enabled or summary.outcome == DUFFEL_DISABLED:
        return "Duffel: inativa (token ausente ou flag desligada)."

    if summary.outcome == DUFFEL_ALERT_SENT:
        # PR #71: Duffel order_flow não envia alerta standalone — entra na
        # mensagem agrupada "compra pendente". Wording sem "enviada".
        n = summary.confirmed_alerts
        if n == 1:
            return "Duffel: ativa; 1 oferta confirmada (compra pendente)."
        return (
            f"Duffel: ativa; {n} ofertas confirmadas (compra pendente)."
        )

    if summary.outcome == DUFFEL_SEND_FAILED:
        return (
            "Duffel: ativa; oferta confirmada, mas envio ao Telegram falhou."
        )

    if summary.outcome == DUFFEL_BLOCKED_FX:
        return "Duffel: ativa, mas bloqueada por câmbio EUR→BRL ausente."

    if summary.outcome == DUFFEL_ABOVE_THRESHOLD:
        return "Duffel: ativa, mas preço acima do teto."

    if summary.outcome == DUFFEL_BLOCKED_CABIN:
        return "Duffel: ativa, mas cabine não confirmada."

    if summary.outcome == DUFFEL_BLOCKED_SUSPICIOUS:
        return "Duffel: ativa, mas preço economicamente suspeito."

    # DUFFEL_NO_OFFER (default): formato genérico com contagem + motivo.
    n = max(0, summary.requests)
    return (
        f"Duffel: ativa; {n} consulta(s) neste ciclo; 0 alertas; "
        f"motivo: sem oferta confirmada."
    )


@dataclass(frozen=True)
class DuffelWatchlistSummary:
    """Snapshot sanitizado do pass da watchlist premium (PR #67/#68).

    `enabled`: watchlist configurada + cap > 0 + provider presente.
    `checked`: nº de combinações Londres/Paris consultadas neste ciclo.
    `confirmed_alerts`: total de 🟢 enviados (business + economy).
    `business_alerts`/`economy_alerts`: quebra por cabine (PR #68).
    NUNCA contém offer_id/token/URL/payload/order_id/passageiro."""

    enabled: bool
    checked: int
    confirmed_alerts: int
    business_alerts: int = 0
    economy_alerts: int = 0


def humanize_duffel_watchlist_status(
    summary: DuffelWatchlistSummary | None,
) -> str | None:
    """Linha do 🧭 p/ a watchlist premium Londres/Paris. `None` quando a
    watchlist não rodou (omite a linha). Distingue executiva (business) de
    econômica muito boa (economy). NUNCA expõe dado sensível."""
    if summary is None or not summary.enabled:
        return None
    parts: list[str] = []
    if summary.business_alerts > 0:
        n = summary.business_alerts
        word = "executiva confirmada" if n == 1 else "executivas confirmadas"
        parts.append(f"{n} {word}")
    if summary.economy_alerts > 0:
        m = summary.economy_alerts
        word = "econômica muito boa" if m == 1 else "econômicas muito boas"
        parts.append(f"{m} {word}")
    if parts:
        return f"Duffel watchlist: {' e '.join(parts)} para Paris/Londres."
    return (
        "Duffel watchlist: Londres/Paris setembro consultada; 0 alertas."
    )


@dataclass(frozen=True)
class DuffelGroupSummary:
    """Estatística do agrupamento de alertas Duffel order_flow (PR #71).

    `confirmed_pending` (X): ofertas confirmadas "compra pendente" no ciclo
    (= agrupadas + suprimidas). `grouped` (Y): incluídas na mensagem única.
    `suppressed_cooldown` (Z): suprimidas pelo cooldown de 6h.
    `message_sent`: se a mensagem agrupada foi de fato enviada."""

    confirmed_pending: int
    grouped: int
    suppressed_cooldown: int
    message_sent: bool = False


def humanize_duffel_group_status(
    summary: DuffelGroupSummary | None,
) -> str | None:
    """Linha do 🧭 sobre o agrupamento Duffel "compra pendente". `None`
    quando nada relevante ocorreu (omite a linha). NUNCA expõe dado
    sensível — só contadores."""
    if summary is None:
        return None
    if summary.confirmed_pending <= 0 and summary.suppressed_cooldown <= 0:
        return None
    return (
        f"Duffel: {summary.confirmed_pending} ofertas confirmadas, compra "
        f"pendente; {summary.grouped} agrupadas; "
        f"{summary.suppressed_cooldown} suprimidas por cooldown."
    )
