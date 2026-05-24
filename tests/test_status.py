from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from flight_mapper.monitor import MonitorResult
from flight_mapper.state import PriceStore
from flight_mapper.status import StatusState, maybe_send_status


class _StubNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok

    def send_alert(self, *args, **kwargs):  # pragma: no cover - unused
        return True


def _result(scanned: int = 12, quotes: int = 6, alerts: int = 0) -> MonitorResult:
    return MonitorResult(
        scanned=scanned,
        quotes_received=quotes,
        alerts_sent=alerts,
        notes=[],
    )


def _populate(store: PriceStore, prices: dict[str, list[float]]) -> None:
    for key, values in prices.items():
        history = store.get(key)
        for value in values:
            history.push(value)
    store.save()


def _populate_q(store: PriceStore, prices: dict[str, list[float]]) -> None:
    """Como `_populate`, mas anexa last_quote com moeda comprovada (BRL,
    cabine não confirmada) — entra no painel como sinal bruto."""
    for key, values in prices.items():
        history = store.get(key)
        for value in values:
            history.push(value)
        parts = key.split("-")
        o, d = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        last = history.prices[-1]
        history.last_quote = {
            "origin": o, "destination": d,
            "departure_date": "2026-09-10", "return_date": "2026-09-17",
            "source": "travelpayouts", "currency": "BRL",
            "amount": last, "amount_brl_estimated": last,
            "cabin": "unknown", "cabin_confirmed": False,
            "trip_type": "round_trip", "actionable_url": False,
            "deep_link": None,
        }
    store.save()


def test_first_run_sends(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    state = StatusState()
    notifier = _StubNotifier()
    state_path = tmp_path / "status.json"

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=state_path,
    )

    assert decision.action == "sent"
    assert decision.reason == "first_run"
    assert len(notifier.sent) == 1
    assert state_path.exists()
    assert state.last_report_at is not None


def test_throttle_blocks_within_window(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    state = StatusState(last_report_at=(now - timedelta(hours=23)).isoformat())
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=tmp_path / "status.json",
        now=now,
    )

    assert decision.action == "skipped"
    assert decision.reason == "throttled"
    assert notifier.sent == []


def test_sends_after_window(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    state = StatusState(last_report_at=(now - timedelta(hours=25)).isoformat())
    notifier = _StubNotifier()
    state_path = tmp_path / "status.json"

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=state_path,
        now=now,
    )

    assert decision.action == "sent"
    assert decision.reason == "window_elapsed"
    assert len(notifier.sent) == 1
    assert state.last_report_at == now.isoformat()


def test_top3_ordering(tmp_path: Path):
    # Valores ≥ piso business round_trip (R$ 4.000) → todos caem em
    # "Sinais brutos de preço" (não econômica, não confirmada). Testa
    # ordenação por preço e cap de 3 na seção.
    store = PriceStore(tmp_path / "h.json")
    _populate_q(
        store,
        {
            "GRU-LHR-business": [9000.0, 4500.0],
            "GRU-MIA-business": [4100.0],
            "GRU-ORD-business": [4300.0],
            "GRU-FRA-business": [9000.0],
            "GRU-SFO-business": [7000.0],
        },
    )
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    assert decision.action == "sent"
    body = notifier.sent[0]
    raw = body.split("👀 Sinais em observação")[1].split("🛡️")[0]
    miami_idx = raw.index("GRU → MIA")
    ord_idx = raw.index("GRU → ORD")
    lhr_idx = raw.index("GRU → LHR")
    assert miami_idx < ord_idx < lhr_idx  # 4100 < 4300 < 4500
    # cap de 3 na seção: as 2 mais caras (SFO 7000, FRA 9000) ficam de fora
    assert "GRU → FRA" not in raw
    assert "GRU → SFO" not in raw
    assert "São Paulo → Miami" in raw
    assert "São Paulo → Chicago" in raw
    assert "São Paulo → Londres" in raw


def test_daily_report_omits_aviasales_links(tmp_path: Path):
    """Relatório diário é heartbeat — não traz links acionáveis nem genéricos."""
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [1207.0], "GRU-LHR-business": [1800.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # padrão antigo
    assert "https://www.aviasales.com/search" not in body
    # padrão novo (parametrizado) também não
    assert "search.aviasales.com/flights" not in body
    # nada de hyperlinks
    assert "<a href" not in body
    assert "conferir" not in body
    assert "Abrir oferta" not in body


def test_status_includes_regional_best_section(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate_q(
        store,
        {
            "GRU-LHR-business": [1800.0],
            "GRU-FRA-business": [3322.0],   # Europa, mais caro que LHR
            "GRU-MIA-business": [1207.0],   # EUA
            "GRU-ORD-business": [1631.0],   # EUA, mais caro que MIA
            "GRU-DXB-business": [2798.0],   # Ásia
        },
    )
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # Sem last_quote → tudo é sinal bruto (cabine não confirmada).
    assert "🟢 Executiva confirmada" in body
    assert "• Nenhuma executiva confirmada agora." in body
    assert "👀 Sinais em observação" in body
    assert "🧭 Status das fontes" in body
    # 3 mais baratas aparecem como sinal bruto: MIA(1207) ORD(1631) LHR(1800)
    assert "São Paulo → Miami (GRU → MIA)" in body
    assert "São Paulo → Chicago (GRU → ORD)" in body
    assert "São Paulo → Londres (GRU → LHR)" in body
    assert "Cabine: não confirmada" in body
    # estrutura antiga aposentada
    assert "📌 Melhores oportunidades monitoradas" not in body
    assert "🌎 Melhor por região" not in body
    assert "Executiva:" not in body


def test_daily_report_shows_link_when_last_quote_actionable(tmp_path: Path):
    """Quando RouteHistory.last_quote tem deep_link acionável (Kiwi), mostra 🔎 no top-3 e regional."""
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-MIA-business")
    history.push(1207.0)
    history.last_quote = {
        "price_brl": 1207.0,
        "origin": "GRU",
        "destination": "MIA",
        "departure_date": "2026-06-15",
        "return_date": "2026-06-22",
        "source": "kiwi",
        "deep_link": "https://www.kiwi.com/deep/GRU-MIA-2026-06-15",
        "detected_at": "2026-05-12T17:30:00+00:00",
        "actionable_url": True,
        "cabin": "business",
        "cabin_confirmed": True,
        "currency": "BRL",
        "amount": 1207.0,
        "amount_brl_estimated": 1207.0,
        "trip_type": "round_trip",
        "provider_note": None,
    }
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # confirmada (Kiwi business BRL) → seção de oportunidades + link
    assert "🟢 Executiva confirmada" in body
    assert "Executiva" in body
    assert "kiwi.com" in body
    assert "Conferir busca" in body
    # Aviasales jamais aparece
    assert "aviasales" not in body


def test_daily_report_omits_link_when_no_last_quote(tmp_path: Path):
    """Sem last_quote, relatório fica sem link mesmo no top-3."""
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [1207.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "search.aviasales.com/flights" not in body
    assert "Conferir busca" not in body


def test_daily_report_omits_link_when_last_quote_not_actionable(tmp_path: Path):
    """last_quote com deep_link quebrado (padrão antigo) → sem link."""
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-MIA-business")
    history.push(1207.0)
    history.last_quote = {
        "price_brl": 1207.0,
        "origin": "GRU",
        "destination": "MIA",
        "departure_date": "2026-06-15",
        "return_date": None,
        "source": "travelpayouts",
        "deep_link": "https://www.aviasales.com/search/GRUMIA",  # padrão antigo
        "detected_at": "2026-05-12T17:30:00+00:00",
        "actionable_url": False,
        "cabin": "business",
        "provider_note": None,
    }
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "Conferir busca" not in body
    assert "GRUMIA" not in body  # URL antiga jamais aparece


def test_daily_report_omits_link_when_last_quote_route_mismatch(tmp_path: Path):
    """Se last_quote.origin/destination diferem da route key, ignora."""
    store = PriceStore(tmp_path / "h.json")
    history = store.get("GRU-MIA-business")
    history.push(1207.0)
    history.last_quote = {
        "price_brl": 1207.0,
        "origin": "ZZZ",  # diferente do route key
        "destination": "MIA",
        "departure_date": "2026-06-15",
        "return_date": "2026-06-22",
        "source": "kiwi",
        "deep_link": "https://www.kiwi.com/deep/ZZZ-MIA-2026-06-15",
        "detected_at": "2026-05-12T17:30:00+00:00",
        "actionable_url": True,
        "cabin": "business",
        "provider_note": None,
    }
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "Conferir busca" not in body


def test_status_includes_average_score_line(tmp_path: Path):
    """Score médio só rotula oportunidades CONFIRMADAS (Regra 6)."""
    store = PriceStore(tmp_path / "h.json")
    h = store.get("GRU-MIA-business")
    h.push(8000.0)
    h.last_quote = {
        "origin": "GRU", "destination": "MIA",
        "departure_date": "2026-11-10", "return_date": "2026-11-17",
        "source": "kiwi", "currency": "BRL", "amount": 8000.0,
        "amount_brl_estimated": 8000.0, "cabin": "business",
        "cabin_confirmed": True, "trip_type": "round_trip",
        # PR #51: score só pinta a seção 🟢 (Executiva confirmada com
        # link clicável). Sem deep_link a rota cai em 🟡 (Verificação
        # manual) e não recebe score — comportamento intencional.
        "actionable_url": True,
        "deep_link": "https://www.kiwi.com/deep/GRU-MIA-2026-11-10",
    }
    store.save()
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "⭐ Score médio (executiva confirmada):" in body
    assert "/100" in body
    # nunca o rótulo antigo (não pode parecer score de Top 3 bruto)
    assert "⭐ Score médio do Top 3:" not in body


def test_status_uses_watchlist_section_title(tmp_path: Path):
    """Heartbeat usa '📌 Melhores oportunidades monitoradas', não 'Melhor por watchlist'."""
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [1207.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # novas seções
    assert "🟢 Executiva confirmada" in body
    assert "👀 Sinais em observação" in body
    assert "🧭 Status das fontes" in body
    # estrutura/jargão antigos não vazam
    assert "📌 Melhores oportunidades monitoradas" not in body
    assert "Melhor por watchlist" not in body
    assert "watchlist" not in body.lower()


def test_regional_section_renames_asia_for_display(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate_q(store, {"GRU-DXB-business": [2798.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # rota aparece como sinal bruto, humanizada; sem rótulo regional antigo
    assert "São Paulo → Dubai (GRU → DXB)" in body
    assert "Cabine: não confirmada" in body
    assert "• Ásia:" not in body
    assert "📌 Melhores oportunidades monitoradas" not in body


def test_status_uses_brl_with_dot_separator(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [10000.0]})
    # Moeda comprovadamente BRL (Kiwi) → relatório pode exibir R$.
    h = store.get("GRU-MIA-business")
    h.last_quote = {
        "origin": "GRU",
        "destination": "MIA",
        "currency": "BRL",
        "amount": 10000.0,
        "amount_brl_estimated": 10000.0,
        "departure_date": "2026-11-10",
    }
    store.save()
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "R$ 10.000" in body
    assert "R$ 10,000" not in body


def test_status_does_not_show_plain_brl_for_unproven_currency(tmp_path: Path):
    """Test 7: histórico legado (sem moeda comprovada) NÃO vira R$ enganoso."""
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-MIA-business": [1919.0]})  # valor "USD" do bug
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    # Rule 4: entrada legada sem moeda comprovada é OMITIDA do painel,
    # não exibida como "moeda não confirmada".
    assert "R$ 1.919" not in body
    assert "moeda não confirmada" not in body
    assert "Entradas legadas sem moeda comprovada (omitidas): 1" in body
    assert "• Nenhum sinal em observação no momento." in body


def test_top3_handles_unknown_airport(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate_q(store, {"XYZ-ABC-business": [999.0]})
    notifier = _StubNotifier()

    maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    body = notifier.sent[0]
    assert "XYZ → ABC" in body
    # relatório diário não inclui links — nem para rotas conhecidas, nem desconhecidas
    assert "https://www.aviasales.com/search" not in body
    assert "search.aviasales.com/flights" not in body


def test_does_not_persist_when_send_fails(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    state_path = tmp_path / "status.json"
    state = StatusState()
    notifier = _StubNotifier(ok=False)

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=state,
        notifier=notifier,
        state_path=state_path,
    )

    assert decision.action == "failed"
    assert decision.reason == "telegram_send_failed"
    assert state.last_report_at is None
    assert not state_path.exists()


def test_zero_quotes_still_renders_full_panel(tmp_path: Path):
    """Sem template degradado: mesmo com 0 cotações, o painel completo
    é renderizado (Rule 1/2)."""
    store = PriceStore(tmp_path / "h.json")
    _populate(store, {"GRU-LHR-business": [1800.0]})
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(scanned=12, quotes=0, alerts=0),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    assert decision.action == "sent"
    body = notifier.sent[0]
    # painel completo sempre
    assert "📊 Ciclo recente" in body
    assert "• Cotações obtidas: 0" in body
    assert "🟢 Executiva confirmada" in body
    assert "👀 Sinais em observação" in body
    assert "💸 Econômica possível" in body
    assert "🛡️ Bloqueios de segurança" in body
    assert "🧭 Status das fontes" in body
    # fallback antigo eliminado
    assert "Retornou 0 cotações" not in body
    assert "Top 3" not in body


def test_empty_store_does_not_crash(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    notifier = _StubNotifier()

    decision = maybe_send_status(
        result=_result(scanned=12, quotes=6, alerts=0),
        store=store,
        state=StatusState(),
        notifier=notifier,
        state_path=tmp_path / "status.json",
    )

    assert decision.action == "sent"
    body = notifier.sent[0]
    assert "• Nenhuma executiva confirmada agora." in body
    assert "• Nenhum sinal em observação no momento." in body


def test_no_notifier_skips_cleanly(tmp_path: Path):
    store = PriceStore(tmp_path / "h.json")
    state_path = tmp_path / "status.json"

    decision = maybe_send_status(
        result=_result(),
        store=store,
        state=StatusState(),
        notifier=None,
        state_path=state_path,
    )

    assert decision.action == "skipped"
    assert decision.reason == "no_notifier"
    assert not state_path.exists()


def test_status_state_round_trip(tmp_path: Path):
    path = tmp_path / "status.json"
    state = StatusState(last_report_at="2026-05-10T12:00:00+00:00")
    state.save(path)
    reloaded = StatusState.load(path)
    assert reloaded.last_report_at == "2026-05-10T12:00:00+00:00"


def test_status_state_load_handles_corrupt_file(tmp_path: Path):
    path = tmp_path / "status.json"
    path.write_text("not json", encoding="utf-8")
    state = StatusState.load(path)
    assert state.last_report_at is None
