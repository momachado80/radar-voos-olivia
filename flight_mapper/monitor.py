"""Orquestrador principal: varre rotas, atualiza histórico e dispara alertas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .airports import is_actionable_url
from .currency import get_usd_brl_rate
from .config import (
    DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY,
    DUFFEL_ORDER_FLOW_ALERT_DISABLED,
    DUFFEL_ORDER_FLOW_ALERT_GROUPED_PUSH,
    DUFFEL_ORDER_FLOW_ALERT_MODES,
)
from .cycle_state import CycleState
from .detector import evaluate, evaluate_ceiling
from .duffel_cooldown import cooldown_key
from .duffel_status import (
    DUFFEL_ABOVE_THRESHOLD,
    DUFFEL_ALERT_SENT,
    DUFFEL_BLOCKED_CABIN,
    DUFFEL_BLOCKED_FX,
    DUFFEL_BLOCKED_SUSPICIOUS,
    DUFFEL_DISABLED,
    DUFFEL_NO_OFFER,
    DUFFEL_SEND_FAILED,
    DuffelGroupSummary,
    DuffelStatusSummary,
    DuffelWatchlistSummary,
)
from .formatting import trip_label_pt
from .notifier import (
    LINK_STATUS_ORDER_FLOW,
    TelegramNotifier,
    build_duffel_pending_offer,
    format_grouped_duffel_pending,
    link_status_for,
)
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
    # PR #67: resumo da watchlist premium (Londres/Paris setembro). None
    # quando a watchlist não rodou. NUNCA contém dado sensível.
    duffel_watchlist_summary: DuffelWatchlistSummary | None = None
    # PR #71: estatística do agrupamento Duffel order_flow (compra pendente).
    duffel_group_summary: DuffelGroupSummary | None = None


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
        duffel_watchlist: list | None = None,
        duffel_watchlist_max_requests: int = 0,
        duffel_watchlist_state=None,
        duffel_cooldown_state=None,
        duffel_order_flow_alert_mode: str = DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY,
        duffel_pool_label: str = "watchlist",
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
        # PR #67: watchlist premium (Londres/Paris setembro). Consultada
        # ANTES da rota genérica, com cap dedicado e rotação (state).
        # Vazia/cap 0 ⇒ no-op total (comportamento Duffel anterior intacto).
        self.duffel_watchlist = duffel_watchlist or []
        self.duffel_watchlist_max_requests = max(0, duffel_watchlist_max_requests)
        self.duffel_watchlist_state = duffel_watchlist_state
        # PR #77: rótulo do pool ativo p/ a linha do 🧭 (broad / watchlist).
        self.duffel_pool_label = duffel_pool_label
        # PR #71: cooldown 6h dos alertas Duffel order_flow agrupados. None ⇒
        # sem persistência (cada ciclo agrupa o que achar, sem supressão).
        self.duffel_cooldown_state = duffel_cooldown_state
        # PR #73: modo do alerta order_flow "compra pendente".
        # - daily_only (default): SEM push standalone; só resumo no diário.
        # - grouped_push: preserva a mensagem agrupada do PR #71 (opt-in).
        # - disabled: suprime do Telegram (só logs).
        # Valor inválido cai p/ o default seguro `daily_only`.
        self.duffel_order_flow_alert_mode = (
            duffel_order_flow_alert_mode
            if duffel_order_flow_alert_mode in DUFFEL_ORDER_FLOW_ALERT_MODES
            else DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY
        )
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

    def _process_one_duffel_quote(
        self, *, route, quote, history_key, label, notes, duffel_store, now_fn,
        expected_cabin: Cabin = Cabin.BUSINESS, threshold_key: str | None = None,
        collector: list | None = None,
    ) -> str:
        """Aplica gates (moeda/cabine/sanidade) + detector de teto e envia
        o alerta 🟢 p/ UMA cotação Duffel. Devolve um código de resultado
        (no_offer/blocked_fx/blocked_cabin/blocked_suspicious/above_threshold/
        alert_sent/send_failed/notifier_absent). Anexa nota a `notes`.

        `label` prefixa a nota (ex.: "watchlist") — vazio mantém o formato
        legado do pass genérico byte a byte. `expected_cabin` é a cabine
        esperada (business/economy — PR #68). `threshold_key` define qual
        teto usar (default `route.key`; economy usa namespace `-economy`)."""
        prefix = f"{label} " if label else ""
        tkey = threshold_key or route.key
        if quote is None:
            notes.append(f"{prefix}{_route_note(route)}: Duffel sem oferta confirmada")
            return "no_offer"

        # GATE DE MOEDA: sem BRL confiável (ex.: EUR sem EUR_BRL_RATE),
        # não avaliamos e não alertamos.
        if quote.amount_brl_estimated is None:
            notes.append(
                f"{prefix}{_route_note(route)}: Duffel bloqueado — câmbio "
                f"{quote.currency}→BRL ausente"
            )
            return "blocked_fx"
        quote.price_brl = quote.amount_brl_estimated

        # GATE DE CABINE (defesa redundante: provider só devolve a cabine
        # pedida confirmada, mas reasseguramos antes do alerta forte).
        if not (quote.cabin == expected_cabin and quote.cabin_confirmed):
            notes.append(
                f"{prefix}{_route_note(route)}: Duffel bloqueado — cabine não confirmada"
            )
            return "blocked_cabin"

        # GATE DE SANIDADE: preço implausível (p/ a cabine/trip) não vira 🟢.
        if is_suspicious_price(route, quote, quote.amount_brl_estimated):
            reason = suspicious_reason(route, quote, quote.amount_brl_estimated)
            notes.append(
                f"{prefix}{_route_note(route)}: Duffel bloqueado — preço suspeito ({reason})"
            )
            return "blocked_suspicious"

        history = duffel_store.get(history_key)
        priority = is_priority(route)
        # Tetos configurados têm magnitude USD; quando o preço veio de
        # conversão (USD/EUR→BRL) escalamos os tetos pela MESMA taxa usada
        # na conversão (quote.fx_rate). BRL-nativo usa tetos como estão.
        effective_rate = (
            quote.fx_rate if quote.currency.upper() != "BRL" else None
        )
        ceiling = evaluate_ceiling(
            history, quote.price_brl, tkey,
            priority=priority, brl_rate=effective_rate,
        )
        legacy = evaluate(history, quote.price_brl, priority=priority)
        decision = ceiling if ceiling.alert else legacy

        history.push(quote.price_brl)
        history.last_quote = _quote_to_dict(quote, now_fn(), provider_note="duffel")

        if not decision.alert:
            notes.append(f"{prefix}{_route_note(route)}: Duffel — {decision.reason}")
            return "above_threshold"

        # Score informativo (sem link acionável: order_flow).
        score_levels = levels_for(tkey)
        if effective_rate is not None:
            score_levels = scaled_levels(score_levels, effective_rate)
        decision.score = compute_opportunity_score(
            quote.price_brl, score_levels, history,
            actionable_url=False, confirmed=True,
            is_hot_route=tkey in HOT_ROUTE_KEYS,
        )

        # PR #71: order_flow NÃO envia alerta standalone — entra na mensagem
        # AGRUPADA "compra pendente", respeitando cooldown de 6h (a menos que
        # o preço melhore ≥5%). Só `direct_link` (futuro) sai imediato.
        if link_status_for(quote) == LINK_STATUS_ORDER_FLOW:
            ck = cooldown_key(quote)
            cd = self.duffel_cooldown_state
            if cd is not None and cd.is_suppressed(ck, quote.price_brl, now_fn()):
                notes.append(
                    f"{prefix}{_route_note(route)}: Duffel suprimido por cooldown (6h)"
                )
                return "cooldown_suppressed"
            if collector is not None:
                collector.append(
                    (
                        build_duffel_pending_offer(quote, decision),
                        ck, quote.price_brl, quote.currency,
                    )
                )
            notes.append(
                f"{prefix}{_route_note(route)}: Duffel coletado p/ agrupamento "
                f"({decision.reason})"
            )
            return "collected"

        # direct_link (futuro): alerta standalone imediato (não agrupa).
        if self.notifier:
            ok = self.notifier.send_alert(quote, decision, priority=priority)
            if ok:
                history.last_alert_at = now_fn().isoformat()
                history.last_alert_price = quote.price_brl
                notes.append(
                    f"{prefix}{_route_note(route)}: ALERTA DUFFEL CONFIRMADO {decision.reason}"
                )
                return "alert_sent"
            notes.append(f"{prefix}{_route_note(route)}: Duffel alerta falhou no envio")
            return "send_failed"
        # Sem notifier (CLI local / teste): nada é enviado.
        notes.append(
            f"{prefix}{_route_note(route)}: Duffel — {decision.reason} (notifier ausente)"
        )
        return "notifier_absent"

    def _run_duffel_watchlist(
        self, *, duffel_store, notes, now_fn, collector,
    ) -> "tuple[DuffelWatchlistSummary | None, int]":
        """Pass premium (PR #67): consulta combinações Londres/Paris setembro
        ANTES da rota genérica, com cap dedicado e rotação. Histórico/dedup
        por combinação de datas. PR #71: ofertas order_flow vão para o
        `collector` (mensagem agrupada), não alertas standalone. Devolve
        `(summary, suprimidas_por_cooldown)`.
        """
        entries = self.duffel_watchlist or []
        cap = self.duffel_watchlist_max_requests
        if not entries or cap <= 0:
            return None, 0

        n = len(entries)
        if self.duffel_watchlist_state is not None:
            idxs = self.duffel_watchlist_state.window(n, cap)
        else:
            idxs = [i % n for i in range(min(cap, n))]

        checked = 0
        business_alerts = 0
        economy_alerts = 0
        suppressed = 0
        for i in idxs:
            entry = entries[i]
            checked += 1
            cabin = getattr(entry, "cabin", "business")
            expected = getattr(entry, "cabin_enum", Cabin.BUSINESS)
            tkey = getattr(entry, "threshold_key", entry.route.key)
            fn = getattr(self.duffel_provider, "quote_for_dates", None)
            if callable(fn):
                quote = fn(
                    entry.route, entry.outbound_date, entry.return_date,
                    cabin=cabin,
                )
            else:
                quote = self.duffel_provider.quote(entry.route)
            code = self._process_one_duffel_quote(
                route=entry.route, quote=quote,
                history_key=entry.history_key, label="watchlist",
                notes=notes, duffel_store=duffel_store, now_fn=now_fn,
                expected_cabin=expected, threshold_key=tkey,
                collector=collector,
            )
            if code in ("collected", "alert_sent"):
                if expected == Cabin.ECONOMY:
                    economy_alerts += 1
                else:
                    business_alerts += 1
            elif code == "cooldown_suppressed":
                suppressed += 1

        # Avança a rotação p/ cobrir as demais combinações no próximo ciclo.
        if self.duffel_watchlist_state is not None:
            self.duffel_watchlist_state.advance(n, cap)
            self.duffel_watchlist_state.save()

        return (
            DuffelWatchlistSummary(
                enabled=True, checked=checked,
                confirmed_alerts=business_alerts + economy_alerts,
                business_alerts=business_alerts, economy_alerts=economy_alerts,
                pool=self.duffel_pool_label,
            ),
            suppressed,
        )

    def run_duffel_confirmations(
        self, routes: list[Route] | None = None
    ) -> MonitorResult:
        """Pass ADITIVO read-only: consulta Duffel para ofertas business
        CONFIRMADAS e envia alerta 🟢 "oferta confirmada" (sem compra
        automática, sem link). Não substitui nem altera `run_once`.

        Ordem (PR #67): watchlist premium Londres/Paris PRIMEIRO, depois a
        rota genérica (GRU-MIA one_way provada + priority + resto).

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
        watchlist_active = (
            bool(self.duffel_watchlist)
            and self.duffel_watchlist_max_requests > 0
        )
        # Desligado quando não há provider OU nada a consultar (genérico E
        # watchlist ambos com cap 0). Senão segue — a watchlist pode rodar
        # mesmo com o cap genérico 0 (e vice-versa).
        if self.duffel_provider is None or (
            self.duffel_max_requests <= 0 and not watchlist_active
        ):
            return MonitorResult(
                scanned=0, quotes_received=0, alerts_sent=0, notes=notes,
                duffel_summary=DuffelStatusSummary(
                    enabled=False, requests=0, confirmed_alerts=0,
                    outcome=DUFFEL_DISABLED,
                ),
            )

        duffel_store = self.duffel_store if self.duffel_store is not None else self.store

        def _now():
            return datetime.now(timezone.utc)

        # PR #71: coletor único de ofertas order_flow p/ a mensagem agrupada
        # + contador global de suprimidas por cooldown.
        # Itens: (DuffelPendingOffer, cooldown_key, price_brl, currency).
        collector: list = []
        suppressed_total = 0

        # ---- WATCHLIST PREMIUM (prioridade, antes da rota genérica) ----
        watchlist_summary, wl_suppressed = self._run_duffel_watchlist(
            duffel_store=duffel_store, notes=notes, now_fn=_now,
            collector=collector,
        )
        suppressed_total += wl_suppressed

        # ---- PASS GENÉRICO (rota provada primeiro) ----
        if routes is None:
            all_ = all_routes()
            priority = [r for r in all_ if is_priority(r)]
            rest = [r for r in all_ if not is_priority(r)]
            # PR #65: rota PROVADA primeiro (GRU-MIA one_way business),
            # depois as priority round_trip, depois o resto.
            routes = [DUFFEL_PROVEN_ROUTE] + priority + rest
        # Cap conservador de requests por ciclo (default 1).
        routes = routes[: self.duffel_max_requests]

        requests = 0
        confirmed_alerts = 0
        blocked = 0
        # Contadores finos p/ derivar o `outcome` canônico do summary.
        n_fx = 0
        n_cabin = 0
        n_suspicious = 0
        n_above = 0
        n_send_failed = 0

        for route in routes:
            requests += 1
            quote = self.duffel_provider.quote(route)
            code = self._process_one_duffel_quote(
                route=route, quote=quote, history_key=f"{route.key}::duffel",
                label="", notes=notes, duffel_store=duffel_store, now_fn=_now,
                collector=collector,
            )
            if code == "blocked_fx":
                blocked += 1
                n_fx += 1
            elif code == "blocked_cabin":
                blocked += 1
                n_cabin += 1
            elif code == "blocked_suspicious":
                blocked += 1
                n_suspicious += 1
            elif code == "above_threshold":
                n_above += 1
            elif code in ("collected", "alert_sent"):
                confirmed_alerts += 1
            elif code == "cooldown_suppressed":
                suppressed_total += 1
            elif code == "send_failed":
                n_send_failed += 1
            # no_offer / notifier_absent → sem contador adicional

        duffel_store.save()

        # ---- OFERTAS order_flow "compra pendente" (PR #71 + PR #73) ----
        # PR #73: order_flow NÃO tem caminho de compra direto ⇒ por padrão
        # NÃO gera push standalone. O modo decide o destino:
        # - grouped_push: envia a mensagem agrupada do PR #71 (opt-in).
        # - daily_only (default): sem push; só resumo no relatório diário.
        # - disabled: suprime do Telegram (só logs).
        grouped = len(collector)
        message_sent = False
        offers = [item[0] for item in collector]
        # Top 3 (por qualidade) p/ a seção opcional do relatório diário.
        # DuffelPendingOffer já é sanitizado (sem offer_id/token/payload).
        top_offers = tuple(
            sorted(offers, key=lambda o: (o.score or 0), reverse=True)[:3]
        )
        mode = self.duffel_order_flow_alert_mode
        if collector and mode == DUFFEL_ORDER_FLOW_ALERT_GROUPED_PUSH:
            if self.notifier is not None:
                try:
                    ok = self.notifier.send(format_grouped_duffel_pending(offers))
                except Exception:
                    ok = False
                if ok:
                    message_sent = True
                    # Registra cooldown SÓ após envio bem-sucedido.
                    if self.duffel_cooldown_state is not None:
                        now = _now()
                        for _offer, ck, price_brl, currency in collector:
                            self.duffel_cooldown_state.record(
                                ck, price_brl, currency, now,
                            )
                        self.duffel_cooldown_state.save(now)
                    notes.append(
                        f"Duffel: mensagem agrupada enviada ({grouped} oferta(s) "
                        f"compra pendente)"
                    )
                else:
                    notes.append("Duffel: envio da mensagem agrupada falhou")
            else:
                notes.append(
                    f"Duffel: {grouped} oferta(s) compra pendente coletadas "
                    f"(notifier ausente — não enviado)"
                )
        elif collector and mode == DUFFEL_ORDER_FLOW_ALERT_DAILY_ONLY:
            notes.append(
                f"Duffel: {grouped} oferta(s) compra pendente — modo "
                f"daily_only (sem push standalone; resumo no relatório diário)"
            )
        elif collector and mode == DUFFEL_ORDER_FLOW_ALERT_DISABLED:
            notes.append(
                f"Duffel: {grouped} oferta(s) compra pendente — modo "
                f"disabled (suprimido do Telegram; só logs)"
            )

        group_summary = DuffelGroupSummary(
            confirmed_pending=grouped + suppressed_total,
            grouped=grouped,
            suppressed_cooldown=suppressed_total,
            message_sent=message_sent,
            mode=mode,
            top_offers=top_offers,
        )

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
            duffel_watchlist_summary=watchlist_summary,
            duffel_group_summary=group_summary,
        )
