"""Provider read-only Duffel: ofertas business CONFIRMADAS via Offer Requests.

Integração de produção do spike PR #63. Princípios invioláveis:

- **Read-only**: só consulta `POST /air/offer_requests` (Offer Requests).
  NUNCA chama `/air/orders`, NUNCA cria order, NUNCA cria payment.
- **Sem deep_link**: o fluxo de compra do Duffel é `order_flow` (API
  server-to-server). O provider devolve `deep_link=None` de propósito.
  O alerta resultante é "oferta confirmada", não link clicável.
- **Sem vazamento**: o `Quote` retornado NUNCA carrega offer_id, token,
  URL crua, request body ou dado de passageiro. Só campos sanitizados
  (preço, moeda, cabine, carrier, datas).
- **Cabine business confirmada**: só devolve `Quote` quando o parser
  classifica `candidate_for_integration` (cabine business + preço). Caso
  contrário devolve `None` (economy / sem cabine / sem oferta / erro).
- **Fail-safe**: qualquer erro de rede/HTTP/parse vira `None` (silêncio),
  nunca exceção propagada nem alerta.
"""

from __future__ import annotations

from datetime import date, timedelta

from .actionability_readiness import (
    DECISION_CANDIDATE,
    duffel_live_search,
    parse_duffel_for_actionability,
)
from .currency import (
    CURRENCY_BRL,
    CURRENCY_EUR,
    CURRENCY_USD,
    get_eur_brl_rate,
    get_usd_brl_rate,
    to_brl,
)
from .providers import Quote
from .regions import Cabin, Route, TripType


# Quanto à frente buscar a partida (mesma janela conservadora do Kiwi).
DUFFEL_LOOKAHEAD_DAYS = 60
DUFFEL_TRIP_LENGTH_DAYS = 7


def _rate_for(currency: str) -> float | None:
    cur = (currency or "").strip().upper()
    if cur == CURRENCY_USD:
        return get_usd_brl_rate()
    if cur == CURRENCY_EUR:
        return get_eur_brl_rate()
    return None


class DuffelProvider:
    """Consulta Duffel Offer Requests (business) e devolve `Quote`
    confirmado ou `None`. Conforma o protocolo `FlightProvider`.

    `urlopen_impl` é injetável para testes (sem rede)."""

    def __init__(
        self,
        access_token: str,
        *,
        lookahead_days: int = DUFFEL_LOOKAHEAD_DAYS,
        trip_length: int = DUFFEL_TRIP_LENGTH_DAYS,
        currency: str = CURRENCY_USD,
        urlopen_impl=None,
    ):
        self.access_token = access_token
        self.lookahead_days = lookahead_days
        self.trip_length = trip_length
        # Moeda pedida ao Duffel. Na prática o Duffel pode devolver a moeda
        # nativa da oferta (ex.: EUR) — convertemos o que vier para BRL.
        self.currency = currency
        self._urlopen_impl = urlopen_impl

    def quote(self, route: Route) -> Quote | None:
        # Token ausente ⇒ silêncio (fail-safe). Defesa redundante: o caller
        # (Monitor/CLI) só instancia o provider com token presente.
        if not self.access_token:
            return None

        outbound = date.today() + timedelta(days=self.lookahead_days)
        trip = "round_trip" if route.trip_type != TripType.ONE_WAY else "one_way"
        return_dt = None
        if trip == "round_trip":
            return_dt = outbound + timedelta(days=self.trip_length)

        payload = duffel_live_search(
            access_token=self.access_token,
            origin=route.origin,
            destination=route.destination,
            trip_type=trip,
            outbound_date=outbound,
            return_date=return_dt,
            cabin_class="business",
            currency=self.currency,
            urlopen_impl=self._urlopen_impl,
        )
        report = parse_duffel_for_actionability(
            payload, route=f"{route.origin}-{route.destination}",
            requested_cabin="business",
        )
        # Só promovemos a Quote ofertas confirmadas (business + preço). Tudo
        # mais (validator_only / not_suitable / blocker de rede) ⇒ None.
        if report.decision != DECISION_CANDIDATE:
            return None
        if not (report.cabin_confirmed and report.price_amount is not None):
            return None

        currency = (report.price_currency or self.currency or "").strip().upper()
        rate = _rate_for(currency)
        brl_estimated = to_brl(report.price_amount, currency, rate)

        airline = report.airlines[0] if report.airlines else None
        # trip_type honesto a partir do report (deriva das slices).
        trip_type = (
            TripType.ROUND_TRIP
            if report.trip_type == "round_trip"
            else TripType.ONE_WAY
        )

        return Quote(
            route=route,
            price_brl=(
                brl_estimated if brl_estimated is not None
                else float(report.price_amount)
            ),
            deep_link=None,  # order_flow: sem link clicável, de propósito
            departure_date=report.outbound_date or outbound.strftime("%Y-%m-%d"),
            return_date=report.return_date,
            source="duffel",
            amount=float(report.price_amount),
            currency=currency or CURRENCY_BRL,
            amount_brl_estimated=brl_estimated,
            fx_rate=rate,
            cabin=Cabin.BUSINESS,
            cabin_confirmed=True,
            trip_type=trip_type,
            airline=airline,
        )
