"""Auditoria de prontidão de provedores. **Read-only e puro**: lê
`os.environ` (sem revelar valores), `data/price_history.json` e os
workflows YAML estáticos. Não faz rede, não envia Telegram, não
modifica nada.

Objetivo: explicar honestamente por que `source=kiwi` está zero e o que
falta para habilitar Amadeus/SerpApi como fontes auxiliares (sem virar
provider de pipeline neste PR).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


# Nomes de env vars. Apenas presença é verificada — nunca valores.
ENV_KIWI = "KIWI_API_KEY"
ENV_TRAVELPAYOUTS = "TRAVELPAYOUTS_TOKEN"
ENV_AMADEUS_ID = "AMADEUS_CLIENT_ID"
ENV_AMADEUS_SECRET = "AMADEUS_CLIENT_SECRET"
ENV_SERPAPI = "SERPAPI_API_KEY"
ENV_USD_BRL = "USD_BRL_RATE"


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    configured: bool
    workflow_exposes: bool
    used_in_pipeline: str            # "primary"/"link_only"/"unused"/"smoke_only"/"unsupported"
    history_count: int               # n de last_quote com source=name
    notes: list[str] = field(default_factory=list)
    recommendation: str = ""


def _present(env: Mapping[str, str], name: str) -> bool:
    """Apenas presença + não-vazio (em runtime do CI um secret ausente
    chega como string vazia, não como ausência da chave)."""
    v = env.get(name)
    return v is not None and str(v).strip() != ""


def _workflow_mentions(workflows_dir: Path, name: str) -> bool:
    """True se algum YAML em `workflows_dir` referencia a env var/secret
    (procura ocorrência textual). Não verifica wiring real do GitHub
    Secret — só o YAML."""
    if not workflows_dir.exists():
        return False
    for yml in sorted(workflows_dir.glob("*.yml")):
        try:
            txt = yml.read_text(encoding="utf-8")
        except OSError:
            continue
        if name in txt:
            return True
    return False


def _history_source_count(history: Mapping[str, dict]) -> dict[str, int]:
    """Conta `last_quote.source` em todo o histórico carregado."""
    counts: dict[str, int] = {}
    for v in history.values():
        lq = v.get("last_quote") if isinstance(v, dict) else None
        if isinstance(lq, dict):
            src = lq.get("source") or "(sem source)"
            counts[src] = counts.get(src, 0) + 1
    return counts


def audit_kiwi(
    env: Mapping[str, str],
    workflows_dir: Path,
    history: Mapping[str, dict],
) -> ProviderStatus:
    configured = _present(env, ENV_KIWI)
    exposes = _workflow_mentions(workflows_dir, ENV_KIWI)
    counts = _history_source_count(history)
    kn = counts.get("kiwi", 0)
    notes: list[str] = []
    if not configured:
        notes.append(f"`{ENV_KIWI}` ausente no ambiente.")
    if exposes:
        notes.append("workflows expõem o secret.")
    else:
        notes.append("workflows NÃO expõem o secret (revisar YAML).")
    notes.append(f"`source=kiwi` no histórico: {kn} entrada(s).")
    if configured and kn == 0:
        notes.append(
            "Secret configurado mas Kiwi nunca apareceu como fonte: "
            "possível 401/403 do Tequila ou sem cobertura nas rotas."
        )
    if configured:
        used = "primary"  # _make_provider usa Kiwi quando truthy
        rec = (
            "Kiwi está configurado e seria provider primário. Se "
            "`source=kiwi` for 0, conferir nos logs do Actions se há "
            "401/403 ou itinerários vazios."
        )
    else:
        used = "unused"
        rec = (
            "Configure `KIWI_API_KEY` em Settings → Secrets and "
            "variables → Actions p/ habilitar cabine confirmada via "
            "selected_cabins=C. Sem ele, Travelpayouts segue como "
            "primary e todo alerta executivo é bloqueado pelo gate de "
            "cabine."
        )
    return ProviderStatus(
        name="Kiwi (Tequila)",
        configured=configured,
        workflow_exposes=exposes,
        used_in_pipeline=used,
        history_count=kn,
        notes=notes,
        recommendation=rec,
    )


def audit_travelpayouts(
    env: Mapping[str, str],
    workflows_dir: Path,
    history: Mapping[str, dict],
) -> ProviderStatus:
    configured = _present(env, ENV_TRAVELPAYOUTS)
    exposes = _workflow_mentions(workflows_dir, ENV_TRAVELPAYOUTS)
    counts = _history_source_count(history)
    tn = counts.get("travelpayouts", 0)
    notes = [
        f"`{ENV_TRAVELPAYOUTS}`: {'presente' if configured else 'ausente'}.",
        f"workflows {'expõem' if exposes else 'NÃO expõem'} o secret.",
        f"`source=travelpayouts` no histórico: {tn} entrada(s).",
        "Endpoint ignora `trip_class` → cabine sempre `unknown`/"
        "não confirmada (documentado). Gate de cabine bloqueia.",
    ]
    used = "primary" if (configured and not _present(env, ENV_KIWI)) else (
        "unused" if not configured else "fallback"
    )
    rec = (
        "Travelpayouts é fallback aceitável apenas para sinais brutos / "
        "deal intelligence (econômica). Não emite alerta executivo."
    )
    return ProviderStatus(
        name="Travelpayouts",
        configured=configured,
        workflow_exposes=exposes,
        used_in_pipeline=used,
        history_count=tn,
        notes=notes,
        recommendation=rec,
    )


def audit_amadeus(
    env: Mapping[str, str],
    workflows_dir: Path,
    history: Mapping[str, dict],
) -> ProviderStatus:
    id_ok = _present(env, ENV_AMADEUS_ID)
    secret_ok = _present(env, ENV_AMADEUS_SECRET)
    configured = id_ok and secret_ok
    exposes = (
        _workflow_mentions(workflows_dir, ENV_AMADEUS_ID)
        and _workflow_mentions(workflows_dir, ENV_AMADEUS_SECRET)
    )
    counts = _history_source_count(history)
    an = counts.get("amadeus", 0)
    notes = [
        f"`{ENV_AMADEUS_ID}`: {'presente' if id_ok else 'ausente'}.",
        f"`{ENV_AMADEUS_SECRET}`: {'presente' if secret_ok else 'ausente'}.",
        "Workflows " + ("expõem" if exposes else "NÃO expõem") + " os secrets.",
        f"`source=amadeus` no histórico: {an} entrada(s).",
        "Endpoint test: https://test.api.amadeus.com (OAuth + "
        "Flight Offers Search v2). Cabin retornada por segmento.",
    ]
    if configured:
        used = "smoke_only"  # neste PR só roda via CLI; não é provider
        rec = (
            "Pronto para `python -m flight_mapper amadeus-smoke` "
            "(chamada real, fora do pipeline). Promoção a provider "
            "principal em PR separado."
        )
    else:
        used = "unused"
        rec = (
            "Cadastrar app em developers.amadeus.com (test env grátis), "
            "configurar AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET em "
            "Secrets, e expor nos workflows. Não é necessário pra "
            "rodar smoke offline com `--mock-file`."
        )
    return ProviderStatus(
        name="Amadeus (test env)",
        configured=configured,
        workflow_exposes=exposes,
        used_in_pipeline=used,
        history_count=an,
        notes=notes,
        recommendation=rec,
    )


def audit_serpapi(
    env: Mapping[str, str],
    workflows_dir: Path,
    history: Mapping[str, dict],
) -> ProviderStatus:
    configured = _present(env, ENV_SERPAPI)
    exposes = _workflow_mentions(workflows_dir, ENV_SERPAPI)
    notes = [
        f"`{ENV_SERPAPI}`: {'presente' if configured else 'ausente'}.",
        f"workflows {'expõem' if exposes else 'NÃO expõem'} o secret.",
        "Uso restrito: validação/benchmark de preço/booking; nunca "
        "fonte de emissão de alerta.",
    ]
    used = "smoke_only" if configured else "unused"
    rec = (
        "Pronto para `python -m flight_mapper serpapi-smoke` "
        "(chamada real, fora do pipeline)."
        if configured
        else "Pendente. Não bloqueia o radar — opcional p/ benchmark."
    )
    return ProviderStatus(
        name="SerpApi (Google Flights)",
        configured=configured,
        workflow_exposes=exposes,
        used_in_pipeline=used,
        history_count=0,
        notes=notes,
        recommendation=rec,
    )


def audit_all(
    env: Mapping[str, str],
    workflows_dir: Path,
    history: Mapping[str, dict],
) -> list[ProviderStatus]:
    return [
        audit_kiwi(env, workflows_dir, history),
        audit_travelpayouts(env, workflows_dir, history),
        audit_amadeus(env, workflows_dir, history),
        audit_serpapi(env, workflows_dir, history),
    ]


def overall_recommendation(statuses: Iterable[ProviderStatus]) -> str:
    """Recomendação objetiva baseada no conjunto."""
    by_name = {s.name: s for s in statuses}
    kiwi = by_name.get("Kiwi (Tequila)")
    amadeus = by_name.get("Amadeus (test env)")
    if kiwi and kiwi.configured and kiwi.history_count > 0:
        return (
            "Kiwi ativo e produzindo cotações. Continuar com Kiwi como "
            "primary; Amadeus/SerpApi ficam como reforço."
        )
    if kiwi and kiwi.configured and kiwi.history_count == 0:
        return (
            "Kiwi configurado mas sem aparecer no histórico. Investigar "
            "logs do Actions (401/403/sem cobertura) antes de adicionar "
            "outras fontes."
        )
    if amadeus and amadeus.configured:
        return (
            "Sem Kiwi; Amadeus pronto p/ smoke. Avaliar promoção a "
            "provider em PR separado (confirma cabine via campo real do "
            "payload)."
        )
    return (
        "Sem fonte com cabine confirmada disponível. Configure "
        "`KIWI_API_KEY` OU `AMADEUS_CLIENT_ID`/`AMADEUS_CLIENT_SECRET`. "
        "Até lá, o radar opera apenas com sinais brutos / deal "
        "intelligence (econômica) — nunca emite alerta executivo."
    )


def format_report(statuses: list[ProviderStatus]) -> str:
    """Texto humano (read-only) — NUNCA inclui valores de secrets."""
    lines: list[str] = ["🔌 Provider readiness", ""]
    for s in statuses:
        lines.append(f"{s.name}")
        lines.append(
            f"  Configurado: {'sim' if s.configured else 'ausente'}"
        )
        lines.append(
            f"  Workflow expõe: "
            f"{'sim' if s.workflow_exposes else 'não'}"
        )
        lines.append(f"  Uso no pipeline: {s.used_in_pipeline}")
        lines.append(f"  Histórico recente: {s.history_count} entrada(s)")
        for n in s.notes:
            lines.append(f"  • {n}")
        lines.append(f"  Recomendação: {s.recommendation}")
        lines.append("")
    lines.append("Recomendação geral")
    lines.append(f"  {overall_recommendation(statuses)}")
    return "\n".join(lines)
