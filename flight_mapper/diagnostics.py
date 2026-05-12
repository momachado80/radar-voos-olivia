"""Diagnostics offline para calibração e auditoria do radar.

Leitura pura do `PriceStore`. **Nenhuma função aqui chama provider,
faz HTTP, instancia TelegramNotifier ou altera estado**. Todas as
operações são read-only sobre o histórico em memória.

Os CLIs em `__main__.py` consomem essas funções e imprimem em stdout.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .airports import humanize_route, is_actionable_url
from .state import PriceStore, RouteHistory
from .thresholds import HOT_ROUTE_KEYS, levels_for
from .watchlists import WATCHLISTS, Watchlist


def percentile(values: list[float], p: float) -> float | None:
    """Percentil simples por interpolação por índice (estilo NumPy 'lower').

    Retorna None para lista vazia. Para n=1, devolve o único valor.
    p deve ser 0..100.
    """
    if not values:
        return None
    if not 0 <= p <= 100:
        raise ValueError("percentile must be between 0 and 100")
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
    return sorted_vals[idx]


@dataclass
class RouteStats:
    key: str
    origin: str
    destination: str
    route_label: str
    samples: int
    latest: float | None
    min_price: float | None
    avg: float | None
    p10: float | None
    p25: float | None
    p50: float | None
    last_quote_present: bool
    last_quote_actionable: bool
    deep_link: str | None
    source: str | None
    detected_at: str | None
    watchlist_label: str | None
    excellent_brl: float | None
    good_brl: float | None
    is_hot: bool


def _split_key(key: str) -> tuple[str, str]:
    parts = key.split("-")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return "", ""


def _watchlist_for(key: str) -> Watchlist | None:
    for wl in WATCHLISTS:
        if key in wl.route_keys:
            return wl
    return None


def stats_for(key: str, history: RouteHistory) -> RouteStats:
    origin, destination = _split_key(key)
    label = humanize_route(origin, destination) if origin and destination else key
    lq = history.last_quote if isinstance(history.last_quote, dict) else None
    deep_link = lq.get("deep_link") if lq else None
    actionable = is_actionable_url(deep_link)
    source = lq.get("source") if lq else None
    detected_at = lq.get("detected_at") if lq else None
    levels = levels_for(key) or {}
    wl = _watchlist_for(key)
    return RouteStats(
        key=key,
        origin=origin,
        destination=destination,
        route_label=label,
        samples=len(history.prices),
        latest=history.prices[-1] if history.prices else None,
        min_price=min(history.prices) if history.prices else None,
        avg=history.average,
        p10=percentile(history.prices, 10),
        p25=percentile(history.prices, 25),
        p50=percentile(history.prices, 50),
        last_quote_present=lq is not None,
        last_quote_actionable=actionable,
        deep_link=deep_link,
        source=source,
        detected_at=detected_at,
        watchlist_label=wl.label if wl else None,
        excellent_brl=levels.get("excellent_brl"),
        good_brl=levels.get("good_brl"),
        is_hot=key in HOT_ROUTE_KEYS,
    )


def all_stats(store: PriceStore) -> list[RouteStats]:
    return [stats_for(k, store.get(k)) for k in sorted(store.keys())]


def suggest_thresholds(stats: RouteStats) -> tuple[int | None, int | None]:
    """Sugere (excellent_brl, good_brl) com base no histórico, arredondado a R$ 50.

    Conservador: excellent ≈ p10, good ≈ p25.
    Requer >= 5 amostras; senão devolve (None, None).
    """
    if stats.samples < 5 or stats.p10 is None or stats.p25 is None:
        return None, None
    excellent = max(50, int(round(stats.p10 / 50.0) * 50))
    good = max(50, int(round(stats.p25 / 50.0) * 50))
    return excellent, good


def simulate_alerts(
    stats_list: list[RouteStats],
    *,
    factor: float = 1.0,
    use_p10: bool = False,
    use_p25: bool = False,
) -> dict:
    """Simula quantas rotas teriam alerta com o cenário pedido.

    - factor: multiplica excellent_brl / good_brl atuais (1.0=current, 0.9=stricter -10%, 1.1=looser +10%).
    - use_p10: usa p10 da própria rota como teto único (substitui factor).
    - use_p25: usa p25 da própria rota como teto único.

    Retorna dict com:
      excellent: rotas com latest <= excellent_brl ajustado
      good_only: rotas que disparam por good mas não por excellent
      total: união (rotas com latest <= good_brl ajustado)
      skipped_no_threshold: rotas ignoradas por falta de teto/percentil
    """
    excellent_hits = 0
    good_hits = 0  # total geral (inclui excellent)
    skipped = 0

    for s in stats_list:
        if s.latest is None:
            skipped += 1
            continue
        if use_p10:
            if s.p10 is None:
                skipped += 1
                continue
            if s.latest <= s.p10:
                good_hits += 1
            continue
        if use_p25:
            if s.p25 is None:
                skipped += 1
                continue
            if s.latest <= s.p25:
                good_hits += 1
            continue
        # cenário factor sobre excellent/good
        if s.good_brl is None:
            skipped += 1
            continue
        good_threshold = s.good_brl * factor
        excellent_threshold = (s.excellent_brl * factor) if s.excellent_brl is not None else None
        if excellent_threshold is not None and s.latest <= excellent_threshold:
            excellent_hits += 1
            good_hits += 1
        elif s.latest <= good_threshold:
            good_hits += 1

    return {
        "excellent": excellent_hits,
        "good_only": good_hits - excellent_hits,
        "total": good_hits,
        "skipped_no_threshold": skipped,
    }


def rank_score(stats: RouteStats) -> int:
    """Score de ranking (0-100) — separado do opportunity score do alerta.

    Componentes:
    - Proximidade do good_brl (até 40): latest mais perto/menor que good_brl pontua mais.
    - Link acionável: 20
    - Presente em watchlist: 15
    - Amostras (até 15): >= 10 → 15; >= 5 → 10; senão 0
    - Hot route: 10
    """
    score = 0
    if stats.good_brl and stats.latest:
        ratio = stats.latest / stats.good_brl
        if ratio <= 0.85:
            score += 40
        elif ratio <= 0.95:
            score += 30
        elif ratio <= 1.0:
            score += 20
        elif ratio <= 1.10:
            score += 10
    if stats.last_quote_actionable:
        score += 20
    if stats.watchlist_label:
        score += 15
    if stats.samples >= 10:
        score += 15
    elif stats.samples >= 5:
        score += 10
    if stats.is_hot:
        score += 10
    return min(score, 100)


def ranked_routes(stats_list: list[RouteStats], top_n: int = 10) -> list[tuple[RouteStats, int]]:
    scored = [(s, rank_score(s)) for s in stats_list]
    scored.sort(key=lambda pair: (-pair[1], pair[0].latest if pair[0].latest is not None else float("inf")))
    return scored[:top_n]


def provider_health(stats_list: list[RouteStats]) -> dict:
    total = len(stats_list)
    with_quote = sum(1 for s in stats_list if s.samples > 0)
    few_samples = sum(1 for s in stats_list if 0 < s.samples < 5)
    with_lq = sum(1 for s in stats_list if s.last_quote_present)
    without_lq = sum(1 for s in stats_list if not s.last_quote_present)
    actionable = sum(1 for s in stats_list if s.last_quote_actionable)
    pct = (actionable / with_lq * 100) if with_lq else 0.0
    return {
        "total_routes": total,
        "with_quote": with_quote,
        "few_samples": few_samples,
        "with_last_quote": with_lq,
        "without_last_quote": without_lq,
        "actionable_links": actionable,
        "actionable_pct": pct,
    }


def audit_links(stats_list: list[RouteStats]) -> dict:
    total_with_lq = sum(1 for s in stats_list if s.last_quote_present)
    actionable = sum(1 for s in stats_list if s.last_quote_actionable)
    non_actionable = sum(1 for s in stats_list if s.last_quote_present and not s.last_quote_actionable)
    legacy_urls: list[RouteStats] = []
    for s in stats_list:
        if not s.deep_link:
            continue
        # Padrão antigo www.aviasales.com/search/<codigos>
        if "aviasales.com/search/" in s.deep_link:
            legacy_urls.append(s)
    no_link = [s for s in stats_list if not s.last_quote_present]
    return {
        "total_with_lq": total_with_lq,
        "actionable": actionable,
        "non_actionable": non_actionable,
        "legacy_urls": legacy_urls,
        "no_link": no_link,
    }


CSV_COLUMNS = [
    "route_key",
    "origin",
    "destination",
    "route_label",
    "latest_price",
    "min_price",
    "avg_price",
    "p10",
    "p25",
    "sample_count",
    "last_quote_detected_at",
    "source",
    "deep_link",
    "actionable_url",
    "watchlist",
]


def export_csv(stats_list: list[RouteStats], out_path: Path) -> int:
    """Escreve CSV com 15 colunas. Retorna número de rotas exportadas."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for s in stats_list:
            writer.writerow(
                [
                    s.key,
                    s.origin,
                    s.destination,
                    s.route_label,
                    s.latest if s.latest is not None else "",
                    s.min_price if s.min_price is not None else "",
                    f"{s.avg:.2f}" if s.avg is not None else "",
                    s.p10 if s.p10 is not None else "",
                    s.p25 if s.p25 is not None else "",
                    s.samples,
                    s.detected_at or "",
                    s.source or "",
                    s.deep_link or "",
                    "true" if s.last_quote_actionable else "false",
                    s.watchlist_label or "",
                ]
            )
    return len(stats_list)
