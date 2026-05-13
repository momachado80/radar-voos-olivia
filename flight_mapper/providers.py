"""Provedores de cotação. Hoje: Travelpayouts (Aviasales), Kiwi Tequila e Mock."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .airports import build_search_url
from .regions import Route


@dataclass
class Quote:
    route: Route
    price_brl: float
    deep_link: str | None
    departure_date: str
    return_date: str | None
    source: str | None = None


class FlightProvider(Protocol):
    def quote(self, route: Route) -> Quote | None: ...


class KiwiTequilaProvider:
    """Consulta tarifas business via api.tequila.kiwi.com.

    A API Tequila exige header `apikey`. O provider é resiliente a indisponibilidade
    parcial: se a chamada falhar para uma rota, retorna None e o monitor pula.
    """

    BASE_URL = "https://api.tequila.kiwi.com/v2/search"

    def __init__(self, api_key: str, lookahead_days: int = 60, trip_length: int = 7):
        self.api_key = api_key
        self.lookahead_days = lookahead_days
        self.trip_length = trip_length

    def quote(self, route: Route) -> Quote | None:
        date_from = date.today() + timedelta(days=14)
        date_to = date.today() + timedelta(days=self.lookahead_days)
        params = {
            "fly_from": route.origin,
            "fly_to": route.destination,
            "date_from": date_from.strftime("%d/%m/%Y"),
            "date_to": date_to.strftime("%d/%m/%Y"),
            "nights_in_dst_from": self.trip_length,
            "nights_in_dst_to": self.trip_length,
            "selected_cabins": "C",
            "curr": "BRL",
            "limit": 1,
            "sort": "price",
        }
        url = f"{self.BASE_URL}?{urlencode(params)}"
        request = Request(url, headers={"apikey": self.api_key, "accept": "application/json"})
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

        items = payload.get("data") or []
        if not items:
            return None
        item = items[0]
        return Quote(
            route=route,
            price_brl=float(item["price"]),
            deep_link=item.get("deep_link"),
            departure_date=item.get("local_departure", "")[:10],
            return_date=(item.get("route", [{}])[-1].get("local_departure", "") or "")[:10] or None,
            source="kiwi",
        )


class TravelpayoutsProvider:
    """Consulta tarifas business via API Travelpayouts (rede de afiliados Aviasales).

    Token grátis após cadastro como afiliado em travelpayouts.com.
    Endpoint: aviasales/v3/prices_for_dates — retorna a oferta mais barata cacheada
    para a rota, com filtro de classe executiva (trip_class=1).
    """

    BASE_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

    def __init__(self, token: str):
        self.token = token

    def quote(self, route: Route) -> Quote | None:
        params = {
            "origin": route.origin,
            "destination": route.destination,
            "currency": "brl",
            "trip_class": 1,
            "sorting": "price",
            "direct": "false",
            "limit": 1,
            "token": self.token,
        }
        url = f"{self.BASE_URL}?{urlencode(params)}"
        request = Request(url, headers={"accept": "application/json"})
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

        if not payload.get("success"):
            return None
        items = payload.get("data") or []
        if not items:
            return None
        item = items[0]

        departure = (item.get("departure_at") or "")[:10]
        return_raw = item.get("return_at")
        return_date = return_raw[:10] if return_raw else None

        # Travelpayouts vira fonte de PREÇO. Não geramos mais link Aviasales
        # porque o endpoint redireciona para russo mesmo com locale=en-us.
        # Alerta só sai se outra fonte (Kiwi) fornecer deep_link acionável
        # (cross-fetch fica para PR seguinte; por hora, deep_link=None força
        # o monitor a contar non_actionable_links_skipped e não enviar Telegram).
        return Quote(
            route=route,
            price_brl=float(item["price"]),
            deep_link=None,
            departure_date=departure,
            return_date=return_date,
            source="travelpayouts",
        )


class MockProvider:
    """Provedor sintético determinístico, útil para testes e dry-run.

    Usa um deep_link estilo Kiwi (host kiwi.com) porque `is_actionable_url`
    rejeita Aviasales por completo. URL falsa, só serve para testes
    exercitarem o caminho "link acionável" sem precisar de chave Kiwi real.
    """

    def __init__(self, seed: int = 0, baseline: float = 8000.0, jitter: float = 0.15):
        self._rng = random.Random(seed)
        self.baseline = baseline
        self.jitter = jitter

    def quote(self, route: Route) -> Quote | None:
        price = self.baseline * (1 + self._rng.uniform(-self.jitter, self.jitter))
        departure = "2026-06-01"
        return_date = "2026-06-08"
        return Quote(
            route=route,
            price_brl=round(price, 2),
            deep_link=f"https://www.kiwi.com/deep/{route.origin}-{route.destination}-{departure}",
            departure_date=departure,
            return_date=return_date,
            source="mock",
        )
