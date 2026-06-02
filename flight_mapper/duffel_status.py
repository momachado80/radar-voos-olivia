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
    """Frase pt-BR p/ a linha do PASS GENÉRICO no 🧭 (rota GRU-MIA + rotas
    do radar). NUNCA expõe dado sensível.

    PR #74: prefixo explícito "Duffel genérico:" para separar do pass da
    watchlist Londres/Paris e do resumo order_flow — evita linhas "Duffel:"
    repetidas que pareciam estados conflitantes.

    Estados cobertos:
    - inativo (token ausente / flag off);
    - oferta confirmada (compra pendente — order_flow);
    - bloqueado por câmbio EUR→BRL ausente;
    - sem oferta abaixo do teto;
    - sem oferta confirmada (+ contagem de consultas);
    - bloqueios de cabine / preço suspeito (estados seguros adicionais).
    """
    if not summary.enabled or summary.outcome == DUFFEL_DISABLED:
        return "Duffel genérico: inativo (token ausente ou flag desligada)."

    if summary.outcome == DUFFEL_ALERT_SENT:
        # PR #71: Duffel order_flow não envia alerta standalone — entra na
        # mensagem agrupada "compra pendente". Wording sem "enviada".
        n = summary.confirmed_alerts
        if n == 1:
            return "Duffel genérico: 1 oferta confirmada (compra pendente)."
        return (
            f"Duffel genérico: {n} ofertas confirmadas (compra pendente)."
        )

    if summary.outcome == DUFFEL_SEND_FAILED:
        return (
            "Duffel genérico: oferta confirmada, mas envio ao Telegram falhou."
        )

    if summary.outcome == DUFFEL_BLOCKED_FX:
        return "Duffel genérico: ativo, mas bloqueado por câmbio EUR→BRL ausente."

    if summary.outcome == DUFFEL_ABOVE_THRESHOLD:
        return "Duffel genérico: ativo, sem oferta abaixo do teto neste ciclo."

    if summary.outcome == DUFFEL_BLOCKED_CABIN:
        return "Duffel genérico: ativo, mas cabine não confirmada."

    if summary.outcome == DUFFEL_BLOCKED_SUSPICIOUS:
        return "Duffel genérico: ativo, mas preço economicamente suspeito."

    # DUFFEL_NO_OFFER (default): formato genérico com contagem + motivo.
    n = max(0, summary.requests)
    return (
        f"Duffel genérico: ativo; {n} consulta(s) neste ciclo; "
        f"sem oferta confirmada."
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
    econômica muito boa (economy). NUNCA expõe dado sensível.

    PR #74: prefixo "Duffel watchlist Londres/Paris:" + deixa explícito que
    as ofertas confirmadas são order_flow ("compra pendente; sem link
    direto"), separando da linha do pass genérico."""
    if summary is None or not summary.enabled:
        return None
    parts: list[str] = []
    if summary.business_alerts > 0:
        n = summary.business_alerts
        word = "oferta executiva confirmada" if n == 1 else "ofertas executivas confirmadas"
        parts.append(f"{n} {word}")
    if summary.economy_alerts > 0:
        m = summary.economy_alerts
        word = "oferta econômica muito boa" if m == 1 else "ofertas econômicas muito boas"
        parts.append(f"{m} {word}")
    if parts:
        return (
            f"Duffel watchlist Londres/Paris: {' e '.join(parts)}, "
            f"compra pendente; sem link direto."
        )
    return (
        "Duffel watchlist Londres/Paris: consultada neste ciclo; "
        "0 ofertas confirmadas."
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
    # PR #73: modo do alerta order_flow vigente neste ciclo
    # (daily_only / grouped_push / disabled) e até 3 ofertas SANITIZADAS
    # (DuffelPendingOffer) p/ a seção opcional do relatório diário. As
    # ofertas já vêm sem offer_id/token/payload/passageiro.
    mode: str = "daily_only"
    top_offers: tuple = ()


def humanize_duffel_group_status(
    summary: DuffelGroupSummary | None,
) -> str | None:
    """Linha do 🧭 sobre o agrupamento Duffel "compra pendente". `None`
    quando nada relevante ocorreu (omite a linha). NUNCA expõe dado
    sensível — só contadores. Usada no modo `grouped_push` (debug).

    PR #74: prefixo "Duffel order_flow (resumo):" — é o ROLL-UP de todas
    as ofertas order_flow do ciclo (genérico + watchlist), distinto das
    linhas por-pass. Evita parecer conflitante com a linha da watchlist."""
    if summary is None:
        return None
    if summary.confirmed_pending <= 0 and summary.suppressed_cooldown <= 0:
        return None
    return (
        f"Duffel order_flow (resumo): {summary.confirmed_pending} ofertas "
        f"confirmadas, compra pendente; {summary.grouped} agrupadas; "
        f"{summary.suppressed_cooldown} suprimidas por cooldown."
    )


def humanize_duffel_group_status_daily(
    summary: DuffelGroupSummary | None,
) -> str | None:
    """Linha do 🧭 para o modo `daily_only` (PR #73): order_flow não gera
    push standalone — só resumo no relatório diário. `None` quando não há
    oferta confirmada. NUNCA expõe dado sensível — só o contador.

    PR #74: prefixo "Duffel order_flow (resumo do ciclo):" — é o ROLL-UP
    de todas as ofertas order_flow (genérico + watchlist), distinto das
    linhas por-pass, então não soa como um estado "Duffel:" conflitante."""
    if summary is None:
        return None
    n = summary.confirmed_pending
    if n <= 0:
        return None
    return (
        f"Duffel order_flow (resumo do ciclo): {n} ofertas confirmadas, "
        f"compra pendente; sem link direto."
    )


def format_duffel_pending_daily_section(
    summary: DuffelGroupSummary | None,
    mode: str = "daily_only",
) -> str:
    """Seção OPCIONAL do relatório diário (modo `daily_only`, PR #73)
    listando até 3 ofertas Duffel order_flow "compra pendente". String
    vazia quando não aplicável (outro modo, sem ofertas). NUNCA expõe
    offer_id/token/payload/URL/passageiro — só rótulos já sanitizados."""
    if summary is None or mode != "daily_only":
        return ""
    offers = getattr(summary, "top_offers", ()) or ()
    if not offers:
        return ""
    lines = ["🟡 Ofertas business confirmadas (Duffel) — buscar no Google Flights"]
    for i, o in enumerate(offers[:3], 1):
        parts = [o.route_label, o.cabin_pt, o.dates, o.price_display]
        if o.target_display:
            parts.append(o.target_display)
        if o.airline:
            parts.append(o.airline)
        lines.append(f"{i}. " + " — ".join(parts))
        # PR #76: link de busca pré-preenchida no Google Flights por oferta.
        search_url = getattr(o, "search_url", None)
        if search_url:
            lines.append(f'   🔎 <a href="{search_url}">Buscar no Google Flights</a>')
    lines.append(
        "Busca pré-preenchida a partir da oferta confirmada pela Duffel. "
        "Preço e disponibilidade podem variar; confira antes de comprar."
    )
    return "\n".join(lines)
