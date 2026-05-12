"""Relatório periódico de vida do robô via Telegram."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .airports import humanize_route
from .formatting import format_brl
from .monitor import MonitorResult
from .notifier import TelegramNotifier
from .regions import REGIONS
from .state import PriceStore


# Display-only: a região "Ásia" agrupa Ásia + Oriente Médio (DXB, DOH, ICN…).
# Renomeada na mensagem do Telegram para refletir isso, sem alterar `regions.py`.
REGION_DISPLAY_LABELS: dict[str, str] = {
    "Ásia": "Ásia/Oriente Médio",
}


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


def _format_top3_line(index: int, key: str, price: float) -> str:
    parts = _split_route_key(key)
    price_str = format_brl(price)
    if parts is None:
        return f"{index}. {key} — {price_str}"
    origin, destination = parts
    label = humanize_route(origin, destination)
    return f"{index}. {label} — {price_str}"


def _region_for_destination(destination: str) -> str | None:
    for region, codes in REGIONS.items():
        if destination in codes:
            return region
    return None


def _best_per_region(latest: list[tuple[str, float]]) -> list[tuple[str, str, float]]:
    """Para cada região conhecida, devolve (região, key, price) do menor preço."""
    by_region: dict[str, tuple[str, float]] = {}
    for key, price in latest:
        parts = _split_route_key(key)
        if parts is None:
            continue
        _, destination = parts
        region = _region_for_destination(destination)
        if region is None:
            continue
        current = by_region.get(region)
        if current is None or price < current[1]:
            by_region[region] = (key, price)

    out: list[tuple[str, str, float]] = []
    for region in REGIONS.keys():
        if region in by_region:
            key, price = by_region[region]
            out.append((region, key, price))
    return out


def _format_regional_line(region: str, key: str, price: float) -> str:
    parts = _split_route_key(key)
    price_str = format_brl(price)
    display_region = REGION_DISPLAY_LABELS.get(region, region)
    if parts is None:
        return f"• {display_region}: {key} — {price_str}"
    origin, destination = parts
    label = humanize_route(origin, destination)
    return f"• {display_region}: {label} — {price_str}"


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
            _format_top3_line(i + 1, key, price)
            for i, (key, price) in enumerate(top3)
        )
    else:
        top3_lines = "Sem histórico disponível ainda."

    regional = _best_per_region(latest)
    regional_block = ""
    if regional:
        regional_lines = "\n".join(
            _format_regional_line(region, key, price)
            for region, key, price in regional
        )
        regional_block = f"\n\n🌎 Melhor por região\n{regional_lines}"

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
        "💸 Top 3 menores preços atuais\n"
        f"{top3_lines}"
        f"{regional_block}\n\n"
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
