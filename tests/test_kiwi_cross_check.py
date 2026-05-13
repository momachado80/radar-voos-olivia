"""Tests para o cross-check Kiwi (FASE 3 do Aviasales-block).

Cenários cobrem:
- Travelpayouts alerta + Kiwi compatível → envia alerta com link Kiwi
- Travelpayouts alerta + Kiwi None → descarta (link comercial indisponível)
- Travelpayouts alerta + Kiwi preço incompatível → descarta
- Travelpayouts alerta + Kiwi sem deep_link → descarta
- Provider primário Kiwi → comportamento atual (sem cross-check)
- Travelpayouts sem link_provider → descarta (silêncio em prod sem KIWI_API_KEY)
- Mensagem contém "Travelpayouts + Kiwi" + nota honesta
- Kiwi só é chamado quando decision.alert=True
- Tolerância 15% e fallback good_brl
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from flight_mapper.detector import LEVEL_EXCELLENT, LEVEL_GOOD
from flight_mapper.monitor import Monitor
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Route
from flight_mapper.state import PriceStore


_ROUTE_LHR = Route("GRU", "LHR", "Europa")
# GRU-LHR thresholds: excellent_brl=1700, good_brl=2000


class _PrimaryProvider:
    """Travelpayouts-like: cota com deep_link=None (não acionável)."""

    def __init__(self, price: float):
        self.price = price
        self.calls = 0

    def quote(self, route: Route) -> Quote | None:
        self.calls += 1
        return Quote(
            route=route,
            price_brl=self.price,
            deep_link=None,
            departure_date="2026-06-15",
            return_date="2026-06-22",
            source="travelpayouts",
        )


class _LinkProvider:
    """Kiwi-like: devolve quotes pré-programadas. None quando esgotar."""

    def __init__(self, queue: list[Quote | None]):
        self._queue: Iterator[Quote | None] = iter(queue)
        self.calls = 0

    def quote(self, route: Route) -> Quote | None:
        self.calls += 1
        try:
            return next(self._queue)
        except StopIteration:
            return None


class _KiwiPrimary:
    """Kiwi-like usado como primary: devolve deep_link acionável direto."""

    def __init__(self, price: float):
        self.price = price
        self.calls = 0

    def quote(self, route: Route) -> Quote | None:
        self.calls += 1
        return Quote(
            route=route,
            price_brl=self.price,
            deep_link=f"https://www.kiwi.com/deep/{route.origin}-{route.destination}-2026-06-15",
            departure_date="2026-06-15",
            return_date="2026-06-22",
            source="kiwi",
        )


class _CaptureNotifier:
    def __init__(self):
        self.alerts: list[tuple[Quote, object]] = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append((quote, decision))
        return True

    def send(self, text):  # pragma: no cover
        return True


_UNSET = object()


def _kiwi_quote(price: float, route: Route, deep_link=_UNSET) -> Quote:
    # Distingue "argumento omitido" (usa default Kiwi URL) de "passou None explicito".
    if deep_link is _UNSET:
        deep_link = f"https://www.kiwi.com/deep/{route.origin}-{route.destination}-2026-06-15"
    return Quote(
        route=route,
        price_brl=price,
        deep_link=deep_link,
        departure_date="2026-06-15",
        return_date="2026-06-22",
        source="kiwi",
    )


# ---------- cenários principais ----------

def test_travelpayouts_alert_with_compatible_kiwi_sends_alert(tmp_path: Path):
    """Travelpayouts cota 1500 (<= excellent 1700) + Kiwi 1550 (compatível) → envia."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote(1550.0, _ROUTE_LHR), _kiwi_quote(1550.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert result.non_actionable_links_skipped == 0
    assert result.actionable_links_generated == 1
    assert len(notifier.alerts) == 1
    sent_quote, _decision = notifier.alerts[0]
    # Preço enviado = preço Travelpayouts (radar de oportunidade)
    assert sent_quote.price_brl == 1500.0
    # Link enviado = link Kiwi (comercial)
    assert "kiwi.com" in sent_quote.deep_link
    # Source = composto
    assert sent_quote.source == "travelpayouts+kiwi"


def test_travelpayouts_alert_with_kiwi_none_is_discarded(tmp_path: Path):
    """Travelpayouts alerta mas Kiwi devolve None → descarta."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([None])  # Kiwi não disponível
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.non_actionable_links_skipped == 1
    assert link.calls == 1
    assert notifier.alerts == []
    assert any("link comercial indisponível" in n for n in result.notes)


def test_travelpayouts_alert_with_kiwi_price_30pct_higher_is_discarded(tmp_path: Path):
    """Kiwi 30% acima do primário E acima de good_brl (2000) → descarta."""
    primary = _PrimaryProvider(price=1500.0)
    # 1500 * 1.30 = 1950 — acima de 1500*1.15=1725, e acima de good_brl=2000?
    # good_brl=2000, 1950 < 2000 → passaria pelo critério good_brl. Vamos usar 2100 (40% maior).
    link = _LinkProvider([_kiwi_quote(2100.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.non_actionable_links_skipped == 1
    assert link.calls == 1
    assert any("preço Kiwi incompatível" in n for n in result.notes)


def test_travelpayouts_alert_with_kiwi_no_deep_link_is_discarded(tmp_path: Path):
    """Kiwi devolve quote mas com deep_link=None → descarta."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote(1550.0, _ROUTE_LHR, deep_link=None)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.non_actionable_links_skipped == 1
    assert any("link comercial indisponível" in n for n in result.notes)


def test_kiwi_as_primary_still_works_without_cross_check(tmp_path: Path):
    """Quando primary já é Kiwi, link_provider=None e cross-check não roda."""
    primary = _KiwiPrimary(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert len(notifier.alerts) == 1
    sent_quote, _ = notifier.alerts[0]
    assert sent_quote.source == "kiwi"
    assert "kiwi.com" in sent_quote.deep_link


def test_travelpayouts_without_link_provider_is_silenced(tmp_path: Path):
    """Sem KIWI_API_KEY → sem link_provider → alerta descartado."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.non_actionable_links_skipped == 1
    assert notifier.alerts == []


def test_alert_message_contains_cross_check_text(tmp_path: Path):
    """A mensagem do alerta cross-checked deve trazer 'Travelpayouts + Kiwi' e a nota honesta."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote(1550.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    assert "Travelpayouts + Kiwi" in body
    assert "Preço detectado no radar Travelpayouts. Link de conferência comercial via Kiwi." in body
    # Link Kiwi é o que aparece como hyperlink
    assert "kiwi.com" in body
    # Nenhuma menção a Aviasales
    assert "aviasales" not in body.lower()


def test_link_provider_called_only_when_decision_alert_true(tmp_path: Path):
    """Quote acima de good_brl → não alerta → link_provider NÃO é chamado."""
    primary = _PrimaryProvider(price=2500.0)  # acima de good_brl=2000
    link = _LinkProvider([_kiwi_quote(2400.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert link.calls == 0  # nunca foi chamado
    assert notifier.alerts == []


def test_kiwi_within_15pct_passes(tmp_path: Path):
    """Kiwi 1700 (~13% acima de 1500) → dentro de 15% → envia."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote(1700.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    sent_quote, _ = notifier.alerts[0]
    assert sent_quote.source == "travelpayouts+kiwi"


def test_kiwi_outside_15pct_but_below_good_passes(tmp_path: Path):
    """Kiwi 1900 (26% acima de 1500, fora 15%) MAS abaixo de good_brl=2000 → envia."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote(1900.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    sent_quote, _ = notifier.alerts[0]
    assert sent_quote.source == "travelpayouts+kiwi"


def test_kiwi_outside_15pct_and_above_good_is_discarded(tmp_path: Path):
    """Kiwi 2100 (40% acima de 1500, fora 15%, acima de good_brl=2000) → descarta."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote(2100.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert any("preço Kiwi incompatível" in n for n in result.notes)
