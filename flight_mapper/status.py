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
from .regions import Cabin, TripType
from .sanity import is_suspicious_price
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


def _format_raw_block(
    index: int, key: str, history: RouteHistory, price: float
) -> str:
    """Sinal bruto multilinha — painel de confiança. Nunca usa
    'Executiva' nem 'oportunidade'."""
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    label = humanize_route(*parts) if parts else key
    return (
        f"{index}. {label} — {price_str}\n"
        f"   Fonte: {_source_name(history)}\n"
        f"   Cabine: não confirmada\n"
        f"   Interpretação: pode ser econômica promocional ou tarifa "
        f"sem classe comprovada."
    )


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

    if result.quotes_received == 0:
        return (
            "⚠️ <b>Radar de Voos Olivia</b>\n"
            f"Último ciclo: {timestamp}\n"
            f"Retornou 0 cotações em {result.scanned} rotas escaneadas. "
            "Provider possivelmente sem ofertas cacheadas para as rotas/datas atuais. "
            "Próxima tentativa no próximo ciclo."
        )

    latest = _latest_prices(store)
    confirmed = sorted(
        (it for it in latest if _is_confirmed(store.get(it[0]))),
        key=lambda x: x[1],
    )[:3]
    raw = sorted(
        (it for it in latest if not _is_confirmed(store.get(it[0]))),
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

    if raw:
        raw_block = "\n".join(
            _format_raw_block(i + 1, key, store.get(key), price)
            for i, (key, price) in enumerate(raw)
        )
    else:
        raw_block = "• Nenhum sinal bruto de preço no momento."

    sources_block = _source_status_block(store, confirmed, raw)

    footer = (
        "ℹ️ Sem oportunidade dentro dos critérios de alerta agora."
        if result.alerts_sent == 0
        else f"🔥 {result.alerts_sent} alerta(s) enviado(s) neste ciclo."
    )

    return (
        "🛰️ <b>Radar de Voos Olivia — relatório diário</b>\n"
        f"Robô ativo. Último ciclo: {timestamp}\n\n"
        "📊 Ciclo recente\n"
        f"• Rotas escaneadas: {result.scanned}\n"
        f"• Cotações obtidas: {result.quotes_received}\n"
        f"• Alertas: {result.alerts_sent}\n\n"
        "📌 Oportunidades confirmadas\n"
        f"{confirmed_score_line}"
        f"{confirmed_lines}\n\n"
        "📡 Sinais brutos de preço\n"
        f"{raw_block}\n\n"
        f"{sources_block}\n\n"
        f"{footer}"
    )


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
