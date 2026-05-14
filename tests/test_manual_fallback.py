"""Tests para o manual purchase fallback (opção 6a).

Quando o pipeline detecta oportunidade legítima mas não há link comercial
acionável (Kiwi indisponível ou ausente), o Monitor pode enviar alerta
manual sem hyperlink, com instrução de pesquisa em Google Flights / Smiles.

Flag: Monitor(manual_purchase_fallback=True) — default True.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from flight_mapper.monitor import Monitor
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import Route
from flight_mapper.state import PriceStore


_ROUTE_LHR = Route("GRU", "LHR", "Europa")


class _PrimaryProvider:
    """Travelpayouts-like: cota com deep_link=None."""

    def __init__(self, price: float):
        self.price = price
        self.calls = 0

    def quote(self, route: Route) -> Quote | None:
        self.calls += 1
        return Quote(
            route=route,
            price_brl=self.price,
            deep_link=None,
            departure_date="2026-11-10",
            return_date="2026-11-17",
            source="travelpayouts",
        )


class _LinkProvider:
    """Kiwi-like: devolve quotes pré-programadas."""

    def __init__(self, queue: list[Quote | None]):
        self._queue: Iterator[Quote | None] = iter(queue)
        self.calls = 0

    def quote(self, route: Route) -> Quote | None:
        self.calls += 1
        try:
            return next(self._queue)
        except StopIteration:
            return None


class _CaptureNotifier:
    def __init__(self):
        self.alerts: list[tuple[Quote, object]] = []

    def send_alert(self, quote, decision, priority=False):
        self.alerts.append((quote, decision))
        return True

    def send(self, text):  # pragma: no cover
        return True


def _kiwi_quote_with_link(price: float, route: Route) -> Quote:
    return Quote(
        route=route,
        price_brl=price,
        deep_link=f"https://www.kiwi.com/deep/{route.origin}-{route.destination}-2026-11-10",
        departure_date="2026-11-10",
        return_date="2026-11-17",
        source="kiwi",
    )


# ---------- Caminho feliz: Kiwi disponível, alerta com link ----------

def test_alert_with_kiwi_link_does_not_trigger_manual_fallback(tmp_path: Path):
    """Kiwi compatível existe → alerta normal com link, não manual."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote_with_link(1550.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
        manual_purchase_fallback=True,  # mesmo ligado, fallback NÃO ativa
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert result.manual_fallback_alerts_sent == 0
    assert result.actionable_links_generated == 1
    sent_quote, _ = notifier.alerts[0]
    assert sent_quote.source == "travelpayouts+kiwi"


# ---------- Manual fallback ativo: alerta sem link, com instrução ----------

def test_manual_fallback_sends_alert_when_no_commercial_link(tmp_path: Path):
    """Sem Kiwi E manual_purchase_fallback=True → envia alerta manual."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
        manual_purchase_fallback=True,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 1
    assert result.manual_fallback_alerts_sent == 1
    assert result.actionable_links_generated == 0
    assert result.non_actionable_links_skipped == 0
    sent_quote, _ = notifier.alerts[0]
    assert sent_quote.source == "manual_purchase"
    assert sent_quote.deep_link is None


def test_manual_fallback_alert_does_not_contain_conferir_busca(tmp_path: Path):
    """Mensagem manual não tem hyperlink '🔎 Conferir busca'."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    assert "🔎" not in body
    assert "Conferir busca" not in body
    assert "<a href" not in body


def test_manual_fallback_alert_does_not_contain_aviasales_as_link(tmp_path: Path):
    """Aviasales pode ser citado no texto explicativo, mas NUNCA como URL/link clicável."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    # Nunca como URL/hyperlink
    assert "aviasales.com" not in body.lower()
    assert "aviasales.ru" not in body.lower()
    assert "search.aviasales" not in body.lower()
    assert "<a href" not in body
    assert "🔎" not in body
    # Mas a menção como texto explicativo é permitida (e desejada)
    assert "Aviasales foi bloqueado" in body


def test_manual_fallback_alert_contains_manual_search_instruction(tmp_path: Path):
    """Mensagem manual contém instrução clara de pesquisa manual."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    # Texto novo
    assert "⚠️ Link de compra confiável indisponível." in body
    assert "Aviasales foi bloqueado porque abriu experiência inadequada." in body
    assert "Pesquise manualmente: GRU → LHR" in body
    assert "Google Flights" in body
    assert "milhas" in body
    assert "executiva" in body


# ---------- Sugestões regionais (EUA / Europa / Ásia) ----------

def _build_manual_alert_text(route, price=1500.0):
    """Helper: monta alerta manual diretamente via format_alert."""
    from flight_mapper.detector import CRITERION_CEILING, LEVEL_GOOD, Decision
    q = Quote(
        route=route,
        price_brl=price,
        deep_link=None,
        departure_date="2026-11-10",
        return_date="2026-11-17",
        source="manual_purchase",
    )
    d = Decision(
        alert=True, reason="...",
        criterion=CRITERION_CEILING, threshold=2000.0,
        level=LEVEL_GOOD, score=65,
    )
    return format_alert(q, d, priority=True)


def test_manual_fallback_eua_route_lists_us_airlines():
    body = _build_manual_alert_text(Route("GRU", "MIA", "EUA"))
    assert "American Airlines" in body
    assert "United" in body
    assert "Delta" in body
    assert "Latam" in body
    assert "Google Flights" in body
    assert "programas de milhas" in body


def test_manual_fallback_europa_route_lists_european_airlines():
    body = _build_manual_alert_text(Route("GRU", "LHR", "Europa"))
    assert "Iberia" in body
    assert "Air France/KLM" in body
    assert "TAP" in body
    assert "Latam" in body
    assert "Google Flights" in body
    assert "programas de milhas" in body
    # Não vaza sugestões de outras regiões
    assert "American Airlines" not in body
    assert "Emirates" not in body


def test_manual_fallback_asia_route_lists_asian_carriers():
    body = _build_manual_alert_text(Route("GRU", "DXB", "Ásia"))
    assert "Emirates" in body
    assert "Qatar" in body
    assert "Turkish" in body
    assert "Google Flights" in body
    assert "programas de milhas" in body
    assert "American Airlines" not in body
    assert "Iberia" not in body


def test_manual_fallback_unknown_region_uses_generic_suggestion():
    """Rota em região fora do mapa cai em sugestão genérica, sem crash."""
    body = _build_manual_alert_text(Route("GRU", "XXX", "Oceania"))
    assert "Google Flights" in body
    assert "programa de milhas" in body  # versão singular do fallback


def test_manual_fallback_alert_contains_route_dates_price_class(tmp_path: Path):
    """Mensagem manual contém todos os dados para pesquisa: rota, datas, preço, classe."""
    primary = _PrimaryProvider(price=1878.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    # rota humanizada
    assert "São Paulo → Londres" in body
    assert "GRU → LHR" in body
    # preço
    assert "R$ 1.878" in body
    # datas
    assert "2026-11-10" in body
    assert "2026-11-17" in body
    # classe
    assert "executiva" in body or "Business" in body


# ---------- Manual fallback desativado: silêncio explícito ----------

def test_manual_fallback_off_discards_alert(tmp_path: Path):
    """manual_purchase_fallback=False E sem link → descarta como antes."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
        manual_purchase_fallback=False,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.manual_fallback_alerts_sent == 0
    assert result.non_actionable_links_skipped == 1
    assert notifier.alerts == []


# ---------- Caminhos paralelos preservados ----------

def test_manual_fallback_does_not_apply_when_kiwi_price_incompatible(tmp_path: Path):
    """Se Kiwi disse 'preço incompatível', confiança no preço primário cai →
    NÃO ativa manual fallback (descarta)."""
    primary = _PrimaryProvider(price=1500.0)
    # Kiwi 2100: 40% acima de primary E acima de good_brl=2000 → incompatível
    link = _LinkProvider([_kiwi_quote_with_link(2100.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
        manual_purchase_fallback=True,
    )

    result = monitor.run_once([_ROUTE_LHR])

    # Manual fallback NÃO dispara quando Kiwi explicitamente discordou do preço
    assert result.alerts_sent == 0
    assert result.manual_fallback_alerts_sent == 0
    assert result.non_actionable_links_skipped == 1


def test_manual_fallback_respects_dedupe(tmp_path: Path):
    """Dedupe continua bloqueando re-envio em janela mesmo no fallback manual."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")

    # Pré-seed: alerta recente para essa rota com preço próximo
    h = store.get("GRU-LHR-business")
    h.last_alert_at = (now - timedelta(hours=2)).isoformat()
    h.last_alert_price = 1500.0

    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
        manual_purchase_fallback=True,
    )
    result = monitor.run_once([_ROUTE_LHR])

    # Dedupe ativo: nenhuma decisão.alert=True → manual fallback nem é considerado
    assert result.alerts_sent == 0
    assert result.manual_fallback_alerts_sent == 0


def test_no_alerts_below_thresholds_with_manual_fallback_on(tmp_path: Path):
    """Preço acima de good_brl → decision.alert=False → nada (manual fallback irrelevante)."""
    primary = _PrimaryProvider(price=2500.0)  # acima de good_brl=2000 do GRU-LHR
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
        manual_purchase_fallback=True,
    )

    result = monitor.run_once([_ROUTE_LHR])

    assert result.alerts_sent == 0
    assert result.manual_fallback_alerts_sent == 0
