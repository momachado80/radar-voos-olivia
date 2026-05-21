"""Relatório periódico de vida do robô via Telegram."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .airports import humanize_route, is_actionable_url
from .formatting import format_brl, format_price
from .monitor import MonitorResult
from .notifier import TelegramNotifier
from .deal_intelligence import deal_label_pt, evaluate_deal
from .regions import Cabin, TripType
from .sanity import SUSPICIOUS_FLOOR_BRL, is_suspicious_price
from .score import compute_opportunity_score
from .state import PriceStore, RouteHistory
from .thresholds import HOT_ROUTE_KEYS, levels_for, scaled_levels


@dataclass
class StatusState:
    last_report_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> "StatusState":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return cls()
        return cls(last_report_at=raw.get("last_report_at"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_report_at": self.last_report_at}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@dataclass
class StatusDecision:
    action: str
    reason: str


def _split_route_key(key: str) -> tuple[str, str] | None:
    parts = key.split("-")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None


def _latest_prices(store: PriceStore) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for key in store.keys():
        history = store.get(key)
        if history.prices:
            items.append((key, history.prices[-1]))
    return items


def _actionable_link_from_history(history: RouteHistory, origin: str, destination: str) -> str | None:
    """Retorna deep_link do `last_quote` apenas se for da mesma rota e acionável."""
    lq = history.last_quote
    if not isinstance(lq, dict):
        return None
    if lq.get("origin") != origin or lq.get("destination") != destination:
        return None
    if not lq.get("departure_date"):
        return None
    link = lq.get("deep_link")
    if is_actionable_url(link):
        return link
    return None


def _price_label(history: RouteHistory, fallback_price: float) -> str:
    """Rótulo de preço honesto p/ relatório.

    Usa a moeda registrada em `last_quote`. Nunca exibe `R$` cru quando
    a moeda não é comprovadamente BRL (entradas legadas sem metadados de
    moeda eram USD rotulado como BRL — o bug que estamos corrigindo).
    """
    lq = history.last_quote if isinstance(history.last_quote, dict) else None
    if not lq or not lq.get("currency"):
        # Histórico legado: moeda não comprovada → não exibir como R$.
        return f"{fallback_price:,.0f} (moeda não confirmada)"
    currency = str(lq.get("currency"))
    amount = lq.get("amount")
    if amount is None:
        amount = fallback_price
    return format_price(
        float(amount),
        currency,
        lq.get("amount_brl_estimated"),
        lq.get("fx_rate"),
    )


def _compute_average_score(store: PriceStore, keys: list[str]) -> int | None:
    """Score médio (0-100) das `keys` informadas, usando last_quote quando disponível."""
    scores: list[int] = []
    for key in keys:
        history = store.get(key)
        if not history.prices:
            continue
        price = history.prices[-1]
        lq = history.last_quote if isinstance(history.last_quote, dict) else None
        actionable = bool(lq.get("actionable_url")) if lq else False
        levels = levels_for(key)
        # Preço convertido de USD → tetos USD precisam escalar p/ BRL.
        if lq and str(lq.get("currency", "")).upper() == "USD" and lq.get("fx_rate"):
            levels = scaled_levels(levels, lq.get("fx_rate"))
        scores.append(
            compute_opportunity_score(
                price,
                levels,
                history,
                actionable_url=actionable,
                confirmed=False,  # heartbeat não confirma; usa flag conservadora
                is_hot_route=key in HOT_ROUTE_KEYS,
            )
        )
    if not scores:
        return None
    return round(sum(scores) / len(scores))


class _SignalQuote:
    """Shim mínimo p/ reusar `sanity.is_suspicious_price` a partir de um
    `last_quote` (dict). Não toca provider/monitor/thresholds."""

    def __init__(self, lq: dict):
        self.suspicious = bool(lq.get("suspicious", False))
        self.currency = str(lq.get("currency") or "")
        try:
            self.cabin = Cabin(str(lq.get("cabin") or "unknown"))
        except ValueError:
            self.cabin = Cabin.UNKNOWN
        try:
            self.trip_type = TripType(str(lq.get("trip_type") or "round_trip"))
        except ValueError:
            self.trip_type = TripType.ROUND_TRIP


def _is_confirmed(history: RouteHistory) -> bool:
    """Oportunidade confirmada: cabine confirmada (business/economy),
    moeda correta e preço NÃO suspeito. Travelpayouts (cabin unknown /
    cabin_confirmed False) nunca conta como confirmada."""
    lq = history.last_quote if isinstance(history.last_quote, dict) else None
    if not lq:
        return False
    if lq.get("cabin_confirmed") is not True:
        return False
    if lq.get("cabin") not in ("business", "economy"):
        return False
    currency = str(lq.get("currency") or "").upper()
    if currency == "USD" and lq.get("amount_brl_estimated") is None:
        return False
    if currency not in ("BRL", "USD"):
        return False
    amount_brl = lq.get("amount_brl_estimated")
    if amount_brl is None and currency == "BRL":
        amount_brl = lq.get("amount")
    if is_suspicious_price(None, _SignalQuote(lq), amount_brl):
        return False
    return True


def _cabin_label(history: RouteHistory) -> str:
    lq = history.last_quote if isinstance(history.last_quote, dict) else {}
    cabin = (lq or {}).get("cabin")
    if cabin == "business":
        return "Executiva"
    if cabin == "economy":
        return "Econômica"
    return "cabine não confirmada"


def _format_confirmed_line(
    index: int, key: str, history: RouteHistory, price: float, link: str | None
) -> str:
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    tag = _cabin_label(history)
    label = humanize_route(*parts) if parts else key
    base = f"{index}. {label} — {price_str} — {tag}"
    if link:
        return f'{base} — 🔎 <a href="{link}">Conferir busca</a>'
    return base


_SOURCE_NAMES = {
    "travelpayouts": "Travelpayouts",
    "travelpayouts+kiwi": "Travelpayouts + Kiwi",
    "kiwi": "Kiwi",
    "mock": "Mock",
    "manual_purchase": "Travelpayouts (cache)",
}


def _source_name(history: RouteHistory) -> str:
    lq = history.last_quote if isinstance(history.last_quote, dict) else {}
    src = (lq or {}).get("source")
    if not src:
        return "desconhecida"
    return _SOURCE_NAMES.get(src, str(src))


def _lq(history: RouteHistory) -> dict:
    return history.last_quote if isinstance(history.last_quote, dict) else {}


def _amount_brl(history: RouteHistory, fallback: float) -> float | None:
    lq = _lq(history)
    amt = lq.get("amount_brl_estimated")
    if amt is None and str(lq.get("currency") or "").upper() == "BRL":
        amt = lq.get("amount")
    if amt is None and not lq.get("currency"):
        return None  # moeda não comprovada — não comparável
    try:
        return float(amt) if amt is not None else None
    except (TypeError, ValueError):
        return None


def _trip_label(history: RouteHistory) -> str:
    trip = (_lq(history) or {}).get("trip_type")
    if trip == "one_way":
        return "somente ida"
    if trip == "round_trip":
        return "ida e volta"
    return "não informado"


def _economy_plausible(history: RouteHistory) -> bool:
    """Sinal bruto cujo preço é implausível p/ executiva mas plausível
    p/ econômica: entre o piso econômico e o piso executivo do trip_type
    (reusa os pisos de sanity, sem rede)."""
    if _is_confirmed(history):
        return False
    lq = _lq(history)
    if not lq:
        return False
    amount = lq.get("amount_brl_estimated")
    if amount is None and str(lq.get("currency") or "").upper() == "BRL":
        amount = lq.get("amount")
    if amount is None:
        return False
    sq = _SignalQuote(lq)
    biz = SUSPICIOUS_FLOOR_BRL.get((sq.trip_type, Cabin.BUSINESS))
    eco = SUSPICIOUS_FLOOR_BRL.get((sq.trip_type, Cabin.ECONOMY))
    if biz is None or eco is None:
        return False
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    return eco <= amt < biz


def _format_raw_block(
    index: int, key: str, history: RouteHistory, price: float
) -> str:
    """Sinal bruto multilinha — fallback p/ quando as fontes diferem.
    Nunca usa 'Executiva'/'Business'/'oportunidade'/'excelente'/'bom'/score."""
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    label = humanize_route(*parts) if parts else key
    return (
        f"{index}. {label} — {price_str} [{_trip_label(history)}]\n"
        f"   Fonte: {_source_name(history)}\n"
        f"   Cabine: não confirmada\n"
        f"   Tipo: {_trip_label(history)}\n"
        f"   Interpretação: pode ser econômica promocional ou tarifa "
        f"sem classe comprovada."
    )


def _format_raw_signals(
    raw: list[tuple[str, float]], store: PriceStore
) -> str:
    """Bloco da seção "📡 Sinais brutos de preço".

    Compacta quando os itens compartilham a MESMA fonte (cabine e
    interpretação já são fixas em sinais brutos): cabeçalho com Fonte/
    Cabine/Interpretação uma única vez no topo + linhas numeradas com
    `[trip]` por item. Se as fontes divergirem, cai no formato
    multilinha por item (`_format_raw_block`) sem perder honestidade.
    """
    if not raw:
        return "• Nenhum sinal bruto de preço no momento."
    sources = {_source_name(store.get(key)) for key, _ in raw}
    if len(sources) != 1:
        return "\n".join(
            _format_raw_block(i + 1, key, store.get(key), price)
            for i, (key, price) in enumerate(raw)
        )
    source = next(iter(sources))
    header = (
        f"Fonte: {source}\n"
        "Cabine: não confirmada\n"
        "Interpretação: podem ser econômica promocional ou tarifa "
        "sem classe comprovada.\n"
    )
    lines: list[str] = []
    for i, (key, price) in enumerate(raw):
        h = store.get(key)
        parts = _split_route_key(key)
        label = humanize_route(*parts) if parts else key
        lines.append(
            f"{i + 1}. {label} — {_price_label(h, price)} [{_trip_label(h)}]"
        )
    return header + "\n" + "\n".join(lines)


def _format_economy_block(
    index: int, key: str, history: RouteHistory, price: float
) -> str:
    """Possível promoção de econômica com inteligência: classifica vs
    banda USD por região/trip + compara com histórico (mediana, p25,
    mínimo recente). Sem rótulo de classe comprovada."""
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    label = humanize_route(*parts) if parts else key
    lq = _lq(history)
    sq = _SignalQuote(lq)
    destination = lq.get("destination") or (parts[1] if parts else "")
    brl = _amount_brl(history, price)
    usd = None
    if str(lq.get("currency") or "").upper() == "USD":
        try:
            usd = float(lq.get("amount")) if lq.get("amount") is not None else None
        except (TypeError, ValueError):
            usd = None
    deal = evaluate_deal(
        destination=destination,
        trip_type=sq.trip_type,
        usd_amount=usd,
        brl_amount=brl,
        prices=history.prices,
    )

    # Linha de classificação (região/trip aparecem só quando aplicável).
    class_suffix = (
        f" ({deal.region}/{deal.trip_type.value})"
        if deal.region and deal.region_band
        else ""
    )
    class_line = f"   Classificação: {deal_label_pt(deal.deal)}{class_suffix}"

    # Linha de histórico.
    h = deal.history
    if h.n == 0:
        hist_line = "   Histórico: sem amostras."
    elif not h.sufficient:
        hist_line = (
            f"   Histórico: insuficiente (n={h.n}, mínimo p/ mediana confiável: 10)"
        )
    else:
        hist_line = (
            f"   Histórico: mediana {format_brl(h.median_brl)} · "
            f"p25 {format_brl(h.p25_brl)} · "
            f"mínimo recente {format_brl(h.min_recent_brl)} (n={h.n})"
        )

    # Linha de desconto.
    if deal.discount_pct is None:
        disc_line = "   Desconto: histórico insuficiente"
    else:
        disc_line = (
            f"   Desconto estimado: {deal.discount_pct:.0%} vs mediana"
        )

    return (
        f"{index}. {label} — {price_str} [{_trip_label(history)}]\n"
        f"   Fonte: {_source_name(history)}\n"
        f"   Cabine: não confirmada\n"
        f"   Tipo: {_trip_label(history)}\n"
        f"{class_line}\n"
        f"{hist_line}\n"
        f"{disc_line}\n"
        f"   Interpretação: preço compatível com econômica promocional; "
        f"classe não comprovada pela fonte.\n"
        f"   Motivo: {deal.reason}"
    )


# Aviso fixo do bloco de econômica — mantém honestidade sobre a classe.
_ECONOMY_WARNING = (
    "⚠️ Cabine não confirmada. Classificado como possível econômica, "
    "não executiva."
)


def _security_block(result: MonitorResult) -> str:
    """🛡️ Bloqueios de segurança do ciclo (contadores já existentes em
    MonitorResult — nenhuma mudança no monitor)."""
    rows: list[tuple[str, int]] = [
        ("cabine não confirmada", result.cabin_blocked),
        ("preço economicamente suspeito", result.suspicious_blocked),
        ("câmbio ausente/ inválido", result.currency_blocked),
        ("link comercial indisponível", result.non_actionable_links_skipped),
        ("cotação stale (2ª checagem)", result.stale_quotes_skipped),
    ]
    active = [f"• {label}: {n}" for label, n in rows if n]
    body = (
        "\n".join(active)
        if active
        else "• Nenhum bloqueio de segurança neste ciclo."
    )
    return "🛡️ Alertas bloqueados por segurança\n" + body


def _no_alert_reason(result: MonitorResult) -> str:
    if result.alerts_sent > 0:
        return f"🔥 {result.alerts_sent} alerta(s) enviado(s) neste ciclo."
    motives: list[str] = []
    if result.cabin_blocked:
        motives.append(
            f"nenhuma cabine confirmada ({result.cabin_blocked} bloqueada(s))"
        )
    if result.suspicious_blocked:
        motives.append(
            f"preços economicamente suspeitos ({result.suspicious_blocked})"
        )
    if result.currency_blocked:
        motives.append(
            f"câmbio ausente/ inválido ({result.currency_blocked})"
        )
    if result.non_actionable_links_skipped:
        motives.append(
            f"link comercial indisponível ({result.non_actionable_links_skipped})"
        )
    if not motives:
        return "ℹ️ Sem oportunidade confirmada agora."
    return "ℹ️ Sem alerta confirmado: " + "; ".join(motives) + "."


def _source_status_block(
    store: PriceStore,
    confirmed: list[tuple[str, float]],
    raw: list[tuple[str, float]],
) -> str:
    """🧭 Status das fontes — derivado do ciclo, sem rede."""

    def _sources(items: list[tuple[str, float]]) -> set[str]:
        out: set[str] = set()
        for key, _ in items:
            lq = store.get(key).last_quote
            if isinstance(lq, dict) and lq.get("source"):
                out.add(str(lq["source"]))
        return out

    raw_srcs = _sources(raw)
    conf_srcs = _sources(confirmed)

    if any(s in raw_srcs for s in ("travelpayouts", "travelpayouts+kiwi", "manual_purchase")):
        tp = "ativo, mas sem cabine confirmada."
    elif "travelpayouts" in conf_srcs:
        tp = "ativo (cabine confirmada)."
    else:
        tp = "sem cotação neste ciclo."

    if "kiwi" in conf_srcs or "travelpayouts+kiwi" in conf_srcs:
        kiwi = "ativo (cabine confirmada)."
    elif "kiwi" in raw_srcs:
        kiwi = "respondeu, mas sem cabine confirmada."
    else:
        kiwi = "sem cotação confirmada neste ciclo."

    if confirmed:
        execs = f"{len(confirmed)} confirmada(s) neste ciclo."
    else:
        execs = "aguardando fonte com cabine confirmada."

    return (
        "🧭 Status das fontes\n"
        f"• Travelpayouts: {tp}\n"
        f"• Kiwi: {kiwi}\n"
        f"• Alertas executivos: {execs}"
    )


def _build_message(result: MonitorResult, store: PriceStore, now: datetime) -> str:
    timestamp = now.strftime("%d/%m %H:%M UTC")

    # Painel SEMPRE renderizado — sem fallback/early-return. Quando
    # quotes==0 as seções ficam vazias com texto claro e o motivo
    # explica. Sem template degradado antigo.
    latest = _latest_prices(store)

    # Qualidade: entradas legadas sem moeda comprovada não entram nas
    # listas principais (eram USD rotulado como BRL — o bug original).
    quality = [
        it for it in latest if _amount_brl(store.get(it[0]), it[1]) is not None
    ]
    legacy_omitted = len(latest) - len(quality)

    # Dedupe: mesma rota/preço/fonte/cabine/trip aparece uma vez só;
    # round_trip × one_way da mesma rota ficam diferenciados pelo trip.
    seen: set[tuple] = set()
    deduped: list[tuple[str, float]] = []
    for key, price in quality:
        lq = _lq(store.get(key))
        sig = (
            lq.get("origin"), lq.get("destination"), round(float(price), 2),
            lq.get("source"), lq.get("cabin"), lq.get("trip_type"),
        )
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append((key, price))

    confirmed = sorted(
        (it for it in deduped if _is_confirmed(store.get(it[0]))),
        key=lambda x: x[1],
    )[:3]
    non_conf = [it for it in deduped if not _is_confirmed(store.get(it[0]))]
    economy = sorted(
        (it for it in non_conf if _economy_plausible(store.get(it[0]))),
        key=lambda x: x[1],
    )[:3]
    raw = sorted(
        (it for it in non_conf if not _economy_plausible(store.get(it[0]))),
        key=lambda x: x[1],
    )[:3]

    if confirmed:
        confirmed_lines = "\n".join(
            _format_confirmed_line(
                i + 1,
                key,
                store.get(key),
                price,
                link=_actionable_link_from_history(
                    store.get(key), *(_split_route_key(key) or ("", ""))
                ),
            )
            for i, (key, price) in enumerate(confirmed)
        )
        avg_score = _compute_average_score(store, [k for k, _ in confirmed])
        # Score só rotula oportunidades confirmadas — nunca sinais brutos.
        confirmed_score_line = (
            f"⭐ Score médio (oportunidades confirmadas): {avg_score}/100\n"
            if avg_score is not None
            else ""
        )
    else:
        confirmed_lines = "• Nenhuma oportunidade confirmada agora."
        confirmed_score_line = ""

    raw_block = _format_raw_signals(raw, store)
    if economy:
        eco_items = "\n".join(
            _format_economy_block(i + 1, key, store.get(key), price)
            for i, (key, price) in enumerate(economy)
        )
        economy_block = f"{eco_items}\n\n{_ECONOMY_WARNING}"
    else:
        economy_block = "• Nenhum sinal compatível com econômica promocional agora."

    sources_block = _source_status_block(store, confirmed, raw + economy)
    security_block = _security_block(result)
    reason = _no_alert_reason(result)
    legacy_line = (
        f"• Entradas legadas sem moeda comprovada (omitidas): {legacy_omitted}\n"
        if legacy_omitted
        else ""
    )

    return (
        "🛰️ <b>Radar de Voos Olivia — relatório diário</b>\n"
        f"Robô ativo. Último ciclo: {timestamp}\n\n"
        "📊 Ciclo recente\n"
        f"• Rotas escaneadas: {result.scanned}\n"
        f"• Cotações obtidas: {result.quotes_received}\n"
        f"• Alertas enviados: {result.alerts_sent}\n"
        f"• Bloqueados por cabine: {result.cabin_blocked}\n"
        f"• Bloqueados por preço suspeito: {result.suspicious_blocked}\n"
        f"• Bloqueados por câmbio: {result.currency_blocked}\n"
        f"• Links comerciais indisponíveis: {result.non_actionable_links_skipped}\n"
        f"{legacy_line}\n"
        "📌 Oportunidades confirmadas\n"
        f"{confirmed_score_line}"
        f"{confirmed_lines}\n\n"
        "📡 Sinais brutos de preço\n"
        f"{raw_block}\n\n"
        "💸 Possíveis promoções de econômica\n"
        f"{economy_block}\n\n"
        f"{security_block}\n\n"
        f"{sources_block}\n\n"
        f"{reason}"
    )


def explain_deals(store: PriceStore, top: int = 5) -> str:
    """Texto read-only (CLI `explain-deals`): top sinais de econômica
    classificados (deal intelligence). Sem rede, sem provider, sem
    Telegram. Não promove sinal bruto a executiva."""
    latest = _latest_prices(store)
    candidates: list[tuple[str, float]] = []
    for key, price in latest:
        h = store.get(key)
        if _is_confirmed(h):
            continue
        if _amount_brl(h, price) is None:
            continue  # entrada legada sem moeda — fora do painel principal
        candidates.append((key, price))

    evals: list[tuple[str, float, object]] = []
    for key, price in candidates:
        h = store.get(key)
        lq = _lq(h)
        sq = _SignalQuote(lq)
        parts = _split_route_key(key)
        destination = lq.get("destination") or (parts[1] if parts else "")
        brl = _amount_brl(h, price)
        usd = None
        if str(lq.get("currency") or "").upper() == "USD":
            try:
                usd = float(lq.get("amount")) if lq.get("amount") is not None else None
            except (TypeError, ValueError):
                usd = None
        ev = evaluate_deal(
            destination=destination,
            trip_type=sq.trip_type,
            usd_amount=usd,
            brl_amount=brl,
            prices=h.prices,
        )
        evals.append((key, price, ev))

    # Ordem: muito_forte > boa > observar > ignorar; dentro de cada,
    # maior desconto primeiro; depois menor preço.
    order = {
        "muito_forte": 0, "boa": 1, "observar": 2, "ignorar": 3,
    }
    evals.sort(key=lambda x: (
        order.get(x[2].deal, 9),
        -(x[2].discount_pct or 0),
        x[1],
    ))
    evals = evals[:top]

    lines: list[str] = []
    lines.append("💸 Top sinais de econômica (classificação read-only)")
    if not evals:
        lines.append("• Nenhum sinal com moeda comprovada para classificar.")
    else:
        for i, (key, price, ev) in enumerate(evals, 1):
            parts = _split_route_key(key)
            label = humanize_route(*parts) if parts else key
            h = store.get(key)
            disc = (
                f"{ev.discount_pct:.0%} vs mediana"
                if ev.discount_pct is not None
                else "histórico insuficiente"
            )
            hist = (
                f"n={ev.history.n}"
                + (
                    f", mediana {format_brl(ev.history.median_brl)}"
                    if ev.history.median_brl is not None
                    else ""
                )
            )
            lines.append(
                f"{i}. {label} — {_price_label(h, price)} "
                f"[{_trip_label(h)}] — {deal_label_pt(ev.deal)}"
            )
            lines.append(f"   Desconto: {disc}; histórico: {hist}")
            lines.append(f"   Motivo: {ev.reason}")
    lines.append("")
    lines.append(_ECONOMY_WARNING)
    return "\n".join(lines)


def explain_status(store: PriceStore, now: datetime | None = None) -> str:
    """Texto read-only (CLI `explain-status`): resumo das fontes, por que
    não houve alerta confirmado, melhores sinais brutos e próximos
    gargalos. Sem rede, sem provider, sem Telegram."""
    now = now or datetime.now(timezone.utc)
    latest = _latest_prices(store)
    confirmed = sorted(
        (it for it in latest if _is_confirmed(store.get(it[0]))),
        key=lambda x: x[1],
    )
    non_conf = [it for it in latest if not _is_confirmed(store.get(it[0]))]
    raw_sorted = sorted(non_conf, key=lambda x: x[1])[:3]

    srcs: set[str] = set()
    for key, _ in latest:
        lq = store.get(key).last_quote
        if isinstance(lq, dict) and lq.get("source"):
            srcs.add(str(lq["source"]))
    src_line = ", ".join(sorted(srcs)) if srcs else "nenhuma"

    lines: list[str] = []
    lines.append("🧭 Resumo das fontes")
    lines.append(f"• Fontes vistas no histórico: {src_line}")
    lines.append(
        "• Travelpayouts não confirma cabine (não vira alerta executivo)."
    )
    lines.append(
        "• Kiwi confirma cabine via filtro server-side quando disponível."
    )
    lines.append("")
    lines.append("❓ Por que não há alerta confirmado")
    if confirmed:
        lines.append(
            f"• Há {len(confirmed)} rota(s) com cabine confirmada no histórico."
        )
    else:
        lines.append(
            "• Nenhuma rota tem cotação com cabine confirmada — todo preço "
            "atual é sinal bruto (Travelpayouts/cabine não confirmada)."
        )
    lines.append("")
    lines.append("📡 Melhores sinais brutos")
    if raw_sorted:
        for i, (key, price) in enumerate(raw_sorted, 1):
            parts = _split_route_key(key)
            label = humanize_route(*parts) if parts else key
            h = store.get(key)
            lines.append(
                f"{i}. {label} — {_price_label(h, price)} — "
                f"Fonte: {_source_name(h)} — Cabine: não confirmada"
            )
    else:
        lines.append("• Nenhum sinal bruto no histórico.")
    lines.append("")
    lines.append("🚧 Próximos gargalos para alerta confirmado")
    lines.append(
        "• Habilitar/garantir fonte com cabine confirmada (Kiwi: "
        "KIWI_API_KEY configurado e com cobertura na rota)."
    )
    lines.append(
        "• Câmbio USD_BRL_RATE presente e válido para converter preços USD."
    )
    lines.append(
        "• Preço acima do piso de sanidade para a classe/trip "
        "(evita falso 'executiva barata')."
    )
    return "\n".join(lines)


def maybe_send_status(
    result: MonitorResult,
    store: PriceStore,
    state: StatusState,
    notifier: TelegramNotifier | None,
    state_path: Path,
    now: datetime | None = None,
    throttle_hours: int = 24,
) -> StatusDecision:
    now = now or datetime.now(timezone.utc)

    if notifier is None:
        return StatusDecision(action="skipped", reason="no_notifier")

    if state.last_report_at:
        try:
            last = datetime.fromisoformat(state.last_report_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(hours=throttle_hours):
                return StatusDecision(action="skipped", reason="throttled")
            reason = "window_elapsed"
        except ValueError:
            reason = "first_run"
    else:
        reason = "first_run"

    text = _build_message(result, store, now)
    ok = notifier.send(text)
    if not ok:
        return StatusDecision(action="failed", reason="telegram_send_failed")

    state.last_report_at = now.isoformat()
    state.save(state_path)
    return StatusDecision(action="sent", reason=reason)
