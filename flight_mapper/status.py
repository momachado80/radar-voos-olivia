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
        """Carrega o estado. Defensivo: arquivo ausente, malformado ou
        com erro de I/O → estado vazio (last_report_at=None). Garante
        que erro em data/status.json NUNCA derruba o ciclo."""
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
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
    """Bloco da seção "👀 Sinais em observação" (PR #51).

    Inclui sinais sem cabine confirmada e sem grading de economia
    promocional. Compacta quando os itens compartilham a MESMA fonte:
    cabeçalho com Fonte/Cabine/Interpretação uma única vez no topo +
    linhas numeradas com `[trip]` por item. Se as fontes divergirem,
    cai no formato multilinha por item (`_format_raw_block`).
    """
    if not raw:
        return "• Nenhum sinal em observação no momento."
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


def _eval_history_deal(history: RouteHistory, key: str, price: float):
    """Helper: deriva inputs e chama `evaluate_deal`. Centraliza o
    parsing de last_quote → (destination, trip_type, usd_amount,
    brl_amount) p/ reuso na partição (filtra `ignorar`) e nos
    formatadores."""
    lq = _lq(history)
    sq = _SignalQuote(lq)
    parts = _split_route_key(key)
    destination = lq.get("destination") or (parts[1] if parts else "")
    brl = _amount_brl(history, price)
    usd = None
    if str(lq.get("currency") or "").upper() == "USD":
        try:
            usd = float(lq.get("amount")) if lq.get("amount") is not None else None
        except (TypeError, ValueError):
            usd = None
    return evaluate_deal(
        destination=destination,
        trip_type=sq.trip_type,
        usd_amount=usd,
        brl_amount=brl,
        prices=history.prices,
    )


def _validation_priority_key(
    item: tuple[str, float], store: PriceStore,
) -> tuple[int, float]:
    """Chave de ordenação p/ priorizar candidatos a validação SerpApi.

    Ordem (PR #56):
    1. deal "muito_forte" (região_band="forte") vem primeiro;
    2. depois deal "boa";
    3. dentro do mesmo deal, menor preço.

    `ev.deal` é o rótulo canônico de `deal_intelligence` (constantes
    DEAL_VERY_STRONG / DEAL_GOOD / DEAL_IGNORE). Quanto menor o
    `deal_rank`, mais forte.
    """
    from .deal_intelligence import DEAL_GOOD, DEAL_IGNORE, DEAL_VERY_STRONG
    key, price = item
    h = store.get(key)
    ev = _eval_history_deal(h, key, price)
    deal_rank = {
        DEAL_VERY_STRONG: 0,
        DEAL_GOOD: 1,
        DEAL_IGNORE: 2,
    }.get(ev.deal, 3)
    return (deal_rank, float(price))


def _select_serpapi_validation_candidates(
    store: PriceStore,
    pool: list[tuple[str, float]],
    *,
    max_n: int,
) -> list:
    """Filtra `pool` p/ candidatos elegíveis a validação SerpApi.

    PR #56: `pool` agora é tipicamente `economy_pool + raw_pool`
    (todos os sinais sem cabine confirmada). Antes filtrávamos só
    raw_pool, perdendo os candidatos mais fortes que já tinham sido
    classificados em economy_pool.

    Critérios (em ordem):
    - rota business (chave contém '-business');
    - USD price em banda 'forte' OU 'boa' (priorização garantida pelo
      sort prévio via `_validation_priority_key`);
    - last_quote com origin/destination/departure_date utilizáveis.

    Devolve até `max_n` `SerpApiValidationCandidate`. Pure — não chama
    rede, não toca PriceStore além de leitura. Caller deve sortear o
    pool antes de chamar (ordem decide quem ganha o slot do cap).
    """
    from .serpapi_validation import SerpApiValidationCandidate
    out: list = []
    for key, price in pool:
        if len(out) >= max_n:
            break
        if "-business" not in key:
            # PR #52: por enquanto só validamos rotas business
            # (econômica não vira "executiva" mesmo se confirmada).
            continue
        h = store.get(key)
        lq = _lq(h)
        if not lq:
            continue
        ev = _eval_history_deal(h, key, price)
        if ev.region_band not in ("forte", "boa"):
            # PR #56: aceitamos "forte" (deal=muito_forte) E "boa"
            # (deal=boa). Sort por _validation_priority_key garante
            # que "forte" é escolhido primeiro.
            continue
        parts = _split_route_key(key) or (None, None)
        origin = lq.get("origin") or parts[0]
        destination = lq.get("destination") or parts[1]
        departure = lq.get("departure_date")
        if not (origin and destination and departure):
            continue
        usd = None
        if str(lq.get("currency") or "").upper() == "USD":
            try:
                usd = float(lq.get("amount")) if lq.get("amount") is not None else None
            except (TypeError, ValueError):
                usd = None
        out.append(SerpApiValidationCandidate(
            key=key,
            origin=str(origin),
            destination=str(destination),
            outbound_date=str(departure),
            return_date=lq.get("return_date") or None,
            travel_class="business",
            expected_usd=usd,
        ))
    return out


def _maybe_validate_with_serpapi(
    store: PriceStore, pool: list[tuple[str, float]],
) -> tuple[dict, "object"]:
    """Wrapper: lê config do ambiente, ordena o `pool` por prioridade
    (muito_forte > boa > preço), filtra candidatos, roda
    `validate_cycle_candidates`. Default DESLIGADO. Falha silenciosa
    em qualquer erro.

    PR #57 (observability): retorna agora tupla
    `(results_dict, SerpApiValidationSummary)` — o summary é sempre
    populado (mesmo quando desabilitado / sem chave / cap zero), p/
    o relatório poder renderizar o status correto no 🧭. `summary`
    NUNCA contém token, URL, payload, post_data nem dados de rota.

    PR #56: `pool` é `economy_pool + raw_pool` para que sinais fortes
    em economy_pool tenham chance de virar 🟡.
    """
    from .config import Config
    from .serpapi_validation import (
        SerpApiValidationBudget,
        SerpApiValidationConfig,
        SerpApiValidationSummary,
        validate_cycle_candidates,
    )

    config = SerpApiValidationConfig.from_env()
    # Tenta carregar budget mesmo quando desabilitado — usuário precisa
    # ver "X/90 queries usadas" no relatório mesmo com flag off, p/
    # entender histórico de consumo.
    try:
        app_config = Config.from_env()
        budget_path = app_config.serpapi_validation_budget_path
    except Exception:
        budget_path = None
    try:
        budget_now = SerpApiValidationBudget.load(budget_path)
    except Exception:
        # Defesa contra arquivo corrompido / I/O — não quebra relatório.
        budget_now = None
    monthly_used = budget_now.count if budget_now is not None else 0

    def _summary(
        considered: int = 0,
        attempted: int = 0,
        skipped: str | None = None,
    ) -> SerpApiValidationSummary:
        return SerpApiValidationSummary(
            enabled=config.enabled,
            api_key_present=bool(config.api_key),
            monthly_budget=config.monthly_budget,
            monthly_used=monthly_used,
            candidates_considered=considered,
            validations_attempted=attempted,
            elevated_to_manual_check=0,
            skipped_reason=skipped,
        )

    if not config.enabled:
        return {}, _summary(skipped="validation_disabled")
    if not config.api_key:
        return {}, _summary(skipped="no_api_key")

    # Ordena por prioridade ANTES de filtrar candidatos. O cap intra-
    # ciclo (max_per_cycle=1) seleciona o 1º elegível segundo a ordem.
    sorted_pool = sorted(
        pool, key=lambda it: _validation_priority_key(it, store),
    )
    candidates = _select_serpapi_validation_candidates(
        store, sorted_pool, max_n=config.max_per_cycle,
    )
    if not candidates:
        return {}, _summary(skipped="no_eligible_candidate")

    try:
        results = validate_cycle_candidates(
            candidates, config, budget_path=budget_path,
        )
    except Exception:
        # Defesa final: NUNCA propaga erro p/ o relatório.
        return {}, _summary(
            considered=len(candidates), skipped="validation_error",
        )

    attempted = len(results)
    skipped = None
    if attempted == 0:
        skipped = "monthly_budget_exhausted"
    return results, _summary(
        considered=len(candidates), attempted=attempted, skipped=skipped,
    )


def _expected_usd_from_route(
    store: PriceStore, key: str,
) -> float | None:
    """Recupera o `expected_usd` do sinal original Travelpayouts a
    partir do `last_quote` da rota (quando currency=='USD')."""
    try:
        lq = _lq(store.get(key))
    except Exception:
        return None
    if str(lq.get("currency") or "").upper() != "USD":
        return None
    try:
        return float(lq.get("amount")) if lq.get("amount") is not None else None
    except (TypeError, ValueError):
        return None


def _informational_validation_line(
    store: PriceStore,
    key: str,
    informational_validations: dict,
) -> str | None:
    """PR #60: frase humana p/ price-mismatch SerpApi (cabine confirmada
    mas preço incompatível). Retorna None se a rota não tem nota
    informativa associada. NUNCA contém token/URL/post_data."""
    res = informational_validations.get(key)
    if res is None:
        return None
    try:
        from .serpapi_validation import humanize_price_mismatch_note
        return humanize_price_mismatch_note(
            res, _expected_usd_from_route(store, key),
        )
    except Exception:
        return None


def _append_informational_validation_notes(
    block_text: str,
    items: list[tuple[str, float]],
    informational_validations: dict,
    store: PriceStore,
) -> str:
    """PR #60: anexa notas informativas SerpApi (price mismatch) ao
    final do bloco 👀, indentadas para combinar com o estilo do bloco.
    Cada nota inclui o rótulo da rota + preço SerpApi + preço original.
    """
    if not informational_validations:
        return block_text
    note_lines: list[str] = []
    for key, _price in items:
        if key not in informational_validations:
            continue
        line = _informational_validation_line(
            store, key, informational_validations,
        )
        if not line:
            continue
        parts = _split_route_key(key) or (None, None)
        if parts and parts[0] and parts[1]:
            label = humanize_route(*parts)
            note_lines.append(f"   {label}: {line}")
        else:
            note_lines.append(f"   {line}")
    if not note_lines:
        return block_text
    return block_text + "\n" + "\n".join(note_lines)


def _format_economy_block(
    index: int, key: str, history: RouteHistory, price: float
) -> str:
    """Possível promoção de econômica com inteligência: classifica vs
    banda USD por região/trip + compara com histórico interno (mediana,
    p25, mínimo recente, quando útil). Sem rótulo de classe comprovada.

    `Classificação` é dirigida APENAS pela banda USD (sem downgrade por
    desconto). Quando o histórico interno é fraco (insuficiente OU
    repetitivo OU sem variação útil), exibe aviso claro em vez de
    `0% vs mediana` enganoso.
    """
    parts = _split_route_key(key)
    price_str = _price_label(history, price)
    label = humanize_route(*parts) if parts else key
    deal = _eval_history_deal(history, key, price)

    # Linha de classificação (região/trip aparecem só quando aplicável).
    class_suffix = (
        f" ({deal.region}/{deal.trip_type.value})"
        if deal.region and deal.region_band
        else ""
    )
    class_line = f"   Classificação: {deal_label_pt(deal.deal)}{class_suffix}"

    # Linha de histórico (rótulo "interno" deixa claro que NÃO é
    # benchmark de mercado).
    h = deal.history
    if h.n == 0:
        hist_line = "   Histórico interno: sem amostras."
    elif not h.sufficient:
        hist_line = (
            f"   Histórico interno: insuficiente "
            f"(n={h.n}, mínimo p/ mediana confiável: 10)"
        )
    elif h.baseline_weak:
        # Suficiente em volume, mas repetitivo / sem variação útil.
        # Frase humana — termo técnico "cache repetitivo" fica só em
        # reason codes / docs internas (booking_actionability.py).
        hist_line = (
            f"   Histórico interno: mediana {format_brl(h.median_brl)} "
            f"(variação muito baixa — a fonte vem repetindo valores "
            f"muito parecidos, n={h.n})"
        )
    else:
        hist_line = (
            f"   Histórico interno: mediana {format_brl(h.median_brl)} · "
            f"p25 {format_brl(h.p25_brl)} · "
            f"mínimo recente {format_brl(h.min_recent_brl)} (n={h.n})"
        )

    # Linha de desconto (só quando o histórico interno é forte o
    # suficiente para significar algo).
    if deal.discount_pct is None:
        if h.baseline_weak and h.sufficient:
            disc_line = (
                "   Desconto: histórico interno ainda fraco para estimar "
                "desconto real."
            )
        else:
            disc_line = "   Desconto: histórico insuficiente."
    else:
        disc_line = (
            f"   Desconto estimado: {deal.discount_pct:.0%} vs mediana interna"
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
    return "🛡️ Bloqueios de segurança\n" + body


def _no_alert_reason(
    result: MonitorResult,
    *,
    manual_check_present: bool = False,
    serpapi_price_mismatch_only: bool = False,
) -> str:
    """PR #60: aceita contexto p/ evitar contradição com as outras
    seções. Se já existe Verificação manual no relatório, a frase
    final reconhece isso em vez de dizer só "sem oportunidade".
    Se SerpApi encontrou business em preço diferente, a frase indica
    explicitamente que o preço original NÃO foi confirmado.
    """
    if result.alerts_sent > 0:
        return f"🔥 {result.alerts_sent} alerta(s) enviado(s) neste ciclo."
    # PR #60: prioriza a frase mais informativa quando há sinais
    # parciais. Evita contradição visual com 🟡 (Verificação manual).
    if manual_check_present:
        return (
            "ℹ️ Sem alerta automático: há verificação manual, mas sem "
            "link simples. Conferir o bloco 🟡."
        )
    if serpapi_price_mismatch_only:
        return (
            "ℹ️ Sem alerta confirmado: SerpApi encontrou executiva na "
            "rota, mas em preço diferente do sinal original — a tarifa "
            "original não foi confirmada como executiva."
        )
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
    serpapi_summary: object | None = None,
    duffel_summary: object | None = None,
) -> str:
    """🧭 Status das fontes — derivado do ciclo, sem rede.

    PR #57: aceita `serpapi_summary` (SerpApiValidationSummary ou None)
    p/ adicionar uma linha humana de observabilidade da validação
    SerpApi. Linha NUNCA contém token, URL, payload ou rota.

    PR #65: aceita `duffel_summary` (DuffelStatusSummary ou None) p/
    adicionar uma linha de observabilidade do pass Duffel. Quando None
    (caminho legado / testes antigos), NENHUMA linha Duffel é renderizada
    — preserva o relatório existente byte a byte. Linha NUNCA contém
    offer_id, token, URL, payload, order_id nem dado de passageiro.
    """

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

    lines = [
        "🧭 Status das fontes",
        f"• Travelpayouts: {tp}",
        f"• Kiwi: {kiwi}",
        f"• Alertas executivos: {execs}",
    ]
    if serpapi_summary is not None:
        try:
            from .serpapi_validation import humanize_validation_summary
            line = humanize_validation_summary(serpapi_summary)
            lines.append(f"• {line}")
        except Exception:
            # Defesa final: erro na renderização da linha SerpApi NÃO
            # pode derrubar o relatório inteiro.
            pass
    if duffel_summary is not None:
        try:
            from .duffel_status import humanize_duffel_status
            lines.append(f"• {humanize_duffel_status(duffel_summary)}")
        except Exception:
            # Defesa final: erro na linha Duffel NÃO derruba o relatório.
            pass
    return "\n".join(lines)


def _build_message(
    result: MonitorResult,
    store: PriceStore,
    now: datetime,
    duffel_summary: object | None = None,
) -> str:
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
    # Promoções de econômica: além de "econômica-plausível" pelo piso
    # de sanidade, exige Classificação != `ignorar` (USD precisa cair
    # numa banda forte ou boa). Itens `ignorar` vão para sinais brutos.
    economy_pool: list[tuple[str, float]] = []
    raw_pool: list[tuple[str, float]] = []
    from .deal_intelligence import DEAL_IGNORE
    for it in non_conf:
        h = store.get(it[0])
        if _economy_plausible(h):
            ev = _eval_history_deal(h, it[0], it[1])
            if ev.deal != DEAL_IGNORE:
                economy_pool.append(it)
                continue
        raw_pool.append(it)
    economy = sorted(economy_pool, key=lambda x: x[1])[:3]
    raw = sorted(raw_pool, key=lambda x: x[1])[:3]

    # PR #52: validação opcional SerpApi para sinais sem cabine
    # confirmada (read-only, opt-in via SERPAPI_VALIDATION_ENABLED).
    # PR #56: pool combinado economy_pool + raw_pool, priorizado por
    # deal_intelligence (muito_forte > boa > preço). Antes só raw_pool
    # entrava, deixando os candidatos mais fortes (que caem em
    # economy_pool após gate de _economy_plausible) sem chance de virar
    # 🟡. Quando SerpApi confirma cabine business + booking option, o
    # sinal sobe para 🟡 Verificação manual (NUNCA 🟢 — vide princípio
    # em docs/radar-operational-policy.md).
    validation_pool = economy_pool + raw_pool
    serpapi_validations, serpapi_summary = _maybe_validate_with_serpapi(
        store, validation_pool,
    )
    # Lista de (key, price, result) que foram elevadas via validação.
    elevated_via_serpapi: list[tuple[str, float, object]] = []
    # PR #60: notas informativas (cabine business confirmada MAS preço
    # SerpApi incompatível com o sinal Travelpayouts). Candidato fica
    # no bloco original (💸/👀) com a nota.
    informational_validations: dict[str, object] = {}
    if serpapi_validations:
        from .booking_actionability import OperationalDecision as _OD
        # Lookup unificado p/ recuperar o preço original do candidato
        # independentemente de qual pool ele estava (economy ou raw).
        price_lookup = {key: price for key, price in validation_pool}
        elevated_keys: set[str] = set()
        for key, res in serpapi_validations.items():
            if (
                res.suggested_decision == _OD.CONFIRMED_MANUAL_CHECK
                and key in price_lookup
            ):
                elevated_via_serpapi.append((key, price_lookup[key], res))
                elevated_keys.add(key)
            elif (
                # PR #60: cabine business confirmada mas preço incompatível
                # com sinal original → não eleva, só anota.
                getattr(res, "cabin_confirmed", False)
                and not getattr(res, "price_compatible", False)
                and key in price_lookup
            ):
                informational_validations[key] = res
        if elevated_keys:
            # Remove dos AMBOS os pools (💸 e 👀) e do top-3 já calculado.
            economy_pool = [
                e for e in economy_pool if e[0] not in elevated_keys
            ]
            raw_pool = [r for r in raw_pool if r[0] not in elevated_keys]
            economy = sorted(economy_pool, key=lambda x: x[1])[:3]
            raw = sorted(raw_pool, key=lambda x: x[1])[:3]

    # PR #57/60: atualiza summary com contagens finais p/ o 🧭
    # renderizar a frase correta — "validado/movido" vs "encontrou
    # executiva mas em preço diferente" vs "tentou e não confirmou".
    from dataclasses import replace as _dc_replace
    serpapi_summary = _dc_replace(
        serpapi_summary,
        elevated_to_manual_check=len(elevated_via_serpapi),
        price_mismatched=len(informational_validations),
    )

    # PR #51: partição decisória dos confirmados.
    # Quem tem cabine confirmada + link clicável (Kiwi deep_link ou
    # equivalente) → "🟢 Executiva confirmada" (CONFIRMED_ACTIONABLE).
    # Quem tem cabine confirmada SEM link → "🟡 Verificação manual"
    # (CONFIRMED_MANUAL_CHECK). Mesma cabine confirmada de antes,
    # apenas split por presença de link acionável.
    actionable_confirmed: list[tuple[str, float]] = []
    manual_check_confirmed: list[tuple[str, float]] = []
    for key, price in confirmed:
        h = store.get(key)
        parts = _split_route_key(key) or ("", "")
        link = _actionable_link_from_history(h, *parts)
        if link:
            actionable_confirmed.append((key, price))
        else:
            manual_check_confirmed.append((key, price))

    if actionable_confirmed:
        actionable_lines = "\n".join(
            _format_confirmed_line(
                i + 1,
                key,
                store.get(key),
                price,
                link=_actionable_link_from_history(
                    store.get(key), *(_split_route_key(key) or ("", ""))
                ),
            )
            for i, (key, price) in enumerate(actionable_confirmed)
        )
        avg_score = _compute_average_score(
            store, [k for k, _ in actionable_confirmed]
        )
        # Score só rotula executiva confirmada — nunca sinais brutos.
        actionable_score_line = (
            f"⭐ Score médio (executiva confirmada): {avg_score}/100\n"
            if avg_score is not None
            else ""
        )
    else:
        actionable_lines = "• Nenhuma executiva confirmada agora."
        actionable_score_line = ""

    manual_lines_list: list[str] = []
    item_num = 0
    for key, price in manual_check_confirmed:
        item_num += 1
        base = _format_confirmed_line(
            item_num, key, store.get(key), price, link=None,
        )
        manual_lines_list.append(base)
        # Texto humano de orientação para o usuário — sem URL,
        # sem token, sem post_data.
        manual_lines_list.append(
            "   Booking encontrado, mas sem link simples. "
            "Ação sugerida: verificar manualmente no Google "
            "Flights ou na companhia."
        )
    # PR #52: itens elevados via validação SerpApi entram aqui mesmo —
    # mesma seção 🟡 com nota indicando que cabine/booking foram
    # validados pela SerpApi (mas o link não é hyperlink simples).
    if elevated_via_serpapi:
        if manual_lines_list:
            manual_lines_list.append("")  # separador visual
        for key, price, res in elevated_via_serpapi:
            from .serpapi_validation import humanize_validation_note
            item_num += 1
            base = _format_confirmed_line(
                item_num, key, store.get(key), price, link=None,
            )
            manual_lines_list.append(base)
            manual_lines_list.append("   " + humanize_validation_note(res))
    if manual_lines_list:
        manual_lines = "\n".join(manual_lines_list)
    else:
        manual_lines = "• Nenhuma oferta confirmada sem link agora."

    observation_block = _format_raw_signals(raw, store)
    # PR #60: anexa nota informativa SerpApi (price mismatch) ao item
    # em 👀 quando aplicável, sem alterar a estrutura do bloco raw.
    observation_block = _append_informational_validation_notes(
        observation_block, raw, informational_validations, store,
    )
    if economy:
        eco_lines: list[str] = []
        for i, (key, price) in enumerate(economy):
            block = _format_economy_block(i + 1, key, store.get(key), price)
            note = _informational_validation_line(
                store, key, informational_validations,
            )
            if note:
                block = block + "\n   " + note
            eco_lines.append(block)
        eco_items = "\n".join(eco_lines)
        economy_block = f"{eco_items}\n\n{_ECONOMY_WARNING}"
    else:
        economy_block = "• Nenhum sinal compatível com econômica promocional agora."

    # `confirmed` (todos os cabin-confirmed) entra no source_status_block
    # como antes — métrica de fontes não muda com a partição decisória.
    sources_block = _source_status_block(
        store, confirmed, raw + economy,
        serpapi_summary=serpapi_summary,
        duffel_summary=duffel_summary,
    )
    security_block = _security_block(result)
    # PR #60: passa contexto p/ evitar frase final contraditória.
    _has_manual_check = bool(
        manual_check_confirmed or elevated_via_serpapi
    )
    _serpapi_mismatch_only = (
        not _has_manual_check
        and not actionable_confirmed
        and bool(informational_validations)
    )
    reason = _no_alert_reason(
        result,
        manual_check_present=_has_manual_check,
        serpapi_price_mismatch_only=_serpapi_mismatch_only,
    )
    legacy_line = (
        f"• Entradas legadas sem moeda comprovada (omitidas): {legacy_omitted}\n"
        if legacy_omitted
        else ""
    )

    # PR #58: 🧠 Leitura do ciclo + 📈 Mudanças desde o último ciclo.
    # Build snapshot atual ANTES de renderizar, compara com prev
    # snapshot, persiste atual. Defensivo: qualquer erro nesse bloco
    # cai p/ frase neutra e o relatório segue.
    cycle_block, changes_block = _render_cycle_overview(
        store=store,
        result=result,
        actionable_confirmed=actionable_confirmed,
        manual_check_confirmed=manual_check_confirmed,
        elevated_via_serpapi=elevated_via_serpapi,
        economy=economy,
        raw=raw,
        serpapi_summary=serpapi_summary,
        deduped=deduped,
    )

    return (
        "🛰️ <b>Radar de Voos Olivia — relatório diário</b>\n"
        f"Robô ativo. Último ciclo: {timestamp}\n\n"
        f"{cycle_block}\n\n"
        f"{changes_block}\n\n"
        "📊 Ciclo recente\n"
        f"• Rotas escaneadas: {result.scanned}\n"
        f"• Cotações obtidas: {result.quotes_received}\n"
        f"• Alertas enviados: {result.alerts_sent}\n"
        f"• Bloqueados por cabine: {result.cabin_blocked}\n"
        f"• Bloqueados por preço suspeito: {result.suspicious_blocked}\n"
        f"• Bloqueados por câmbio: {result.currency_blocked}\n"
        f"• Links comerciais indisponíveis: {result.non_actionable_links_skipped}\n"
        f"{legacy_line}\n"
        "🟢 Executiva confirmada\n"
        f"{actionable_score_line}"
        f"{actionable_lines}\n\n"
        "🟡 Verificação manual\n"
        f"{manual_lines}\n\n"
        "💸 Econômica possível\n"
        f"{economy_block}\n\n"
        "👀 Sinais em observação\n"
        f"{observation_block}\n\n"
        f"{security_block}\n\n"
        f"{sources_block}\n\n"
        f"{reason}"
    )


def _render_cycle_overview(
    *,
    store: PriceStore,
    result: MonitorResult,
    actionable_confirmed: list[tuple[str, float]],
    manual_check_confirmed: list[tuple[str, float]],
    elevated_via_serpapi: list[tuple[str, float, object]],
    economy: list[tuple[str, float]],
    raw: list[tuple[str, float]],
    serpapi_summary: object,
    deduped: list[tuple[str, float]],
) -> tuple[str, str]:
    """Constrói (🧠 block, 📈 block) e persiste o snapshot atual.
    Defensivo: qualquer falha → frases neutras + relatório segue."""
    try:
        from .config import Config
        from .cycle_summary import (
            CycleSnapshot,
            compute_changes,
            derive_main_bottleneck,
            format_executive_reading,
        )
        from .serpapi_validation import humanize_validation_summary
    except Exception:
        return ("🧠 Leitura do ciclo\n• (leitura indisponível)",
                "📈 Mudanças desde o último ciclo\n• (sem dados)")

    # Best signal: prioriza economy (preço bom + econ plausível) > raw
    best_label: str | None = None
    best_has_cabin = False
    best_source = economy if economy else raw
    if best_source:
        b_key, b_price = sorted(best_source, key=lambda x: x[1])[0]
        h = store.get(b_key)
        lq = h.last_quote if isinstance(h.last_quote, dict) else {}
        route_human = _humanize_route_for_overview(b_key)
        # Preço em USD quando disponível; fallback BRL
        usd = None
        if str(lq.get("currency") or "").upper() == "USD":
            try:
                usd = float(lq.get("amount")) if lq.get("amount") is not None else None
            except (TypeError, ValueError):
                usd = None
        if usd is not None:
            best_label = f"{route_human} por US$ {usd:.0f}"
        else:
            best_label = f"{route_human} por R$ {b_price:,.0f}".replace(",", ".")
        best_has_cabin = bool(lq.get("cabin_confirmed"))

    serpapi_line = ""
    try:
        serpapi_line = humanize_validation_summary(serpapi_summary)
    except Exception:
        serpapi_line = ""

    bottleneck = derive_main_bottleneck(
        cabin_blocked=getattr(result, "cabin_blocked", 0),
        suspicious_blocked=getattr(result, "suspicious_blocked", 0),
        currency_blocked=getattr(result, "currency_blocked", 0),
        non_actionable_links_skipped=getattr(
            result, "non_actionable_links_skipped", 0,
        ),
    )

    reading = format_executive_reading(
        actionable_count=len(actionable_confirmed),
        manual_check_count=(
            len(manual_check_confirmed) + len(elevated_via_serpapi)
        ),
        best_signal_label=best_label,
        best_signal_has_cabin=best_has_cabin,
        serpapi_one_liner=serpapi_line,
        main_bottleneck=bottleneck,
    )
    cycle_block = "🧠 Leitura do ciclo\n" + reading

    # 📈 Mudanças
    try:
        app_config = Config.from_env()
        snapshot_path = app_config.cycle_snapshot_path
    except Exception:
        snapshot_path = None
    prev = CycleSnapshot.load(snapshot_path)
    # Atual:
    current_prices: dict[str, float] = {k: float(p) for k, p in deduped}
    manual_keys: list[str] = [k for k, _ in manual_check_confirmed]
    manual_keys += [k for k, _, _ in elevated_via_serpapi]
    s_used = getattr(serpapi_summary, "monthly_used", 0) or 0
    s_elevated = getattr(serpapi_summary, "elevated_to_manual_check", 0) or 0
    current = CycleSnapshot(
        snapshot_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        latest_prices=current_prices,
        manual_check_keys=tuple(manual_keys),
        serpapi_used=int(s_used),
        serpapi_elevated=int(s_elevated),
    )
    changes = compute_changes(prev, current)
    if changes:
        changes_block = "📈 Mudanças desde o último ciclo\n" + "\n".join(
            f"• {line}" for line in changes
        )
    else:
        changes_block = (
            "📈 Mudanças desde o último ciclo\n"
            "• Sem mudança relevante desde o último ciclo."
        )

    # Persistência (defensiva)
    current.save(snapshot_path)

    return cycle_block, changes_block


def _humanize_route_for_overview(key: str) -> str:
    """`GRU-MIA-one_way-business` → `GRU → MIA`. Sem rede."""
    parts = key.split("-")
    if len(parts) >= 2:
        return f"{parts[0]} → {parts[1]}"
    return key


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

    # Ordem: muito_forte > boa > ignorar; dentro de cada, maior
    # desconto primeiro; depois menor preço.
    order = {"muito_forte": 0, "boa": 1, "ignorar": 2}
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
    duffel_summary: object | None = None,
) -> StatusDecision:
    now = now or datetime.now(timezone.utc)

    if notifier is None:
        return StatusDecision(action="skipped", reason="no_notifier")

    if state.last_report_at:
        try:
            last = datetime.fromisoformat(state.last_report_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            delta = now - last
            # PR #59: defesa contra clock skew. Se `last_report_at`
            # acabar gravado no FUTURO (relógio adiantado em um run
            # anterior, edição manual, restore de backup antigo), o
            # delta vira negativo e a checagem `delta < 24h` sempre
            # passaria, deixando o bot mudo p/ sempre. Tratamos como
            # estado inválido → manda o heartbeat e re-escreve com
            # timestamp correto.
            if delta < timedelta(0):
                reason = "first_run_clock_skew"
            elif delta < timedelta(hours=throttle_hours):
                return StatusDecision(action="skipped", reason="throttled")
            else:
                reason = "window_elapsed"
        except ValueError:
            # ISO inválido → trata como primeiro run, sobrescreve.
            reason = "first_run"
    else:
        reason = "first_run"

    text = _build_message(result, store, now, duffel_summary=duffel_summary)
    ok = notifier.send(text)
    if not ok:
        return StatusDecision(action="failed", reason="telegram_send_failed")

    state.last_report_at = now.isoformat()
    state.save(state_path)
    return StatusDecision(action="sent", reason=reason)
