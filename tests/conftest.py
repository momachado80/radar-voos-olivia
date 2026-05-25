"""Configuração pytest compartilhada.

Adicionado em PR #58: a partir desse PR, `flight_mapper/status.py`
escreve `data/cycle_snapshot.json` ao final de cada `_build_message`.
Antes desse PR, alguns testes existentes invocavam `_build_message` /
`maybe_send_status` sem redirecionar o `data_dir` real do repo — o
que funcionava porque nada escrevia. Agora isso poluiria
`data/cycle_snapshot.json` no working tree a cada `pytest`.

Solução geral: autouse fixture que redireciona `Config.from_env()
.data_dir` para `tmp_path` em TODOS os testes. Testes que explicitamente
querem o caminho real do repo (raros) podem pedir `monkeypatch.undo()`
no início, mas nenhum existe hoje.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_data_dir(monkeypatch, tmp_path):
    """Redireciona Config.from_env().data_dir → tmp_path/data em
    TODOS os testes. Evita poluir `data/*` real do repo (price_history,
    cycle_snapshot, serpapi_validation_budget etc.) durante pytest."""
    from flight_mapper.config import Config

    real_from_env = Config.from_env

    @classmethod
    def _fake_from_env(cls, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        # Mutação após construção é segura — Config é dataclass mutável.
        cfg.data_dir = tmp_path / "data"
        return cfg

    monkeypatch.setattr(Config, "from_env", _fake_from_env)
