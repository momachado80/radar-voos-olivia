# Radar de Voos Olivia

Monitor automático de tarifas em classe executiva (business) saindo de São Paulo (GRU/CGH) para Europa, Estados Unidos e Ásia. Quando o preço cai significativamente abaixo da média histórica da rota, dispara um alerta no Telegram.

## Como funciona

A cada execução (cron a cada 30 minutos via GitHub Actions), o programa:

1. Cota as rotas prioritárias (sempre) + um chunk das demais via API Travelpayouts/Aviasales.
2. Atualiza o histórico rolante de preços (50 amostras por rota) em `data/price_history.json`.
3. Compara o preço atual com a média histórica da rota.
4. Dispara alerta no Telegram quando a queda passa do limite e não houve alerta repetido recente.

### Rotas prioritárias

`GRU → SFO` e `GRU → JFK` são marcadas como prioritárias em `flight_mapper/regions.py`. Para essas rotas:
- São cotadas a **cada ciclo** (não esperam a varredura geral chegar até elas).
- Threshold de alerta mais sensível: **15%** de queda (vs. 25% nas demais).
- Janela de dedupe menor: **12h** (vs. 24h nas demais).
- Mensagem do Telegram vem com 🔥 pra destacar.

O estado (histórico + cursor de ciclo) é commitado automaticamente pelo workflow.

## Setup

### Secrets necessários

Em `Settings → Secrets and variables → Actions`, cadastre:

- `TELEGRAM_BOT_TOKEN` — token do bot (sem o prefixo `bot`).
- `TELEGRAM_CHAT_ID` — ID numérico do chat ou canal de destino.
- `TRAVELPAYOUTS_TOKEN` — token de afiliado Travelpayouts (gratuito, cadastre em https://www.travelpayouts.com → painel de desenvolvedor).
- `KIWI_API_KEY` *(opcional, fallback)* — chave Tequila do Kiwi. Só usado se `TRAVELPAYOUTS_TOKEN` estiver ausente.

Sem nenhum dos dois tokens de provedor, o monitor roda em modo Mock (estrutura funciona, mas não traz cotações reais).

### Habilitar Actions

`Settings → Actions → General → Allow all actions and reusable workflows`.

### Validar Telegram

Rode o workflow `telegram-smoke-test` manualmente. Se receber `✅ Radar de Voos Olivia conectado com sucesso`, está tudo certo.

## Comandos locais

```bash
pip install -r requirements.txt
python -m flight_mapper test           # smoke do Telegram
python -m flight_mapper scan --mock    # varredura completa com dados sintéticos
python -m flight_mapper cycle --mock   # processa o próximo chunk
pytest                                 # roda os testes
```

## Trocar de provedor

A interface `FlightProvider` em `flight_mapper/providers.py` aceita qualquer implementação que retorne `Quote | None` para uma `Route`. Para usar Amadeus, Duffel ou outra fonte, basta criar uma nova classe e plugar em `__main__.py`.

## Estrutura

```
flight_mapper/
  config.py        # carrega env vars
  cycle_state.py   # cursor de chunk pra dividir rotas entre execuções
  detector.py      # decisão de alertar (média histórica + dedupe)
  monitor.py       # orquestrador
  notifier.py      # Telegram bot API
  providers.py     # TravelpayoutsProvider + KiwiTequilaProvider + MockProvider
  regions.py       # rotas GRU/CGH → Europa, EUA, Ásia
  state.py         # histórico rolante persistido em JSON
tests/             # 21 testes pytest
.github/workflows/
  flight-mapper.yml          # cron */30 * * * *
  telegram-smoke-test.yml    # workflow_dispatch
```
