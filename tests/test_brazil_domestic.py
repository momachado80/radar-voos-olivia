"""Testes do PR #87 — trechos DOMÉSTICOS brasileiros (CGH ↔ SSA / BSB).

A Olivia pediu promoções São Paulo (Congonhas) ↔ Salvador e São Paulo
(Congonhas) ↔ Brasília, nos dois sentidos. Isso exigiu destravar TRÊS
bloqueios que fariam a promo doméstica falhar em SILÊNCIO:

1. **Pool só saía de GRU.** `build_broad_candidate_pool` fixava
   `origin="GRU"`; SSA→CGH e BSB→CGH eram impossíveis de expressar.
2. **Piso de sanidade internacional.** `SUSPICIOUS_FLOOR_BRL` exigia
   ≥ R$ 1.000 p/ econômica ida — uma promo real CGH→BSB (~R$ 210) seria
   bloqueada como "preço suspeito" e nunca viraria alerta.
3. **Teto não escalado p/ oferta BRL-nativa.** Os tetos são USD; quando a
   oferta vinha em BRL o código comparava R$ contra número USD cru. Rota
   doméstica é justamente onde a Duffel devolve BRL nativo ⇒ silêncio.

Cobre ainda a calibração "só promoção" e os rótulos de aeroporto.
"""

from __future__ import annotations

from datetime import date

import pytest

from flight_mapper.airports import humanize_route
from flight_mapper.detector import (
    CRITERION_CEILING,
    LEVEL_GOOD,
    Decision,
    evaluate_ceiling,
)
from flight_mapper.duffel_broad import (
    BROAD_DOMESTIC_SPECS,
    DOMESTIC_LOOKAHEAD_DAYS,
    build_broad_candidate_pool,
)
from flight_mapper.duffel_watchlist import DuffelWatchEntry, DuffelWatchlistState
from flight_mapper.monitor import Monitor
from flight_mapper.notifier import format_alert
from flight_mapper.providers import Quote
from flight_mapper.regions import (
    BRAZIL_AIRPORTS,
    Cabin,
    Route,
    TripType,
    is_domestic_brazil,
)
from flight_mapper.sanity import is_suspicious_price, suspicious_reason
from flight_mapper.state import PriceStore, RouteHistory
from flight_mapper.thresholds import levels_for


RATE = 5.5  # USD→BRL usado nas asserções de calibração


# ----------------- helpers -----------------


class _StubNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages: list[str] = []
        self.grouped: list[str] = []

    def send_alert(self, quote, decision, priority=False) -> bool:
        self.messages.append(format_alert(quote, decision, priority=priority))
        return self.ok

    def send(self, text) -> bool:
        self.grouped.append(text)
        return self.ok


class _ScriptedDuffel:
    def __init__(self, by_dates=None):
        self.calls: list[tuple] = []
        self._by_dates = by_dates or {}

    def quote_for_dates(self, route, outbound_date, return_date=None, *,
                        cabin="business"):
        self.calls.append((route.key, outbound_date, return_date, cabin))
        return self._by_dates.get((route.key, outbound_date, return_date))


def _alerts(threshold_key: str, price_brl: float) -> bool:
    """Roda o gate de teto com escala USD→BRL, como o pass Duffel faz."""
    return evaluate_ceiling(
        RouteHistory(), price_brl, threshold_key,
        priority=False, brl_rate=RATE,
    ).alert


# ----------------- 1. geografia / is_domestic_brazil -----------------


@pytest.mark.parametrize("org,dst", [
    ("CGH", "SSA"), ("SSA", "CGH"), ("CGH", "BSB"), ("BSB", "CGH"),
    ("GRU", "GIG"), ("SDU", "CNF"),
])
def test_is_domestic_brazil_true_for_brazilian_pairs(org, dst):
    assert is_domestic_brazil(Route(org, dst, "Brasil")) is True


@pytest.mark.parametrize("org,dst", [
    ("GRU", "LHR"), ("GRU", "MIA"), ("LHR", "CDG"), ("CGH", "EZE"),
])
def test_is_domestic_brazil_false_for_international(org, dst):
    assert is_domestic_brazil(Route(org, dst, "Europa")) is False


def test_is_domestic_brazil_defensive_on_none_route():
    """Alguns caminhos do relatório chamam a sanidade sem `Route` real.
    Rota ausente ⇒ internacional (piso mais rígido) e NUNCA AttributeError."""
    assert is_domestic_brazil(None) is False


def test_brazil_airports_include_the_requested_ones():
    for code in ("CGH", "SSA", "BSB"):
        assert code in BRAZIL_AIRPORTS


def test_airport_labels_are_humanized():
    assert humanize_route("CGH", "SSA") == "São Paulo → Salvador (CGH → SSA)"
    assert humanize_route("BSB", "CGH") == "Brasília → São Paulo (BSB → CGH)"


# ----------------- 2. pool cobre os 4 trechos, nos dois sentidos -----------------


def test_pool_contains_all_four_domestic_pairs_both_directions():
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    pairs = {(e.route.origin, e.route.destination) for e in pool}
    for org, dst, _region, _city in BROAD_DOMESTIC_SPECS:
        assert (org, dst) in pairs, f"faltou {org}→{dst}"
    # Os DOIS sentidos de cada trecho pedido.
    assert ("CGH", "SSA") in pairs and ("SSA", "CGH") in pairs
    assert ("CGH", "BSB") in pairs and ("BSB", "CGH") in pairs


def test_domestic_pairs_cover_both_cabins_and_trip_types():
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    for org, dst, _r, _c in BROAD_DOMESTIC_SPECS:
        combos = {
            (e.cabin, e.route.trip_type)
            for e in pool
            if (e.route.origin, e.route.destination) == (org, dst)
        }
        assert combos == {
            ("business", TripType.ROUND_TRIP),
            ("business", TripType.ONE_WAY),
            ("economy", TripType.ROUND_TRIP),
            ("economy", TripType.ONE_WAY),
        }, f"{org}→{dst} incompleto: {combos}"


def test_domestic_entries_come_first_in_rotation():
    """A Olivia priorizou o doméstico — a rotação deve cobri-lo já nos
    primeiros ciclos após o deploy, não depois de 24 destinos."""
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    first = [(e.route.origin, e.route.destination) for e in pool[:4]]
    assert first == [
        (org, dst) for org, dst, _r, _c in BROAD_DOMESTIC_SPECS
    ]


def test_domestic_uses_shorter_booking_window_than_international():
    """Promo doméstica abre com menos antecedência e a viagem é mais curta:
    a janela doméstica não pode ser a internacional de 90d/10 noites."""
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    dom = next(e for e in pool if e.route.origin == "CGH")
    intl = next(e for e in pool if e.route.origin == "GRU")
    assert dom.outbound_date < intl.outbound_date
    assert dom.outbound_date == "2026-07-16"  # 2026-06-01 + 45d
    assert DOMESTIC_LOOKAHEAD_DAYS < 90


# ----------------- 3. TRAVA: todo trecho doméstico tem teto -----------------


def test_every_domestic_entry_has_a_threshold():
    """Sem teto, `levels_for` devolve None, `evaluate_ceiling` responde
    alert=False e a promo NUNCA vira alerta — falha silenciosa. Este teste
    impede que um trecho doméstico entre no pool sem o teto."""
    pool = build_broad_candidate_pool(today=date(2026, 6, 1))
    domestic = [e for e in pool if is_domestic_brazil(e.route)]
    assert domestic, "pool sem entradas domésticas"
    faltando = [
        e.threshold_key for e in domestic if levels_for(e.threshold_key) is None
    ]
    assert not faltando, f"trechos domésticos sem teto: {faltando}"


def test_domestic_thresholds_are_far_below_international():
    """Sanidade da calibração: doméstico é ordem de grandeza menor."""
    assert (
        levels_for("CGH-SSA-economy")["good_brl"]
        < levels_for("GRU-LHR-economy")["good_brl"]
    )
    assert (
        levels_for("CGH-BSB-business")["good_brl"]
        < levels_for("GRU-LHR-business")["good_brl"]
    )


def test_both_directions_share_the_same_threshold():
    """CGH→SSA e SSA→CGH têm o mesmo nível de preço; tetos assimétricos
    fariam um sentido alertar e o outro não, sem motivo."""
    for a, b in (("CGH-SSA", "SSA-CGH"), ("CGH-BSB", "BSB-CGH")):
        for suffix in ("business", "economy",
                       "one_way-business", "one_way-economy"):
            assert levels_for(f"{a}-{suffix}") == levels_for(f"{b}-{suffix}"), (
                f"{a}-{suffix} != {b}-{suffix}"
            )


# ----------------- 4. TRAVA: piso de sanidade doméstico -----------------


def _usd_quote(route, brl, cabin=Cabin.ECONOMY, trip=TripType.ONE_WAY):
    """Oferta em USD já convertida p/ BRL — é o caso em que o piso roda."""
    return Quote(
        route=route, price_brl=brl, deep_link=None,
        departure_date="2026-07-16", return_date=None, source="duffel",
        amount=brl / RATE, currency="USD", amount_brl_estimated=brl,
        fx_rate=RATE, cabin=cabin, cabin_confirmed=True, trip_type=trip,
    )


@pytest.mark.parametrize("brl", [210.0, 250.0, 400.0])
def test_domestic_promo_is_not_flagged_suspicious(brl):
    """REGRESSÃO do bloqueio silencioso: com o piso internacional
    (R$ 1.000 p/ econômica ida) toda promo doméstica seria barrada."""
    r = Route("CGH", "BSB", "Brasil", trip_type=TripType.ONE_WAY)
    assert is_suspicious_price(r, _usd_quote(r, brl), brl) is False


def test_domestic_floor_still_catches_absurd_price():
    """O piso não sumiu — só foi recalibrado. Tarifa em USD rotulada como
    BRL (o bug que o piso existe p/ pegar) continua bloqueada."""
    r = Route("CGH", "BSB", "Brasil", trip_type=TripType.ONE_WAY)
    assert is_suspicious_price(r, _usd_quote(r, 30.0), 30.0) is True
    reason = suspicious_reason(r, _usd_quote(r, 30.0), 30.0)
    assert "doméstico" in reason


def test_international_floor_unchanged():
    """O piso internacional NÃO pode ter afrouxado — o caso-bug original
    (US$ 232 GRU→MIA ≈ R$ 1.276 business ida) segue bloqueado."""
    r = Route("GRU", "MIA", "EUA", trip_type=TripType.ONE_WAY)
    q = _usd_quote(r, 1276.0, cabin=Cabin.BUSINESS)
    assert is_suspicious_price(r, q, 1276.0) is True
    assert "internacional" in suspicious_reason(r, q, 1276.0)


# ----------------- 5. TRAVA: teto escala mesmo com oferta BRL-nativa -----------------


def test_brl_native_quote_scales_threshold_end_to_end(tmp_path, monkeypatch):
    """REGRESSÃO do 3º bloqueio silencioso. A Duffel devolve BRL nativo em
    trecho doméstico. Antes, oferta BRL pulava a escala USD→BRL e comparava
    R$ 450 contra o teto cru `125` ⇒ nunca alertava. Agora escala sempre.
    """
    monkeypatch.setenv("USD_BRL_RATE", str(RATE))
    entry = DuffelWatchEntry(
        route=Route("CGH", "SSA", "Brasil",
                    trip_type=TripType.ROUND_TRIP, cabin=Cabin.BUSINESS),
        outbound_date="2026-07-16", return_date="2026-07-20", cabin="economy",
    )
    assert entry.threshold_key == "CGH-SSA-economy"

    # R$ 450 ida e volta CGH→SSA: promo real, abaixo do "bom" (125 × 5,5).
    quote = Quote(
        route=entry.route, price_brl=450.0, deep_link=None,
        departure_date=entry.outbound_date, return_date=entry.return_date,
        source="duffel", amount=450.0, currency="BRL",
        amount_brl_estimated=450.0,
        cabin=Cabin.ECONOMY, cabin_confirmed=True,
        trip_type=TripType.ROUND_TRIP, airline="G3",
    )
    notifier = _StubNotifier()
    monitor = Monitor(
        provider=object(), notifier=notifier,
        store=PriceStore(tmp_path / "main.json"),
        duffel_provider=_ScriptedDuffel(by_dates={
            (entry.route.key, entry.outbound_date, entry.return_date): quote,
        }),
        duffel_store=PriceStore(tmp_path / "duffel.json"),
        duffel_max_requests=0,
        duffel_watchlist=[entry],
        duffel_watchlist_max_requests=1,
        duffel_watchlist_state=DuffelWatchlistState(path=None, offset=0),
        duffel_order_flow_alert_mode="grouped_push",
    )
    monitor.run_duffel_confirmations(routes=[])
    assert notifier.grouped, (
        "promo doméstica BRL-nativa deveria alertar — se falhou, o teto "
        "voltou a ser comparado sem escala USD→BRL"
    )
    msg = notifier.grouped[0]
    assert "São Paulo → Salvador" in msg


# ----------------- 6. calibração "só promoção" -----------------


@pytest.mark.parametrize("tkey,promo,tabela", [
    ("CGH-SSA-economy", 450, 1100),
    ("SSA-CGH-economy", 500, 1200),
    ("CGH-BSB-economy", 380, 900),
    ("BSB-CGH-one_way-economy", 210, 520),
    ("CGH-SSA-one_way-economy", 240, 600),
    ("CGH-SSA-business", 1150, 2600),
    ("CGH-BSB-business", 950, 2200),
    ("BSB-CGH-one_way-business", 520, 1300),
])
def test_promo_alerts_and_table_fare_stays_silent(tkey, promo, tabela):
    assert _alerts(tkey, promo), f"{tkey}: R$ {promo} devia ALERTAR"
    assert not _alerts(tkey, tabela), f"{tkey}: R$ {tabela} devia silenciar"


def test_domestic_promo_clears_the_domestic_sanity_floor():
    """Coerência entre as duas travas: o preço que o TETO aceita não pode
    ser barrado pelo PISO — senão o alerta morre no gate seguinte."""
    from flight_mapper.sanity import DOMESTIC_SUSPICIOUS_FLOOR_BRL
    casos = [
        ("CGH-BSB-one_way-economy", TripType.ONE_WAY, Cabin.ECONOMY),
        ("CGH-SSA-economy", TripType.ROUND_TRIP, Cabin.ECONOMY),
        ("CGH-SSA-one_way-business", TripType.ONE_WAY, Cabin.BUSINESS),
        ("CGH-SSA-business", TripType.ROUND_TRIP, Cabin.BUSINESS),
    ]
    for tkey, trip, cabin in casos:
        excellent_brl = levels_for(tkey)["excellent_brl"] * RATE
        floor = DOMESTIC_SUSPICIOUS_FLOOR_BRL[(trip, cabin)]
        assert floor < excellent_brl, (
            f"{tkey}: piso R$ {floor:.0f} barraria promo excelente "
            f"R$ {excellent_brl:.0f}"
        )
