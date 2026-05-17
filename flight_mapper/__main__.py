"""CLI: python -m flight_mapper {scan|cycle|hot-scan|test|preview-messages|
calibrate-routes|simulate-thresholds|rank-routes|provider-health|audit-links|export-history}."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import diagnostics
from .config import Config
from .cycle_state import CycleState
from .detector import (
    CRITERION_AVERAGE_DROP,
    CRITERION_CEILING,
    LEVEL_EXCELLENT,
    LEVEL_GOOD,
    Decision,
)
from .formatting import format_brl
from .monitor import Monitor, MonitorResult
from .notifier import TelegramNotifier, format_alert
from .providers import KiwiTequilaProvider, MockProvider, Quote, TravelpayoutsProvider
from .regions import Cabin, Route
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


def _make_link_provider(config: Config, primary):
    """Provider auxiliar SÓ para validar/obter link comercial acionável.

    - Se o primário já é Kiwi: retorna None (Kiwi devolve link próprio).
    - Se KIWI_API_KEY está setado: instancia KiwiTequilaProvider como link_provider.
    - Caso contrário: None (sem cross-check; Travelpayouts puro = silêncio).
    """
    if isinstance(primary, KiwiTequilaProvider):
        return None
    if config.kiwi_api_key:
        return KiwiTequilaProvider(api_key=config.kiwi_api_key)
    return None


def cmd_scan(args: argparse.Namespace) -> int:
    config = Config.from_env()
    provider = _make_provider(config, args.mock)
    notifier = _make_notifier(config)
    store = PriceStore(config.history_path)
    link_provider = _make_link_provider(config, provider)
    monitor = Monitor(
        provider=provider, notifier=notifier, store=store, link_provider=link_provider,
    )
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
    link_provider = _make_link_provider(config, provider)
    monitor = Monitor(
        provider=provider, notifier=notifier, store=store, cycle=cycle,
        link_provider=link_provider,
    )
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
    link_provider = _make_link_provider(config, provider)
    monitor = Monitor(
        provider=provider, notifier=notifier, store=store, link_provider=link_provider,
    )
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

    # Aviasales foi bloqueado por completo (evidência real: redirecionamento
    # para experiência russa apesar do locale=en-us). Os exemplos abaixo usam
    # deep_link estilo Kiwi para demonstrar o caminho "alerta com link acionável".
    print("=" * 60)
    print("1. ALERTA EXCELENTE com link funcional (Kiwi)")
    print("=" * 60)
    quote_excellent = Quote(
        route=Route("GRU", "CDG", "Europa"),
        price_brl=2300.0,
        deep_link="https://www.kiwi.com/deep/GRU-CDG-2026-06-15-2026-06-22",
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="kiwi",
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
    )
    decision_excellent = Decision(
        alert=True,
        reason="preço R$ 2300 <= alvo R$ 2400 (nível excellent)",
        criterion=CRITERION_CEILING,
        threshold=2400.0,
        level=LEVEL_EXCELLENT,
        score=94,
    )
    print(format_alert(quote_excellent, decision_excellent, priority=True))

    print()
    print("=" * 60)
    print("2. ALERTA BOM com link funcional (Kiwi)")
    print("=" * 60)
    quote_good = Quote(
        route=Route("GRU", "LHR", "Europa"),
        price_brl=1900.0,
        deep_link="https://www.kiwi.com/deep/GRU-LHR-2026-07-10-2026-07-17",
        departure_date="2026-07-10",
        return_date="2026-07-17",
        source="kiwi",
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
    )
    decision_good = Decision(
        alert=True,
        reason="preço R$ 1900 <= alvo R$ 2000 (nível good)",
        criterion=CRITERION_CEILING,
        threshold=2000.0,
        level=LEVEL_GOOD,
        score=81,
    )
    print(format_alert(quote_good, decision_good, priority=True))

    print()
    print("=" * 60)
    print("3. ALERTA OBSERVAR (formato compacto, como entraria no relatório)")
    print("=" * 60)
    print(
        "Alertas com score 60-74 ainda podem disparar pelo detector (ceiling/legacy),\n"
        "mas tipicamente aparecem como sinal de acompanhar, não como urgência."
    )
    print()
    print("👁️ OBSERVAR — Score 64/100 — São Paulo → Amsterdã (GRU → AMS) — R$ 2.150")
    print("  alvo R$ 2.500 · 🛒 Travelpayouts (cache) · 🕒 12/05 14:00 BRT")

    print()
    print("=" * 60)
    print("4. RELATÓRIO DIÁRIO com last_quote acionável (top-3 + watchlists + score médio)")
    print("=" * 60)
    samples_with_lq = {
        "GRU-MIA-business": (1207.0, "https://www.kiwi.com/deep/GRU-MIA-2026-06-15"),
        "GRU-ORD-business": (1631.0, "https://www.kiwi.com/deep/GRU-ORD-2026-06-15"),
        "GRU-LHR-business": (1794.0, "https://www.kiwi.com/deep/GRU-LHR-2026-06-15"),
        "GRU-CDG-business": (2483.0, "https://www.kiwi.com/deep/GRU-CDG-2026-06-15"),
        "GRU-LIS-business": (1987.0, "https://www.kiwi.com/deep/GRU-LIS-2026-06-15"),
        "GRU-DXB-business": (2798.0, "https://www.kiwi.com/deep/GRU-DXB-2026-06-15"),
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
    print("4b. ALERTA com link Aviasales (bloqueado pelo is_actionable_url)")
    print("=" * 60)
    from .airports import is_actionable_url as _is_actionable
    aviasales_url = (
        "https://search.aviasales.com/flights/?origin_iata=GRU"
        "&destination_iata=CDG&depart_date=2026-06-15&return_date=2026-06-22"
        "&adults=1&children=0&infants=0&trip_class=1&currency=usd"
        "&locale=en-us&marker_locale=en-us"
    )
    print(f"URL Aviasales: {aviasales_url}")
    print(f"is_actionable_url({aviasales_url[:50]}...) = {_is_actionable(aviasales_url)}")
    print(
        "→ Monitor descarta alerta com este link (count non_actionable_links_skipped += 1).\n"
        "  Travelpayouts agora retorna deep_link=None — sem link, sem alerta enviado."
    )
    print()
    print("Kiwi (caminho preferido quando KIWI_API_KEY estiver setado):")
    kiwi_url = "https://www.kiwi.com/deep/GRU-CDG-2026-06-15"
    print(f"  URL: {kiwi_url}")
    print(f"  is_actionable_url() = {_is_actionable(kiwi_url)}")

    print()
    print("=" * 60)
    print("4c. ALERTA MANUAL SEM LINK COMERCIAL (manual_purchase_fallback)")
    print("=" * 60)
    print(
        "Quando Travelpayouts detecta oportunidade mas Kiwi não está disponível\n"
        "(KIWI_API_KEY ausente ou Kiwi sem cobertura para a rota), o Monitor\n"
        "envia alerta manual sem hyperlink, com instrução de pesquisa manual."
    )
    print()
    quote_manual = Quote(
        route=Route("GRU", "LHR", "Europa"),
        price_brl=1878.0,
        deep_link=None,
        departure_date="2026-11-10",
        return_date="2026-11-17",
        source="manual_purchase",
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
    )
    decision_manual = Decision(
        alert=True,
        reason="preço R$ 1878 <= alvo R$ 2000 (nível good)",
        criterion=CRITERION_CEILING,
        threshold=2000.0,
        level=LEVEL_GOOD,
        score=65,
    )
    print(format_alert(quote_manual, decision_manual, priority=True))

    print()
    print("=" * 60)
    print("4d. ALERTA BLOQUEADO — cabine não confirmada")
    print("=" * 60)
    print(
        "Travelpayouts não confirma a classe (o endpoint ignora trip_class).\n"
        "O Monitor BLOQUEIA o alerta forte e NÃO envia Telegram. Nota gerada:\n"
        "  GRU→MIA: alerta bloqueado: cabine não confirmada (cabin=unknown)\n"
        "Mesmo se o notifier fosse chamado (não é), o título seria honesto:"
    )
    print()
    quote_unconfirmed = Quote(
        route=Route("GRU", "MIA", "EUA"),
        price_brl=1276.0,
        deep_link=None,
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="travelpayouts",
        amount=232.0,
        currency="USD",
        amount_brl_estimated=1276.0,
        fx_rate=5.5,
        cabin=Cabin.UNKNOWN,
        cabin_confirmed=False,
    )
    decision_unconfirmed = Decision(
        alert=True,
        reason="preço R$ 1276 <= alvo (nível excellent)",
        criterion=CRITERION_CEILING,
        threshold=1300.0,
        level=LEVEL_EXCELLENT,
        score=90,
    )
    print(format_alert(quote_unconfirmed, decision_unconfirmed, priority=True))
    print()
    print(
        "→ Titulo honesto: sem nivel forte e sem rotulo de classe. "
        "O suspeito 'US$ 232 GRU-MIA' nao passa como oportunidade executiva."
    )

    print()
    print("=" * 60)
    print("5. OPERATIONAL SUMMARY (FASE 3 — preview do PR seguinte)")
    print("=" * 60)
    print(
        "FASE 3 (operational_summary.json) está deferida para um PR focado em\n"
        "evitar race entre flight-mapper e flight-hot-scan. Quando entrar,\n"
        "data/operational_summary.json conterá algo como:"
    )
    print()
    import json as _json
    print(_json.dumps(
        {
            "generated_at": "2026-05-12T17:00:00+00:00",
            "kind": "hot-scan",
            "counters": {
                "scanned": 10,
                "quotes_received": 9,
                "alerts_sent": 0,
                "stale_quotes_skipped": 0,
                "non_actionable_links_skipped": 0,
                "actionable_links_generated": 0,
            },
            "last_alert_at": None,
            "last_alert_route": None,
        },
        indent=2,
        ensure_ascii=False,
    ))
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


def cmd_preview_links(args: argparse.Namespace) -> int:
    """Imprime variantes de URL de busca para teste manual no navegador.

    Não usa rede, não envia Telegram, não toca data/.
    """
    from .airports import build_search_url

    origin = "GRU"
    destination = "LHR"
    depart = "2026-07-10"
    ret = "2026-07-17"

    print("=" * 78)
    print(f"Variantes de URL Aviasales para {origin} → {destination}  "
          f"({depart} → {ret})")
    print("Abra cada link no navegador e verifique idioma/moeda da página.")
    print("=" * 78)
    print()

    print("A) URL atual (default em produção neste PR — en-us + usd):")
    print("   " + build_search_url(origin, destination, depart, ret))
    print()

    print("B) Variação: locale=en-us + marker_locale=en-us + currency=usd")
    print("   (idêntica à atual neste PR — mostrada explicitamente para comparação)")
    print("   " + build_search_url(origin, destination, depart, ret,
                                   locale="en-us", currency="usd"))
    print()

    print("C) Variação: locale=en-gb + marker_locale=en-gb + currency=usd")
    print("   " + build_search_url(origin, destination, depart, ret,
                                   locale="en-gb", currency="usd"))
    print()

    print("D) Variação: locale=en + marker_locale=en + currency=usd")
    print("   " + build_search_url(origin, destination, depart, ret,
                                   locale="en", currency="usd"))
    print()

    print("E) Variação alternativa de domínio")
    print("   (aviasales.com/searches/new não tem documentação clara; intencionalmente")
    print("    omitido para não criar URL frágil. Se preferir, valide manualmente.)")
    print()

    print("-" * 78)
    print("Roteiro:")
    print("- Se A/B abrirem em inglês com USD: o default deste PR está OK.")
    print("- Se ainda servir russo: Aviasales não respeita locale aqui;")
    print("  próximo PR deve priorizar Kiwi deep_link (quando KIWI_API_KEY presente).")
    return 0


# ============================================================
# Calibration & Diagnostics (read-only; no provider, no telegram, no HTTP)
# ============================================================


_CURRENCY_DISCLAIMER = (
    "⚠️ MOEDA: valores refletem a moeda de origem registrada no histórico. "
    "Entradas legadas (anteriores à correção de moeda) vêm do Travelpayouts "
    "em USD e NÃO são BRL comprovado — trate como estimativa. "
    "Após a correção, cada cotação registra currency/amount_brl_estimated/"
    "fx_rate; defina USD_BRL_RATE para conversão confiável."
)


def _print_currency_disclaimer() -> None:
    print(_CURRENCY_DISCLAIMER)
    print()


def _load_diag_store() -> "PriceStore | None":
    """Carrega o store. Devolve None se vazio (sem rotas no histórico)."""
    config = Config.from_env()
    store = PriceStore(config.history_path)
    if not list(store.keys()):
        return None
    _print_currency_disclaimer()
    return store


def _empty_history_msg() -> str:
    return (
        "Sem dados suficientes em data/price_history.json — "
        "rode `python -m flight_mapper cycle` ou aguarde o cron para acumular histórico."
    )


def _fmt_brl(value):
    if value is None:
        return "—"
    return format_brl(value)


def cmd_calibrate_routes(args: argparse.Namespace) -> int:
    store = _load_diag_store()
    if store is None:
        print(_empty_history_msg())
        return 0
    stats = diagnostics.all_stats(store)
    headers = ["ROTA", "SAMPLES", "LATEST", "MIN", "AVG", "P10", "P25", "EXCELLENT", "GOOD", "SUGGEST_EXC", "SUGGEST_GOOD"]
    print(" | ".join(headers))
    print("-" * 140)
    for s in stats:
        sugg_e, sugg_g = diagnostics.suggest_thresholds(s)
        print(
            " | ".join(
                [
                    f"{s.key:24s} {s.route_label}",
                    f"{s.samples:3d}",
                    _fmt_brl(s.latest).rjust(10),
                    _fmt_brl(s.min_price).rjust(10),
                    _fmt_brl(s.avg).rjust(10),
                    _fmt_brl(s.p10).rjust(10),
                    _fmt_brl(s.p25).rjust(10),
                    _fmt_brl(s.excellent_brl).rjust(10),
                    _fmt_brl(s.good_brl).rjust(10),
                    _fmt_brl(sugg_e).rjust(10),
                    _fmt_brl(sugg_g).rjust(10),
                ]
            )
        )
    return 0


def cmd_simulate_thresholds(args: argparse.Namespace) -> int:
    store = _load_diag_store()
    if store is None:
        print(_empty_history_msg())
        return 0
    stats = diagnostics.all_stats(store)
    scenarios = [
        ("current", dict(factor=1.0)),
        ("stricter -10%", dict(factor=0.9)),
        ("looser +10%", dict(factor=1.1)),
        ("p10 cutoff", dict(use_p10=True)),
        ("p25 cutoff", dict(use_p25=True)),
    ]
    print("Simulação de alertas sobre LATEST de cada rota.")
    print("stricter -10% = teto menor → menos alertas. looser +10% = teto maior → mais alertas.")
    print()
    print(f"{'SCENARIO':<20} {'TOTAL':>8} {'EXCELLENT':>10} {'GOOD_ONLY':>10} {'SKIPPED':>10}")
    print("-" * 64)
    for name, kwargs in scenarios:
        result = diagnostics.simulate_alerts(stats, **kwargs)
        print(
            f"{name:<20} {result['total']:>8} {result['excellent']:>10} "
            f"{result['good_only']:>10} {result['skipped_no_threshold']:>10}"
        )
    return 0


def cmd_rank_routes(args: argparse.Namespace) -> int:
    store = _load_diag_store()
    if store is None:
        print(_empty_history_msg())
        return 0
    stats = diagnostics.all_stats(store)
    ranked = diagnostics.ranked_routes(stats, top_n=args.top)
    print(
        "Rank de rotas promissoras (rank_score 0-100, separado do opportunity score do alerta)."
    )
    print()
    for i, (s, score) in enumerate(ranked, 1):
        link_flag = "link ✓" if s.last_quote_actionable else "sem link"
        wl_flag = f"watchlist: {s.watchlist_label}" if s.watchlist_label else "sem watchlist"
        hot_flag = "hot" if s.is_hot else ""
        latest_str = _fmt_brl(s.latest)
        good_str = _fmt_brl(s.good_brl)
        flags = ", ".join(filter(None, [link_flag, wl_flag, hot_flag, f"{s.samples} amostras"]))
        print(
            f"{i:2d}. {s.key:24s} {s.route_label} — rank_score {score}/100 — "
            f"{latest_str} (alvo good {good_str}) — {flags}"
        )
    return 0


def cmd_provider_health(args: argparse.Namespace) -> int:
    store = _load_diag_store()
    if store is None:
        print(_empty_history_msg())
        return 0
    stats = diagnostics.all_stats(store)
    health = diagnostics.provider_health(stats)
    print("Cobertura histórica de cotações (snapshot do que está em data/price_history.json,")
    print("não consulta o provider em tempo real).")
    print()
    print(f"Total rotas em histórico:    {health['total_routes']}")
    print(f"Com cotação (>=1 amostra):   {health['with_quote']}")
    print(f"Poucas amostras (<5):        {health['few_samples']}")
    print(f"Com last_quote:              {health['with_last_quote']}")
    print(f"Sem last_quote:              {health['without_last_quote']}")
    print(
        f"Link acionável:              {health['actionable_links']}"
        f"/{health['with_last_quote']} ({health['actionable_pct']:.1f}%)"
    )
    return 0


def cmd_audit_links(args: argparse.Namespace) -> int:
    store = _load_diag_store()
    if store is None:
        print(_empty_history_msg())
        return 0
    stats = diagnostics.all_stats(store)
    audit = diagnostics.audit_links(stats)
    print(f"Total last_quote presentes:  {audit['total_with_lq']}")
    print(f"Acionáveis:                  {audit['actionable']}")
    print(f"Não acionáveis:              {audit['non_actionable']}")
    print(f"URLs antigas (/search/...):  {len(audit['legacy_urls'])}")
    if audit["legacy_urls"]:
        print()
        print("Rotas com URL antiga (corrigir se persistir):")
        for s in audit["legacy_urls"]:
            print(f"  {s.key}: {s.deep_link}")
    if audit["no_link"]:
        print()
        print(f"Top rotas sem link funcional ({min(5, len(audit['no_link']))} mostradas):")
        for s in audit["no_link"][:5]:
            print(f"  {s.key} ({s.route_label})")
    return 0


def cmd_export_history(args: argparse.Namespace) -> int:
    if not args.out:
        print("Use --out /path/to/history.csv")
        return 0
    store = _load_diag_store()
    if store is None:
        print(_empty_history_msg())
        return 0
    stats = diagnostics.all_stats(store)
    out_path = Path(args.out)
    n = diagnostics.export_csv(stats, out_path)
    print(f"exported {n} routes to {out_path}")
    return 0


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

    p_preview_links = sub.add_parser(
        "preview-links",
        help="Imprime variantes de URL Aviasales para teste manual no navegador.",
    )
    p_preview_links.set_defaults(func=cmd_preview_links)

    # ----- Calibration & Diagnostics (read-only) -----
    p_cal = sub.add_parser(
        "calibrate-routes",
        help="Stats por rota + sugestão de thresholds (leitura do histórico).",
    )
    p_cal.set_defaults(func=cmd_calibrate_routes)

    p_sim = sub.add_parser(
        "simulate-thresholds",
        help="Simula quantos alertas teriam ocorrido em diferentes cenários de teto.",
    )
    p_sim.set_defaults(func=cmd_simulate_thresholds)

    p_rank = sub.add_parser(
        "rank-routes",
        help="Lista rotas mais promissoras (rank_score, não confundir com alert score).",
    )
    p_rank.add_argument("--top", type=int, default=10, help="Top N rotas (default 10)")
    p_rank.set_defaults(func=cmd_rank_routes)

    p_phealth = sub.add_parser(
        "provider-health",
        help="Cobertura histórica de cotações (snapshot do data/, sem consultar provider).",
    )
    p_phealth.set_defaults(func=cmd_provider_health)

    p_audit = sub.add_parser(
        "audit-links",
        help="Auditoria de links em last_quote (acionáveis, antigos, ausentes).",
    )
    p_audit.set_defaults(func=cmd_audit_links)

    p_export = sub.add_parser(
        "export-history",
        help="Exporta histórico em CSV (requer --out PATH).",
    )
    p_export.add_argument("--out", default=None, help="Caminho do CSV de saída")
    p_export.set_defaults(func=cmd_export_history)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
