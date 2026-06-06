"""Resumo executivo + detecção de mudanças do ciclo, p/ o Telegram.

Lê o estado do ciclo atual (latest_prices + listas de candidatos por
seção decisória + summary SerpApi) e gera duas seções concisas:

1. 🧠 Leitura do ciclo (frase humana de até ~3 linhas).
2. 📈 Mudanças desde o último ciclo (lista curta, max 5 linhas).

Persistência mínima: `data/cycle_snapshot.json` com
`{snapshot_at, latest_prices, manual_check_keys, serpapi_used,
serpapi_elevated}` — schema FECHADO. NUNCA contém token, URL,
post_data, payload, carriers nem rota de SerpApi.

Funções puras (compute_changes, format_executive_reading) — sem rede,
sem I/O. `CycleSnapshot.load/save` defensivos contra arquivo corrompido.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Mudanças de preço abaixo desse percentual são ignoradas — ruído de
# arredondamento, FX, cache. Acima disso entra como "queda" / "alta".
PRICE_CHANGE_THRESHOLD_PCT = 0.05  # 5%

# Máximo de linhas no bloco 📈 — evita poluir relatório.
MAX_CHANGE_LINES = 5


@dataclass(frozen=True)
class CycleSnapshot:
    """Snapshot mínimo do estado do ciclo, p/ detecção de mudança no
    próximo. Schema fechado — NUNCA contém token, URL, payload,
    post_data, carriers nem qualquer dado sensível do SerpApi.
    """

    snapshot_at: str                       # ISO 8601 UTC
    latest_prices: dict[str, float]        # {route_key: price_brl}
    manual_check_keys: tuple[str, ...]     # rotas atualmente em 🟡
    serpapi_used: int                      # queries SerpApi usadas no mês
    serpapi_elevated: int                  # candidatos elevados nesta cycle
    # PR #78: estado do orçamento mensal SerpApi neste ciclo. Quando True,
    # a frase "SerpApi gastou X queries neste ciclo" é SUBSTITUÍDA pela
    # frase de orçamento esgotado — o delta pode ser ruído de snapshots
    # anteriores e não representa gasto novo no ciclo atual.
    serpapi_budget_exhausted: bool = False

    @classmethod
    def empty(cls) -> "CycleSnapshot":
        return cls(
            snapshot_at="",
            latest_prices={},
            manual_check_keys=(),
            serpapi_used=0,
            serpapi_elevated=0,
            serpapi_budget_exhausted=False,
        )

    @classmethod
    def load(cls, path: Path | None) -> "CycleSnapshot":
        """Carrega do disco. Schema-strict: ignora campos extras /
        valores inválidos. Arquivo ausente ou corrompido → empty()."""
        if path is None or not path.exists():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return cls.empty()
        if not isinstance(raw, dict):
            return cls.empty()
        snapshot_at = str(raw.get("snapshot_at") or "")
        # latest_prices: dict[str, float] — descarta entradas malformadas
        raw_prices = raw.get("latest_prices") or {}
        latest_prices: dict[str, float] = {}
        if isinstance(raw_prices, dict):
            for k, v in raw_prices.items():
                if not isinstance(k, str):
                    continue
                try:
                    latest_prices[k] = float(v)
                except (TypeError, ValueError):
                    continue
        # manual_check_keys: tuple[str, ...]
        raw_keys = raw.get("manual_check_keys") or []
        if not isinstance(raw_keys, list):
            raw_keys = []
        manual_keys = tuple(str(k) for k in raw_keys if isinstance(k, str))
        try:
            serpapi_used = max(0, int(raw.get("serpapi_used") or 0))
        except (TypeError, ValueError):
            serpapi_used = 0
        try:
            serpapi_elevated = max(0, int(raw.get("serpapi_elevated") or 0))
        except (TypeError, ValueError):
            serpapi_elevated = 0
        serpapi_exhausted = bool(raw.get("serpapi_budget_exhausted") or False)
        return cls(
            snapshot_at=snapshot_at,
            latest_prices=latest_prices,
            manual_check_keys=manual_keys,
            serpapi_used=serpapi_used,
            serpapi_elevated=serpapi_elevated,
            serpapi_budget_exhausted=serpapi_exhausted,
        )

    def save(self, path: Path | None) -> None:
        """Grava o snapshot. Schema FECHADO. Falha silenciosa em I/O."""
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "snapshot_at": self.snapshot_at,
                        "latest_prices": self.latest_prices,
                        "manual_check_keys": list(self.manual_check_keys),
                        "serpapi_used": self.serpapi_used,
                        "serpapi_elevated": self.serpapi_elevated,
                        "serpapi_budget_exhausted": self.serpapi_budget_exhausted,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


def _humanize_route(key: str) -> str:
    """`GRU-MIA-one_way-business` → `GRU → MIA`. Sem rede."""
    parts = key.split("-")
    if len(parts) >= 2:
        return f"{parts[0]} → {parts[1]}"
    return key


def _signed_change_pct(prev: float, curr: float) -> float:
    if prev <= 0:
        return 0.0
    return (curr - prev) / prev


def compute_changes(
    prev: CycleSnapshot,
    current: CycleSnapshot,
    *,
    threshold_pct: float = PRICE_CHANGE_THRESHOLD_PCT,
    max_lines: int = MAX_CHANGE_LINES,
    serpapi_monthly_budget: int = 0,
) -> list[str]:
    """Compara dois snapshots e devolve até `max_lines` linhas humanas
    descrevendo as mudanças relevantes (PT). Pura — sem rede, sem I/O.

    Tipos de mudança detectados (em ordem de prioridade):
    1. novo candidato em 🟡 Verificação manual;
    2. queda de preço > threshold_pct;
    3. alta de preço > threshold_pct;
    4. novo melhor preço (rota nova com preço mais baixo);
    5. SerpApi: consumo de queries no ciclo / elevação confirmada.

    Se nada relevante: retorna lista vazia (caller decide a frase
    "Sem mudança relevante").
    """
    if prev.snapshot_at == "":
        # Primeiro ciclo registrado — não há base de comparação.
        return []
    changes: list[str] = []

    # 1. Manual check: candidatos novos em 🟡 são prioridade.
    new_in_manual = set(current.manual_check_keys) - set(prev.manual_check_keys)
    for key in sorted(new_in_manual):
        changes.append(
            f"⬆️ {_humanize_route(key)} subiu para Verificação manual."
        )
        if len(changes) >= max_lines:
            return changes

    # 2-3. Preços com mudança relevante.
    price_moves: list[tuple[float, str]] = []
    for key, curr_price in current.latest_prices.items():
        prev_price = prev.latest_prices.get(key)
        if prev_price is None:
            continue
        if prev_price <= 0:
            continue
        delta = _signed_change_pct(prev_price, curr_price)
        if abs(delta) < threshold_pct:
            continue
        arrow = "↘️" if delta < 0 else "↗️"
        verb = "caiu" if delta < 0 else "subiu"
        pct = abs(delta) * 100
        line = (
            f"{arrow} {_humanize_route(key)} {verb} "
            f"{pct:.0f}% (R$ {prev_price:,.0f} → R$ {curr_price:,.0f})"
            .replace(",", ".")
        )
        price_moves.append((delta, line))
    # Ordena por magnitude (maior mudança primeiro). Quedas (delta<0)
    # vêm antes de altas de mesma magnitude — quedas são mais
    # actionable.
    price_moves.sort(key=lambda x: (x[0] >= 0, -abs(x[0])))
    for _, line in price_moves:
        changes.append(line)
        if len(changes) >= max_lines:
            return changes

    # 4. Rotas totalmente novas (sem preço prévio) — limitamos a 1
    # linha agregada p/ não poluir.
    new_keys = sorted(
        set(current.latest_prices.keys()) - set(prev.latest_prices.keys())
    )
    if new_keys:
        if len(new_keys) == 1:
            k = new_keys[0]
            price = current.latest_prices[k]
            changes.append(
                f"🆕 nova cotação: {_humanize_route(k)} a "
                f"R$ {price:,.0f}".replace(",", ".")
            )
        else:
            changes.append(
                f"🆕 {len(new_keys)} novas cotações registradas neste ciclo."
            )
        if len(changes) >= max_lines:
            return changes

    # 5. SerpApi: delta de consumo + elevações.
    delta_used = max(0, current.serpapi_used - prev.serpapi_used)
    if current.serpapi_elevated > 0:
        plural = "" if current.serpapi_elevated == 1 else "s"
        changes.append(
            f"🔎 SerpApi confirmou {current.serpapi_elevated} "
            f"candidato{plural} neste ciclo."
        )
    elif current.serpapi_budget_exhausted:
        # PR #78: orçamento mensal esgotado ANTES do ciclo — não houve
        # gasto novo, mesmo se o delta vs snapshot anterior for > 0. Mostra
        # a frase real e suprime o "gastou X queries".
        denom = serpapi_monthly_budget if serpapi_monthly_budget > 0 else current.serpapi_used
        changes.append(
            f"🔎 SerpApi já consumiu {current.serpapi_used}/{denom} "
            "queries no mês; validação pausada."
        )
    elif delta_used > 0:
        changes.append(
            f"🔎 SerpApi gastou {delta_used} queries neste ciclo "
            "(não confirmou executiva)."
        )

    return changes[:max_lines]


def format_executive_reading(
    *,
    actionable_count: int,
    manual_check_count: int,
    best_signal_label: str | None,  # ex.: "GRU → MIA por US$ 208"
    best_signal_has_cabin: bool,
    serpapi_one_liner: str,         # vem do humanize_validation_summary
    main_bottleneck: str | None,    # ex.: "12 sinais sem cabine confirmada"
) -> str:
    """Frase humana 🧠 — até 3 sentenças. PURA, sem rede, sem I/O.

    NUNCA contém URL, token, post_data, payload, query string.
    """
    sentences: list[str] = []

    if actionable_count > 0:
        plural = "" if actionable_count == 1 else "s"
        sentences.append(
            f"{actionable_count} oportunidade{plural} executiva "
            f"acionável neste ciclo. Conferir o link no bloco 🟢."
        )
    elif manual_check_count > 0:
        plural = "" if manual_check_count == 1 else "s"
        sentences.append(
            f"{manual_check_count} candidato{plural} para Verificação "
            "manual. Conferir o bloco 🟡."
        )
    else:
        if best_signal_label:
            cab = (
                "com cabine confirmada"
                if best_signal_has_cabin
                else "mas sem cabine confirmada"
            )
            sentences.append(
                f"Não há executiva acionável agora. Melhor sinal "
                f"bruto: {best_signal_label}, {cab}."
            )
        else:
            sentences.append(
                "Sem sinais relevantes neste ciclo."
            )

    # SerpApi: pega a frase ja sanitizada do humanize_validation_summary
    # e adiciona como 2ª sentença, sem dobrar o prefixo "SerpApi:".
    if serpapi_one_liner:
        sentences.append(serpapi_one_liner)

    if main_bottleneck:
        sentences.append(f"Gargalo principal: {main_bottleneck}.")

    return " ".join(sentences)


def derive_main_bottleneck(
    *,
    cabin_blocked: int,
    suspicious_blocked: int,
    currency_blocked: int,
    non_actionable_links_skipped: int,
) -> str | None:
    """Retorna o nome PT do maior contador de bloqueio do ciclo, se houver.
    Se todos zero, devolve None."""
    candidates = [
        (cabin_blocked, "sinais sem cabine confirmada"),
        (suspicious_blocked, "preços economicamente suspeitos"),
        (currency_blocked, "cotações sem câmbio confiável"),
        (non_actionable_links_skipped, "ofertas sem link comercial"),
    ]
    candidates.sort(reverse=True, key=lambda x: x[0])
    top_n, top_label = candidates[0]
    if top_n <= 0:
        return None
    return f"{top_n} {top_label}"
