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
    # 5 cenários da spec atualizada
    assert "1. ALERTA EXCELENTE" in out
    assert "2. ALERTA BOM" in out
    assert "3. ALERTA OBSERVAR" in out
    assert "4. RELATÓRIO DIÁRIO" in out
    assert "5. OPERATIONAL SUMMARY" in out
    # nível + score no título
    assert "🚨 EXCELENTE — Score 94/100" in out
    assert "🎯 BOM — Score 81/100" in out
    # mensagens humanizadas presentes
    assert "São Paulo → Paris" in out
    assert "São Paulo → Londres" in out
    # alerta com link funcional usa Kiwi (Aviasales foi bloqueado)
    assert "kiwi.com" in out
    # cenário explícito mostra Aviasales sendo rejeitado
    assert "4b. ALERTA com link Aviasales" in out
    assert "bloqueado pelo is_actionable_url" in out
    # cenários PR E: cabin/trip-aware
    assert "4f. BUSINESS ONE-WAY confirmado" in out
    assert "Business em promoção (somente ida)" in out
    assert "4g. ECONÔMICA ONE-WAY confirmada" in out
    assert "Econômica em promoção (somente ida)" in out
    assert "4h. MANUAL FALLBACK one-way econômica" in out
    assert "2026-09-12, econômica." in out
    # cenário 4e (PR D): alerta bloqueado por preço economicamente suspeito
    assert "4e. ALERTA BLOQUEADO — preço economicamente suspeito" in out
    assert "suspicious=True" in out
    assert "abaixo do piso" in out
    # cenário 4d (PR C): alerta bloqueado por cabine não confirmada
    assert "4d. ALERTA BLOQUEADO — cabine não confirmada" in out
    assert "alerta bloqueado: cabine não confirmada (cabin=unknown)" in out
    block_section = out.split("4d.")[1].split("=" * 60)[1]
    assert "⚠️ Cabine não confirmada — verificar" in block_section
    assert "🚨 EXCELENTE" not in block_section
    assert "Business em promoção" not in block_section
    # cenário 4c: alerta manual sem link comercial
    assert "4c. ALERTA MANUAL SEM LINK COMERCIAL" in out
    assert "manual_purchase_fallback" in out
    assert "Link comercial automático indisponível." in out
    assert "Pesquise manualmente: GRU → LHR" in out
    # Disclaimer dos links auxiliares
    assert "Links auxiliares de pesquisa, não oferta confirmada." in out
    # alerta manual NÃO contém o hyperlink comercial "Conferir busca" nem aviasales,
    # mas PODE conter links auxiliares de pesquisa.
    manual_section = out.split("4c.")[1].split("=" * 60)[1].split("=" * 60)[0]
    assert "🔎 Conferir busca" not in manual_section
    assert "Conferir busca" not in manual_section
    assert "aviasales" not in manual_section.lower()
    # links auxiliares presentes no cenário manual
    assert "Pesquisar no Google" in manual_section
    assert "Pesquisar no Google Flights" in manual_section
    assert "Pesquisar no Kayak" in manual_section
    # bloqueio explícito de hosts do Aviasales
    assert "search.aviasales.com" not in manual_section.lower()
    assert "aviasales.ru" not in manual_section.lower()
    # relatório separa oportunidades confirmadas de sinais brutos
    assert "📌 Oportunidades confirmadas" in out
    assert "💸 Top 3 sinais brutos de menor preço" in out
    assert "📡 Observação" in out
    assert "cabine não confirmada" in out
    # estrutura antiga aposentada
    assert "📌 Melhores oportunidades monitoradas" not in out
    assert "🌎 Melhor por região" not in out
    assert "⭐ Score médio do Top 3:" not in out
    # observação FASE 3 deferida
    assert "FASE 3" in out
    assert "deferida" in out


def test_preview_messages_does_not_touch_data_dir(capsys, monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    rc = main(["preview-messages"])
    assert rc == 0
    # nada criado em ./data
    data_dir = tmp_path / "data"
    assert not data_dir.exists()


def test_preview_links_prints_4_variants(capsys, monkeypatch, tmp_path: Path):
    """preview-links imprime variantes A/B/C/D para teste manual no navegador."""
    monkeypatch.chdir(tmp_path)
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("preview-links não deve usar rede")),
    )

    rc = main(["preview-links"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "A) URL atual" in out
    assert "B) Variação: locale=en-us" in out
    assert "C) Variação: locale=en-gb" in out
    assert "D) Variação: locale=en " in out  # espaço para distinguir de "en-us"/"en-gb"
    # roteiro de decisão
    assert "próximo PR deve priorizar Kiwi" in out
    # URL canônica nova aparece
    assert "locale=en-us" in out
    assert "currency=usd" in out
    # nenhum arquivo criado
    assert not (tmp_path / "data").exists()


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
