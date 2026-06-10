"""Spike read-only: avalia provedores quanto a "actionability" — capacidade
de devolver, num único fluxo, cabine confirmada + preço + URL/link
clicável para compra. Diagnóstico, sem integração de produção.

Princípio: read-only, sem rede em testes (consome fixtures), sem
exposição de token/URL bruta nos logs (apenas domínio + presença).
Não mexe em monitor/providers/notifier/detector/state — só serve para
informar decisão de qual provider promover a fonte de alerta executivo.

Estados de decisão (regras do PR #61):
- candidate_for_integration : cabine confirmada + link acionável + preço
- validator_only            : cabine confirmada SEM link acionável
- insufficient              : link acionável SEM cabine confirmada
- not_suitable              : nem cabine nem link
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .booking_actionability import (
    BookingActionability,
    classify_actionability,
)
from .serpapi_client import (
    SerpApiBookingOption,
    SerpApiOffer,
    parse_booking_options_from_file,
    parse_search_from_file,
    url_domain,
)


# Decisão final por provider (snake_case, estável para parsing externo).
DECISION_CANDIDATE = "candidate_for_integration"
DECISION_VALIDATOR_ONLY = "validator_only"
DECISION_INSUFFICIENT = "insufficient"
DECISION_NOT_SUITABLE = "not_suitable"


@dataclass(frozen=True)
class ActionabilityReport:
    """Snapshot SANITIZADO de capacidade de UM provider para UMA rota.

    NUNCA contém token bruto, URL completa, post_data, payload ou
    query string sensível — só os campos enumerados abaixo.
    """

    provider: str
    route: str                       # "GRU-MIA"
    outbound_date: str | None
    return_date: str | None
    trip_type: str                   # "one_way" | "round_trip" | "unknown"
    cabin_confirmed: bool
    price_amount: float | None
    price_currency: str | None
    airlines: tuple[str, ...]
    actionable_url: bool             # True se há URL clicável simples
    booking_flow: str                # "deep_link" | "google_post" | "amadeus_pricing_required" | "none" | "unknown"
    booking_domain: str | None       # apenas o host do link, nunca URL completa
    blockers: tuple[str, ...]        # códigos snake_case
    decision: str                    # ver DECISION_*
    # PR #84: voos extraídos do OUTBOUND slice no formato canônico "IATA+nº"
    # (ex.: ("AF447",) p/ direto, ("AF447","KL1234") p/ conexão). Vazia se
    # o payload não traz `marketing_carrier_flight_number` em algum segmento
    # — informação pública (consta em boarding pass, app da cia, e-mail) que
    # narra a busca no Google Flights direto pro voo exato.
    flight_numbers: tuple[str, ...] = ()


def apply_decision(
    *,
    cabin_confirmed: bool,
    actionable_url: bool,
    has_price: bool,
) -> str:
    """Regra pura do goal:
    - cabine + link + preço → candidate_for_integration
    - cabine SEM link       → validator_only
    - link SEM cabine       → insufficient
    - nada                  → not_suitable
    """
    if cabin_confirmed and actionable_url and has_price:
        return DECISION_CANDIDATE
    if cabin_confirmed and not actionable_url:
        return DECISION_VALIDATOR_ONLY
    if actionable_url and not cabin_confirmed:
        return DECISION_INSUFFICIENT
    return DECISION_NOT_SUITABLE


# ----------------- Amadeus -----------------


def parse_amadeus_for_actionability(
    offers: Sequence,  # list[AmadeusOffer]
    route: str,
) -> ActionabilityReport:
    """Amadeus Self-Service não devolve deep_link de booking. Cabine
    é confirmada por segmento. Vira `validator_only` quando cabine
    business presente; `not_suitable` se nem isso."""
    if not offers:
        return ActionabilityReport(
            provider="amadeus", route=route,
            outbound_date=None, return_date=None, trip_type="unknown",
            cabin_confirmed=False, price_amount=None,
            price_currency=None, airlines=(),
            actionable_url=False, booking_flow="none",
            booking_domain=None,
            blockers=("empty_payload",),
            decision=DECISION_NOT_SUITABLE,
        )
    first = offers[0]
    cabin_confirmed = bool(getattr(first, "cabin_confirmed", False))
    price = getattr(first, "price_total", None)
    currency = getattr(first, "currency", None)
    carriers = tuple(getattr(first, "carriers", ()) or ())
    blockers: list[str] = ["no_booking_link_in_payload"]
    if not cabin_confirmed:
        blockers.append("cabin_mixed_or_unknown")
    blockers.append("requires_amadeus_pricing_orders_api_for_booking")
    decision = apply_decision(
        cabin_confirmed=cabin_confirmed,
        actionable_url=False,
        has_price=price is not None,
    )
    _ret = getattr(first, "return_date", None)
    return ActionabilityReport(
        provider="amadeus", route=route,
        outbound_date=getattr(first, "departure_date", None),
        return_date=_ret,
        trip_type="round_trip" if _ret else "one_way",
        cabin_confirmed=cabin_confirmed,
        price_amount=float(price) if price is not None else None,
        price_currency=currency,
        airlines=carriers,
        actionable_url=False,
        booking_flow="amadeus_pricing_required",
        booking_domain=None,
        blockers=tuple(blockers),
        decision=decision,
    )


# ----------------- SerpApi -----------------


def parse_serpapi_for_actionability(
    offers: Sequence[SerpApiOffer],
    route: str,
    requested_cabin: str = "business",
    booking_options: Sequence[SerpApiBookingOption] | None = None,
) -> ActionabilityReport:
    """SerpApi devolve cabine + preço + booking_token. Link final
    depende de `classify_actionability` sobre o booking_options:
    google_post_only NUNCA é considerado link acionável."""
    if not offers:
        return ActionabilityReport(
            provider="serpapi", route=route,
            outbound_date=None, return_date=None, trip_type="unknown",
            cabin_confirmed=False, price_amount=None,
            price_currency=None, airlines=(),
            actionable_url=False, booking_flow="none",
            booking_domain=None,
            blockers=("empty_payload",),
            decision=DECISION_NOT_SUITABLE,
        )
    # Pega 1ª oferta com a cabine pedida quando possível.
    target_cabin = (requested_cabin or "").strip().lower()
    selected = next(
        (o for o in offers if o.cabin.value == target_cabin),
        offers[0],
    )
    cabin_confirmed = selected.cabin.value == target_cabin

    blockers: list[str] = []
    if not cabin_confirmed:
        blockers.append("cabin_mismatch_first_offer")

    actionable_url = False
    booking_flow = "unknown"
    booking_domain: str | None = None

    if booking_options is not None:
        kind = classify_actionability(list(booking_options))
        if kind in (
            BookingActionability.AIRLINE_SIMPLE_LINK,
            BookingActionability.OTA_SIMPLE_LINK,
            BookingActionability.MIXED_SIMPLE_AND_POST,
        ):
            actionable_url = True
            booking_flow = "deep_link"
            # Pega o domínio do 1º booking_option com URL não-POST.
            for opt in booking_options:
                if opt.booking_url and not opt.has_post_data:
                    booking_domain = url_domain(opt.booking_url)
                    break
        elif kind == BookingActionability.GOOGLE_POST_ONLY:
            blockers.append("booking_google_post_only")
            booking_flow = "google_post"
            # Domínio é google.com — registramos apenas para auditoria.
            for opt in booking_options:
                if opt.booking_url:
                    booking_domain = url_domain(opt.booking_url)
                    break
        elif kind == BookingActionability.NO_CLICKABLE_URL:
            blockers.append("booking_no_clickable_url")
            booking_flow = "none"
        elif kind == BookingActionability.EMPTY_BOOKING_OPTIONS:
            blockers.append("booking_empty_options")
            booking_flow = "none"
        else:
            blockers.append("booking_actionability_unknown")
    else:
        blockers.append("booking_options_not_provided")

    if selected.booking_token:
        # Anota presença para o auditor saber que o token EXISTE; não
        # vazamos o valor.
        blockers.append("booking_token_present_but_not_expanded")
        # Remove inconsistência — se já temos booking_options classificado,
        # esse flag é redundante. Deixamos só quando booking_options é None.
        if booking_options is not None:
            blockers.remove("booking_token_present_but_not_expanded")

    decision = apply_decision(
        cabin_confirmed=cabin_confirmed,
        actionable_url=actionable_url,
        has_price=selected.price is not None,
    )
    return ActionabilityReport(
        provider="serpapi", route=route,
        outbound_date=selected.departure_date or None,
        return_date=selected.return_date,
        trip_type="round_trip" if selected.return_date else "one_way",
        cabin_confirmed=cabin_confirmed,
        price_amount=selected.price,
        price_currency=selected.currency,
        airlines=tuple(selected.carriers),
        actionable_url=actionable_url,
        booking_flow=booking_flow,
        booking_domain=booking_domain,
        blockers=tuple(blockers),
        decision=decision,
    )


# ----------------- Kiwi (Tequila) -----------------


def parse_kiwi_for_actionability(
    payload: dict,
    route: str,
    requested_cabin: str = "business",
) -> ActionabilityReport:
    """Kiwi Tequila com `selected_cabins=C` devolve cabine business
    confirmada server-side + `deep_link` clicável. Se o payload trouxer
    pelo menos um item válido, é o ÚNICO candidato real a integração
    no momento (no presente repo).

    Payload esperado: {"data": [{...}, ...]} no formato Tequila Search.
    """
    items = (payload or {}).get("data") or []
    # PR #62: `kiwi_live_search` injeta `_blocker` em erros de rede/HTTP.
    live_blocker = (payload or {}).get("_blocker") if isinstance(payload, dict) else None
    if not items:
        empty_blockers = ("empty_payload",)
        if isinstance(live_blocker, str) and live_blocker:
            empty_blockers = ("empty_payload", f"live_{live_blocker}")
        return ActionabilityReport(
            provider="kiwi", route=route,
            outbound_date=None, return_date=None, trip_type="unknown",
            cabin_confirmed=False, price_amount=None,
            price_currency=None, airlines=(),
            actionable_url=False, booking_flow="none",
            booking_domain=None,
            blockers=empty_blockers,
            decision=DECISION_NOT_SUITABLE,
        )
    item = items[0]
    deep_link = item.get("deep_link") if isinstance(item, dict) else None
    price = item.get("price") if isinstance(item, dict) else None
    currency = (payload.get("currency") or "EUR") if isinstance(payload, dict) else None
    airlines = tuple(item.get("airlines") or []) if isinstance(item, dict) else ()
    # Kiwi com selected_cabins=C → cabine confirmada server-side.
    cabin_confirmed = (requested_cabin or "").strip().lower() == "business"
    actionable_url = bool(deep_link)
    blockers: list[str] = []
    if not actionable_url:
        blockers.append("deep_link_absent")
    if not cabin_confirmed:
        blockers.append("requested_cabin_not_business")
    booking_domain = url_domain(deep_link) if deep_link else None
    decision = apply_decision(
        cabin_confirmed=cabin_confirmed,
        actionable_url=actionable_url,
        has_price=price is not None,
    )
    # PR #62: Tequila one-way response não traz return date; round-trip
    # traz `route` com pernas de ida + volta. Inferimos trip_type a
    # partir do número de segmentos quando possível.
    out_date = item.get("local_departure", "")[:10] if isinstance(item, dict) else None
    ret_date = None
    if isinstance(item, dict):
        legs = item.get("route") or []
        if isinstance(legs, list) and len(legs) >= 2:
            last = legs[-1]
            if isinstance(last, dict):
                ret_date = (last.get("local_departure", "") or "")[:10] or None
    trip_type = "round_trip" if ret_date else "one_way"
    return ActionabilityReport(
        provider="kiwi", route=route,
        outbound_date=out_date,
        return_date=ret_date,
        trip_type=trip_type,
        cabin_confirmed=cabin_confirmed,
        price_amount=float(price) if price is not None else None,
        price_currency=currency,
        airlines=airlines,
        actionable_url=actionable_url,
        booking_flow="deep_link" if actionable_url else "none",
        booking_domain=booking_domain,
        blockers=tuple(blockers),
        decision=decision,
    )


# ----------------- Kiwi live (PR #62) -----------------


KIWI_TEQUILA_URL = "https://api.tequila.kiwi.com/v2/search"


def kiwi_live_search(
    *,
    api_key: str,
    origin: str,
    destination: str,
    trip_type: str,
    outbound_date,
    return_date=None,
    currency: str = "BRL",
    timeout: int = 20,
    urlopen_impl=None,
) -> dict:
    """Lança UMA query real ao Kiwi Tequila p/ rota+cabine business.
    Read-only. NUNCA loga URL completa nem header `apikey`. Em qualquer
    erro de rede/HTTP, devolve `{'data': [], '_blocker': <code>}` —
    nunca propaga exceção. Caller usa o blocker como informação extra
    no `ActionabilityReport`.

    `urlopen_impl` é injetável para testes (default = urllib.request.urlopen).
    """
    from datetime import date as _date
    from urllib.parse import urlencode
    from urllib.request import Request
    from urllib.error import HTTPError, URLError

    if urlopen_impl is None:
        from urllib.request import urlopen as urlopen_impl  # type: ignore

    fmt = lambda d: d.strftime("%d/%m/%Y")
    if isinstance(outbound_date, str):
        from datetime import datetime as _dt
        outbound_date = _dt.strptime(outbound_date, "%Y-%m-%d").date()
    if isinstance(return_date, str) and return_date:
        from datetime import datetime as _dt
        return_date = _dt.strptime(return_date, "%Y-%m-%d").date()
    elif return_date == "":
        return_date = None

    params = {
        "fly_from": origin,
        "fly_to": destination,
        "date_from": fmt(outbound_date),
        "date_to": fmt(outbound_date),
        "selected_cabins": "C",
        "curr": currency,
        "limit": 1,
        "sort": "price",
    }
    is_round = (trip_type or "").strip().lower() == "round_trip"
    if not is_round:
        params["flight_type"] = "oneway"
    else:
        if return_date is not None:
            nights = max(1, (return_date - outbound_date).days)
        else:
            nights = 7
        params["nights_in_dst_from"] = nights
        params["nights_in_dst_to"] = nights

    url = f"{KIWI_TEQUILA_URL}?{urlencode(params)}"
    req = Request(
        url,
        headers={"apikey": api_key, "accept": "application/json"},
    )
    try:
        with urlopen_impl(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        return {"data": [], "_blocker": f"http_{exc.code}"}
    except URLError:
        return {"data": [], "_blocker": "network_error"}
    except json.JSONDecodeError:
        return {"data": [], "_blocker": "invalid_json_response"}
    except Exception:
        # Defesa final — qualquer outra exceção (inclusive timeout)
        # vira no-op silencioso, sem vazar detalhes.
        return {"data": [], "_blocker": "unknown_error"}


# ----------------- Duffel (PR #63) -----------------

# Duffel API: https://api.duffel.com/air/offer_requests + /air/offers
# Reservas via /air/orders (order_flow). NÃO há deep_link público —
# o fluxo é "API order": criar order pela API com payment + passageiros.
# Para o spike, isso significa que `actionable_url` é sempre False
# (não há URL clicável simples). Decision depende só do trio
# cabin + price + booking_flow documentado.

# Mapeamento de cabine: Duffel usa snake_case ("business","first",
# "premium_economy","economy"). Consideramos confirmada quando TODOS
# os passageiros do 1º segmento têm `cabin_class == requested_cabin`.
DUFFEL_API_URL = "https://api.duffel.com/air/offer_requests"


def _duffel_first_offer(payload: dict) -> dict | None:
    """Extrai 1ª oferta de payloads em qualquer forma documentada:
    `{"data":{"offers":[...]}}` (offer_request) ou `{"data":[...]}` (list)."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        offers = data.get("offers") or []
    elif isinstance(data, list):
        offers = data
    else:
        offers = []
    if not offers or not isinstance(offers[0], dict):
        return None
    return offers[0]


def parse_duffel_for_actionability(
    payload: dict,
    route: str,
    requested_cabin: str = "business",
) -> ActionabilityReport:
    """Duffel offers REST: cabine + preço + order_flow documentado.
    Sem deep_link público — `actionable_url=False`, `booking_flow=order_flow`.
    Decision:
    - cabin + price → candidate_for_integration (apesar de actionable_url=False,
      pois order_flow conta como fluxo de booking documentado por API).
    - cabin sem price → validator_only.
    - sem cabin → not_suitable.
    """
    live_blocker = payload.get("_blocker") if isinstance(payload, dict) else None
    offer = _duffel_first_offer(payload)
    if offer is None:
        empty_blockers = ("empty_payload",)
        if isinstance(live_blocker, str) and live_blocker:
            empty_blockers = ("empty_payload", f"live_{live_blocker}")
        return ActionabilityReport(
            provider="duffel", route=route,
            outbound_date=None, return_date=None, trip_type="unknown",
            cabin_confirmed=False, price_amount=None,
            price_currency=None, airlines=(),
            actionable_url=False, booking_flow="none",
            booking_domain=None,
            blockers=empty_blockers,
            decision=DECISION_NOT_SUITABLE,
        )

    target = (requested_cabin or "").strip().lower()
    slices = offer.get("slices") or []
    first_slice = slices[0] if slices and isinstance(slices[0], dict) else {}
    segments = first_slice.get("segments") or []
    first_seg = segments[0] if segments and isinstance(segments[0], dict) else {}
    seg_pax = first_seg.get("passengers") or []
    # Cabin confirmada quando TODOS os passageiros do 1º segmento têm
    # cabin_class == requested_cabin. Conservador: se vazio → False.
    cabin_confirmed = bool(seg_pax) and all(
        isinstance(p, dict)
        and (p.get("cabin_class") or "").strip().lower() == target
        for p in seg_pax
    )

    price_raw = offer.get("total_amount")
    try:
        price_amount = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price_amount = None
    currency = offer.get("total_currency")

    # Airline: owner.iata_code é o mais estável; fallback p/ marketing_carrier.
    owner = offer.get("owner") if isinstance(offer.get("owner"), dict) else {}
    owner_code = (owner or {}).get("iata_code")
    carriers: list[str] = []
    if owner_code:
        carriers.append(owner_code)
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        mk = seg.get("marketing_carrier") or {}
        code = mk.get("iata_code") if isinstance(mk, dict) else None
        if code and code not in carriers:
            carriers.append(code)

    out_date = (first_seg.get("departing_at") or "")[:10] or None
    last_slice = slices[-1] if slices else first_slice
    last_segments = (
        last_slice.get("segments") if isinstance(last_slice, dict) else None
    ) or []
    ret_date = None
    if len(slices) >= 2 and last_segments:
        ret_date = (last_segments[0].get("departing_at") or "")[:10] or None
    trip_type = "round_trip" if ret_date else "one_way"

    blockers: list[str] = ["no_clickable_deep_link_in_payload"]
    if not cabin_confirmed:
        blockers.append("cabin_mismatch_or_absent")
    if price_amount is None:
        blockers.append("price_absent")
    # order_flow exige criar /air/orders com pagamento — fora do spike.
    blockers.append("requires_duffel_orders_api_for_booking")

    # Regra do goal: Duffel com cabin+price+order_flow documentado é
    # candidate_for_integration. Sem cabin → not_suitable. Sem price → validator.
    if cabin_confirmed and price_amount is not None:
        decision = DECISION_CANDIDATE
    elif cabin_confirmed and price_amount is None:
        decision = DECISION_VALIDATOR_ONLY
    else:
        decision = DECISION_NOT_SUITABLE

    # PR #84: voos do OUTBOUND slice — `marketing_carrier.iata_code` +
    # `marketing_carrier_flight_number` → "AF447". Segmento sem flight_number
    # é PULADO (degrada silenciosamente; o alerta volta ao formato sem voo).
    # Round-trip: ignoramos voos do return slice — pra Olivia conferir basta
    # o voo de ida na busca; o filtro de cabine já cobre os dois sentidos.
    flight_numbers: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        mk = seg.get("marketing_carrier") or {}
        code = mk.get("iata_code") if isinstance(mk, dict) else None
        fn = seg.get("marketing_carrier_flight_number")
        if code and fn:
            flight_numbers.append(f"{code}{fn}")

    return ActionabilityReport(
        provider="duffel", route=route,
        outbound_date=out_date, return_date=ret_date, trip_type=trip_type,
        cabin_confirmed=cabin_confirmed,
        price_amount=price_amount,
        price_currency=currency,
        airlines=tuple(carriers),
        actionable_url=False,
        booking_flow="order_flow",
        booking_domain=None,
        blockers=tuple(blockers),
        decision=decision,
        flight_numbers=tuple(flight_numbers),
    )


def duffel_live_search(
    *,
    access_token: str,
    origin: str,
    destination: str,
    trip_type: str,
    outbound_date,
    return_date=None,
    cabin_class: str = "business",
    currency: str = "USD",
    timeout: int = 20,
    urlopen_impl=None,
) -> dict:
    """1 chamada real ao Duffel /air/offer_requests com 1 passageiro adulto.
    Read-only. NUNCA loga URL, header Authorization, payload bruto ou
    order_id. Em qualquer erro de rede/HTTP, devolve
    `{'data': {'offers': []}, '_blocker': <code>}` — nunca propaga exceção.
    Caller usa `_blocker` para incluir no `ActionabilityReport`.

    `urlopen_impl` é injetável para testes (default = urllib.request.urlopen).
    """
    from datetime import date as _date
    from urllib.request import Request
    from urllib.error import HTTPError, URLError

    if urlopen_impl is None:
        from urllib.request import urlopen as urlopen_impl  # type: ignore

    if isinstance(outbound_date, str):
        from datetime import datetime as _dt
        outbound_date = _dt.strptime(outbound_date, "%Y-%m-%d").date()
    if isinstance(return_date, str) and return_date:
        from datetime import datetime as _dt
        return_date = _dt.strptime(return_date, "%Y-%m-%d").date()
    elif return_date == "":
        return_date = None

    slices = [
        {
            "origin": origin,
            "destination": destination,
            "departure_date": outbound_date.strftime("%Y-%m-%d"),
        }
    ]
    if (trip_type or "").strip().lower() == "round_trip":
        ret = return_date or outbound_date
        slices.append(
            {
                "origin": destination,
                "destination": origin,
                "departure_date": ret.strftime("%Y-%m-%d"),
            }
        )

    body = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"}],
            "cabin_class": (cabin_class or "business").strip().lower(),
            "currency": currency,
        }
    }
    encoded = json.dumps(body).encode("utf-8")
    req = Request(
        DUFFEL_API_URL + "?return_offers=true",
        data=encoded,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Duffel-Version": "v2",
        },
        method="POST",
    )
    try:
        with urlopen_impl(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        return {"data": {"offers": []}, "_blocker": f"http_{exc.code}"}
    except URLError:
        return {"data": {"offers": []}, "_blocker": "network_error"}
    except json.JSONDecodeError:
        return {"data": {"offers": []}, "_blocker": "invalid_json_response"}
    except Exception:
        return {"data": {"offers": []}, "_blocker": "unknown_error"}


# ----------------- Travelpayouts -----------------


def parse_travelpayouts_for_actionability(
    payload: dict,
    route: str,
) -> ActionabilityReport:
    """Travelpayouts Aviasales cache devolve preço + datas mas:
    - sem cabine confirmada (endpoint não respeita trip_class);
    - sem deep_link comercial confiável (Aviasales redireciona p/ russo).

    Resultado quase sempre `not_suitable` — útil só como sinal cru
    de preço (fora do escopo deste spike)."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not data:
        return ActionabilityReport(
            provider="travelpayouts", route=route,
            outbound_date=None, return_date=None, trip_type="unknown",
            cabin_confirmed=False, price_amount=None,
            price_currency=None, airlines=(),
            actionable_url=False, booking_flow="none",
            booking_domain=None,
            blockers=("empty_payload",),
            decision=DECISION_NOT_SUITABLE,
        )
    # `data` é tipicamente um dict aninhado (destino → preço). Para o
    # spike só precisamos da forma geral.
    blockers = (
        "no_cabin_confirmation_from_provider",
        "no_actionable_deep_link",
    )
    return ActionabilityReport(
        provider="travelpayouts", route=route,
        outbound_date=None, return_date=None, trip_type="unknown",
        cabin_confirmed=False,
        price_amount=None,  # spike não extrai preço — só sinaliza estado
        price_currency=None,
        airlines=(),
        actionable_url=False, booking_flow="none",
        booking_domain=None,
        blockers=blockers,
        decision=DECISION_NOT_SUITABLE,
    )


# ----------------- formatter -----------------


def _fmt_yes_no(v: bool) -> str:
    return "yes" if v else "no"


def format_actionability_report(report: ActionabilityReport) -> str:
    """Formato determinístico chave=valor por linha. NUNCA inclui
    token, URL completa, post_data ou payload — só presença + domínio."""
    lines = [
        f"provider:        {report.provider}",
        f"route:           {report.route}",
        f"trip_type:       {report.trip_type}",
        f"outbound_date:   {report.outbound_date or '(n/a)'}",
        f"return_date:     {report.return_date or '(n/a)'}",
        f"cabin_confirmed: {_fmt_yes_no(report.cabin_confirmed)}",
        f"price_amount:    "
        f"{f'{report.price_amount:.2f}' if report.price_amount is not None else '(n/a)'}",
        f"price_currency:  {report.price_currency or '(n/a)'}",
        f"airlines:        {','.join(report.airlines) if report.airlines else '(n/a)'}",
        f"actionable_url:  {_fmt_yes_no(report.actionable_url)}",
        f"booking_flow:    {report.booking_flow}",
        f"booking_domain:  {report.booking_domain or '(n/a)'}",
        f"blockers:        "
        f"{','.join(report.blockers) if report.blockers else '(none)'}",
        f"decision:        {report.decision}",
    ]
    return "\n".join(lines)


# ----------------- CLI helpers -----------------


def load_and_parse(
    provider: str,
    fixture_path: Path,
    *,
    route: str,
    requested_cabin: str = "business",
    booking_options_path: Path | None = None,
) -> ActionabilityReport:
    """Carrega fixture do disco e roda o parser apropriado.

    Não faz rede. Argumento `booking_options_path` é opcional para
    SerpApi (combina search + booking_options para classificar
    actionability final).
    """
    provider = (provider or "").strip().lower()
    if provider == "amadeus":
        from .amadeus_client import parse_offers_from_file
        offers = parse_offers_from_file(str(fixture_path))
        return parse_amadeus_for_actionability(offers, route=route)
    if provider == "serpapi":
        offers = parse_search_from_file(str(fixture_path))
        booking_options = None
        if booking_options_path is not None:
            booking_options = parse_booking_options_from_file(
                str(booking_options_path)
            )
        return parse_serpapi_for_actionability(
            offers, route=route,
            requested_cabin=requested_cabin,
            booking_options=booking_options,
        )
    if provider == "kiwi":
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        return parse_kiwi_for_actionability(
            payload, route=route, requested_cabin=requested_cabin,
        )
    if provider == "travelpayouts":
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        return parse_travelpayouts_for_actionability(payload, route=route)
    if provider == "duffel":
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        return parse_duffel_for_actionability(
            payload, route=route, requested_cabin=requested_cabin,
        )
    raise ValueError(f"provider desconhecido: {provider!r}")
