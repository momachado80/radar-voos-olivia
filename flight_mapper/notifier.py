"""Envio de mensagens via Telegram Bot API."""

from __future__ import annotations

import json
import sys
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


def _duffel_headline(decision: Decision, trip_suffix: str) -> str:
    """Título de oferta Duffel business CONFIRMADA. Enfatiza a oportunidade
    executiva confirmada — não o score (que vira linha secundária). O
    score NUNCA entra aqui (regra do PR #66).

    - abaixo do alvo (ceiling excellent/good) → "🟢 EXECUTIVA CONFIRMADA
      — abaixo do alvo";
    - queda histórica (legacy) → "🟢 EXECUTIVA CONFIRMADA — queda detectada";
    - sem nível/critério forte → "🟢 EXECUTIVA CONFIRMADA".
    """
    below_target = (
        decision.criterion == CRITERION_CEILING
        and decision.level in (LEVEL_EXCELLENT, LEVEL_GOOD)
    )
    if below_target:
        return f"🟢 EXECUTIVA CONFIRMADA — abaixo do alvo{trip_suffix}"
    if decision.criterion == CRITERION_CEILING and decision.threshold is not None:
        return f"🟢 EXECUTIVA CONFIRMADA — abaixo do alvo{trip_suffix}"
    if decision.drop_pct is not None:
        return f"🟢 EXECUTIVA CONFIRMADA — queda detectada{trip_suffix}"
    return f"🟢 EXECUTIVA CONFIRMADA{trip_suffix}"


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
    if is_duffel and quote.cabin_confirmed and quote.cabin == Cabin.BUSINESS:
        # PR #66: oferta Duffel confirmada lidera com a oportunidade
        # executiva confirmada, não com o score (que vira linha secundária).
        level_prefix = ""
        headline = _duffel_headline(decision, trip_suffix)
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
        # Duffel: oferta business CONFIRMADA via order_flow. NÃO há link
        # clicável (booking é API server-to-server). NUNCA expomos
        # offer_id / token / payload — só fonte, carrier e a ação manual
        # de verificação no painel do Duffel.
        extras.append("🟢 Oferta confirmada por Duffel; sem compra automática.")
        extras.append("🛒 Fonte: Duffel (Offer Request, cabine business confirmada)")
        if quote.airline:
            extras.append(f"🛫 Companhia: {quote.airline}")
        # PR #66: score como linha SECUNDÁRIA (não no título) — não deve
        # ser a mensagem emocional principal de uma executiva confirmada.
        if decision.score is not None:
            extras.append(f"Score operacional: {decision.score}/100")
        extras.append("booking_flow: order_flow (sem link direto de compra)")
        extras.append("Ação: verificar no Duffel Dashboard.")
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
