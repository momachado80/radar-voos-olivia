"""Testes do PR #88 — spike de leitura de feeds caça-promoção.

O módulo existe para RESPONDER, com dado real, se os feeds RSS servem como
fonte das promoções relâmpago (que não passam por API). Estes testes rodam
100% offline e garantem que:

1. O parser lê RSS 2.0 e Atom.
2. O casamento de rota é por PALAVRA INTEIRA — sem isso "gru" casaria em
   "Grupo" e "ssa" em "passagem", enchendo o Telegram de lixo.
3. Acento não atrapalha ("Brasília" casa com "brasilia").
4. Preço em reais é extraído nos formatos usados pelos sites.
5. `fetch_feed` NUNCA levanta exceção — falha vira relatório.
6. INVARIANTE DE ESCOPO: o módulo não manda Telegram, não cria Quote, não
   mexe em teto/link_status. Feed é notícia, não oferta confirmada.
"""

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from flight_mapper.promo_feeds import (
    PROMO_FEEDS,
    FeedItem,
    extract_price_brl,
    fetch_feed,
    format_readiness_report,
    parse_feed,
)


FIXTURE = Path(__file__).parent / "fixtures" / "promo_feed_sample.xml"


def _items():
    return parse_feed(FIXTURE.read_bytes())


# ----------------- 1. parser -----------------


def test_parses_rss_items():
    items = _items()
    assert len(items) == 4
    assert items[0].title.startswith("Passagens para Salvador")
    assert items[0].link.startswith("https://www.melhoresdestinos.com.br/")
    assert "GOL" in items[0].summary  # HTML do description foi limpo
    assert "<p>" not in items[0].summary


def test_parses_atom_feed():
    atom = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Promo para Salvador por R$ 420</title>
        <link href="https://exemplo.com/a"/>
        <published>2026-08-04T09:00:00Z</published>
        <summary>Saindo de Congonhas.</summary>
      </entry>
    </feed>"""
    items = parse_feed(atom)
    assert len(items) == 1
    assert items[0].link == "https://exemplo.com/a"
    assert items[0].matched_routes()


def test_parse_feed_raises_on_garbage():
    """XML inválido deve estourar aqui — `fetch_feed` é quem captura."""
    with pytest.raises(Exception):
        parse_feed(b"isto nao e xml")


# ----------------- 2. casamento por palavra inteira -----------------


def test_matches_relevant_routes():
    items = _items()
    assert "Salvador" in items[0].matched_routes()
    assert "Brasília" in items[1].matched_routes()


def test_ignores_unrelated_item():
    """Hotel em Gramado não pode virar alerta de passagem."""
    assert _items()[2].matched_routes() == []


@pytest.mark.parametrize("title,summary", [
    ("Grupo de viajantes cresce e passagem fica cara", ""),
    ("Nova regra de bagagem para grupos", "passagem mais cara"),
])
def test_substring_traps_do_not_match(title, summary):
    """REGRESSÃO do ruído: 'gru' dentro de 'Grupo' e 'ssa' dentro de
    'passagem' NÃO podem casar. Sem fronteira de palavra, praticamente
    todo post do site viraria alerta."""
    assert FeedItem(title, "", "", summary).matched_routes() == []


def test_accent_insensitive_match():
    assert FeedItem("Voos para Brasilia", "", "", "").matched_routes() == ["Brasília"]
    assert FeedItem("Voos para Brasília", "", "", "").matched_routes() == ["Brasília"]


def test_iata_codes_match_as_whole_words():
    assert "Salvador" in FeedItem("Trecho CGH-SSA barato", "", "", "").matched_routes()
    assert "Brasília" in FeedItem("Promo BSB ida e volta", "", "", "").matched_routes()


# ----------------- 3. extração de preço -----------------


@pytest.mark.parametrize("text,expected", [
    ("a partir de R$ 398 ida e volta", 398.0),
    ("por R$ 1.234,50 saindo", 1234.50),
    ("R$1.099", 1099.0),
    ("R$ 89,90", 89.90),
    ("sem preço nenhum aqui", None),
    ("", None),
])
def test_extract_price_brl(text, expected):
    assert extract_price_brl(text) == expected


# ----------------- 4. fetch_feed nunca estoura -----------------


def test_fetch_feed_handles_http_error():
    def boom(req, timeout=None):
        raise HTTPError(req.full_url, 403, "Forbidden", {}, None)

    f = fetch_feed("X", "https://exemplo.com/feed", urlopen_impl=boom)
    assert f.ok is False and f.status == 403
    assert "403" in f.error


def test_fetch_feed_handles_network_error():
    def boom(req, timeout=None):
        raise URLError("dns falhou")

    f = fetch_feed("X", "https://exemplo.com/feed", urlopen_impl=boom)
    assert f.ok is False and "dns falhou" in f.error


def test_fetch_feed_handles_invalid_xml():
    class _Resp:
        status = 200

        def read(self, n=None):
            return b"<rss><channel><item>sem fechar"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    f = fetch_feed("X", "https://exemplo.com/feed",
                   urlopen_impl=lambda req, timeout=None: _Resp())
    assert f.ok is False
    assert "parse falhou" in f.error


def test_fetch_feed_success_path():
    body = FIXTURE.read_bytes()

    class _Resp:
        status = 200

        def read(self, n=None):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    f = fetch_feed("Melhores", "https://exemplo.com/feed",
                   urlopen_impl=lambda req, timeout=None: _Resp())
    assert f.ok is True and f.status == 200 and len(f.items) == 4


def test_fetch_feed_sends_browser_user_agent():
    """WordPress/CDN recusa UA vazio — o spike falharia por motivo errado."""
    captured = {}

    class _Resp:
        status = 200

        def read(self, n=None):
            return FIXTURE.read_bytes()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        return _Resp()

    fetch_feed("X", "https://exemplo.com/feed", urlopen_impl=_open)
    assert "Mozilla" in captured["ua"]


# ----------------- 5. relatório -----------------


def test_report_reports_failure_clearly():
    from flight_mapper.promo_feeds import FeedFetch
    report = format_readiness_report([
        FeedFetch(name="Site", url="https://x/feed", error="HTTP 403 Forbidden"),
    ])
    assert "FALHOU" in report
    assert "Nenhum feed respondeu" in report


def test_report_counts_matches():
    from flight_mapper.promo_feeds import FeedFetch
    body = FIXTURE.read_bytes()
    report = format_readiness_report([
        FeedFetch(name="Melhores", url="u", ok=True, status=200,
                  raw_bytes=len(body), items=parse_feed(body)),
    ])
    assert "4 itens" in report
    assert "itens citando as rotas da Olivia: 2" in report


def test_feed_list_targets_brazilian_promo_sites():
    urls = " ".join(u for _, u in PROMO_FEEDS)
    assert "melhoresdestinos" in urls
    assert "passageirodeprimeira" in urls


# ----------------- 6. INVARIANTE DE ESCOPO -----------------


def _module_ast():
    import ast
    src = (
        Path(__file__).resolve().parents[1] / "flight_mapper" / "promo_feeds.py"
    ).read_text(encoding="utf-8")
    return ast.parse(src)


def _imported_modules(tree) -> set[str]:
    import ast
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
            mods.update(a.name for a in node.names)
    return mods


def _identifiers(tree) -> set[str]:
    import ast
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_module_never_imports_the_alerting_stack():
    """Feed é NOTÍCIA, não oferta confirmada. Se este módulo importar o
    notifier/detector/providers, a manchete de um site pode virar alerta
    com o mesmo peso de uma oferta Duffel com cabine confirmada.

    Checagem via AST (não por texto) — comentários citam essas palavras de
    propósito, ao explicar justamente o que NÃO se faz aqui."""
    mods = _imported_modules(_module_ast())
    for proibido in (
        "notifier", "detector", "providers", "monitor", "thresholds",
        "duffel_provider", "state",
    ):
        assert not any(proibido in m for m in mods), (
            f"promo_feeds não pode importar {proibido!r}; importa {mods}"
        )


def test_module_never_calls_alerting_or_booking_symbols():
    ids = _identifiers(_module_ast())
    for proibido in (
        "send_alert", "send", "Quote", "evaluate_ceiling", "link_status_for",
        "format_alert",
    ):
        assert proibido not in ids, f"promo_feeds não pode usar {proibido!r}"


def test_module_uses_no_credentials():
    """Só GET em feed público — nenhum token/credencial no CÓDIGO."""
    ids = _identifiers(_module_ast())
    for proibido in ("api_key", "API_KEY", "token", "Authorization", "secret"):
        assert proibido not in ids, f"promo_feeds não pode usar {proibido!r}"
