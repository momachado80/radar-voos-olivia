"""Cliente SerpApi (Google Flights) read-only — validação/benchmark.

**Nunca** vira provider de pipeline nem fonte de emissão de alerta;
serve para conferir preço e booking_token (link clicável) externamente.
Smoke offline via `parse_search` / `parse_search_from_file`. Chamada
real só via CLI explícito `serpapi-smoke` sem `--mock-file`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .regions import Cabin, TripType


BASE_URL = "https://serpapi.com/search.json"


class SerpApiError(RuntimeError):
    pass


class SerpApiAuthError(SerpApiError):
    pass


@dataclass(frozen=True)
class SerpApiOffer:
    price: float | None
    currency: str | None
    cabin: Cabin
    cabin_raw: str
    trip_type: TripType
    type_raw: str
    booking_token: str | None
    departure_token: str | None
    departure_date: str
    return_date: str | None
    carriers: list[str] = field(default_factory=list)
    raw: dict | None = None


def _normalize_cabin(raw: str | None) -> Cabin:
    s = (raw or "").strip().lower()
    if "business" in s:
        return Cabin.BUSINESS
    if "first" in s:
        return Cabin.UNKNOWN  # não suportado pelo nosso modelo
    if "premium" in s or "economy" in s:
        return Cabin.ECONOMY
    return Cabin.UNKNOWN


def _infer_trip_type(payload: dict, offer: dict) -> tuple[TripType, str]:
    """SerpApi reflete o `type` em search_parameters ou no offer."""
    type_raw = ""
    sp = payload.get("search_parameters") or {}
    if isinstance(sp.get("type"), str):
        type_raw = sp["type"]
    elif isinstance(offer.get("type"), str):
        type_raw = offer["type"]
    t = type_raw.lower()
    if "round" in t or t == "1":
        return TripType.ROUND_TRIP, type_raw or "round_trip"
    if "one" in t or t == "2":
        return TripType.ONE_WAY, type_raw or "one_way"
    return TripType.ROUND_TRIP, type_raw or "(desconhecido)"


def _first(payload_lists: list[dict] | None) -> dict | None:
    if not payload_lists:
        return None
    return payload_lists[0]


def parse_search(payload: dict) -> list[SerpApiOffer]:
    """Função pura. Extrai ofertas de `best_flights` + `other_flights`."""
    if not isinstance(payload, dict):
        raise SerpApiError("payload inválido (não é dict)")
    sp = payload.get("search_parameters") or {}
    currency = (sp.get("currency") or "USD").upper()
    travel_class_param = sp.get("travel_class") or sp.get("travelClass")
    departure_date = str(sp.get("outbound_date") or "")
    return_date = sp.get("return_date") or None

    out: list[SerpApiOffer] = []
    groups = []
    if isinstance(payload.get("best_flights"), list):
        groups.append(("best", payload["best_flights"]))
    if isinstance(payload.get("other_flights"), list):
        groups.append(("other", payload["other_flights"]))
    for _, items in groups:
        for offer in items:
            if not isinstance(offer, dict):
                continue
            try:
                price = float(offer.get("price")) if offer.get("price") is not None else None
            except (TypeError, ValueError):
                price = None
            # Cabine: SerpApi pode trazer `travel_class` por segmento ou
            # herdar de search_parameters.
            cabin_raw = ""
            flights = offer.get("flights") or []
            if isinstance(flights, list) and flights:
                first = flights[0] if isinstance(flights[0], dict) else {}
                cabin_raw = (
                    first.get("travel_class") or first.get("travelClass") or ""
                )
            if not cabin_raw and travel_class_param:
                cabin_raw = str(travel_class_param)
            cabin = _normalize_cabin(cabin_raw)
            trip_type, type_raw = _infer_trip_type(payload, offer)
            carriers: list[str] = []
            for seg in flights or []:
                if isinstance(seg, dict):
                    air = seg.get("airline")
                    if isinstance(air, str):
                        carriers.append(air)
            out.append(SerpApiOffer(
                price=price,
                currency=currency,
                cabin=cabin,
                cabin_raw=cabin_raw or "(sem campo)",
                trip_type=trip_type,
                type_raw=type_raw,
                booking_token=offer.get("booking_token"),
                departure_token=offer.get("departure_token"),
                departure_date=departure_date,
                return_date=return_date,
                carriers=carriers,
                raw=offer,
            ))
    return out


def parse_search_from_file(path: str) -> list[SerpApiOffer]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return parse_search(payload)


class SerpApiClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: int = 20):
        if not api_key:
            raise SerpApiAuthError("api_key obrigatório")
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    def search_google_flights(
        self,
        *,
        origin: str,
        destination: str,
        outbound_date: str,
        return_date: str | None = None,
        travel_class: str = "business",
        currency: str = "USD",
    ) -> list[SerpApiOffer]:
        # SerpApi Google Flights: type 1=round trip, 2=one way.
        trip_type_param = "1" if return_date else "2"
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": outbound_date,
            "type": trip_type_param,
            "travel_class": travel_class,
            "currency": currency,
            "api_key": self.api_key,
        }
        if return_date:
            params["return_date"] = return_date
        url = f"{self.base_url}?{urlencode(params)}"
        req = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:  # pragma: no cover
                detail = "<sem corpo>"
            if exc.code in (401, 403):
                raise SerpApiAuthError(
                    f"auth falhou ({exc.code}): {detail}"
                ) from exc
            raise SerpApiError(
                f"HTTP {exc.code} {exc.reason}: {detail}"
            ) from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SerpApiError(f"resposta não-JSON: {exc}") from exc
        return parse_search(payload)
