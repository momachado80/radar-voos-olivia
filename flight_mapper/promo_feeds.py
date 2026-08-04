"""Leitura de feeds RSS de sites brasileiros caça-promoção (PR #88 — spike).

CONTEXTO. O radar só enxerga o que está em API (Duffel, Google Flights via
SerpApi). As promoções mais agressivas do doméstico brasileiro — as
relâmpago de GOL/Azul/LATAM — costumam sair em campanha própria e NÃO
circulam por API nenhuma. O canal que as pega são os sites que monitoram
promoção 24/7 e publicam em minutos.

ESTE MÓDULO É UM SPIKE DE VERIFICAÇÃO. Ele responde, com dado real, três
perguntas antes de qualquer integração de produto:
  1. Os feeds respondem e são RSS válido?
  2. Quantos itens trazem e com que cara?
  3. Dá pra filtrar pelas rotas da Olivia sem afogar em ruído — e dá pra
     extrair preço do título/resumo?

NÃO É FONTE DE ALERTA. Nada aqui vira `Quote`, teto, `link_status` ou push
no Telegram. Um item de feed é uma NOTÍCIA (headline + link do artigo), não
uma oferta com cabine confirmada — misturar as duas coisas quebraria a
regra de honestidade do radar.

Read-only: só HTTP GET em feed público. Sem token, sem credencial, sem
dado de passageiro. Funções puras exceto `fetch_feed` (I/O isolado e
injetável p/ teste).
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Feeds candidatos. Todos WordPress ⇒ convenção `/feed`.
PROMO_FEEDS: tuple[tuple[str, str], ...] = (
    ("Melhores Destinos", "https://www.melhoresdestinos.com.br/feed"),
    ("Passageiro de Primeira", "https://passageirodeprimeira.com/feed"),
    ("Passagens Imperdíveis", "https://passagensimperdiveis.com.br/feed"),
)

# Termos das rotas da Olivia (PR #87): CGH ↔ SSA e CGH ↔ BSB. Guardamos o
# termo já normalizado (minúsculo, sem acento) — ver `_normalize`.
ROUTE_TERMS: dict[str, tuple[str, ...]] = {
    "Salvador": ("salvador", "ssa"),
    "Brasília": ("brasilia", "bsb"),
    "São Paulo": ("sao paulo", "congonhas", "cgh", "guarulhos", "gru"),
}

# Teto de leitura: feed de notícia não passa de alguns MB. Evita baixar
# resposta absurda por engano.
MAX_FEED_BYTES = 5 * 1024 * 1024

_ACCENTS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
    "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
)


def _normalize(text: str) -> str:
    """minúsculo + sem acento, p/ casar 'Brasília' com 'brasilia'."""
    return (text or "").translate(_ACCENTS).lower()


@dataclass(frozen=True)
class FeedItem:
    """Um item do feed. Só conteúdo público já publicado pelo site."""

    title: str
    link: str
    published: str
    summary: str

    @property
    def haystack(self) -> str:
        return _normalize(f"{self.title} {self.summary}")

    def matched_routes(self) -> list[str]:
        """Rotas da Olivia citadas neste item (rótulo legível)."""
        hay = self.haystack
        hits: list[str] = []
        for label, terms in ROUTE_TERMS.items():
            if any(_term_in(hay, t) for t in terms):
                hits.append(label)
        return hits


def _term_in(haystack: str, term: str) -> bool:
    """Casamento por PALAVRA INTEIRA. Sem isso, a sigla 'gru' casaria
    dentro de 'grupo' e 'ssa' dentro de 'passagem' — ruído garantido."""
    return re.search(rf"\b{re.escape(term)}\b", haystack) is not None


@dataclass
class FeedFetch:
    """Resultado da leitura de UM feed. Nunca levanta exceção — o spike
    precisa reportar a falha, não morrer nela."""

    name: str
    url: str
    ok: bool = False
    status: int | None = None
    error: str | None = None
    raw_bytes: int = 0
    items: list[FeedItem] = field(default_factory=list)


def fetch_feed(
    name: str, url: str, *, timeout: int = 25, urlopen_impl=None,
) -> FeedFetch:
    """GET no feed + parse. Único ponto de I/O; `urlopen_impl` injetável."""
    opener = urlopen_impl or urlopen
    req = Request(url, headers={
        # UA de navegador: WordPress + CDN costumam recusar UA vazio.
        "User-Agent": (
            "Mozilla/5.0 (compatible; radar-voos-olivia/1.0; leitor RSS)"
        ),
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    })
    try:
        with opener(req, timeout=timeout) as resp:
            body = resp.read(MAX_FEED_BYTES)
            status = getattr(resp, "status", None) or getattr(resp, "code", None)
    except HTTPError as exc:
        return FeedFetch(name=name, url=url, status=exc.code,
                         error=f"HTTP {exc.code} {exc.reason}")
    except URLError as exc:
        return FeedFetch(name=name, url=url, error=f"rede: {exc.reason}")
    except Exception as exc:  # pragma: no cover - defensivo
        return FeedFetch(name=name, url=url, error=f"{type(exc).__name__}: {exc}")

    try:
        items = parse_feed(body)
    except Exception as exc:
        return FeedFetch(name=name, url=url, status=status,
                         raw_bytes=len(body),
                         error=f"parse falhou: {type(exc).__name__}: {exc}")
    return FeedFetch(name=name, url=url, ok=True, status=status,
                     raw_bytes=len(body), items=items)


def _text(node, tag: str) -> str:
    found = node.find(tag)
    if found is None or found.text is None:
        return ""
    return html.unescape(found.text).strip()


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def parse_feed(xml_bytes: bytes) -> list[FeedItem]:
    """RSS 2.0 → lista de `FeedItem`. Função pura.

    Cobre também Atom (`<entry>`), caso algum site troque de formato.
    """
    root = ET.fromstring(xml_bytes)
    items: list[FeedItem] = []

    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = _text(node, "title") or _text(node, "{*}title")
        link = _text(node, "link")
        if not link:  # Atom guarda o link em @href
            for child in node:
                if child.tag.split("}")[-1] == "link":
                    link = child.attrib.get("href", "")
                    break
        published = (
            _text(node, "pubDate")
            or _text(node, "published")
            or _text(node, "updated")
        )
        summary = _strip_html(
            _text(node, "description") or _text(node, "summary")
        )
        if title or link:
            items.append(FeedItem(
                title=title, link=link, published=published, summary=summary,
            ))
    return items


# Preço BRL em texto: "R$ 398", "R$ 1.234", "R$ 1.234,56".
_PRICE_RE = re.compile(r"R\$\s*([\d]{1,3}(?:\.\d{3})*(?:,\d{2})?)")


def extract_price_brl(text: str) -> float | None:
    """Primeiro preço em reais citado no texto, ou None.

    Serve só p/ medir, no spike, se dá pra ler valor do título. NÃO é
    fonte de preço para alerta — headline não é oferta confirmada.
    """
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:  # pragma: no cover - regex já garante o formato
        return None


def format_readiness_report(
    fetches: list[FeedFetch], *, sample: int = 5,
) -> str:
    """Relatório humano do spike: responde 'os feeds servem?' com dado."""
    out: list[str] = ["🔍 Readiness dos feeds de promoção (spike PR #88)", ""]
    total_items = 0
    total_matched = 0
    any_ok = False

    for f in fetches:
        out.append(f"── {f.name}")
        out.append(f"   url: {f.url}")
        if not f.ok:
            out.append(f"   ❌ FALHOU — {f.error}")
            out.append("")
            continue
        any_ok = True
        total_items += len(f.items)
        matched = [(i, i.matched_routes()) for i in f.items]
        matched = [(i, m) for i, m in matched if m]
        total_matched += len(matched)
        out.append(
            f"   ✅ HTTP {f.status} — {f.raw_bytes} bytes — "
            f"{len(f.items)} itens"
        )
        out.append(f"   itens citando as rotas da Olivia: {len(matched)}")

        out.append("   amostra (títulos publicados):")
        for item in f.items[:sample]:
            price = extract_price_brl(f"{item.title} {item.summary}")
            price_txt = f" [preço lido: R$ {price:.0f}]" if price else ""
            out.append(f"     • {item.title[:110]}{price_txt}")

        if matched:
            out.append("   itens que BATEM com as rotas monitoradas:")
            for item, hits in matched[:sample]:
                price = extract_price_brl(f"{item.title} {item.summary}")
                price_txt = f" [R$ {price:.0f}]" if price else " [sem preço no texto]"
                out.append(f"     • [{'/'.join(hits)}] {item.title[:90]}{price_txt}")
        else:
            out.append("   (nenhum item citou Salvador/Brasília/São Paulo agora)")
        out.append("")

    out.append("── Veredito")
    if not any_ok:
        out.append("   ❌ Nenhum feed respondeu. Integração NÃO se sustenta.")
    else:
        out.append(f"   feeds OK: {sum(1 for f in fetches if f.ok)}/{len(fetches)}")
        out.append(f"   itens lidos: {total_items}")
        out.append(f"   itens nas rotas da Olivia: {total_matched}")
        out.append(
            "   Obs.: item de feed é NOTÍCIA (headline + link), não oferta "
            "confirmada com cabine. Entraria no Telegram como fonte "
            "separada, nunca misturada com a Duffel."
        )
    return "\n".join(out)


def run_readiness(
    *, feeds: tuple[tuple[str, str], ...] | None = None, urlopen_impl=None,
) -> tuple[str, list[FeedFetch]]:
    """Executa o spike completo. Devolve (relatório, resultados crus)."""
    chosen = feeds or PROMO_FEEDS
    fetches = [
        fetch_feed(name, url, urlopen_impl=urlopen_impl)
        for name, url in chosen
    ]
    return format_readiness_report(fetches), fetches
