"""CLI: python -m flight_mapper {scan|cycle|test|preview-messages}."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .config import Config
from .cycle_state import CycleState
from .detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    Decision,
)
from .monitor import Monitor, MonitorResult
from .notifier import TelegramNotifier, format_alert
from .providers import KiwiTequilaProvider, MockProvider, Quote, TravelpayoutsProvider
from .regions import Route
from .state import PriceStore
from .status import StatusState, _build_message, maybe_send_status


def _make_provider(config: Config, use_mock: bool):
    if use_mock:
        return MockProvider()
    if config.travelpayouts_token:
        return TravelpayoutsProvider(token=config.travelpayouts_token)
    if config.kiwi_api_key:
        return KiwiTequilaProvider(api_key=config.kiwi_api_key)
    print(
        "Sem TRAVELPAYOUTS_TOKEN nem KIWI_API_KEY — usando MockProvider",
        file=sys.stderr,
    )
    return MockProvider()


def _make_notifier(config: Config) -> TelegramNotifier | None:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return None
    return TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)


def cmd_scan(args: argparse.Namespace) -> int:
    config = Config.from_env()
    provider = _make_provider(config, args.mock)
    notifier = _make_notifier(config)
    store = PriceStore(config.history_path)
    monitor = Monitor(provider=provider, notifier=notifier, store=store)
    result = monitor.run_once()
    print(f"scanned={result.scanned} quotes={result.quotes_received} alerts={result.alerts_sent}")
    for note in result.notes:
        print(f"  {note}")
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    config = Config.from_env()
    provider = _make_provider(config, args.mock)
    notifier = _make_notifier(config)
    store = PriceStore(config.history_path)
    cycle = CycleState.load(config.cycle_path)
    monitor = Monitor(provider=provider, notifier=notifier, store=store, cycle=cycle)
    result = monitor.run_cycle()
    print(f"cycle scanned={result.scanned} quotes={result.quotes_received} alerts={result.alerts_sent}")
    for note in result.notes:
        print(f"  {note}")

    status_state = StatusState.load(config.status_path)
    decision = maybe_send_status(
        result=result,
        store=store,
        state=status_state,
        notifier=notifier,
        state_path=config.status_path,
        throttle_hours=config.status_throttle_hours,
    )
    print(f"status action={decision.action} reason={decision.reason}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Imprime mensagens-exemplo no terminal. Sem rede, sem secrets, sem `data/`."""
    import tempfile
    from pathlib import Path

    print("=" * 60)
    print("ALERTA POR PREÇO-ALVO (ceiling)")
    print("=" * 60)
    quote_ceiling = Quote(
        route=Route("GRU", "CDG", "Europa"),
        price_brl=2350.0,
        deep_link="https://www.aviasales.com/search/GRU1506CDG22061",
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="travelpayouts",
    )
    decision_ceiling = Decision(
        alert=True,
        reason="preço R$ 2350 <= teto R$ 2400",
        criterion=CRITERION_CEILING,
        threshold=2400.0,
    )
    print(format_alert(quote_ceiling, decision_ceiling, priority=True))

    print()
    print("=" * 60)
    print("ALERTA POR QUEDA VS MÉDIA (legado)")
    print("=" * 60)
    quote_drop = Quote(
        route=Route("GRU", "LHR", "Europa"),
        price_brl=1700.0,
        deep_link="https://www.aviasales.com/search/GRU1007LHR17071",
        departure_date="2026-07-10",
        return_date="2026-07-17",
        source="travelpayouts",
    )
    decision_drop = Decision(
        alert=True,
        reason="queda de 8% vs média histórica",
        average=1855.0,
        drop_pct=0.083,
        criterion=CRITERION_AVERAGE_DROP,
    )
    print(format_alert(quote_drop, decision_drop, priority=True))

    print()
    print("=" * 60)
    print("RELATÓRIO DIÁRIO (top-3 + melhor por região)")
    print("=" * 60)
    samples = {
        "GRU-MIA-business": 1207.0,
        "GRU-ORD-business": 1631.0,
        "GRU-LHR-business": 1794.0,
        "GRU-CDG-business": 2483.0,
        "GRU-LIS-business": 1987.0,
        "GRU-DXB-business": 2798.0,
        "GRU-NRT-business": 3999.0,
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        seeded = PriceStore(Path(tmpdir) / "preview.json")
        for key, price in samples.items():
            seeded.get(key).push(price)
        fake_result = MonitorResult(
            scanned=12, quotes_received=7, alerts_sent=0, notes=[]
        )
        now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
        print(_build_message(fake_result, seeded, now))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    config = Config.from_env()
    notifier = _make_notifier(config)
    if notifier is None:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes", file=sys.stderr)
        return 2
    ok = notifier.send("✅ Radar de Voos Olivia conectado com sucesso")
    print("ok" if ok else "falha no envio")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flight_mapper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Varredura completa de todas as rotas")
    p_scan.add_argument("--mock", action="store_true", help="Força MockProvider")
    p_scan.set_defaults(func=cmd_scan)

    p_cycle = sub.add_parser("cycle", help="Varredura do próximo chunk de rotas")
    p_cycle.add_argument("--mock", action="store_true", help="Força MockProvider")
    p_cycle.set_defaults(func=cmd_cycle)

    p_test = sub.add_parser("test", help="Smoke test do canal Telegram")
    p_test.set_defaults(func=cmd_test)

    p_preview = sub.add_parser(
        "preview-messages",
        help="Imprime mensagens-exemplo no terminal (sem rede, sem secrets)",
    )
    p_preview.set_defaults(func=cmd_preview)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
