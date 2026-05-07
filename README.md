# Radar de Voos Olivia

Monitor automático de tarifas em classe executiva (business) saindo de São Paulo (GRU/CGH) para Europa, Estados Unidos e Ásia. Quando o preço cai significativamente abaixo da média histórica da rota, dispara um alerta no Telegram.

## Como funciona

A cada execução (cron a cada 30 minutos via GitHub Actions), o programa:

1. Cota um chunk de rotas via API Kiwi Tequila (configurável).
2. Atualiza o histórico rolante de preços (50 amostras por rota) em `data/price_history.json`.
3. Compara o preço atual com a média histórica da rota.
4. Dispara alerta no Telegram se a queda for ≥ 25% e não houver alerta repetido em 24h.

O estado (histórico + cursor de ciclo) é commitado automaticamente pelo workflow.

## Setup

### Secrets necessários

Em `Settings → Secrets and variables → Actions`, cadastre:

- `TELEGRAM_BOT_TOKEN` — token do bot (sem o prefixo `bot`).
- `TELEGRAM_CHAT_ID` — ID numérico do chat ou canal de destino.
- `KIWI_API_KEY` *(opcional)* — chave Tequila do Kiwi. Sem ela, o monitor roda em modo Mock (útil para validar a estrutura, mas não traz cotações reais).

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
  providers.py     # KiwiTequilaProvider + MockProvider
  regions.py       # rotas GRU/CGH → Europa, EUA, Ásia
  state.py         # histórico rolante persistido em JSON
tests/             # 13 testes pytest
.github/workflows/
  flight-mapper.yml          # cron */30 * * * *
  telegram-smoke-test.yml    # workflow_dispatch
```
