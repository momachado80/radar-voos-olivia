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
from .score import compute_opportunity_score
from .state import PriceStore, RouteHistory
from .thresholds import HOT_ROUTE_KEYS, levels_for, scaled_levels
from .watchlists import Watchlist, best_per_watchlist


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


def _format_top3_line(
    index: int, key: str, history: RouteHistory, price: float, link: str | None = None
) -> str:
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    if parts is None:
        return f"{index}. {key} — {price_str}"
    origin, destination = parts
    label = humanize_route(origin, destination)
    base = f"{index}. {label} — {price_str}"
    if link:
        return f'{base} — 🔎 <a href="{link}">Conferir busca</a>'
    return base


def _format_watchlist_line(
    watchlist: Watchlist,
    key: str,
    history: RouteHistory,
    price: float,
    link: str | None = None,
) -> str:
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    if parts is None:
        return f"• {watchlist.label}: {key} — {price_str}"
    origin, destination = parts
    label = humanize_route(origin, destination)
    base = f"• {watchlist.label}: {label} — {price_str}"
    if link:
        return f'{base} — 🔎 <a href="{link}">Conferir busca</a>'
    return base


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
    top3 = sorted(latest, key=lambda x: x[1])[:3]
    if top3:
        top3_lines = "\n".join(
            _format_top3_line(
                i + 1,
                key,
                store.get(key),
                price,
                link=_actionable_link_from_history(
                    store.get(key), *(_split_route_key(key) or ("", ""))
                ),
            )
            for i, (key, price) in enumerate(top3)
        )
        avg_score = _compute_average_score(store, [k for k, _ in top3])
        avg_score_line = (
            f"⭐ Score médio do Top 3: {avg_score}/100\n" if avg_score is not None else ""
        )
    else:
        top3_lines = "Sem histórico disponível ainda."
        avg_score_line = ""

    watchlist_best = best_per_watchlist(store)
    watchlist_block = ""
    if watchlist_best:
        watchlist_lines = "\n".join(
            _format_watchlist_line(
                wl,
                key,
                store.get(key),
                price,
                link=_actionable_link_from_history(
                    store.get(key), *(_split_route_key(key) or ("", ""))
                ),
            )
            for wl, key, price in watchlist_best
        )
        watchlist_block = f"\n\n📌 Melhores oportunidades monitoradas\n{watchlist_lines}"

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
        f"{avg_score_line}"
        "💸 Top 3 menores preços atuais\n"
        f"{top3_lines}"
        f"{watchlist_block}\n\n"
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
