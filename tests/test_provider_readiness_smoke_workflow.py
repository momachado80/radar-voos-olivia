"""Invariantes estáticas do workflow `provider-readiness-smoke.yml`.

Garante que o workflow é manual, não expõe secrets de Telegram, não
mexe em `data/`, e não toca os workflows do motor. Sem rede.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


WORKFLOWS = Path(".github/workflows")
WF = WORKFLOWS / "provider-readiness-smoke.yml"


def _load() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    # YAML interpreta a chave `on` como literal True (boolean).
    return doc.get(True) if True in doc else doc.get("on") or {}


def test_workflow_exists():
    assert WF.exists(), f"workflow ausente: {WF}"


def test_workflow_is_manual_only_no_schedule():
    doc = _load()
    on = _on(doc)
    assert "workflow_dispatch" in on
    assert "schedule" not in on, "workflow não pode ter cron/schedule"
    assert "push" not in on
    assert "pull_request" not in on


def test_workflow_does_not_expose_telegram_secrets():
    raw = WF.read_text(encoding="utf-8")
    # nem ENV literal nem referência a secrets.TELEGRAM_*
    assert "TELEGRAM_BOT_TOKEN" not in raw
    assert "TELEGRAM_CHAT_ID" not in raw
    assert "secrets.TELEGRAM" not in raw


def test_workflow_does_not_touch_data_or_other_workflows():
    raw = WF.read_text(encoding="utf-8")
    # nenhum git commit / push / write em data/
    assert "git commit" not in raw
    assert "git push" not in raw
    assert "git add" not in raw
    # sem chamadas aos workflows do motor
    assert "flight-mapper" not in raw
    assert "flight-hot-scan" not in raw


def test_workflow_uses_only_read_permissions():
    doc = _load()
    perms = doc.get("permissions") or {}
    # contents: read (sem write); sem outras escritas
    assert perms.get("contents") == "read"
    for k, v in perms.items():
        assert v != "write", f"permission '{k}' write não permitida"


def test_workflow_runs_only_provider_readiness_and_serpapi_smoke():
    raw = WF.read_text(encoding="utf-8")
    assert "python -m flight_mapper provider-readiness" in raw
    assert "python -m flight_mapper serpapi-smoke" in raw
    # exemplo de args definidos
    assert "--route GRU-MIA" in raw
    assert "--trip round_trip" in raw
    assert "--cabin business" in raw
    # NÃO roda amadeus-smoke real (Amadeus está disponível só por mock-file
    # neste PR; chamada real fora do escopo deste workflow).
    assert "amadeus-smoke" not in raw


def test_workflow_serpapi_step_isolates_env():
    """O step do SerpApi smoke só recebe `SERPAPI_API_KEY` como secret.
    `RAW_MAX_BOOKING_OPTIONS` é input do dispatch (não-secret), passado
    via env p/ evitar shell injection do `${{ inputs.* }}` em linha."""
    doc = _load()
    steps = doc["jobs"]["smoke"]["steps"]
    serpapi_step = next(s for s in steps if "serpapi" in (s.get("name") or "").lower())
    env = serpapi_step.get("env") or {}
    # Único secret exposto
    assert env.get("SERPAPI_API_KEY") == "${{ secrets.SERPAPI_API_KEY }}"
    # Nenhuma referência a outro `secrets.*` no env do step
    for k, v in env.items():
        if k == "SERPAPI_API_KEY":
            continue
        assert "secrets." not in str(v), (
            f"env '{k}' não pode trazer outro secret: {v!r}"
        )
    # TELEGRAM nunca pode aparecer
    for k in env:
        assert "TELEGRAM" not in k


def test_workflow_provider_readiness_step_uses_only_audit_secrets():
    """O step de auditoria não recebe nenhum secret de Telegram."""
    doc = _load()
    steps = doc["jobs"]["smoke"]["steps"]
    pr_step = next(s for s in steps if s.get("name") == "Provider readiness")
    env = pr_step.get("env") or {}
    assert set(env.keys()) == {
        "KIWI_API_KEY",
        "AMADEUS_CLIENT_ID",
        "AMADEUS_CLIENT_SECRET",
        "SERPAPI_API_KEY",
    }
    for k in env:
        assert "TELEGRAM" not in k


def test_other_workflows_untouched():
    """flight-mapper.yml, flight-hot-scan.yml e telegram-smoke-test.yml
    permanecem no diretório (não foram removidos por este PR)."""
    for fname in ("flight-mapper.yml", "flight-hot-scan.yml"):
        assert (WORKFLOWS / fname).exists(), f"workflow do motor sumiu: {fname}"


def test_workflow_serpapi_step_fetches_booking_options():
    """O workflow manual sempre expande booking_token(s) business."""
    raw = WF.read_text(encoding="utf-8")
    assert "--fetch-booking-options" in raw
    # Valor agora vem da var de shell (capada entre 1 e 3 antes do CLI).
    assert '--max-booking-options "$MAX_BOOKING_OPTIONS"' in raw


def test_workflow_serpapi_step_caps_booking_options_via_shell():
    """Cap de 3 booking_tokens é aplicado no shell antes do CLI.
    Defesa: input do dispatch pode ser qualquer string; o shell
    normaliza p/ inteiro 1..3 (não-numérico/vazio → 1; > 3 → 3)."""
    raw = WF.read_text(encoding="utf-8")
    # Não pode passar literal > 3 no CLI (defesa contra hardcode acidental).
    for n in range(4, 20):
        assert f"--max-booking-options {n}" not in raw, (
            f"literal {n} não autorizado no workflow"
        )
    # Cap explícito a 3 no shell
    assert "MAX_BOOKING_OPTIONS=3" in raw
    assert '-gt 3' in raw
    # Default seguro = 1 se input vier vazio
    assert 'RAW_MAX_BOOKING_OPTIONS:-1' in raw
    # Validação numérica (não-dígitos → 1)
    assert "[[ \"$MAX_BOOKING_OPTIONS\" =~ ^[0-9]+$ ]]" in raw


def test_workflow_dispatch_input_max_booking_options_exists():
    """workflow_dispatch.inputs.max_booking_options com default '1'
    é a única forma de o usuário disparar com profundidade maior."""
    doc = _load()
    on = _on(doc)
    wd = on.get("workflow_dispatch") or {}
    inputs = wd.get("inputs") or {}
    assert "max_booking_options" in inputs, (
        "input max_booking_options ausente"
    )
    spec = inputs["max_booking_options"]
    assert str(spec.get("default")) == "1", "default deve ser '1'"
    assert "1 a 3" in (spec.get("description") or ""), (
        "description deve declarar o intervalo 1..3"
    )
    # Apenas os inputs autorizados:
    #   max_booking_options, debug_booking_fields,
    #   departure_token_followup, max_departure_followups.
    # Rota / cabine / trip NÃO podem virar input — risco de varredura
    # em massa via dispatch.
    assert set(inputs.keys()) == {
        "max_booking_options",
        "debug_booking_fields",
        "departure_token_followup",
        "max_departure_followups",
    }, (
        f"inputs não autorizados; achei: {sorted(inputs)}"
    )


def test_workflow_dispatch_input_debug_booking_fields_exists():
    """workflow_dispatch.inputs.debug_booking_fields existe, é boolean
    e tem default false (auditoria fica explicitamente opt-in)."""
    doc = _load()
    on = _on(doc)
    wd = on.get("workflow_dispatch") or {}
    inputs = wd.get("inputs") or {}
    assert "debug_booking_fields" in inputs
    spec = inputs["debug_booking_fields"]
    assert spec.get("type") == "boolean"
    # YAML `false` vira Python False
    assert spec.get("default") is False, (
        "default deve ser false (auditoria opt-in)"
    )
    assert "read-only" in (spec.get("description") or "").lower()


def test_workflow_passes_debug_flag_only_when_input_truthy():
    """O step só passa --debug-booking-fields quando o input vier
    como variante truthy (true/yes/y/1 — case-insensitive). Gate
    tolerante contra UI quirks (GH passa "true"/"false", mas se um
    dia mudar para "True"/"TRUE", continuamos funcionando)."""
    raw = WF.read_text(encoding="utf-8")
    assert "RAW_DEBUG_BOOKING_FIELDS" in raw
    # Lowercase normalization
    assert "tr '[:upper:]' '[:lower:]'" in raw
    # Regex truthy
    assert "(true|yes|y|1)" in raw
    # flag adicionada via array (sem injection)
    assert 'EXTRA+=("--debug-booking-fields")' in raw
    # Log auto-diagnóstico: confirma estado do flag
    assert "--debug-booking-fields ENABLED" in raw
    assert "--debug-booking-fields DISABLED" in raw
    # Log do comando real executado (humano consegue rastrear)
    assert "[debug] executing:" in raw


def test_workflow_shell_truthy_gate_simulation(tmp_path):
    """Simula a lógica do gate em bash isolado (sem rede).

    Garante que: vazio / 'false' / 'FALSE' / 'no' / '0' → flag OFF;
    'true' / 'True' / 'TRUE' / 'yes' / 'Y' / '1' → flag ON.
    """
    import subprocess
    script = tmp_path / "gate.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'set -eu\n'
        'RAW_DEBUG_BOOKING_FIELDS="$1"\n'
        'EXTRA=()\n'
        'DEBUG_NORM="$(printf \'%s\' "${RAW_DEBUG_BOOKING_FIELDS}" '
        '| tr \'[:upper:]\' \'[:lower:]\')"\n'
        'if [[ "$DEBUG_NORM" =~ ^(true|yes|y|1)$ ]]; then\n'
        '  EXTRA+=("--debug-booking-fields")\n'
        'fi\n'
        'echo "${EXTRA[*]:-<empty>}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)

    def _run(arg: str) -> str:
        return subprocess.run(
            ["bash", str(script), arg],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    # Truthy
    for v in ["true", "True", "TRUE", "yes", "Y", "1"]:
        assert _run(v) == "--debug-booking-fields", f"truthy '{v}' falhou"
    # Falsy
    for v in ["", "false", "FALSE", "no", "0", "abc", "TRUE TRUE"]:
        assert _run(v) == "<empty>", f"falsy '{v}' disparou flag"


def test_workflow_dispatch_input_departure_token_followup_exists():
    """workflow_dispatch.inputs.departure_token_followup é boolean,
    default false (2º hop opt-in)."""
    doc = _load()
    on = _on(doc)
    wd = on.get("workflow_dispatch") or {}
    inputs = wd.get("inputs") or {}
    assert "departure_token_followup" in inputs
    spec = inputs["departure_token_followup"]
    assert spec.get("type") == "boolean"
    assert spec.get("default") is False, "default deve ser false (opt-in)"
    desc = (spec.get("description") or "").lower()
    assert "read-only" in desc
    assert "2" in desc  # menciona 2º hop


def test_workflow_dispatch_input_max_departure_followups_exists():
    """max_departure_followups com default '1' e descrição mencionando 1..3."""
    doc = _load()
    on = _on(doc)
    wd = on.get("workflow_dispatch") or {}
    inputs = wd.get("inputs") or {}
    assert "max_departure_followups" in inputs
    spec = inputs["max_departure_followups"]
    assert str(spec.get("default")) == "1"
    assert "1 a 3" in (spec.get("description") or "")


def test_workflow_passes_departure_followup_flag_only_when_input_truthy():
    """O step só passa --fetch-departure-token-followup +
    --max-departure-followups quando o input vier como variante truthy."""
    raw = WF.read_text(encoding="utf-8")
    assert "RAW_DEPARTURE_TOKEN_FOLLOWUP" in raw
    assert "RAW_MAX_DEPARTURE_FOLLOWUPS" in raw
    # Mesmo gate tolerante reutilizado
    assert 'EXTRA+=("--fetch-departure-token-followup")' in raw
    assert 'EXTRA+=("--max-departure-followups" "$MAX_DEPARTURE_FOLLOWUPS")' in raw
    # Auto-diagnóstico
    assert "--fetch-departure-token-followup ENABLED" in raw
    assert "--fetch-departure-token-followup DISABLED" in raw
    assert "departure_token_followup=" in raw
    assert "max_departure_followups=" in raw


def test_workflow_caps_max_departure_followups_at_three():
    """Defesa contra typo: cap a 3 no shell antes do CLI."""
    raw = WF.read_text(encoding="utf-8")
    # Cap explícito a 3 no shell
    assert "MAX_DEPARTURE_FOLLOWUPS=3" in raw
    # Nenhum literal --max-departure-followups N>3 no CLI
    for n in range(4, 20):
        assert f"--max-departure-followups {n}" not in raw, (
            f"literal {n} não autorizado"
        )


def test_workflow_shell_departure_followup_gate_simulation(tmp_path):
    """Sandbox bash: gate truthy/falsy do followup."""
    import subprocess
    script = tmp_path / "gate.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'set -eu\n'
        'RAW_DEPARTURE_TOKEN_FOLLOWUP="$1"\n'
        'EXTRA=()\n'
        'FOLLOWUP_NORM="$(printf \'%s\' "${RAW_DEPARTURE_TOKEN_FOLLOWUP}" '
        '| tr \'[:upper:]\' \'[:lower:]\')"\n'
        'if [[ "$FOLLOWUP_NORM" =~ ^(true|yes|y|1)$ ]]; then\n'
        '  EXTRA+=("--fetch-departure-token-followup")\n'
        'fi\n'
        'echo "${EXTRA[*]:-<empty>}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)

    def _run(arg: str) -> str:
        return subprocess.run(
            ["bash", str(script), arg],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    for v in ["true", "True", "TRUE", "yes", "Y", "1"]:
        assert _run(v) == "--fetch-departure-token-followup", (
            f"truthy '{v}' falhou"
        )
    for v in ["", "false", "FALSE", "no", "0", "abc"]:
        assert _run(v) == "<empty>", f"falsy '{v}' disparou flag"


def test_workflow_passes_fixed_dates_to_serpapi_smoke():
    """Datas fixas no workflow eliminam ambiguidade de request type
    (passa --return-date → request type=1 round_trip)."""
    raw = WF.read_text(encoding="utf-8")
    assert "--departure 2026-09-10" in raw
    assert "--return-date 2026-09-17" in raw
    assert "--trip round_trip" in raw
    assert "--cabin business" in raw
    assert "--route GRU-MIA" in raw
