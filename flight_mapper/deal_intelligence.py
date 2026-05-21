"""Deal Intelligence: transforma sinais brutos de preço em inteligência
de promoção (econômica). Funções PURAS — sem rede, sem I/O, sem estado.

Não substitui o gate de cabine: sinais Travelpayouts/cabine não
confirmada continuam sendo possíveis econômicas (nunca executiva
confirmada). Apenas classifica o quanto o preço é interessante DADO
que estamos olhando como econômica e DADO o histórico da rota.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Sequence

from .regions import REGIONS, TripType


# Pisos USD por (região, trip_type) — econômica. Abaixo de `strong`:
# preço muito forte; abaixo de `good`: preço bom; senão, observar/ignorar.
ECONOMY_BANDS_USD: dict[tuple[str, TripType], tuple[float, float]] = {
    ("EUA", TripType.ONE_WAY): (250.0, 350.0),
    ("Europa", TripType.ONE_WAY): (350.0, 500.0),
    ("Ásia", TripType.ONE_WAY): (500.0, 700.0),
    ("EUA", TripType.ROUND_TRIP): (450.0, 650.0),
    ("Europa", TripType.ROUND_TRIP): (600.0, 850.0),
    ("Ásia", TripType.ROUND_TRIP): (800.0, 1100.0),
}

# Histórico mínimo para considerar mediana/p25 confiáveis.
MIN_HISTORY_SAMPLES = 10
# Janela "recente" para `min_recent`.
RECENT_WINDOW = 10


# Classificação final de promoção. Dirigida pela banda USD apenas
# (desconto vs mediana é informativo, não rebaixa).
DEAL_VERY_STRONG = "muito_forte"
DEAL_GOOD = "boa"
DEAL_IGNORE = "ignorar"


@dataclass(frozen=True)
class HistoryStats:
    n: int
    median_brl: float | None
    p25_brl: float | None
    min_recent_brl: float | None
    sufficient: bool          # n >= MIN_HISTORY_SAMPLES
    baseline_weak: bool       # histórico repetitivo / variação muito baixa
                              # ⇒ não dá p/ estimar desconto real


@dataclass(frozen=True)
class DealEvaluation:
    deal: str                       # muito_forte | boa | ignorar
    region: str | None              # "EUA" / "Europa" / "Ásia" / None
    region_band: str | None         # "forte" / "boa" / None (banda USD pura)
    usd_amount: float | None
    brl_amount: float | None
    trip_type: TripType
    history: HistoryStats
    discount_pct: float | None      # vs mediana; None se histórico insuficiente
    reason: str


def region_for_destination(destination: str) -> str | None:
    """Retorna 'EUA'/'Europa'/'Ásia' ou None se desconhecido."""
    for region, dests in REGIONS.items():
        if destination in dests:
            return region
    return None


def _percentile_sorted(sorted_vals: list[float], q: float) -> float:
    """p25 com interpolação linear simples. `q` em [0,1]."""
    if not sorted_vals:
        raise ValueError("vazio")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def history_stats(prices: Sequence[float]) -> HistoryStats:
    """Estatísticas defensivas sobre o histórico (BRL).

    Sem amostras → tudo None, `sufficient=False`. Abaixo de
    `MIN_HISTORY_SAMPLES` ainda calcula mas marca `sufficient=False`
    para o chamador decidir como mostrar.
    """
    vals = [float(p) for p in prices if p is not None]
    n = len(vals)
    if n == 0:
        return HistoryStats(
            n=0, median_brl=None, p25_brl=None, min_recent_brl=None,
            sufficient=False, baseline_weak=True,
        )
    med = float(median(vals))
    p25 = _percentile_sorted(sorted(vals), 0.25)
    recent = vals[-RECENT_WINDOW:]
    rec_min = min(recent)
    rec_max = max(recent)
    sufficient = n >= MIN_HISTORY_SAMPLES
    # Histórico fraco: amostras insuficientes OU repetitivo (poucos
    # valores únicos no janela recente) OU amplitude muito baixa
    # (< 2% da mediana). Cobre o caso "cache Travelpayouts devolve
    # o MESMO preço várias vezes → mediana == preço atual → desconto
    # 0% que NÃO representa o preço normal da rota".
    unique_recent = len({round(v, 2) for v in recent})
    narrow_range = (
        med > 0 and (rec_max - rec_min) / med < 0.02
    )
    baseline_weak = (
        (not sufficient) or unique_recent <= 2 or narrow_range
    )
    return HistoryStats(
        n=n,
        median_brl=round(med, 2),
        p25_brl=round(p25, 2),
        min_recent_brl=round(rec_min, 2),
        sufficient=sufficient,
        baseline_weak=baseline_weak,
    )


def usd_band(
    usd_amount: float | None, region: str | None, trip_type: TripType
) -> str | None:
    """'forte' / 'boa' / None — banda USD pura (sem histórico)."""
    if usd_amount is None or region is None:
        return None
    band = ECONOMY_BANDS_USD.get((region, trip_type))
    if band is None:
        return None
    strong, good = band
    if usd_amount < strong:
        return "forte"
    if usd_amount < good:
        return "boa"
    return None


def evaluate_deal(
    *,
    destination: str,
    trip_type: TripType,
    usd_amount: float | None,
    brl_amount: float | None,
    prices: Sequence[float],
) -> DealEvaluation:
    """Classifica um sinal de econômica.

    A banda USD por (região, trip_type) dirige a classificação:
      • USD abaixo do piso `muito forte` → `muito_forte`
      • USD na faixa `boa` (entre piso `muito forte` e `boa`) → `boa`
      • USD acima de ambos → `ignorar` (não entra em "Possíveis
        promoções de econômica")
    O desconto vs mediana é informativo, e SÓ é calculado quando o
    histórico interno NÃO é fraco (ver `HistoryStats.baseline_weak`).
    Desconto nunca rebaixa a classificação — evita o paradoxo
    "USD abaixo do piso forte, mas classificado como boa".
    """
    region = region_for_destination(destination)
    stats = history_stats(prices)
    band = usd_band(usd_amount, region, trip_type)

    # Desconto só quando o histórico interno tem variação útil. Caso
    # contrário, deixar None e o relatório mostra "histórico interno
    # ainda fraco".
    discount: float | None = None
    if (
        not stats.baseline_weak
        and stats.median_brl
        and brl_amount is not None
        and stats.median_brl > 0
    ):
        discount = round((stats.median_brl - brl_amount) / stats.median_brl, 4)

    # Classificação dirigida pela banda USD (sem downgrade por desconto).
    if band == "forte":
        deal = DEAL_VERY_STRONG
        reason = (
            f"USD abaixo do piso muito forte ({region}/{trip_type.value})"
        )
    elif band == "boa":
        deal = DEAL_GOOD
        reason = f"USD abaixo do piso bom ({region}/{trip_type.value})"
    else:
        deal = DEAL_IGNORE
        if region is None:
            reason = f"região desconhecida para destino {destination!r}"
        else:
            reason = (
                f"USD acima das faixas de econômica para "
                f"{region}/{trip_type.value}"
            )

    return DealEvaluation(
        deal=deal,
        region=region,
        region_band=band,
        usd_amount=usd_amount,
        brl_amount=brl_amount,
        trip_type=trip_type,
        history=stats,
        discount_pct=discount,
        reason=reason,
    )


def deal_label_pt(deal: str) -> str:
    """Rótulo PT humano para uso no relatório / CLI."""
    return {
        DEAL_VERY_STRONG: "muito forte",
        DEAL_GOOD: "boa",
        DEAL_IGNORE: "ignorar",
    }.get(deal, deal)
