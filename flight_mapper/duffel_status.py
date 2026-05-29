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
        n = summary.confirmed_alerts
        if n == 1:
            return "Duffel: ativa; 1 oferta confirmada enviada como alerta."
        return (
            f"Duffel: ativa; {n} ofertas confirmadas enviadas como alerta."
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
    """Snapshot sanitizado do pass da watchlist premium (PR #67).

    `enabled`: watchlist configurada + cap > 0 + provider presente.
    `checked`: nº de combinações Londres/Paris consultadas neste ciclo.
    `confirmed_alerts`: nº de 🟢 enviados a partir da watchlist.
    NUNCA contém offer_id/token/URL/payload/order_id/passageiro."""

    enabled: bool
    checked: int
    confirmed_alerts: int


def humanize_duffel_watchlist_status(
    summary: DuffelWatchlistSummary | None,
) -> str | None:
    """Linha do 🧭 p/ a watchlist premium Londres/Paris. `None` quando a
    watchlist não rodou (omite a linha). NUNCA expõe dado sensível."""
    if summary is None or not summary.enabled:
        return None
    if summary.confirmed_alerts > 0:
        n = summary.confirmed_alerts
        exec_word = (
            "executiva confirmada" if n == 1 else "executivas confirmadas"
        )
        return (
            f"Duffel watchlist: {n} {exec_word} para Paris/Londres."
        )
    return (
        "Duffel watchlist: Londres/Paris setembro consultada; 0 alertas."
    )
