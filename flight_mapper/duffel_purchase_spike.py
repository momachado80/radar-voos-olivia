"""Spike read-only/sandbox: avalia o CAMINHO DE COMPRA do Duffel — da oferta
(Offer Request) até a hipotética criação de ordem — SEM pagamento, SEM
bilhete, SEM dados de passageiro, SEM integração de produção.

Princípios invioláveis (PR #70):
- **Só `POST /air/offer_requests`** (criar Offer Request é busca, não reserva).
  NUNCA chama `/air/orders`, `/air/payments`, nem qualquer endpoint que
  cobre/reserve/emita. Não há "criar ordem" aqui.
- **Sem dry-run falso:** o Duffel NÃO tem endpoint de validação/dry-run de
  ordem. Não fingimos um. Reportamos o bloqueio honestamente.
- **Sem leak:** o relatório NUNCA expõe token, offer_id, order_id,
  payment_id, payload cru, URL completa ou dado de passageiro — só campos
  sanitizados (preço/moeda/cia + presença booleana de campos estruturais).
- **Sem Telegram, sem produção:** módulo isolado, sem tocar monitor/alertas.

Fatos do contrato público da API Duffel (NÃO inferidos do payload):
- Não existe endpoint de validação/dry-run de ordem — `POST /air/orders`
  cria ordem REAL (reserva/segura inventário; com instant payment, paga).
- Criar ordem exige dados de passageiro reais (given_name, family_name,
  born_on, etc.) + objeto de pagamento.
- O ponto onde pagamento/emissão se torna possível/obrigatório é o
  `POST /air/orders` (instant payment) ou o fluxo hold→pay (ainda ordem real).
- Token de TESTE: cria Offer Requests/ordens de teste (não emite bilhete
  real, não cobra); token LIVE: cobra/emite de verdade. Este spike só usa
  token de teste e mesmo assim NÃO cria ordem.
"""

from __future__ import annotations

from dataclasses import dataclass

from .actionability_readiness import _duffel_first_offer, parse_duffel_for_actionability


# Fatos do contrato Duffel (constantes — não vêm do payload).
DUFFEL_HAS_ORDER_DRY_RUN = False
DUFFEL_ORDER_REQUIRES_PASSENGER_PII = True


# Próximos passos seguros possíveis (enum textual estável).
SAFE_NEXT_DASHBOARD = "A) dashboard_manual_recovery"
SAFE_NEXT_ORDER_PII = "B) order_api_requires_sensitive_data"
SAFE_NEXT_NO_PATH = "C) no_safe_purchase_path"
SAFE_NEXT_LIVE_APPROVAL = "D) live_approval_required"


@dataclass(frozen=True)
class PurchasePathReport:
    """Snapshot SANITIZADO do caminho de compra de UMA oferta Duffel.

    NUNCA contém token, offer_id, order_id, payment_id, URL, payload cru
    nem dado de passageiro. `has_offer_id`/`has_passenger_ids` são apenas
    PRESENÇA booleana dos campos estruturais (não os valores)."""

    provider: str            # "duffel"
    environment: str         # "test"
    route: str
    cabin: str
    trip_type: str
    offer_found: bool
    cabin_confirmed: bool
    price_amount: float | None
    price_currency: str | None
    airline: str | None
    dashboard_recovery: str                       # yes | no | unknown
    order_creation_requires_passenger_data: str   # yes | no | unknown
    dry_run_available: str                        # yes | no | unknown
    safe_next_step: str                           # SAFE_NEXT_*
    blockers: tuple[str, ...]
    recommendation: str
    # Presença estrutural (booleana) dos campos que uma ordem referenciaria.
    has_offer_id: bool
    has_passenger_ids: bool


_RECOMMENDATIONS = {
    SAFE_NEXT_DASHBOARD: (
        "Há recuperação manual da oferta (URL segura) — usar caminho de "
        "dashboard/recuperação manual; nenhuma criação de ordem pelo robô."
    ),
    SAFE_NEXT_ORDER_PII: (
        "Implementar Orders API depois é possível, MAS exige dados de "
        "passageiro reais + pagamento no momento da criação da ordem, e o "
        "Duffel não oferece validação/dry-run. Requer aprovação explícita "
        "futura. Por ora, sem caminho de compra automático seguro no robô; "
        "manter alerta como 'oferta confirmada, compra pendente'."
    ),
    SAFE_NEXT_NO_PATH: (
        "Sem oferta utilizável neste teste e sem caminho de compra seguro "
        "sem criar ordem/pagamento. Reexecutar o spike ou tratar como "
        "'compra pendente'."
    ),
    SAFE_NEXT_LIVE_APPROVAL: (
        "Caminho depende de acesso live / aprovação comercial explícita "
        "antes de ser útil. Não prosseguir sem aprovação."
    ),
}


def parse_duffel_purchase_path(
    payload: dict,
    *,
    route: str,
    requested_cabin: str = "business",
    trip_type: str = "one_way",
    environment: str = "test",
) -> PurchasePathReport:
    """Analisa o caminho de compra a partir do payload de Offer Request.

    Reusa `parse_duffel_for_actionability` p/ cabine/preço/cia e adiciona a
    análise de order-flow (campos estruturais + fatos do contrato Duffel).
    Pure: sem rede, sem I/O."""
    ar = parse_duffel_for_actionability(
        payload, route=route, requested_cabin=requested_cabin,
    )
    offer = _duffel_first_offer(payload)
    offer_found = offer is not None

    has_offer_id = bool(offer and offer.get("id"))
    passengers = (offer or {}).get("passengers") or []
    has_passenger_ids = bool(passengers) and all(
        isinstance(p, dict) and p.get("id") for p in passengers
    )

    # Recuperação por dashboard: o payload de oferta do Duffel NÃO traz URL
    # de recuperação para o usuário final (o Dashboard é do desenvolvedor e
    # mostra ordens, não ofertas compartilháveis). Detectamos uma URL de
    # recuperação se algum dia existir; hoje → "no".
    recovery_url_present = bool(
        isinstance(offer, dict) and offer.get("recovery_url")
    )
    dashboard_recovery = "yes" if recovery_url_present else "no"

    dry_run_available = "yes" if DUFFEL_HAS_ORDER_DRY_RUN else "no"
    order_requires_pax = (
        "yes" if DUFFEL_ORDER_REQUIRES_PASSENGER_PII else "unknown"
    )

    blockers: list[str] = [
        "no_validation_only_order_endpoint",
        "order_creation_requires_passenger_pii",
        "order_creation_requires_payment_at_order_creation",
        "no_consumer_dashboard_recovery_url",
    ]
    if not offer_found:
        blockers.append("no_offer_in_test_search")
    else:
        if not has_offer_id:
            blockers.append("offer_missing_offer_id")
        if not has_passenger_ids:
            blockers.append("offer_missing_passenger_ids")
        if not ar.cabin_confirmed:
            blockers.append("cabin_not_confirmed")
    # PR #62/#63: blocker de rede/HTTP propagado pelo live search.
    live_blocker = payload.get("_blocker") if isinstance(payload, dict) else None
    if isinstance(live_blocker, str) and live_blocker:
        blockers.append(f"live_{live_blocker}")

    # Decisão do próximo passo seguro.
    if not offer_found:
        safe_next_step = SAFE_NEXT_NO_PATH
    elif dashboard_recovery == "yes":
        safe_next_step = SAFE_NEXT_DASHBOARD
    elif order_requires_pax == "yes" and dry_run_available != "yes":
        safe_next_step = SAFE_NEXT_ORDER_PII
    else:
        safe_next_step = SAFE_NEXT_LIVE_APPROVAL

    return PurchasePathReport(
        provider="duffel",
        environment=environment,
        route=route,
        cabin=(requested_cabin or "").strip().lower() or "business",
        trip_type=trip_type,
        offer_found=offer_found,
        cabin_confirmed=ar.cabin_confirmed,
        price_amount=ar.price_amount,
        price_currency=ar.price_currency,
        airline=(ar.airlines[0] if ar.airlines else None),
        dashboard_recovery=dashboard_recovery,
        order_creation_requires_passenger_data=order_requires_pax,
        dry_run_available=dry_run_available,
        safe_next_step=safe_next_step,
        blockers=tuple(blockers),
        recommendation=_RECOMMENDATIONS[safe_next_step],
        has_offer_id=has_offer_id,
        has_passenger_ids=has_passenger_ids,
    )


def _yes_no(v: bool) -> str:
    return "yes" if v else "no"


def format_purchase_path_report(report: PurchasePathReport) -> str:
    """Formato determinístico chave: valor. NUNCA inclui token, offer_id,
    order_id, payment_id, URL completa, payload ou dado de passageiro."""
    lines = [
        f"provider:                              {report.provider}",
        f"environment:                           {report.environment}",
        f"route:                                 {report.route}",
        f"cabin:                                 {report.cabin}",
        f"trip_type:                             {report.trip_type}",
        f"offer_found:                           {_yes_no(report.offer_found)}",
        f"cabin_confirmed:                       {_yes_no(report.cabin_confirmed)}",
        f"price:                                 "
        f"{f'{report.price_amount:.2f}' if report.price_amount is not None else '(n/a)'}",
        f"currency:                              {report.price_currency or '(n/a)'}",
        f"airline:                               {report.airline or '(n/a)'}",
        f"dashboard_recovery:                    {report.dashboard_recovery}",
        f"order_creation_requires_passenger_data: "
        f"{report.order_creation_requires_passenger_data}",
        f"dry_run_available:                     {report.dry_run_available}",
        f"safe_next_step:                        {report.safe_next_step}",
        f"blockers:                              "
        f"{','.join(report.blockers) if report.blockers else '(none)'}",
        f"recommendation:                        {report.recommendation}",
    ]
    return "\n".join(lines)
