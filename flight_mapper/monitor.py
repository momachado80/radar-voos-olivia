"""Orquestrador principal: varre rotas, atualiza histórico e dispara alertas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .cycle_state import CycleState
from .detector import evaluate
from .notifier import TelegramNotifier
from .providers import FlightProvider
from .regions import Route, all_routes
from .state import PriceStore


@dataclass
class MonitorResult:
    scanned: int
    quotes_received: int
    alerts_sent: int
    notes: list[str]


class Monitor:
    def __init__(
        self,
        provider: FlightProvider,
        notifier: TelegramNotifier | None,
        store: PriceStore,
        cycle: CycleState | None = None,
        chunk_size: int = 8,
    ):
        self.provider = provider
        self.notifier = notifier
        self.store = store
        self.cycle = cycle
        self.chunk_size = chunk_size

    def run_once(self, routes: list[Route] | None = None) -> MonitorResult:
        routes = routes if routes is not None else all_routes()
        notes: list[str] = []
        quotes_received = 0
        alerts_sent = 0

        for route in routes:
            quote = self.provider.quote(route)
            if quote is None:
                notes.append(f"{route.origin}→{route.destination}: sem cotação")
                continue
            quotes_received += 1
            history = self.store.get(route.key)
            decision = evaluate(history, quote.price_brl)
            history.push(quote.price_brl)

            if decision.alert and self.notifier and decision.average is not None and decision.drop_pct is not None:
                ok = self.notifier.send_alert(quote, decision.average, decision.drop_pct)
                if ok:
                    history.last_alert_at = datetime.now(timezone.utc).isoformat()
                    history.last_alert_price = quote.price_brl
                    alerts_sent += 1
                    notes.append(f"{route.origin}→{route.destination}: ALERTA {decision.reason}")
                else:
                    notes.append(f"{route.origin}→{route.destination}: alerta falhou no envio")
            else:
                notes.append(f"{route.origin}→{route.destination}: {decision.reason}")

        self.store.save()
        return MonitorResult(
            scanned=len(routes),
            quotes_received=quotes_received,
            alerts_sent=alerts_sent,
            notes=notes,
        )

    def run_cycle(self) -> MonitorResult:
        if self.cycle is None:
            return self.run_once()
        all_ = all_routes()
        start, end = self.cycle.next_chunk(len(all_), self.chunk_size)
        chunk = all_[start:end]
        result = self.run_once(chunk)
        self.cycle.advance(len(all_), self.chunk_size)
        self.cycle.save()
        return result
