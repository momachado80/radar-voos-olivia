"""Envio de mensagens via Telegram Bot API."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .providers import Quote


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
                return bool(payload.get("ok"))
        except (HTTPError, URLError, json.JSONDecodeError):
            return False

    def send_alert(self, quote: Quote, average: float, drop_pct: float) -> bool:
        link_line = f'\n<a href="{quote.deep_link}">Abrir oferta</a>' if quote.deep_link else ""
        text = (
            f"✈️ <b>Business em promoção</b>\n"
            f"{quote.route.origin} → {quote.route.destination} ({quote.route.region})\n"
            f"💰 R$ {quote.price_brl:,.0f} (média R$ {average:,.0f}, queda {drop_pct:.0%})\n"
            f"📅 {quote.departure_date}"
            + (f" → {quote.return_date}" if quote.return_date else "")
            + link_line
        )
        return self.send(text)
