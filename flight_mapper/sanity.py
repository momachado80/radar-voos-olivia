"""Regras de sanidade econômica: classifica preços implausíveis.

Defesa-em-profundidade: mesmo com cabine confirmada, um preço
absurdamente baixo para voo internacional de longa distância (ex.: o
suspeito ``US$ 232 GRU→MIA``) não deve virar EXCELENTE/BOM sem
verificação. Não substitui o gate de cabine (PR C) nem os thresholds —
é uma checagem adicional, conservadora e explícita.

Escopo (decisão de produto): o piso de preço só se aplica a cotações
**não-BRL-nativas** (USD/estrangeiras), que é a superfície real do bug
(cache Travelpayouts estilo USD). Cotações BRL-nativas confirmadas
(Kiwi/Mock) já são governadas pelos thresholds calibrados + gate de
cabine, então não passam por este piso. `quote.suspicious=True`
(sinalização vinda do provider) bloqueia independentemente da moeda.

Funções puras: sem rede, sem I/O, sem estado.
"""

from __future__ import annotations

from .currency import CURRENCY_BRL
from .regions import Cabin, Route, TripType

# Pisos mínimos plausíveis em BRL por (trip_type, cabin) para voo
# internacional de longa distância (todas as rotas monitoradas hoje são
# GRU → intl). Abaixo disso o preço é economicamente implausível.
#
# Calibração: ficam ABAIXO do menor preço legítimo já escalado pelos
# thresholds (GRU-MIA business EXCELENTE ≈ R$6.050 com USD_BRL_RATE=5.5),
# evitando falso-positivo, e ACIMA do caso-bug US$232≈R$1.276. Constantes
# fáceis de ajustar; cobertas por testes.
SUSPICIOUS_FLOOR_BRL: dict[tuple[TripType, Cabin], float] = {
    (TripType.ONE_WAY, Cabin.BUSINESS): 2500.0,
    (TripType.ROUND_TRIP, Cabin.BUSINESS): 4000.0,
    (TripType.ONE_WAY, Cabin.ECONOMY): 1000.0,
    (TripType.ROUND_TRIP, Cabin.ECONOMY): 1800.0,
}


def _floor_for(quote) -> float | None:
    return SUSPICIOUS_FLOOR_BRL.get((quote.trip_type, quote.cabin))


def is_suspicious_price(
    route: Route, quote, amount_brl_estimated: float | None
) -> bool:
    """True se a cotação é economicamente suspeita.

    - `quote.suspicious=True` (provider já sinalizou) ⇒ True, qualquer moeda.
    - Caso contrário, só avaliamos o piso para cotações NÃO-BRL-nativas
      (USD/estrangeira). BRL-nativo ⇒ não aplica piso (governado pelos
      thresholds + gate de cabine).
    - Sem `amount_brl_estimated` confiável ⇒ não classificável aqui
      (o gate de moeda do Monitor trata isso antes).
    - Sem piso configurado para (trip_type, cabin) ⇒ não suspeito.
    """
    if getattr(quote, "suspicious", False):
        return True
    if (quote.currency or "").strip().upper() == CURRENCY_BRL:
        return False
    if amount_brl_estimated is None:
        return False
    floor = _floor_for(quote)
    if floor is None:
        return False
    return amount_brl_estimated < floor


def suspicious_reason(
    route: Route, quote, amount_brl_estimated: float | None
) -> str | None:
    """Mensagem humana do motivo, ou None se não suspeito."""
    if not is_suspicious_price(route, quote, amount_brl_estimated):
        return None
    if getattr(quote, "suspicious", False):
        return "preço sinalizado como suspeito pelo provedor"
    floor = _floor_for(quote)
    return (
        f"preço R$ {amount_brl_estimated:.0f} abaixo do piso plausível "
        f"R$ {floor:.0f} para {quote.cabin.value} {quote.trip_type.value} "
        f"internacional"
    )
