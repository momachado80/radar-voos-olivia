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
from flight_mapper.regions import Cabin, Route
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
            cabin=Cabin.BUSINESS,
            cabin_confirmed=True,
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
        cabin=Cabin.BUSINESS,
        cabin_confirmed=True,
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
    """Mensagem manual não tem hyperlink comercial '🔎 Conferir busca'.

    Pode conter '🔎 Pesquisar no ...' (links auxiliares), mas nunca o link
    comercial confirmado representado por 'Conferir busca'.
    """
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
    assert "🔎 Conferir busca" not in body
    assert "Conferir busca" not in body


def test_manual_fallback_alert_does_not_contain_aviasales(tmp_path: Path):
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
    assert "aviasales" not in body.lower()


def test_manual_fallback_alert_contains_auxiliary_search_links(tmp_path: Path):
    """Alerta manual traz 3 links auxiliares clicáveis e o disclaimer."""
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
    # Disclaimer explícito: links são pesquisa, não oferta confirmada
    assert "Links auxiliares de pesquisa, não oferta confirmada." in body
    assert "não oferta confirmada" in body
    # 3 links clicáveis nomeados
    assert "🔎 <a href=" in body
    assert "Pesquisar no Google" in body
    assert "Pesquisar no Google Flights" in body
    assert "Pesquisar no Kayak" in body
    # cada label corresponde a um hyperlink
    assert body.count('🔎 <a href="') == 3


def test_manual_fallback_auxiliary_links_include_route_date_and_class(tmp_path: Path):
    """Cada URL auxiliar inclui origem, destino, data e business/executiva."""
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
    # extrair somente as URLs entre href="..."
    import re

    urls = re.findall(r'href="([^"]+)"', body)
    assert len(urls) == 3, f"esperava 3 links auxiliares, obteve {urls}"
    for url in urls:
        lowered = url.lower()
        assert "gru" in lowered
        assert "lhr" in lowered
        assert "2026-11-10" in url
        assert "business" in lowered
        # Nenhum dos links auxiliares aponta para Aviasales (todas as formas)
        assert "aviasales" not in lowered
        assert "search.aviasales.com" not in lowered
        assert "aviasales.ru" not in lowered


def test_manual_fallback_alert_never_contains_aviasales_hosts(tmp_path: Path):
    """Garantia explícita: nenhuma das formas conhecidas do Aviasales aparece."""
    primary = _PrimaryProvider(price=1500.0)
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=None, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True).lower()
    assert "aviasales" not in body
    assert "search.aviasales.com" not in body
    assert "aviasales.ru" not in body


def test_kiwi_alert_does_not_contain_auxiliary_search_links(tmp_path: Path):
    """Quando há link comercial confiável (Kiwi), o alerta NÃO mostra links auxiliares."""
    primary = _PrimaryProvider(price=1500.0)
    link = _LinkProvider([_kiwi_quote_with_link(1550.0, _ROUTE_LHR)])
    notifier = _CaptureNotifier()
    store = PriceStore(tmp_path / "h.json")
    monitor = Monitor(
        provider=primary, notifier=notifier, store=store,
        link_provider=link, confirm_alerts=False,
    )

    monitor.run_once([_ROUTE_LHR])

    sent_quote, decision = notifier.alerts[0]
    body = format_alert(sent_quote, decision, priority=True)
    # disclaimer só existe no fluxo manual
    assert "Links auxiliares de pesquisa, não oferta confirmada." not in body
    # labels dos auxiliares não devem aparecer
    assert "Pesquisar no Google" not in body
    assert "Pesquisar no Google Flights" not in body
    assert "Pesquisar no Kayak" not in body
    # alerta com Kiwi mantém "Conferir busca"
    assert "🔎 <a href=" in body and "Conferir busca" in body


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
    assert "⚠️ Link comercial automático indisponível." in body
    assert "Pesquise manualmente: GRU → LHR" in body
    assert "Google Flights" in body
    assert "executiva" in body


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
    # Importante: o Monitor usa wall-clock (datetime.now). O seed precisa
    # estar relativo a esse mesmo relógio — não a uma data fixa — para
    # garantir que o teste seja determinístico em qualquer dia/hora.
    now = datetime.now(timezone.utc)
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
