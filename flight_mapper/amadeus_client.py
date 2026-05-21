"""Cliente Amadeus read-only para smoke. **Não** é provider de pipeline
neste PR — só roda via CLI explícito (`amadeus-smoke`) ou parsing puro
(`parse_flight_offers`) sob teste com fixture.

Test env: https://test.api.amadeus.com (cadastro gratuito em
developers.amadeus.com). OAuth client-credentials + Flight Offers
Search v2. **Não envia Telegram. Não toca PriceStore.**

Cabine vem do payload (campo real `travelerPricings[].fareDetails
BySegment[].cabin` ∈ {ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST}),
permitindo `cabin_confirmed=True` em produção futura.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .regions import Cabin, TripType


BASE_URL_TEST = "https://test.api.amadeus.com"


class AmadeusAuthError(RuntimeError):
    pass


class AmadeusError(RuntimeError):
    pass


@dataclass(frozen=True)
class AmadeusOffer:
    price_total: float
    currency: str
    cabin: Cabin
    cabin_raw: str            # texto bruto do payload (BUSINESS/ECONOMY/MIXED)
    cabin_confirmed: bool     # True se todos os segmentos têm a mesma cabin
    trip_type: TripType
    departure_date: str
    return_date: str | None
    carriers: list[str] = field(default_factory=list)
    raw: dict | None = None


def _segment_cabins(offer: dict) -> list[str]:
    out: list[str] = []
    for tp in offer.get("travelerPricings") or []:
        for det in tp.get("fareDetailsBySegment") or []:
            cab = det.get("cabin")
            if isinstance(cab, str):
                out.append(cab.upper())
    return out


def _normalize_cabin(raw: str) -> Cabin:
    raw = (raw or "").upper()
    if raw == "BUSINESS":
        return Cabin.BUSINESS
    if raw in ("ECONOMY", "PREMIUM_ECONOMY"):
        return Cabin.ECONOMY
    return Cabin.UNKNOWN


def parse_flight_offers(payload: dict) -> list[AmadeusOffer]:
    """Função pura. Aceita o JSON cru da Amadeus e devolve offers."""
    if not isinstance(payload, dict):
        raise AmadeusError("payload inválido (não é dict)")
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    offers: list[AmadeusOffer] = []
    for raw_offer in data:
        if not isinstance(raw_offer, dict):
            continue
        price = raw_offer.get("price") or {}
        try:
            total = float(price.get("total"))
        except (TypeError, ValueError):
            continue
        currency = str(price.get("currency") or "").upper() or "USD"

        itineraries = raw_offer.get("itineraries") or []
        one_way_flag = bool(raw_offer.get("oneWay"))
        if one_way_flag or len(itineraries) == 1:
            trip_type = TripType.ONE_WAY
        else:
            trip_type = TripType.ROUND_TRIP

        # Datas
        def _first_segment_at(it_idx: int) -> str | None:
            try:
                segs = itineraries[it_idx]["segments"]
                at = segs[0]["departure"]["at"]
                return str(at)[:10]
            except (KeyError, IndexError, TypeError):
                return None

        departure_date = _first_segment_at(0) or ""
        return_date = (
            _first_segment_at(1) if trip_type == TripType.ROUND_TRIP else None
        )

        cabins = _segment_cabins(raw_offer)
        if not cabins:
            cabin_raw = "UNKNOWN"
            cabin = Cabin.UNKNOWN
            cabin_confirmed = False
        else:
            unique = set(cabins)
            if len(unique) == 1:
                cabin_raw = next(iter(unique))
                cabin = _normalize_cabin(cabin_raw)
                # Confirmado quando reconhecemos a cabine (business/economy);
                # cabines não mapeadas (FIRST, etc.) viram UNKNOWN/False.
                cabin_confirmed = cabin in (Cabin.BUSINESS, Cabin.ECONOMY)
            else:
                cabin_raw = "MIXED:" + "/".join(sorted(unique))
                cabin = Cabin.UNKNOWN
                cabin_confirmed = False

        carriers = list(raw_offer.get("validatingAirlineCodes") or [])

        offers.append(AmadeusOffer(
            price_total=total,
            currency=currency,
            cabin=cabin,
            cabin_raw=cabin_raw,
            cabin_confirmed=cabin_confirmed,
            trip_type=trip_type,
            departure_date=departure_date,
            return_date=return_date,
            carriers=carriers,
            raw=raw_offer,
        ))
    return offers


def parse_offers_from_file(path: str) -> list[AmadeusOffer]:
    """Carrega fixture/JSON e parseia. Útil em CLI com --mock-file e em
    testes — não toca rede."""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return parse_flight_offers(payload)


class AmadeusClient:
    """Cliente mínimo: OAuth client-credentials + Flight Offers Search.

    Chamadas reais NÃO são feitas por testes (urlopen monkeypatchado) e
    só rodam via CLI explícito `amadeus-smoke` sem `--mock-file`.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = BASE_URL_TEST,
        timeout: int = 20,
    ):
        if not client_id or not client_secret:
            raise AmadeusAuthError("client_id e client_secret obrigatórios")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: str | None = None

    def _request(self, req: Request) -> dict[str, Any]:
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:  # pragma: no cover
                detail = "<sem corpo>"
            if exc.code in (401, 403):
                raise AmadeusAuthError(
                    f"auth falhou ({exc.code}): {detail}"
                ) from exc
            raise AmadeusError(
                f"HTTP {exc.code} {exc.reason}: {detail}"
            ) from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AmadeusError(f"resposta não-JSON: {exc}") from exc

    def fetch_token(self) -> str:
        """POST /v1/security/oauth2/token (client_credentials)."""
        url = f"{self.base_url}/v1/security/oauth2/token"
        body = urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode("utf-8")
        req = Request(url, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })
        payload = self._request(req)
        token = payload.get("access_token")
        if not token:
            raise AmadeusAuthError(
                f"resposta sem access_token: {payload}"
            )
        self._token = str(token)
        return self._token

    def search_offers(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str | None = None,
        adults: int = 1,
        travel_class: str = "BUSINESS",
        max_results: int = 3,
    ) -> list[AmadeusOffer]:
        """GET /v2/shopping/flight-offers com Bearer token."""
        if not self._token:
            self.fetch_token()
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date,
            "adults": str(adults),
            "travelClass": travel_class.upper(),
            "max": str(max_results),
            "currencyCode": "USD",
        }
        if return_date:
            params["returnDate"] = return_date
        url = f"{self.base_url}/v2/shopping/flight-offers?{urlencode(params)}"
        req = Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })
        payload = self._request(req)
        return parse_flight_offers(payload)
