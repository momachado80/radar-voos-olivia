"""Envio de mensagens via Telegram Bot API."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .airports import is_actionable_url, route_airport_label, route_city_label
from .detector import CRITERION_CEILING, LEVEL_EXCELLENT, LEVEL_GOOD, Decision
from .formatting import format_brl, format_detection_time, format_source
from .providers import Quote


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
    level_prefix = _level_title(decision.level, decision.score)
    city_line = route_city_label(quote.route.origin, quote.route.destination)
    iata_line = route_airport_label(quote.route.origin, quote.route.destination)

    is_ceiling = decision.criterion == CRITERION_CEILING and decision.threshold is not None
    if is_ceiling:
        price_line = (
            f"💰 {format_brl(quote.price_brl)} "
            f"(alvo {format_brl(decision.threshold)})"
        )
    else:
        if decision.average is not None and decision.drop_pct is not None:
            price_line = (
                f"💰 {format_brl(quote.price_brl)} "
                f"(média {format_brl(decision.average)}, queda {decision.drop_pct:.0%})"
            )
        else:
            price_line = f"💰 {format_brl(quote.price_brl)}"

    criterion_line = _level_criterion_line(decision)
    dates = quote.departure_date + (f" → {quote.return_date}" if quote.return_date else "")

    head_lines: list[str] = [f"{city_line} ({quote.route.region})"]
    if iata_line != city_line:
        head_lines.append(iata_line)

    detection_line = f"🕒 Encontrado em: {format_detection_time(now)}"

    extras: list[str] = [detection_line, criterion_line]
    if is_ceiling:
        extras.append("⚠️ Preço pode mudar rápido. Conferir agora.")
    source_label = format_source(quote.source)
    if source_label:
        extras.append(f"🛒 Fonte: {source_label}")
    if is_actionable_url(quote.deep_link):
        extras.append(f'🔎 <a href="{quote.deep_link}">Conferir busca</a>')
    else:
        extras.append(
            "⚠️ Link direto indisponível. Conferir manualmente na fonte pela rota "
            f"{quote.route.origin} → {quote.route.destination}."
        )

    return (
        f"✈️ <b>{flag}{level_prefix}Business em promoção</b>\n"
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
