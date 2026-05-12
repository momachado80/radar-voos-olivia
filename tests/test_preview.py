from __future__ import annotations

from pathlib import Path

from flight_mapper.__main__ import main


def test_preview_messages_runs_and_prints_examples(capsys, monkeypatch, tmp_path: Path):
    # rodar de dentro de tmp_path: garante que se algo escrever em "data/", caia aqui
    monkeypatch.chdir(tmp_path)

    # bloquear acesso à rede via urlopen — teste falha se algo tentar abrir socket
    import urllib.request

    def _no_network(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("preview-messages não deve usar rede")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    rc = main(["preview-messages"])

    assert rc == 0
    out = capsys.readouterr().out
    # 5 cenários da spec
    assert "1. ALERTA EXCELENTE" in out
    assert "2. ALERTA BOM" in out
    assert "3. ALERTA QUE SERIA DESCARTADO" in out
    assert "4. RELATÓRIO DIÁRIO com last_quote acionável" in out
    assert "5. RELATÓRIO DIÁRIO SEM last_quote" in out
    # nível no título
    assert "🚨 EXCELENTE" in out
    assert "🎯 BOM" in out
    # mensagens humanizadas presentes
    assert "São Paulo → Paris" in out
    assert "São Paulo → Londres" in out
    # alerta com link funcional aparece pelo menos em cenários 1, 2 e 4
    assert "search.aviasales.com/flights/" in out
    # fallback de link aparece no cenário 3
    assert "Link direto indisponível" in out
    assert "R$ 1.700" in out
    # bloco regional
    assert "🌎 Melhor por região" in out


def test_preview_messages_does_not_touch_data_dir(capsys, monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    rc = main(["preview-messages"])
    assert rc == 0
    # nada criado em ./data
    data_dir = tmp_path / "data"
    assert not data_dir.exists()


def test_preview_messages_does_not_require_secrets(capsys, monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TRAVELPAYOUTS_TOKEN",
        "KIWI_API_KEY",
        "STATUS_REPORT_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)
    rc = main(["preview-messages"])
    assert rc == 0
