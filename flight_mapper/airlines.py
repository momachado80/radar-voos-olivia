"""Humanização de códigos IATA de companhias aéreas (PR #83).

A Duffel devolve a companhia operadora como código IATA de 2 letras
(`AF`, `LA`, `NH`, etc.). Para a Olivia o IATA não diz muito ("AF" =
quem?); para o filtro de busca no Google Flights, o NOME comercial da
companhia narra a busca direto pra ela (vs. tudo que aparece na rota).

Funções puras: sem rede, sem I/O. Conjunto inicial cobre as companhias
que operam (ou conectam) as rotas do pool broad PR #81.
"""

from __future__ import annotations


# IATA → nome comercial. Nome escolhido pelo "termo que o Google Flights
# reconhece" (ex.: "TAP Air Portugal", não "TAP" sozinho — TAP isolado
# colide com sigla de torneira de cerveja no índice do Google).
AIRLINES: dict[str, str] = {
    # ----- Brasil / América do Sul -----
    "LA": "LATAM",
    "JJ": "LATAM",                 # operador histórico LATAM Brasil
    "G3": "GOL",
    "AD": "Azul",
    "AR": "Aerolíneas Argentinas",
    "AV": "Avianca",
    "H2": "Sky Airline",
    "JA": "JetSmart",
    "CM": "Copa Airlines",
    # ----- América do Norte -----
    "AA": "American Airlines",
    "DL": "Delta Air Lines",
    "UA": "United Airlines",
    "B6": "JetBlue",
    "AS": "Alaska Airlines",
    "AC": "Air Canada",
    "AM": "Aeromexico",
    "Y4": "Volaris",
    "NK": "Spirit Airlines",
    "F9": "Frontier Airlines",
    # ----- Europa -----
    "AF": "Air France",
    "KL": "KLM",
    "LH": "Lufthansa",
    "LX": "Swiss",
    "OS": "Austrian Airlines",
    "SN": "Brussels Airlines",
    "BA": "British Airways",
    "VS": "Virgin Atlantic",
    "IB": "Iberia",
    "UX": "Air Europa",
    "TP": "TAP Air Portugal",
    "AZ": "ITA Airways",
    "AY": "Finnair",
    "SK": "SAS",
    # ----- Ásia -----
    "NH": "ANA",
    "JL": "Japan Airlines",
    "CX": "Cathay Pacific",
    "MU": "China Eastern",
    "CA": "Air China",
    "CZ": "China Southern",
    "KE": "Korean Air",
    "OZ": "Asiana Airlines",
    "SQ": "Singapore Airlines",
    "TG": "Thai Airways",
    # ----- Oriente Médio (long-haul connecting) -----
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "EY": "Etihad Airways",
    "TK": "Turkish Airlines",
    "SV": "Saudia",
}


def airline_name(iata: str | None) -> str | None:
    """`AF` → `Air France`. None/sigla desconhecida ⇒ None.

    Não inventa nome: companhia desconhecida volta `None` para o chamador
    cair no fallback (ex.: alerta mostra só o IATA, busca Google Flights
    fica sem filtro de cia)."""
    if not iata:
        return None
    return AIRLINES.get(iata.strip().upper())


def airline_label(iata: str | None) -> str | None:
    """Rótulo "Nome (IATA)" p/ exibição no alerta. `AF` → `Air France (AF)`.

    Sigla desconhecida cai pro próprio IATA bruto (`XX` → `XX`); sigla
    vazia/None devolve None."""
    if not iata:
        return None
    code = iata.strip().upper()
    name = AIRLINES.get(code)
    return f"{name} ({code})" if name else code
