"""Orquestrador principal: varre rotas, atualiza histórico e dispara alertas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .airports import is_actionable_url
from .currency import get_usd_brl_rate
from .cycle_state import CycleState
from .detector import evaluate, evaluate_ceiling
from .duffel_status import (
    DUFFEL_ABOVE_THRESHOLD,
    DUFFEL_ALERT_SENT,
    DUFFEL_BLOCKED_CABIN,
    DUFFEL_BLOCKED_FX,
    DUFFEL_BLOCKED_SUSPICIOUS,
    DUFFEL_DISABLED,
    DUFFEL_NO_OFFER,
    DUFFEL_SEND_FAILED,
    DuffelStatusSummary,
)
from .formatting import trip_label_pt
from .notifier import TelegramNotifier
from .providers import FlightProvider, Quote
from .regions import Cabin, Route, TripType, all_routes, is_priority
from .sanity import is_suspicious_price, suspicious_reason
from .score import compute_opportunity_score
from .state import PriceStore
from .thresholds import HOT_ROUTE_KEYS, levels_for, scaled_levels


CONFIRMATION_TOLERANCE_PCT = 0.05  # 5%: segunda quote dentro disso ainda confirma
LINK_PRICE_COMPATIBILITY_RATIO = 1.15  # Kiwi pode ser até 15% mais caro que o primário

# PR #65: rota PROVADA pelo readiness smoke do Duffel (GRU-MIA one_way
# business, cabin_confirmed=yes, decision=candidate_for_integration). É a
# única confirmada end-to-end, então o pass Duffel a consulta PRIMEIRO.
# `all_routes()` só gera round_trip business — esta one_way não está lá.
DUFFEL_PROVEN_ROUTE = Route(
    origin="GRU", destination="MIA", region="EUA",
    trip_type=TripType.ONE_WAY, cabin=Cabin.BUSINESS,
)


@dataclass
class MonitorResult:
    scanned: int
    quotes_received: int
    alerts_sent: int
    notes: list[str] = field(default_factory=list)
    stale_quotes_skipped: int = 0
    non_actionable_links_skipped: int = 0
    actionable_links_generated: int = 0
    manual_fallback_alerts_sent: int = 0
    currency_blocked: int = 0
    cabin_blocked: int = 0
    suspicious_blocked: int = 0
    # Pass Duffel (read-only confirmed offers). Zero quando desligado.
    duffel_requests: int = 0
    duffel_confirmed_alerts: int = 0
    duffel_blocked: int = 0
    # Resumo sanitizado p/ o 🧭 Status das fontes (PR #65). None quando
    # o pass Duffel nem rodou. NUNCA contém offer_id/token/payload.
    duffel_summary: DuffelStatusSummary | None = None


def _route_note(route: Route) -> str:
    """Prefixo trip-aware das notas/logs do scan, ex.:
    `GRU→MIA [ida e volta]` / `GRU→MIA [somente ida]`. Só formatação —
    não altera nenhuma decisão de alerta."""
    return (
        f"{route.origin}→{route.destination} "
        f"[{trip_label_pt(route.trip_type)}]"
    )


def _quote_to_dict(quote: Quote, now: datetime, *, provider_note: str | None = None) -> dict:
    return {
        "price_brl": quote.price_brl,
        "amount": quote.amount,
        "currency": quote.currency,
        "amount_brl_estimated": quote.amount_brl_estimated,
        "fx_rate": quote.fx_rate,
        "origin": quote.route.origin,
        "destination": quote.route.destination,
        "departure_date": quote.departure_date,
        "return_date": quote.return_date,
        "source": quote.source,
        "deep_link": quote.deep_link,
        "detected_at": now.isoformat(),
        "actionable_url": is_actionable_url(quote.deep_link),
        "cabin": quote.cabin.value,
        "cabin_confirmed": quote.cabin_confirmed,
        "trip_type": quote.trip_type.value,
        "provider_note": provider_note,
    }


class Monitor:
    def __init__(
        self,
        provider: FlightProvider,
        notifier: TelegramNotifier | None,
        store: PriceStore,
        cycle: CycleState | None = None,
        chunk_size: int = 8,
        confirm_alerts: bool = True,
        link_provider: FlightProvider | None = None,
        manual_purchase_fallback: bool = True,
        duffel_provider: FlightProvider | None = None,
        duffel_store: PriceStore | None = None,
        duffel_max_requests: int = 0,
    ):
        self.provider = provider
        self.notifier = notifier
        self.store = store
        self.cycle = cycle
        self.chunk_size = chunk_size
        self.confirm_alerts = confirm_alerts
        # Duffel (read-only confirmed offers): pass ADITIVO e isolado.
        # Desligado quando `duffel_provider is None` (zero chamada, zero
        # mudança de comportamento). Histórico/dedup em store próprio
        # (`duffel_store`) para NUNCA poluir o store principal/relatórios.
        self.duffel_provider = duffel_provider
        self.duffel_store = duffel_store
        self.duffel_max_requests = max(0, duffel_max_requests)
        # `link_provider`: provider auxiliar SÓ para validar/obter link comercial
        # quando o primário não fornece link acionável (caso Travelpayouts).
        # Quando definido, cross-check é executado antes do envio do alerta.
        self.link_provider = link_provider
        # `manual_purchase_fallback`: quando True (default), oportunidades que
        # passam todos os filtros (detector + confirmação + dedupe) mas não têm
        # link comercial acionável ainda geram alerta — sem hyperlink, com
        # instrução de pesquisa manual (Google Flights / Smiles / cia aérea).
        # Útil enquanto Kiwi não está aprovado.
        self.manual_purchase_fallback = manual_purchase_fallback

    def _resolve_actionable_link(
        self, route: Route, primary_quote: Quote
    ) -> tuple[Quote | None, str]:
        """Resolve o link comercial do alerta.

        Retorna (quote_para_envio, reason).
        - quote=primary_quote, reason="primary_link_ok": link primário já é acionável.
        - quote=composto, reason="cross_checked": cross-check Kiwi forneceu link compatível.
        - quote=None, reason="link_comercial_indisponivel": sem cross-check viável
          (sem link_provider, Kiwi retornou None, ou sem deep_link acionável).
        - quote=None, reason="preco_kiwi_incompativel": Kiwi retornou mas preço acima
          de tolerância e acima de good_brl.
        """
        if is_actionable_url(primary_quote.deep_link):
            return primary_quote, "primary_link_ok"

        if self.link_provider is None:
            return None, "link_comercial_indisponivel"

        print(
            f"cross-checking link via Kiwi for {route.origin}→{route.destination}",
            flush=True,
        )
        kiwi_quote = self.link_provider.quote(route)
        if kiwi_quote is None or not is_actionable_url(kiwi_quote.deep_link):
            print("Kiwi unavailable or no actionable link", flush=True)
            return None, "link_comercial_indisponivel"

        levels = levels_for(route.key) or {}
        good_brl = levels.get("good_brl")
        within_ratio = kiwi_quote.price_brl <= primary_quote.price_brl * LINK_PRICE_COMPATIBILITY_RATIO
        below_good = good_brl is not None and kiwi_quote.price_brl <= good_brl
        if not (within_ratio or below_good):
            print(
                f"Kiwi price incompatible: kiwi={kiwi_quote.price_brl:.0f} "
                f"primary={primary_quote.price_brl:.0f} good={good_brl}",
                flush=True,
            )
            return None, "preco_kiwi_incompativel"

        # Composto: preço do primário (que disparou o alerta), link do Kiwi
        composite = Quote(
            route=route,
            price_brl=primary_quote.price_brl,
            deep_link=kiwi_quote.deep_link,
            departure_date=kiwi_quote.departure_date,
            return_date=kiwi_quote.return_date,
            source="travelpayouts+kiwi",
        )
        print(
            f"cross-check OK: kiwi={kiwi_quote.price_brl:.0f} primary={primary_quote.price_brl:.0f}",
            flush=True,
        )
        return composite, "cross_checked"

    def _confirm(self, route: Route, first_price: float) -> tuple[bool, Quote | None]:
        """Segunda chamada ao provider para confirmar oportunidade.

        Retorna (confirmed, quote_to_use).
        - confirmed=True se a segunda quote vier com preço dentro de
          CONFIRMATION_TOLERANCE_PCT do primeiro (ou melhor).
        - confirmed=False se a segunda quote não vier ou vier acima da tolerância.
        """
        print(f"confirming alert for {route.origin}→{route.destination}", flush=True)
        second = self.provider.quote(route)
        if second is None:
            print("stale quote skipped", flush=True)
            return False, None
        max_allowed = first_price * (1 + CONFIRMATION_TOLERANCE_PCT)
        if second.price_brl <= max_allowed:
            print("confirmed alert", flush=True)
            return True, second
        print(
            f"stale quote skipped (second {second.price_brl:.0f} > tolerated {max_allowed:.0f})",
            flush=True,
        )
        return False, second

    def run_once(self, routes: list[Route] | None = None) -> MonitorResult:
        routes = routes if routes is not None else all_routes()
        notes: list[str] = []
        quotes_received = 0
        alerts_sent = 0
        stale_quotes_skipped = 0
        non_actionable_links_skipped = 0
        actionable_links_generated = 0
        manual_fallback_alerts_sent = 0
        currency_blocked = 0
        cabin_blocked = 0
        suspicious_blocked = 0

        def _now():
            return datetime.now(timezone.utc)

        brl_rate = get_usd_brl_rate()

        for route in routes:
            priority = is_priority(route)
            quote = self.provider.quote(route)
            if quote is None:
                notes.append(f"{_route_note(route)}: sem cotação")
                continue
            quotes_received += 1
            history = self.store.get(route.key)

            # GATE DE MOEDA: sem BRL confiável, NÃO avaliamos, NÃO empurramos
            # para o histórico e NUNCA enviamos Telegram. Evita o bug de
            # tratar USD como BRL e mandar "R$ 2.079" para tarifa US$ 2.079.
            # Cotações já em BRL (Kiwi/Mock) têm amount_brl_estimated setado
            # e passam direto — só USD sem câmbio confiável é bloqueado.
            if quote.amount_brl_estimated is None:
                currency_blocked += 1
                notes.append(
                    f"{_route_note(route)}: "
                    f"alerta bloqueado: câmbio USD_BRL_RATE ausente ou inválido "
                    f"(currency={quote.currency})"
                )
                continue

            # Normaliza a cotação para BRL antes de qualquer comparação.
            quote.price_brl = quote.amount_brl_estimated

            # GATE DE CABINE: a rota é monitorada como executiva
            # (route.cabin=business). Só seguimos para alerta se a cotação
            # confirmar a classe executiva (cabin=business E
            # cabin_confirmed=True). Travelpayouts não confirma cabine
            # (endpoint ignora trip_class) ⇒ cabin=unknown ⇒ bloqueado.
            # Evita o bug de chamar "Business em promoção" / "EXCELENTE"
            # uma tarifa cuja classe o provedor nunca confirmou (ex.: o
            # suspeito "US$ 232 Business GRU-MIA"). Preservamos o histórico
            # (continuidade da série) e NUNCA chamamos o notifier.
            cabin_confirmed_business = (
                quote.cabin == Cabin.BUSINESS and quote.cabin_confirmed
            )
            if route.cabin == Cabin.BUSINESS and not cabin_confirmed_business:
                history.push(quote.price_brl)
                history.last_quote = _quote_to_dict(quote, _now())
                cabin_blocked += 1
                notes.append(
                    f"{_route_note(route)}: "
                    f"alerta bloqueado: cabine não confirmada "
                    f"(cabin={quote.cabin.value})"
                )
                continue

            # GATE DE SANIDADE: mesmo com cabine confirmada, um preço
            # economicamente implausível (ex.: US$ 232 ≈ R$ 1.276 em
            # business internacional) não pode virar EXCELENTE/BOM. Só
            # se aplica a cotações não-BRL-nativas/USD (superfície real
            # do bug, cache Travelpayouts); `quote.suspicious=True` do
            # provider bloqueia em qualquer moeda. Preservamos o
            # histórico e NUNCA chamamos o notifier.
            if is_suspicious_price(route, quote, quote.amount_brl_estimated):
                history.push(quote.price_brl)
                history.last_quote = _quote_to_dict(quote, _now())
                suspicious_blocked += 1
                reason = suspicious_reason(
                    route, quote, quote.amount_brl_estimated
                )
                notes.append(
                    f"{_route_note(route)}: "
                    f"alerta bloqueado: preço economicamente suspeito "
                    f"({reason})"
                )
                continue

            # Só escalamos tetos USD→BRL quando houve conversão de moeda.
            # Cotação nativa em BRL usa os tetos como estão (comportamento
            # legado preservado).
            effective_rate = (
                brl_rate if quote.currency.upper() != "BRL" else None
            )

            ceiling_decision = evaluate_ceiling(
                history, quote.price_brl, route.key,
                priority=priority, brl_rate=effective_rate,
            )
            legacy_decision = evaluate(history, quote.price_brl, priority=priority)
            decision = ceiling_decision if ceiling_decision.alert else legacy_decision

            history.push(quote.price_brl)
            history.last_quote = _quote_to_dict(quote, _now())

            if not decision.alert:
                notes.append(f"{_route_note(route)}: {decision.reason}")
                continue

            quote_to_send = quote
            if self.confirm_alerts:
                confirmed, second_quote = self._confirm(route, quote.price_brl)
                if not confirmed:
                    stale_quotes_skipped += 1
                    notes.append(
                        f"{_route_note(route)}: stale_quote_skipped ({decision.reason})"
                    )
                    continue
                quote_to_send = second_quote or quote
                history.last_quote = _quote_to_dict(
                    quote_to_send, _now(), provider_note="second-confirmation"
                )

            resolved_quote, resolve_reason = self._resolve_actionable_link(route, quote_to_send)
            is_manual_fallback = False
            if resolved_quote is None:
                # Caso 1: preço Kiwi incompatível → diminui confiança no preço; nunca cai em manual
                # Caso 2: link comercial indisponível + manual_purchase_fallback → envia alerta manual
                if (
                    resolve_reason == "link_comercial_indisponivel"
                    and self.manual_purchase_fallback
                ):
                    print(
                        f"manual purchase fallback alert sent for {route.origin}→{route.destination}",
                        flush=True,
                    )
                    quote_to_send = Quote(
                        route=route,
                        price_brl=quote_to_send.price_brl,
                        deep_link=None,
                        departure_date=quote_to_send.departure_date,
                        return_date=quote_to_send.return_date,
                        source="manual_purchase",
                        amount=quote_to_send.amount,
                        currency=quote_to_send.currency,
                        amount_brl_estimated=quote_to_send.amount_brl_estimated,
                        fx_rate=quote_to_send.fx_rate,
                        # Preserva cabine/trip já confirmados pela cotação
                        # de origem — senão o alerta manual renderiza
                        # "cabine não confirmada" para uma tarifa que JÁ
                        # passou o gate de cabine como business confirmado.
                        cabin=quote_to_send.cabin,
                        cabin_confirmed=quote_to_send.cabin_confirmed,
                        trip_type=quote_to_send.trip_type,
                        suspicious=quote_to_send.suspicious,
                    )
                    is_manual_fallback = True
                else:
                    non_actionable_links_skipped += 1
                    if resolve_reason == "preco_kiwi_incompativel":
                        msg = "alerta descartado: preço Kiwi incompatível"
                    else:
                        msg = "alerta descartado: link comercial indisponível"
                    notes.append(f"{_route_note(route)}: {msg}")
                    continue
            else:
                quote_to_send = resolved_quote
                actionable_links_generated += 1

            # Score informativo embutido na decision (não filtra)
            score_levels = levels_for(route.key)
            if effective_rate is not None:
                score_levels = scaled_levels(score_levels, effective_rate)
            decision.score = compute_opportunity_score(
                quote_to_send.price_brl,
                score_levels,
                history,
                actionable_url=not is_manual_fallback,
                confirmed=self.confirm_alerts,
                is_hot_route=route.key in HOT_ROUTE_KEYS,
            )

            if self.notifier:
                ok = self.notifier.send_alert(quote_to_send, decision, priority=priority)
                if ok:
                    history.last_alert_at = _now().isoformat()
                    history.last_alert_price = quote_to_send.price_brl
                    alerts_sent += 1
                    if is_manual_fallback:
                        manual_fallback_alerts_sent += 1
                        notes.append(
                            f"{_route_note(route)}: ALERTA MANUAL {decision.reason}"
                        )
                    else:
                        notes.append(
                            f"{_route_note(route)}: ALERTA {decision.reason}"
                        )
                else:
                    notes.append(
                        f"{_route_note(route)}: alerta falhou no envio"
                    )
            else:
                notes.append(
                    f"{_route_note(route)}: {decision.reason} (notifier ausente)"
                )

        self.store.save()
        return MonitorResult(
            scanned=len(routes),
            quotes_received=quotes_received,
            alerts_sent=alerts_sent,
            notes=notes,
            stale_quotes_skipped=stale_quotes_skipped,
            non_actionable_links_skipped=non_actionable_links_skipped,
            actionable_links_generated=actionable_links_generated,
            manual_fallback_alerts_sent=manual_fallback_alerts_sent,
            currency_blocked=currency_blocked,
            cabin_blocked=cabin_blocked,
            suspicious_blocked=suspicious_blocked,
        )

    def run_cycle(self) -> MonitorResult:
        if self.cycle is None:
            return self.run_once()
        all_ = all_routes()
        priority = [r for r in all_ if is_priority(r)]
        rest = [r for r in all_ if not is_priority(r)]
        start, end = self.cycle.next_chunk(len(rest), self.chunk_size)
        chunk = rest[start:end]
        result = self.run_once(priority + chunk)
        self.cycle.advance(len(rest), self.chunk_size)
        self.cycle.save()
        return result

    def run_duffel_confirmations(
        self, routes: list[Route] | None = None
    ) -> MonitorResult:
        """Pass ADITIVO read-only: consulta Duffel para ofertas business
        CONFIRMADAS e envia alerta 🟢 "oferta confirmada" (sem compra
        automática, sem link). Não substitui nem altera `run_once`.

        Invariantes:
        - No-op total se `duffel_provider is None` ou cap == 0 (test 1).
        - Reaproveita gates de moeda + sanidade e o detector de teto
          (`evaluate_ceiling`) — mesmo padrão de qualidade/dedup do radar.
        - Duffel não tem deep_link (order_flow) ⇒ NÃO passa por
          `_resolve_actionable_link`; é esperado e correto.
        - Histórico/dedup ISOLADO em `self.duffel_store` (nunca o store
          principal) ⇒ relatórios de status/ciclo intactos.
        """
        notes: list[str] = []
        if self.duffel_provider is None or self.duffel_max_requests <= 0:
            # Desligado: devolve summary explícito p/ o 🧭 mostrar "inativa".
            return MonitorResult(
                scanned=0, quotes_received=0, alerts_sent=0, notes=notes,
                duffel_summary=DuffelStatusSummary(
                    enabled=False, requests=0, confirmed_alerts=0,
                    outcome=DUFFEL_DISABLED,
                ),
            )

        duffel_store = self.duffel_store if self.duffel_store is not None else self.store

        if routes is None:
            all_ = all_routes()
            priority = [r for r in all_ if is_priority(r)]
            rest = [r for r in all_ if not is_priority(r)]
            # PR #65: rota PROVADA primeiro (GRU-MIA one_way business),
            # depois as priority round_trip, depois o resto.
            routes = [DUFFEL_PROVEN_ROUTE] + priority + rest
        # Cap conservador de requests por ciclo (default 1).
        routes = routes[: self.duffel_max_requests]

        def _now():
            return datetime.now(timezone.utc)

        requests = 0
        confirmed_alerts = 0
        blocked = 0
        # Contadores finos p/ derivar o `outcome` canônico do summary.
        n_fx = 0
        n_cabin = 0
        n_suspicious = 0
        n_above = 0
        n_no_offer = 0
        n_send_failed = 0

        for route in routes:
            requests += 1
            quote = self.duffel_provider.quote(route)
            if quote is None:
                n_no_offer += 1
                notes.append(f"{_route_note(route)}: Duffel sem oferta confirmada")
                continue

            # GATE DE MOEDA: sem BRL confiável (ex.: EUR sem EUR_BRL_RATE),
            # não avaliamos e não alertamos.
            if quote.amount_brl_estimated is None:
                blocked += 1
                n_fx += 1
                notes.append(
                    f"{_route_note(route)}: Duffel bloqueado — câmbio "
                    f"{quote.currency}→BRL ausente"
                )
                continue
            quote.price_brl = quote.amount_brl_estimated

            # GATE DE CABINE (defesa redundante: provider só devolve
            # business confirmado, mas reasseguramos antes do alerta forte).
            if not (quote.cabin == Cabin.BUSINESS and quote.cabin_confirmed):
                blocked += 1
                n_cabin += 1
                notes.append(
                    f"{_route_note(route)}: Duffel bloqueado — cabine não confirmada"
                )
                continue

            # GATE DE SANIDADE: preço business implausível não vira 🟢.
            if is_suspicious_price(route, quote, quote.amount_brl_estimated):
                blocked += 1
                n_suspicious += 1
                reason = suspicious_reason(route, quote, quote.amount_brl_estimated)
                notes.append(
                    f"{_route_note(route)}: Duffel bloqueado — preço suspeito ({reason})"
                )
                continue

            history = duffel_store.get(f"{route.key}::duffel")
            priority = is_priority(route)
            # Tetos configurados têm magnitude USD; quando o preço veio de
            # conversão (USD/EUR→BRL) escalamos os tetos pela MESMA taxa
            # usada na conversão (quote.fx_rate). BRL-nativo usa tetos como
            # estão — idêntico ao comportamento de `run_once`.
            effective_rate = (
                quote.fx_rate if quote.currency.upper() != "BRL" else None
            )
            ceiling = evaluate_ceiling(
                history, quote.price_brl, route.key,
                priority=priority, brl_rate=effective_rate,
            )
            legacy = evaluate(history, quote.price_brl, priority=priority)
            decision = ceiling if ceiling.alert else legacy

            history.push(quote.price_brl)
            history.last_quote = _quote_to_dict(quote, _now(), provider_note="duffel")

            if not decision.alert:
                n_above += 1
                notes.append(f"{_route_note(route)}: Duffel — {decision.reason}")
                continue

            # Score informativo (sem link acionável: order_flow).
            score_levels = levels_for(route.key)
            if effective_rate is not None:
                score_levels = scaled_levels(score_levels, effective_rate)
            decision.score = compute_opportunity_score(
                quote.price_brl, score_levels, history,
                actionable_url=False,
                confirmed=True,
                is_hot_route=route.key in HOT_ROUTE_KEYS,
            )

            if self.notifier:
                ok = self.notifier.send_alert(quote, decision, priority=priority)
                if ok:
                    history.last_alert_at = _now().isoformat()
                    history.last_alert_price = quote.price_brl
                    confirmed_alerts += 1
                    notes.append(
                        f"{_route_note(route)}: ALERTA DUFFEL CONFIRMADO {decision.reason}"
                    )
                else:
                    n_send_failed += 1
                    notes.append(f"{_route_note(route)}: Duffel alerta falhou no envio")
            else:
                # Sem notifier (CLI local / teste): nada é enviado, então
                # `confirmed_alerts` NÃO incrementa (é "enviados de fato").
                # Em produção o heartbeat só roda com notifier presente, então
                # este ramo não afeta a observabilidade do 🧭.
                notes.append(
                    f"{_route_note(route)}: Duffel — {decision.reason} (notifier ausente)"
                )

        duffel_store.save()

        # Deriva o `outcome` canônico por prioridade (o mais informativo
        # primeiro). Com cap=1 há um único request, mas a ordem cobre cap>1.
        if confirmed_alerts > 0:
            outcome = DUFFEL_ALERT_SENT
        elif n_send_failed > 0:
            outcome = DUFFEL_SEND_FAILED
        elif n_fx > 0:
            outcome = DUFFEL_BLOCKED_FX
        elif n_above > 0:
            outcome = DUFFEL_ABOVE_THRESHOLD
        elif n_suspicious > 0:
            outcome = DUFFEL_BLOCKED_SUSPICIOUS
        elif n_cabin > 0:
            outcome = DUFFEL_BLOCKED_CABIN
        else:
            outcome = DUFFEL_NO_OFFER

        summary = DuffelStatusSummary(
            enabled=True,
            requests=requests,
            confirmed_alerts=confirmed_alerts,
            outcome=outcome,
        )
        return MonitorResult(
            scanned=len(routes),
            quotes_received=requests,
            alerts_sent=confirmed_alerts,
            notes=notes,
            duffel_requests=requests,
            duffel_confirmed_alerts=confirmed_alerts,
            duffel_blocked=blocked,
            duffel_summary=summary,
        )
