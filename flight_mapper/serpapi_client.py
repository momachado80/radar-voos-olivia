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
    """SerpApi reflete o `type` em search_parameters ou no offer.

    SerpApi pode devolver `search_parameters.type` como string ou
    inteiro (1/2). Normalizamos para string antes de classificar.
    Quando a busca foi round_trip mas o offer-level type é "One way"
    (cada perna listada separadamente antes do booking_token), o
    chamador detecta divergência via `audit_trip_consistency`.
    """
    type_raw = ""
    sp = payload.get("search_parameters") or {}
    sp_type = sp.get("type")
    if sp_type is not None:
        type_raw = str(sp_type)
    elif offer.get("type") is not None:
        type_raw = str(offer.get("type"))
    t = type_raw.lower()
    if "round" in t or t == "1":
        return TripType.ROUND_TRIP, type_raw or "round_trip"
    if "one" in t or t == "2":
        return TripType.ONE_WAY, type_raw or "one_way"
    return TripType.ROUND_TRIP, type_raw or "(desconhecido)"


def audit_trip_consistency(
    requested: TripType, payload: dict
) -> str | None:
    """Compara o trip_type solicitado com o que o payload sugere.

    Retorna:
    - None se o payload é consistente com o pedido;
    - "payload_trip_inconclusive" se o payload diverge (ou não dá
      pra confirmar) — caso típico do SerpApi devolver offers com
      `type="One way"` mesmo em busca round_trip (cada perna listada
      separadamente até o booking_token).

    Função PURA — nunca rebaixa nada sozinha, só sinaliza para o log.
    """
    sp = payload.get("search_parameters") or {}
    sp_type = sp.get("type")
    sp_str = str(sp_type).strip().lower() if sp_type is not None else ""
    requested_is_round = requested is TripType.ROUND_TRIP
    sp_is_round = "round" in sp_str or sp_str == "1"
    sp_is_one = "one" in sp_str or sp_str == "2"

    if sp_type is not None:
        if requested_is_round and sp_is_one:
            return "payload_trip_inconclusive"
        if (not requested_is_round) and sp_is_round:
            return "payload_trip_inconclusive"

    offer_types: list[str] = []
    for key in ("best_flights", "other_flights"):
        for o in payload.get(key) or []:
            if isinstance(o, dict) and o.get("type") is not None:
                offer_types.append(str(o["type"]).strip().lower())
    if requested_is_round and offer_types:
        if all(("one" in t) or t == "2" for t in offer_types):
            return "payload_trip_inconclusive"
    return None


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


# Mapeamento canônico p/ o parâmetro `travel_class` do SerpApi
# (engine=google_flights). A API espera inteiros 1-4; enviar string
# como "business" faz o servidor responder 400 "Unsupported '0' for
# travel class.". Helper aceita string ou int e normaliza.
_SERPAPI_TRAVEL_CLASS: dict[str, int] = {
    "economy": 1,
    "premium_economy": 2,
    "premium economy": 2,
    "premiumeconomy": 2,
    "business": 3,
    "first": 4,
}


def _resolve_travel_class(value) -> int:
    """Aceita 'business'/'BUSINESS'/3 → int 1-4 esperado pelo SerpApi.

    Levanta `SerpApiError` em valores desconhecidos (string ou int).
    """
    if isinstance(value, int):
        if value not in (1, 2, 3, 4):
            raise SerpApiError(
                f"travel_class inteiro inválido: {value} (esperado 1-4)"
            )
        return value
    code = _SERPAPI_TRAVEL_CLASS.get(str(value).strip().lower())
    if code is None:
        raise SerpApiError(f"travel_class desconhecido: {value!r}")
    return code


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
            "travel_class": _resolve_travel_class(travel_class),
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

    def fetch_booking_options(
        self,
        *,
        booking_token: str,
        departure_id: str,
        arrival_id: str,
        outbound_date: str,
        return_date: str | None = None,
        travel_class: str | int = "business",
        currency: str = "USD",
    ) -> list["SerpApiBookingOption"]:
        """Busca booking options reais a partir do `booking_token`.

        Read-only: nunca abre o link, nunca toca PriceStore, nunca
        envia Telegram. SerpApi exige reenviar os parâmetros da busca
        original junto com o `booking_token` — caso contrário a API
        responde 400.
        """
        if not booking_token:
            raise SerpApiError("booking_token obrigatório")
        trip_type_param = "1" if return_date else "2"
        params = {
            "engine": "google_flights",
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "type": trip_type_param,
            "travel_class": _resolve_travel_class(travel_class),
            "currency": currency,
            "booking_token": booking_token,
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
        return parse_booking_options(payload)


@dataclass(frozen=True)
class SerpApiBookingOption:
    """Uma opção de booking (provider + URL clicável) derivada do
    booking_token. NUNCA vira alerta — só validação read-only."""
    provider: str | None        # ex.: "Latam Airlines", "Kissandfly"
    provider_raw: str           # rótulo cru (`book_with` ou `option_title`)
    price: float | None
    currency: str | None
    booking_url: str | None     # URL clicável (se houver)
    has_post_data: bool         # alguns booking_request exigem POST
    raw: dict | None = None


def _coerce_price(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_booking_options(payload: dict) -> list[SerpApiBookingOption]:
    """Função pura. Extrai opções de `booking_options[].together`."""
    if not isinstance(payload, dict):
        raise SerpApiError("payload inválido (não é dict)")
    sp = payload.get("search_parameters") or {}
    currency = (sp.get("currency") or "USD").upper()
    raw_options = payload.get("booking_options") or []
    if not isinstance(raw_options, list):
        return []
    out: list[SerpApiBookingOption] = []
    for entry in raw_options:
        if not isinstance(entry, dict):
            continue
        # SerpApi agrupa em `together` (round trip) ou `departing`/
        # `returning` separados; pegamos o primeiro grupo presente
        # para fins de leitura.
        group = (
            entry.get("together")
            or entry.get("departing")
            or entry.get("returning")
        )
        if not isinstance(group, dict):
            continue
        provider_raw = (
            group.get("book_with")
            or group.get("option_title")
            or ""
        )
        provider = provider_raw or None
        price = _coerce_price(group.get("price"))
        br = group.get("booking_request") or {}
        if not isinstance(br, dict):
            br = {}
        url = br.get("url") if isinstance(br.get("url"), str) else None
        has_post = bool(br.get("post_data"))
        out.append(SerpApiBookingOption(
            provider=provider,
            provider_raw=provider_raw or "(sem rótulo)",
            price=price,
            currency=currency,
            booking_url=url,
            has_post_data=has_post,
            raw=group,
        ))
    return out


def parse_booking_options_from_file(path: str) -> list[SerpApiBookingOption]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return parse_booking_options(payload)


def url_domain(url: str | None) -> str | None:
    """Extrai o domínio (host) de uma URL sem importar urlparse pesado."""
    if not url or not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        return host or None
    except Exception:
        return None
