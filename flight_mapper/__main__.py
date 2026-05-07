"""CLI: python -m flight_mapper {scan|cycle|test}."""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .cycle_state import CycleState
from .monitor import Monitor
from .notifier import TelegramNotifier
from .providers import KiwiTequilaProvider, MockProvider, TravelpayoutsProvider
from .state import PriceStore


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
