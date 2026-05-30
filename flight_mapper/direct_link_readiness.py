"""Spike read-only (PR #72): avalia provedores quanto a DIRECT BOOKING /
DEEP LINK — capacidade de devolver uma URL que abre a OFERTA EXATA ou um
fluxo de booking real, NÃO uma página de busca genérica.

Princípio do goal: "No deep_link = not suitable". Diagnóstico puro, sem
rede em testes (consome fixtures/dicts), sem integração de produção, sem
scraping, sem Telegram. NUNCA expõe token/query secrets/URL crua nos logs
— só o domínio + tipo do link.

Estados de `deep_link_type`:
- exact_offer    : URL abre a oferta exata (ex.: Kiwi deep_link, affiliate
                   deeplink com parâmetros de oferta).
- booking_flow   : há fluxo de booking via API (Duffel order_flow) — NÃO é
                   URL clicável; not_suitable p/ ESTE goal.
- generic_search : só URL de busca genérica (Google Flights search, etc.).
- none           : sem link.

Decisão:
- candidate_for_integration : deep_link_type == exact_offer.
- not_suitable              : generic_search / none / booking_flow.
- blocked_commercially      : provider exige aprovação comercial manual
                              sem caminho self-service (sinalizado via
                              `commercial_blocked=True` no input).
"""

from __future__ import annotations

from dataclasses import dataclass

from .serpapi_client import url_domain


# deep_link_type
DL_EXACT_OFFER = "exact_offer"
DL_BOOKING_FLOW = "booking_flow"
DL_GENERIC_SEARCH = "generic_search"
DL_NONE = "none"

# decision
DECISION_CANDIDATE = "candidate_for_integration"
DECISION_NOT_SUITABLE = "not_suitable"
DECISION_BLOCKED_COMMERCIAL = "blocked_commercially"


# Hosts cujo path de busca é uma página genérica (não abre oferta exata).
_GENERIC_SEARCH_HOSTS = (
    "google.com", "google.com/travel", "kayak.com", "momondo.com",
    "skyscanner.com", "skyscanner.net", "expedia.com",
)

# Marcadores de path/query que denunciam busca genérica (não oferta exata).
_GENERIC_PATH_MARKERS = ("/search", "/flights/results", "/travel/flights")


@dataclass(frozen=True)
class DirectLinkReport:
    """Snapshot SANITIZADO de um provider quanto a direct booking link.

    NUNCA contém token, query string, URL completa ou payload — só
    presença booleana + domínio + tipo do link."""

    provider: str
    route: str
    trip_type: str
    dates: str
    cabin_available: bool
    price_available: bool
    deep_link_available: bool
    deep_link_type: str          # DL_*
    deep_link_domain: str | None  # só o host, nunca URL completa
    decision: str                # DECISION_*
    blockers: tuple[str, ...]
    next_step: str


def classify_deep_link(
    url: str | None,
    *,
    booking_flow: bool = False,
) -> str:
    """Classifica o tipo de link SEM expor a URL.

    - `booking_flow=True` (ex.: Duffel order_flow) → DL_BOOKING_FLOW,
      independentemente de URL (não há checkout clicável).
    - URL ausente / não-http → DL_NONE.
    - host/path de busca genérica → DL_GENERIC_SEARCH.
    - caso contrário (host de cia/OTA/Kiwi com parâmetros de oferta) →
      DL_EXACT_OFFER.
    """
    if booking_flow:
        return DL_BOOKING_FLOW
    if not url or not isinstance(url, str):
        return DL_NONE
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return DL_NONE
    host = (url_domain(u) or "").lower()
    if not host:
        return DL_NONE
    low = u.lower()
    # Busca genérica: host conhecido de metasearch OU path de resultados.
    if any(h in host for h in _GENERIC_SEARCH_HOSTS):
        # google.com/travel/clk (deep redirect) ainda é genérico p/ nós.
        return DL_GENERIC_SEARCH
    if any(m in low for m in _GENERIC_PATH_MARKERS):
        return DL_GENERIC_SEARCH
    return DL_EXACT_OFFER


_NEXT_STEPS = {
    DECISION_CANDIDATE: (
        "Validar deep_link em rota/data reais (sandbox/affiliate) e abrir "
        "PR futuro de integração; manter sanitização (só domínio nos logs)."
    ),
    DECISION_NOT_SUITABLE: (
        "Descartar p/ alerta acionável: sem deep_link de oferta exata. "
        "Pode seguir como sinal confirmado 'compra pendente' (Duffel) ou "
        "validador, nunca como link de compra."
    ),
    DECISION_BLOCKED_COMMERCIAL: (
        "Requer aprovação comercial/affiliate sem caminho self-service. "
        "Parquear até haver acesso aprovado; não integrar."
    ),
}


def parse_direct_link_offer(
    offer: dict,
    *,
    provider: str,
    route: str,
    trip_type: str = "round_trip",
) -> DirectLinkReport:
    """Analisa UM offer sanitizado (dict) de um provider candidato.

    Campos esperados (todos opcionais, tolerante a ausência):
    - `deep_link` / `booking_url` : URL (classificada, nunca exposta).
    - `booking_flow` (bool)       : True p/ order_flow (Duffel-like).
    - `commercial_blocked` (bool) : True se exige aprovação manual.
    - `price` / `price_amount`    : presença → price_available.
    - `cabin` / `cabin_class`     : presença → cabin_available.
    - `outbound_date`/`return_date`: p/ a linha de datas.
    Pure: sem rede, sem I/O.
    """
    booking_flow = bool(offer.get("booking_flow"))
    commercial_blocked = bool(offer.get("commercial_blocked"))
    url = offer.get("deep_link") or offer.get("booking_url")
    dl_type = classify_deep_link(url, booking_flow=booking_flow)
    deep_link_available = dl_type == DL_EXACT_OFFER
    domain = url_domain(url) if (url and not booking_flow) else None

    price_available = (
        offer.get("price") is not None
        or offer.get("price_amount") is not None
    )
    cabin_available = bool(offer.get("cabin") or offer.get("cabin_class"))

    out = offer.get("outbound_date") or offer.get("departure_date") or ""
    ret = offer.get("return_date") or ""
    dates = out + (f" → {ret}" if ret else "")

    blockers: list[str] = []
    if commercial_blocked:
        blockers.append("requires_manual_commercial_approval")
    if dl_type == DL_BOOKING_FLOW:
        blockers.append("order_flow_not_clickable_checkout")
    elif dl_type == DL_GENERIC_SEARCH:
        blockers.append("only_generic_search_url")
    elif dl_type == DL_NONE:
        blockers.append("no_deep_link")
    if not cabin_available:
        blockers.append("cabin_not_available")
    if not price_available:
        blockers.append("price_not_available")

    # Decisão: aprovação comercial bloqueia tudo; senão exige exact_offer.
    if commercial_blocked:
        decision = DECISION_BLOCKED_COMMERCIAL
    elif deep_link_available:
        decision = DECISION_CANDIDATE
    else:
        decision = DECISION_NOT_SUITABLE

    return DirectLinkReport(
        provider=provider,
        route=route,
        trip_type=trip_type,
        dates=dates or "(n/a)",
        cabin_available=cabin_available,
        price_available=price_available,
        deep_link_available=deep_link_available,
        deep_link_type=dl_type,
        deep_link_domain=domain,
        decision=decision,
        blockers=tuple(blockers),
        next_step=_NEXT_STEPS[decision],
    )


def _yes_no(v: bool) -> str:
    return "yes" if v else "no"


def format_direct_link_report(report: DirectLinkReport) -> str:
    """Formato determinístico chave: valor. NUNCA inclui token, query
    string, URL completa ou payload — só presença + domínio + tipo."""
    lines = [
        f"provider:             {report.provider}",
        f"route:                {report.route}",
        f"trip_type:            {report.trip_type}",
        f"dates:                {report.dates}",
        f"cabin_available:      {_yes_no(report.cabin_available)}",
        f"price_available:      {_yes_no(report.price_available)}",
        f"deep_link_available:  {_yes_no(report.deep_link_available)}",
        f"deep_link_type:       {report.deep_link_type}",
        f"deep_link_domain:     {report.deep_link_domain or '(n/a)'}",
        f"decision:             {report.decision}",
        f"blockers:             "
        f"{','.join(report.blockers) if report.blockers else '(none)'}",
        f"next_step:            {report.next_step}",
    ]
    return "\n".join(lines)


def load_and_parse_direct_link(
    provider: str,
    fixture_path,
    *,
    route: str,
    trip_type: str = "round_trip",
) -> DirectLinkReport:
    """Carrega fixture JSON (dict de offer sanitizado) e roda o parser.
    Sem rede."""
    import json
    from pathlib import Path

    payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    # Aceita {"offer": {...}} ou o offer direto.
    offer = payload.get("offer") if isinstance(payload, dict) and "offer" in payload else payload
    return parse_direct_link_offer(
        offer if isinstance(offer, dict) else {},
        provider=provider, route=route, trip_type=trip_type,
    )
