"""Envio de mensagens via Telegram Bot API."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .airports import is_actionable_url, route_airport_label, route_city_label
from .auxiliary_links import build_auxiliary_search_links
from .detector import CRITERION_CEILING, LEVEL_EXCELLENT, LEVEL_GOOD, Decision
from .formatting import (
    cabin_label_pt,
    format_brl,
    format_detection_time,
    format_fx_line,
    format_price,
    format_rate,
    format_source,
    trip_label_pt,
)
from .providers import Quote
from .regions import Cabin, TripType


def _level_title(level: str | None, score: int | None = None) -> str:
    """Marcador de nível no título do alerta, com score informativo opcional."""
    score_suffix = f" — Score {score}/100" if score is not None else ""
    if level == LEVEL_EXCELLENT:
        return f"🚨 EXCELENTE{score_suffix} — "
    if level == LEVEL_GOOD:
        return f"🎯 BOM{score_suffix} — "
    if score is not None:
        return f"📉 Score {score}/100 — "
    return ""


def _duffel_pending_headline(quote: Quote, trip_suffix: str) -> str:
    """Título de oferta Duffel CONFIRMADA (booking_flow=order_flow) — PR #69,
    revisado no PR #76.

    Regra de produto: a oferta é confirmada pela Duffel, mas a Duffel não dá
    link de checkout. O robô cruza com o Google Flights e leva a usuária à
    BUSCA PRÉ-PREENCHIDA — não é a oferta travada, então segue 🟡 (não 🟢) e
    nunca "clique para comprar". Cabine visível p/ não perder o sinal."""
    if quote.cabin == Cabin.BUSINESS:
        cab_part = " — Executiva"
    elif quote.cabin == Cabin.ECONOMY:
        cab_part = " — Econômica"
    else:
        cab_part = ""
    return f"🟡 Oferta confirmada{cab_part} — buscar no Google Flights{trip_suffix}"


# link_status normalizado (PR #68): descreve a ACIONABILIDADE do link em
# TODO alerta, sem prometer checkout onde não há.
LINK_STATUS_DIRECT = "direct_link"        # deep_link clicável real (ex.: Kiwi)
LINK_STATUS_ORDER_FLOW = "order_flow"     # Duffel: ordem via API, sem link
LINK_STATUS_AUX = "auxiliary_search"      # URL de busca gerada, não é checkout
LINK_STATUS_NONE = "none"                 # sem link


def link_status_for(quote: Quote) -> str:
    """Classifica a acionabilidade do link da cotação (PR #68).

    - Duffel ⇒ SEMPRE `order_flow` (sem link direto de compra), a menos
      que código futuro mude explicitamente.
    - deep_link clicável real (Kiwi/composto) ⇒ `direct_link`.
    - manual_purchase (links auxiliares de busca) ⇒ `auxiliary_search`.
    - caso contrário ⇒ `none`.
    """
    if quote.source == "duffel":
        return LINK_STATUS_ORDER_FLOW
    if is_actionable_url(quote.deep_link):
        return LINK_STATUS_DIRECT
    if quote.source == "manual_purchase":
        return LINK_STATUS_AUX
    return LINK_STATUS_NONE


@dataclass(frozen=True)
class DuffelPendingOffer:
    """Linha SANITIZADA de uma oferta Duffel order_flow p/ a mensagem
    agrupada (PR #71). NUNCA contém offer_id/token/payload/URL/passageiro —
    só rótulos legíveis já formatados + score p/ ordenação."""

    route_label: str       # "São Paulo → Londres"
    cabin_pt: str          # "Executiva" | "Econômica"
    dates: str             # "2026-09-02 → 2026-09-12" | "2026-09-10"
    price_display: str     # "964 EUR ≈ R$ 5.784"
    target_display: str    # "alvo R$ 14.400" | ""
    airline: str | None    # "AF"
    score: int | None      # p/ ordenar por qualidade (desc)
    # PR #76: link de busca PRÉ-PREENCHIDA no Google Flights (rota/datas/
    # cabine). NÃO é a oferta Duffel travada — atalho de busca. Só dados
    # públicos no URL (sem offer_id/token/preço/payload/passageiro).
    search_url: str | None = None
    # PR #79: rótulo PT do trip_type p/ a linha "Busca Google Flights:
    # somente ida/ida e volta, cabine ..." — garante que a busca abra com
    # o tipo correto e que a Olivia leia o que está sendo aberto.
    trip_pt: str = ""


def build_duffel_pending_offer(quote: Quote, decision: Decision) -> DuffelPendingOffer:
    """Constrói a linha sanitizada da oferta a partir do quote/decision.
    Só usa campos não-sensíveis do Quote (rota/cabine/datas/preço/cia)."""
    from .google_flights_link import duffel_google_flights_url
    route_label = route_city_label(quote.route.origin, quote.route.destination)
    if quote.cabin == Cabin.BUSINESS:
        cabin_pt = "Executiva"
    elif quote.cabin == Cabin.ECONOMY:
        cabin_pt = "Econômica"
    else:
        cabin_pt = "Cabine não confirmada"
    show_return = (
        quote.trip_type == TripType.ROUND_TRIP and bool(quote.return_date)
    )
    dates = quote.departure_date + (
        f" → {quote.return_date}" if show_return else ""
    )
    price_display = format_price(
        quote.amount if quote.amount is not None else quote.price_brl,
        quote.currency, quote.amount_brl_estimated, quote.fx_rate,
    )
    target_display = (
        f"alvo {format_brl(decision.threshold)}"
        if decision.threshold is not None else ""
    )
    return DuffelPendingOffer(
        route_label=route_label, cabin_pt=cabin_pt, dates=dates,
        price_display=price_display, target_display=target_display,
        airline=quote.airline, score=decision.score,
        search_url=duffel_google_flights_url(quote),
        # PR #79: trip_pt humano para a linha de label.
        trip_pt=trip_label_pt(quote.trip_type),
    )


def _duffel_search_label_line(quote: Quote) -> str:
    """Linha PT-BR de label da busca pré-preenchida (PR #79).
    Ex.: 'Busca Google Flights: somente ida, cabine executiva.'
    Sem URL, sem dado sensível — só rótulos de trip_type + cabine."""
    trip_pt = trip_label_pt(quote.trip_type)
    if quote.cabin == Cabin.ECONOMY:
        cabin_pt = "econômica"
    else:
        # rota Duffel é monitorada como executiva (gate de cabine garante).
        cabin_pt = "executiva"
    return f"Busca Google Flights: {trip_pt}, cabine {cabin_pt}."


def format_grouped_duffel_pending(offers: list[DuffelPendingOffer]) -> str:
    """Mensagem ÚNICA agrupando ofertas Duffel order_flow confirmadas
    (PR #71). Lista até 5 (por qualidade), some o excedente. PR #76: cada
    oferta traz o link de busca PRÉ-PREENCHIDA no Google Flights. NUNCA
    expõe dado sensível (o URL só tem rota/datas/cabine públicas)."""
    ordered = sorted(offers, key=lambda o: (o.score or 0), reverse=True)
    top = ordered[:5]
    extra = len(ordered) - len(top)
    lines = ["🟡 Ofertas confirmadas pela Duffel — buscar no Google Flights"]
    for i, o in enumerate(top, 1):
        parts = [o.route_label, o.cabin_pt, o.dates, o.price_display]
        if o.target_display:
            parts.append(o.target_display)
        if o.airline:
            parts.append(o.airline)
        lines.append(f"{i}. " + " — ".join(parts))
        if o.search_url:
            lines.append(f'   🔎 <a href="{o.search_url}">Buscar no Google Flights</a>')
            # PR #79: label trip_type+cabine na sub-linha da oferta.
            if o.trip_pt:
                cab_low = (
                    "econômica" if o.cabin_pt.lower() == "econômica" else "executiva"
                )
                lines.append(
                    f"      Busca Google Flights: {o.trip_pt}, cabine {cab_low}."
                )
    if extra > 0:
        lines.append(f"+{extra} outras ofertas confirmadas no ciclo.")
    lines.append(
        "Busca pré-preenchida a partir da oferta confirmada pela Duffel. "
        "Preço e disponibilidade podem variar; confira antes de comprar."
    )
    return "\n".join(lines)


def _duffel_cambio_prefix(quote: Quote) -> str:
    """Prefixo do câmbio p/ embutir no parêntese do preço de ofertas Duffel
    em moeda estrangeira: `câmbio EUR_BRL_RATE=6.00; `. Vazio quando não
    aplicável (BRL ou sem fx_rate). NUNCA expõe token/URL/payload."""
    if quote.source != "duffel":
        return ""
    if quote.fx_rate is None or (quote.currency or "").strip().upper() == "BRL":
        return ""
    var = f"{quote.currency.strip().upper()}_BRL_RATE"
    return f"câmbio {var}={format_rate(quote.fx_rate)}; "


def _level_criterion_line(decision: Decision) -> str:
    if decision.criterion == CRITERION_CEILING:
        if decision.level == LEVEL_EXCELLENT:
            return "🚨 Critério: preço excelente — abaixo do alvo de oportunidade"
        if decision.level == LEVEL_GOOD:
            return "🎯 Critério: preço bom — abaixo do teto configurado"
        return "🎯 Critério: preço abaixo do alvo configurado para esta rota"
    return "📉 Critério: queda histórica acima do limite"


def format_alert(
    quote: Quote,
    decision: Decision,
    priority: bool = False,
    now: datetime | None = None,
) -> str:
    """Monta o texto HTML do alerta. Função pura, sem efeitos colaterais."""
    now = now or datetime.now(timezone.utc)
    flag = "🔥 " if priority else ""
    # Guarda defensiva (Regra 5 do PR C): só rotulamos "Business em
    # promoção" + nível EXCELENTE/BOM quando a cabine foi confirmada como
    # executiva. Caso contrário, título honesto e sem nível forte. Em
    # produção o Monitor já bloqueia antes do notifier; isto garante que
    # nenhum caminho (preview/manual) renderize "Business" sem confirmação.
    # Título por cabine (Regra 1 do PR E):
    # - business confirmado  → "Business em promoção"
    # - economy  confirmado  → "Econômica em promoção"
    # - unknown/não confirmado → nunca "Business"; aviso honesto sem nível.
    trip_suffix = f" ({trip_label_pt(quote.trip_type)})"
    is_duffel = quote.source == "duffel"
    duffel_pending = (
        is_duffel
        and quote.cabin_confirmed
        and link_status_for(quote) == LINK_STATUS_ORDER_FLOW
    )
    if duffel_pending:
        # PR #69: Duffel order_flow = oferta confirmada SEM caminho de compra
        # direto ⇒ NÃO é alerta verde totalmente acionável. Vira 🟡 "compra
        # pendente" (cabine no título; valor/alvo seguem no corpo).
        level_prefix = ""
        headline = _duffel_pending_headline(quote, trip_suffix)
    elif quote.cabin_confirmed and quote.cabin == Cabin.BUSINESS:
        level_prefix = _level_title(decision.level, decision.score)
        headline = f"Business em promoção{trip_suffix}"
    elif quote.cabin_confirmed and quote.cabin == Cabin.ECONOMY:
        level_prefix = _level_title(decision.level, decision.score)
        headline = f"Econômica em promoção{trip_suffix}"
    else:
        level_prefix = ""
        headline = f"⚠️ Cabine não confirmada — verificar{trip_suffix}"
    city_line = route_city_label(quote.route.origin, quote.route.destination)
    iata_line = route_airport_label(quote.route.origin, quote.route.destination)

    price_display = format_price(
        quote.amount if quote.amount is not None else quote.price_brl,
        quote.currency,
        quote.amount_brl_estimated,
        quote.fx_rate,
    )
    is_ceiling = decision.criterion == CRITERION_CEILING and decision.threshold is not None
    # PR #66: p/ Duffel (moeda estrangeira confirmada + BRL estimado), o
    # câmbio entra no MESMO parêntese do preço, ex.:
    # `964 EUR ≈ R$ 5.784 (câmbio EUR_BRL_RATE=6.00; alvo R$ 6.000)`.
    cambio = _duffel_cambio_prefix(quote)
    if is_ceiling:
        price_line = (
            f"💰 {price_display} "
            f"({cambio}alvo {format_brl(decision.threshold)})"
        )
    else:
        if decision.average is not None and decision.drop_pct is not None:
            price_line = (
                f"💰 {price_display} "
                f"({cambio}média {format_brl(decision.average)}, "
                f"queda {decision.drop_pct:.0%})"
            )
        elif cambio:
            # Sem teto/queda mas com câmbio Duffel: mostra só o câmbio.
            price_line = f"💰 {price_display} ({cambio.rstrip('; ')})"
        else:
            price_line = f"💰 {price_display}"

    # Regra 6/7: câmbio em linha própria sempre que houve conversão USD→BRL
    # (vale também p/ manual fallback, que preserva currency/fx_rate). Duffel
    # NÃO usa esta linha — o câmbio já está embutido no parêntese do preço.
    fx_line = (
        format_fx_line(quote.fx_rate)
        if (quote.currency.upper() == "USD" and not is_duffel)
        else None
    )
    if fx_line:
        price_line = f"{price_line}\n{fx_line}"

    criterion_line = _level_criterion_line(decision)
    # Datas (Regra 3): seta de volta só em round_trip COM return_date.
    # one_way / sem retorno ⇒ só a data de ida, sem seta vazia.
    show_return = (
        quote.trip_type == TripType.ROUND_TRIP and bool(quote.return_date)
    )
    dates = quote.departure_date + (
        f" → {quote.return_date}" if show_return else ""
    )

    head_lines: list[str] = [f"{city_line} ({quote.route.region})"]
    if iata_line != city_line:
        head_lines.append(iata_line)

    detection_line = f"🕒 Encontrado em: {format_detection_time(now)}"

    extras: list[str] = [detection_line, criterion_line]
    if is_ceiling:
        extras.append("⚠️ Preço pode mudar rápido. Conferir agora.")

    if quote.source == "duffel":
        # Duffel: oferta CONFIRMADA via order_flow — SEM caminho de compra
        # direto (booking é API server-to-server). PR #69: não rotulamos
        # como compra acionável; é "oferta confirmada, compra pendente".
        # NUNCA expomos offer_id / token / payload — só fonte, cabine,
        # carrier e a ação manual no Dashboard. NUNCA "clique para comprar".
        _cab_pt = "econômica" if quote.cabin == Cabin.ECONOMY else "business"
        extras.append(f"🛒 Fonte: Duffel (Offer Request, cabine {_cab_pt} confirmada)")
        if quote.airline:
            # PR #83: prefere "Nome (IATA)" quando o IATA está mapeado em
            # `airlines.py`; cai pro IATA bruto caso contrário (sem inventar).
            from .airlines import airline_label
            extras.append(
                f"🛫 Companhia: {airline_label(quote.airline) or quote.airline}"
            )
        # Score como linha SECUNDÁRIA (não no título).
        if decision.score is not None:
            extras.append(f"Score operacional: {decision.score}/100")
        # PR #76: cruzamento Duffel → Google Flights. O alerta passa a levar
        # a usuária à BUSCA PRÉ-PREENCHIDA no Google Flights (rota/datas/
        # cabine), em vez do Duffel Dashboard (painel de dev, sem compra).
        # Honestidade: NÃO é a oferta Duffel travada — atalho de busca.
        from .google_flights_link import duffel_google_flights_url
        _gf = duffel_google_flights_url(quote)
        if _gf:
            extras.append(f'🔎 <a href="{_gf}">Buscar esta oferta no Google Flights</a>')
            # PR #79: deixa explícito o trip_type+cabine que vai abrir, p/
            # a Olivia ler o que será aberto antes de clicar.
            extras.append(_duffel_search_label_line(quote))
            extras.append(
                "Busca pré-preenchida a partir da oferta confirmada pela "
                "Duffel. Preço e disponibilidade podem variar; confira antes "
                "de comprar."
            )
        else:
            extras.append("Ação: verificar no Duffel Dashboard.")
            extras.append("Oferta confirmada, mas sem caminho de compra direto.")
    elif quote.source == "manual_purchase":
        # Manual purchase fallback: preço veio do Travelpayouts mas não há
        # link comercial acionável (Kiwi indisponível, Aviasales bloqueado).
        # Em vez de hyperlink comercial, oferecemos links auxiliares de
        # pesquisa (Google Flights, Kayak, Expedia) — clicáveis mas
        # claramente marcados como NÃO sendo oferta confirmada.
        extras.append("🛒 Fonte: Travelpayouts (cache)")
        dates_label = quote.departure_date + (
            f" → {quote.return_date}" if show_return else ""
        )
        cabin_label = cabin_label_pt(quote.cabin, quote.cabin_confirmed)
        extras.append("⚠️ Link comercial automático indisponível.")
        extras.append("Links auxiliares de pesquisa, não oferta confirmada.")
        extras.append(
            f"Pesquise manualmente: {quote.route.origin} → {quote.route.destination}, "
            f"{dates_label}, {cabin_label}."
        )
        for label, url in build_auxiliary_search_links(quote):
            extras.append(f'🔎 <a href="{url}">{label}</a>')
    else:
        source_label = format_source(quote.source)
        if source_label:
            extras.append(f"🛒 Fonte: {source_label}")
        if quote.source == "travelpayouts+kiwi":
            extras.append(
                "ℹ️ Preço detectado no radar Travelpayouts. "
                "Link de conferência comercial via Kiwi."
            )
        if is_actionable_url(quote.deep_link):
            extras.append(f'🔎 <a href="{quote.deep_link}">Conferir busca</a>')
        else:
            extras.append(
                "⚠️ Link direto indisponível. Conferir manualmente na fonte pela rota "
                f"{quote.route.origin} → {quote.route.destination}."
            )

    # PR #68: link_status normalizado em TODO alerta — explícito sobre a
    # acionabilidade do link, sem prometer checkout onde não há.
    extras.append(f"🔗 link_status: {link_status_for(quote)}")

    return (
        f"✈️ <b>{flag}{level_prefix}{headline}</b>\n"
        + "\n".join(head_lines)
        + f"\n{price_line}\n"
        + f"📅 {dates}\n"
        + "\n".join(extras)
    )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send(self, text: str) -> bool:
        # `disable_web_page_preview=true` evita que o Telegram busque preview
        # do servidor do destino (Aviasales) e mostre conteúdo em russo no chat.
        # O link continua clicável; só o embed/preview é desativado.
        body = urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = Request(self._url, data=body, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not payload.get("ok"):
                    print(f"telegram retornou ok=false: {payload}", file=sys.stderr)
                    return False
                return True
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = "<sem corpo>"
            print(f"telegram HTTP {exc.code} {exc.reason}: {detail}", file=sys.stderr)
            return False
        except URLError as exc:
            print(f"telegram URLError: {exc.reason}", file=sys.stderr)
            return False
        except json.JSONDecodeError as exc:
            print(f"telegram resposta não-JSON: {exc}", file=sys.stderr)
            return False

    def send_alert(self, quote: Quote, decision: Decision, priority: bool = False) -> bool:
        return self.send(format_alert(quote, decision, priority=priority))
