"""Correção e conversão de moeda dos preços de voo.

Contexto crítico: o endpoint Travelpayouts `aviasales/v3/prices_for_dates`
ignora o parâmetro `currency=brl` e devolve valores em **USD**. O código
antigo rotulava `item["price"]` como `price_brl` sem validar, gerando
alertas como "R$ 2.079" para tarifas que são US$ 2.079 (≈ R$ 11k+).

Política:
- Toda cotação carrega `currency` explícita.
- USD é convertido para BRL via taxa de câmbio de runtime (env
  `USD_BRL_RATE`), nunca por rede.
- Sem taxa confiável OU moeda desconhecida ⇒ `to_brl` devolve `None`,
  e o chamador (Monitor) BLOQUEIA o alerta automático.
"""

from __future__ import annotations

import os
from typing import Mapping

CURRENCY_USD = "USD"
CURRENCY_BRL = "BRL"

# Banda de sanidade para a taxa USD→BRL. Fora disso tratamos como
# configuração inválida (provável erro de digitação do operador).
_RATE_MIN = 1.0
_RATE_MAX = 20.0

USD_BRL_RATE_ENV = "USD_BRL_RATE"


def get_usd_brl_rate(env: Mapping[str, str] | None = None) -> float | None:
    """Lê a taxa USD→BRL de `USD_BRL_RATE`. Pura, sem rede.

    Retorna `None` se ausente, não-numérica ou fora da banda de sanidade
    — sinalizando que não há câmbio confiável e alertas devem ser
    bloqueados.
    """
    source = env if env is not None else os.environ
    raw = source.get(USD_BRL_RATE_ENV)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return None
    if not (_RATE_MIN < rate < _RATE_MAX):
        return None
    return rate


def to_brl(amount: float, currency: str, rate: float | None) -> float | None:
    """Converte `amount` na moeda `currency` para BRL.

    - BRL: identidade (já está em BRL).
    - USD: `amount * rate`; `None` se não há taxa confiável.
    - Qualquer outra moeda: `None` (não confiável, não inventamos câmbio).
    """
    cur = (currency or "").strip().upper()
    if cur == CURRENCY_BRL:
        return round(float(amount), 2)
    if cur == CURRENCY_USD:
        if rate is None:
            return None
        return round(float(amount) * rate, 2)
    return None
