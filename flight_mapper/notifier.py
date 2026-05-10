"""Envio de mensagens via Telegram Bot API."""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .airports import route_airport_label, route_city_label
from .providers import Quote


SOURCE_LABELS = {
    "travelpayouts": "Travelpayouts (cache)",
    "kiwi": "Kiwi",
    "mock": "Mock (sintético)",
}


def format_alert(quote: Quote, average: float, drop_pct: float, priority: bool = False) -> str:
    """Monta o texto HTML do alerta. Função pura, sem efeitos colaterais."""
    flag = "🔥 " if priority else ""
    city_line = route_city_label(quote.route.origin, quote.route.destination)
    iata_line = route_airport_label(quote.route.origin, quote.route.destination)

    dates = quote.departure_date + (f" → {quote.return_date}" if quote.return_date else "")

    extras: list[str] = []
    if quote.source:
        label = SOURCE_LABELS.get(quote.source, quote.source)
        extras.append(f"🛒 Fonte: {label}")
    if quote.deep_link:
        extras.append(f'🔎 <a href="{quote.deep_link}">Conferir busca</a>')
    extras_block = ("\n" + "\n".join(extras)) if extras else ""

    return (
        f"✈️ <b>{flag}Business em promoção</b>\n"
        f"{city_line} ({quote.route.region})\n"
        f"{iata_line}\n"
        f"💰 R$ {quote.price_brl:,.0f} (média R$ {average:,.0f}, queda {drop_pct:.0%})\n"
        f"📅 {dates}"
        f"{extras_block}"
    )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send(self, text: str) -> bool:
        body = urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
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

    def send_alert(self, quote: Quote, average: float, drop_pct: float, priority: bool = False) -> bool:
        return self.send(format_alert(quote, average, drop_pct, priority=priority))
