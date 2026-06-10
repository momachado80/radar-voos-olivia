"""Testes do PR #83 — humanização de IATA de companhia aérea.

Cobre `flight_mapper/airlines.py`:
1. `airline_name(iata)` mapeia IATA conhecido p/ nome comercial.
2. IATA desconhecido / vazio / None ⇒ None (não inventa).
3. Insensível a case e espaços (`" af "` → "Air France").
4. `airline_label(iata)` formata "Nome (IATA)" e cai pro IATA bruto se
   desconhecido.
5. O conjunto cobre, no mínimo, todas as cias que operam (com vôo direto
   ou conexão típica) as rotas do pool broad PR #81.
"""

from __future__ import annotations

import pytest

from flight_mapper.airlines import AIRLINES, airline_label, airline_name


# ---------------- mapeamento básico ----------------


@pytest.mark.parametrize(
    "iata,expected",
    [
        ("AF", "Air France"),
        ("LA", "LATAM"),
        ("TP", "TAP Air Portugal"),
        ("NH", "ANA"),
        ("JL", "Japan Airlines"),
        ("AC", "Air Canada"),
        ("CM", "Copa Airlines"),
        ("BA", "British Airways"),
        ("IB", "Iberia"),
    ],
)
def test_airline_name_maps_known_iata(iata, expected):
    assert airline_name(iata) == expected


@pytest.mark.parametrize("iata", ["", None, "ZZ", "XXX", "  "])
def test_airline_name_returns_none_for_unknown_or_empty(iata):
    assert airline_name(iata) is None


def test_airline_name_is_case_and_whitespace_insensitive():
    assert airline_name("af") == "Air France"
    assert airline_name(" LA ") == "LATAM"


# ---------------- airline_label ----------------


def test_airline_label_formats_known_iata_with_parens():
    assert airline_label("AF") == "Air France (AF)"
    assert airline_label("LA") == "LATAM (LA)"
    assert airline_label("TP") == "TAP Air Portugal (TP)"


def test_airline_label_falls_back_to_raw_iata_when_unknown():
    """Sigla desconhecida cai pro próprio IATA (não inventa nome)."""
    assert airline_label("ZZ") == "ZZ"
    assert airline_label("xy") == "XY"  # normaliza upper


def test_airline_label_returns_none_for_empty():
    assert airline_label(None) is None
    assert airline_label("") is None


# ---------------- cobertura do pool broad PR #81 ----------------


def test_airlines_cover_main_carriers_of_broad_pool_regions():
    """Sanity: o mapa cobre, no mínimo, uma cia âncora de cada região do pool
    broad PR #81. Se faltar, alertas em uma região inteira mostram só IATA."""
    expected_anchors_by_region = {
        "Brasil/SA": {"LA", "G3", "AD", "AR", "AV", "CM"},
        "EUA":       {"AA", "DL", "UA"},
        "Canadá":    {"AC"},
        "Europa":    {"AF", "KL", "LH", "BA", "IB", "TP", "AZ"},
        "Ásia":      {"NH", "JL", "CX", "MU", "CA"},
        "M. Oriente":{"EK", "QR", "TK"},
    }
    missing: dict[str, set[str]] = {}
    for region, anchors in expected_anchors_by_region.items():
        gap = anchors - AIRLINES.keys()
        if gap:
            missing[region] = gap
    assert not missing, f"cias âncora ausentes: {missing}"

