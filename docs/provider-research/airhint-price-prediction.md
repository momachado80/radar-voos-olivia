# AirHint — pesquisa de provider de price prediction

**Status:** pesquisa exploratória. **Não** implementar provider ainda.
**Escopo deste documento:** decidir se vale (e como) avaliar o AirHint
como camada de _price timing intelligence_ para o Radar de Voos Olivia,
**sem** alterar motor, workflows, `data/` ou Telegram.

---

## 1. O que o AirHint oferece (segundo o site público)

Resumo do material de marketing acessível publicamente (sem login):

- Recomendação de **comprar agora vs. esperar** para uma rota/data específica.
- Previsão de **probabilidade de queda de preço** num horizonte curto
  (dias a algumas semanas).
- **Alertas por e-mail / monitoramento de rota** atrelado a conta de
  usuário final (B2C).
- Cobertura focada em algumas dezenas de companhias aéreas — não é claro
  publicamente se inclui as cias relevantes para `EUROPE` / `USA` / `ASIA`
  do Radar (LATAM, COPA, AA, Latam, Avianca, Iberia, etc.).
- Linguagem de marketing menciona "machine learning" + dados históricos
  de preço para gerar a previsão. O **modelo, features e SLA não são
  publicados**.

Nada disso está em `tests/fixtures/`. Nenhum arquivo deste repositório
foi alterado para confirmar essas afirmações — são afirmações do site
do AirHint, registradas aqui como hipótese, não fato verificado.

## 2. Existe API pública documentada?

**Não encontrada.** A busca por:

- developer/docs/api links no site público;
- pacotes oficiais (npm, PyPI, etc.) com nome `airhint*`;
- documentação OpenAPI/Swagger públicas;
- exemplos de integração de terceiros indexados;

não retornou uma rota self-service clara para desenvolvedores. Existe
menção marketing a **"B2B airfare prediction solutions"** para parceiros
integrarem em sites de busca de voos, mas isso é vendido como contrato
comercial — não há plano free-tier nem chave self-service equivalente
ao que SerpApi / Travelpayouts / Amadeus oferecem.

**Conclusão verificável:** sem contato comercial explícito, **não há
forma legítima de chamar o AirHint via API.**

## 3. Há menção a B2B / API / parcerias?

Sim, no nível de marketing. O site descreve casos de uso B2B
(agências, OTAs, ferramentas de busca) mas:

- Não publica endpoints, autenticação, schema, rate limits ou pricing.
- Pede contato via formulário/e-mail para "fale conosco" / "solicite
  uma proposta".
- Não há SDK oficial em linguagem alguma.

**Conclusão:** B2B é um canal **fechado**, não-self-service. Qualquer
avanço exige iniciar contato comercial — não código.

## 4. Dados úteis (se o acesso for liberado)

Para encaixar no Radar de Voos Olivia, o payload mínimo aceitável
seria algo como:

| Campo | Tipo | Uso no Radar |
|---|---|---|
| `route` | `{origin, destination}` IATA | chave de junção com `Quote.route` |
| `outbound_date` | `YYYY-MM-DD` | junção com `Quote.departure_date` |
| `return_date` | `YYYY-MM-DD` ou `null` | distinguir round-trip / one-way |
| `cabin` | `economy/premium/business/first` | alinhar com `Cabin` enum |
| `currency` | ISO 4217 (`USD`/`BRL`/`EUR`) | sanity vs `Quote.currency` |
| `recommendation` | `buy_now` / `wait` / `watch` | enriquecer relatório, **não** detector |
| `drop_probability` | `float [0..1]` | _gate_ adicional opcional |
| `confidence` | `float [0..1]` ou `low/med/high` | atenuar recomendações fracas |
| `predicted_drop_pct` | `float` (negativo = queda) | nota humana no Telegram |
| `prediction_horizon_days` | `int` | "vale esperar por N dias" |
| `airline` (opcional) | string ou lista | filtrar quando provider gera por carrier |
| `model_version` | string | reprodutibilidade da decisão |
| `generated_at` | ISO 8601 UTC | TTL / staleness |

Faltando qualquer um de `recommendation`/`drop_probability`/`confidence`
o campo seria ruído — o `_compute_decision_inputs` do Radar funcionaria,
mas a camada AirHint não acrescentaria sinal acionável.

## 5. Como complementaria as fontes atuais

| Camada | Função hoje | O que o AirHint **acrescentaria** |
|---|---|---|
| Travelpayouts | preço corrente bruto, cache, sem cabine confirmada | dimensão **temporal**: "esse preço é bom hoje, ou cai amanhã?" |
| Kiwi (Tequila) | preço + deep link clicável, cabine às vezes confirmada | _veto_ "esperar" quando AirHint sinalizar queda alta — evita disparar alerta de preço médio |
| Amadeus | benchmark business confirmado, sem link de compra | nada novo — AirHint complementa **preço**, Amadeus complementa **cabine confirmada** |
| SerpApi smoke | validação de preço/booking real, fora do pipeline | nada — SerpApi é diagnóstico; AirHint seria sinal de produção |
| `deal_intelligence.py` (PR #38) | classifica boa/muito_forte/ignorar a partir de banda USD + mediana histórica | refina a banda com sinal externo: "USD abaixo do piso E AirHint diz buy_now" = grau de confiança superior |

Caso ideal: AirHint não substitui nada — vira um **terceiro eixo**
(temporal) ao lado dos dois eixos atuais (cabine, banda regional).
Status interno seria como **honest pricing** já trata fontes:
informativo, com fallback explícito.

## 6. Riscos

| Risco | Severidade | Mitigação |
|---|---|---|
| Sem API pública self-service | **alto** | depende de contato comercial; não dá pra prototipar sozinho |
| Scraping do site público | **alto** + **bloqueador** | nunca fazer — viola provavelmente ToS, frágil, e expõe credencial pessoal se for autenticado |
| Login B2C atrelado a conta pessoal da Olivia | médio | mesmo se o site permitisse "scrap autenticado", credencial pessoal nunca pode entrar em CI/CD |
| Cobertura desigual por cia/rota | médio | exigir `predicted_routes_supported` no contrato; senão silenciar provider para rotas sem cobertura (mesmo padrão de `coverage_skipped` do Radar) |
| Precisão não verificável | alto | precisaria backtest contra `data/price_history.json` antes de virar gate; manter sempre como sinal informativo, não bloqueador |
| Termos de uso desconhecidos | médio | exigir contrato escrito antes de qualquer chamada; sem isso, código fica como _stub_ |
| Latência / disponibilidade | médio | tratar AirHint sempre como **opcional** — falha silenciosa, never block o pipeline |
| Custo | **desconhecido** | provavelmente recurring B2B; orçamento separado, não free-tier |

## 7. Arquitetura proposta (se algum dia o acesso vier)

**Nada disso entra agora. Esboço para conversa futura.**

```
flight_mapper/
  price_timing.py            ← interface PriceTimingProvider (read-only)
                                .lookup(route, departure_date, return_date,
                                        cabin) -> PriceTiming | None
                                Pura, sem efeitos colaterais.
  price_timing_airhint.py    ← implementação SÓ se contrato B2B existir.
                                Sem scraping, sem endpoint não documentado.
                                Falha silenciosa em qualquer erro → None.
  price_timing_disabled.py   ← provider default (no-op) quando não há
                                contrato/credencial. Garante que o pipeline
                                roda sem AirHint configurado.
```

Pontos chave:

- **Interface `PriceTimingProvider`** com método único `lookup(...)` →
  `PriceTiming | None`. Sem `__init__` exigindo credencial — quem
  configura é o `flight_mapper/config.py`.
- **Sem mexer no motor neste primeiro passo.** O sinal só apareceria no
  `status.py` / `deal_intelligence.py` como _nota humana_ ("AirHint: wait,
  drop_probability=0.62"). Nunca derruba um alerta nem amplifica score
  no detector.
- **TTL e staleness explícitos.** `PriceTiming.generated_at` mais velho
  que N horas → trata como ausente.
- **Backtest obrigatório antes de gate.** Antes de qualquer feature
  flag que use AirHint para bloquear/atrasar alerta, rodar um audit
  contra `data/price_history.json` confirmando precisão >= X em Y
  rotas — senão fica apenas informativo.
- **Feature flag** (`AIRHINT_ENABLED`) começa em `false`. Default em
  `Config` retorna o `PriceTimingDisabledProvider`. O motor não muda.

## 8. Recomendação

**Não implementar provider AirHint agora.** Sequência sugerida, em
ordem de bloqueio:

1. **(humano)** Olivia entra em contato comercial com o AirHint via
   formulário público pedindo:
   - documentação de API / Swagger;
   - exemplos de payload (recommendation/drop_probability/confidence);
   - lista de companhias e rotas cobertas;
   - termos de uso, pricing, SLA;
   - se aceitam um trial/sandbox sem cartão.
2. **(humano)** Avaliar resposta. Sem documentação técnica clara ou
   contrato com termos de uso explícitos → **fechar essa frente**.
3. **(se passou em 2)** Abrir PR pequeno só com a interface
   `PriceTimingProvider` + `PriceTimingDisabledProvider` (no-op).
   Sem `AirHintProvider` ainda. Pytest cobre apenas a interface.
4. **(se passou em 3, e tem credencial real)** Abrir PR separado com
   `AirHintProvider`, gated por env var, com:
   - fixture de payload real (anonimizada);
   - testes offline (urlopen monkeypatched, como o padrão dos PRs
     #40-#48 do SerpApi smoke);
   - audit/log sanitizado (mesmo princípio: never log credentials,
     never log full URL).
5. **(se passou em 4)** Backtest contra `data/price_history.json`
   ANTES de qualquer integração no detector.
6. **Só depois disso**, considerar como `deal_intelligence.py` ou
   `status.py` pode mostrar o sinal — e ainda assim apenas informativo
   no primeiro release.

**Critério explícito de "matar a frente":** se em ~2 semanas após o
contato comercial não houver resposta com documentação técnica
acionável (ou se a resposta for "scraping é o caminho"), encerrar a
investigação. O Radar continua funcionando com Kiwi/Travelpayouts/
Amadeus/SerpApi exatamente como hoje.

---

## Notas operacionais

- **Nenhum arquivo deste documento confirma comportamento do AirHint
  por observação direta.** As afirmações sobre o site são repetições
  do material de marketing e da pesquisa humana que originou este
  documento, não validação de campos JSON nem inspeção de payload.
- **Nenhum scraping foi feito** para produzir este documento.
  Nenhum endpoint não-documentado foi chamado. Nenhuma credencial
  foi armazenada.
- **Motor de produção, workflows, `data/`, Telegram: intactos.**
  Este é o único arquivo deste PR.
