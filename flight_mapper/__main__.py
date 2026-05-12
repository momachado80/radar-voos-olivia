"""CLI: python -m flight_mapper {scan|cycle|hot-scan|test|preview-messages}."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .config import Config
from .cycle_state import CycleState
from .detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    LEVEL_GOOD,
    Decision,
)
from .monitor import Monitor, MonitorResult
from .notifier import TelegramNotifier, format_alert
from .providers import KiwiTequilaProvider, MockProvider, Quote, TravelpayoutsProvider
from .regions import Route
from .state import PriceStore
from .status import StatusState, _build_message, maybe_send_status
from .thresholds import hot_routes


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


def cmd_hot_scan(args: argparse.Namespace) -> int:
    """Varre apenas as rotas quentes (HOT_ROUTE_KEYS).

    Reusa o pipeline atual do `Monitor.run_once`: ceiling primeiro,
    depois detector legado de queda vs média. Salva estado normalmente.
    """
    config = Config.from_env()
    provider = _make_provider(config, args.mock)
    notifier = _make_notifier(config)
    store = PriceStore(config.history_path)
    routes = hot_routes()
    monitor = Monitor(provider=provider, notifier=notifier, store=store)
    result = monitor.run_once(routes)
    print(
        f"hot-scan scanned={result.scanned} "
        f"quotes={result.quotes_received} alerts={result.alerts_sent}"
    )
    for note in result.notes:
        print(f"  {note}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Imprime mensagens-exemplo no terminal. Sem rede, sem secrets, sem `data/`."""
    import tempfile
    from pathlib import Path

    from .airports import build_search_url

    print("=" * 60)
    print("1. ALERTA EXCELENTE com link funcional")
    print("=" * 60)
    quote_excellent = Quote(
        route=Route("GRU", "CDG", "Europa"),
        price_brl=2300.0,
        deep_link=build_search_url("GRU", "CDG", "2026-06-15", "2026-06-22"),
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="travelpayouts",
    )
    decision_excellent = Decision(
        alert=True,
        reason="preço R$ 2300 <= alvo R$ 2400 (nível excellent)",
        criterion=CRITERION_CEILING,
        threshold=2400.0,
        level=LEVEL_EXCELLENT,
    )
    print(format_alert(quote_excellent, decision_excellent, priority=True))

    print()
    print("=" * 60)
    print("2. ALERTA BOM com link funcional")
    print("=" * 60)
    quote_good = Quote(
        route=Route("GRU", "LHR", "Europa"),
        price_brl=1900.0,
        deep_link=build_search_url("GRU", "LHR", "2026-07-10", "2026-07-17"),
        departure_date="2026-07-10",
        return_date="2026-07-17",
        source="travelpayouts",
    )
    decision_good = Decision(
        alert=True,
        reason="preço R$ 1900 <= alvo R$ 2000 (nível good)",
        criterion=CRITERION_CEILING,
        threshold=2000.0,
        level=LEVEL_GOOD,
    )
    print(format_alert(quote_good, decision_good, priority=True))

    print()
    print("=" * 60)
    print("3. ALERTA QUE SERIA DESCARTADO por link não acionável")
    print("=" * 60)
    quote_broken = Quote(
        route=Route("GRU", "JFK", "EUA"),
        price_brl=1700.0,
        deep_link="https://www.aviasales.com/search/GRUJFK",  # link quebrado
        departure_date="2026-08-05",
        return_date="2026-08-15",
        source="travelpayouts",
    )
    decision_broken = Decision(
        alert=True,
        reason="preço R$ 1700 <= alvo R$ 1800 (nível excellent)",
        criterion=CRITERION_CEILING,
        threshold=1800.0,
        level=LEVEL_EXCELLENT,
    )
    print("(em produção, monitor descarta este alerta e loga 'link não acionável')")
    print(format_alert(quote_broken, decision_broken, priority=True))

    print()
    print("=" * 60)
    print("4. RELATÓRIO DIÁRIO com last_quote acionável (top-3 + regional com link)")
    print("=" * 60)
    samples_with_lq = {
        "GRU-MIA-business": (1207.0, build_search_url("GRU", "MIA", "2026-06-15", "2026-06-22")),
        "GRU-ORD-business": (1631.0, build_search_url("GRU", "ORD", "2026-06-15", "2026-06-22")),
        "GRU-LHR-business": (1794.0, build_search_url("GRU", "LHR", "2026-06-15", "2026-06-22")),
        "GRU-CDG-business": (2483.0, build_search_url("GRU", "CDG", "2026-06-15", "2026-06-22")),
        "GRU-LIS-business": (1987.0, build_search_url("GRU", "LIS", "2026-06-15", "2026-06-22")),
        "GRU-DXB-business": (2798.0, build_search_url("GRU", "DXB", "2026-06-15", "2026-06-22")),
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        seeded = PriceStore(Path(tmpdir) / "preview.json")
        now = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
        for key, (price, link) in samples_with_lq.items():
            history = seeded.get(key)
            history.push(price)
            o, d, _ = key.split("-")
            history.last_quote = {
                "price_brl": price,
                "origin": o,
                "destination": d,
                "departure_date": "2026-06-15",
                "return_date": "2026-06-22",
                "source": "travelpayouts",
                "deep_link": link,
                "detected_at": now.isoformat(),
                "actionable_url": True,
                "cabin": "business",
                "provider_note": None,
            }
        fake_result = MonitorResult(scanned=12, quotes_received=6, alerts_sent=0, notes=[])
        print(_build_message(fake_result, seeded, now))

    print()
    print("=" * 60)
    print("5. RELATÓRIO DIÁRIO SEM last_quote (sem links)")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmpdir:
        seeded2 = PriceStore(Path(tmpdir) / "preview2.json")
        for key, (price, _) in samples_with_lq.items():
            seeded2.get(key).push(price)  # sem last_quote
        fake_result2 = MonitorResult(scanned=12, quotes_received=6, alerts_sent=0, notes=[])
        print(_build_message(fake_result2, seeded2, datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)))
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

    p_hot = sub.add_parser(
        "hot-scan",
        help="Varre apenas as rotas quentes (HOT_ROUTE_KEYS)",
    )
    p_hot.add_argument("--mock", action="store_true", help="Força MockProvider")
    p_hot.set_defaults(func=cmd_hot_scan)

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
