# Spike: provider acionável para alertas de executiva

**Status:** pesquisa + spike funcional (PR #61). Não integra nenhum provider
novo em produção. Apenas avalia, com fixtures, qual dos providers atuais
(ou candidatos externos) pode devolver, num único fluxo:

1. cabine business confirmada
2. preço final
3. rota + datas
4. companhia
5. link clicável simples ou fluxo de booking documentado
6. cobertura Brasil → EUA/Europa

**Resumo executivo (atualizado PR #63):**
- **Kiwi Tequila:** parser confirmou-o como `candidate_for_integration`
  em fixture (PR #61) e o smoke real existe (PR #62). Porém **`KIWI_API_KEY`
  não está disponível** — Kiwi não respondeu ao contato comercial.
  Status executável: **bloqueado** até resposta. Não pode bloquear o produto.
- **Duffel:** promovido a próximo candidato executável (PR #63). Tem API
  pública documentada (`/air/offer_requests` → `/air/offers` → `/air/orders`),
  retorna cabine `business` confirmada por segmento + preço final + airline.
  Sem deep_link público (booking via order_flow). Decisão de produto:
  **se Duffel devolver cabin+price+order_flow em rota real,
  `candidate_for_integration`** (booking_flow API conta como fluxo
  documentado de ação).
- Amadeus e SerpApi seguem **validator_only**. Travelpayouts segue
  **not_suitable** para alerta executivo.
- **Conclusão honesta:** se nem Kiwi nem Duffel se confirmarem, o produto
  aceita que radar é **observação** (👀/💸/🟡 informativos), não alerta
  executivo de compra.

---

## 1. Como rodar o spike

```bash
# Modo audit original (sem args) — preservado, não muda
python -m flight_mapper provider-readiness

# Modo actionability spike (PR #61) — read-only via fixture
python -m flight_mapper provider-readiness \
  --provider {amadeus|serpapi|kiwi|travelpayouts} \
  --route GRU-MIA --cabin business \
  --mock-file tests/fixtures/<fixture>.json \
  [--booking-options-file tests/fixtures/<booking>.json]   # só p/ serpapi
```

Saída é determinística, formato `chave: valor` (13 linhas):

```
provider:        <name>
route:           <ORG-DEST>
outbound_date:   <YYYY-MM-DD | (n/a)>
return_date:     <YYYY-MM-DD | (n/a)>
cabin_confirmed: yes | no
price_amount:    <float | (n/a)>
price_currency:  <ISO | (n/a)>
airlines:        <list | (n/a)>
actionable_url:  yes | no
booking_flow:    deep_link | google_post | amadeus_pricing_required | none | unknown
booking_domain:  <host | (n/a)>          ← NUNCA URL completa
blockers:        <comma-sep snake_case | (none)>
decision:        candidate_for_integration | validator_only | insufficient | not_suitable
```

**Garantias de não-leak:** o parser nunca expõe token, URL completa,
query string, post_data nem payload bruto. Apenas o domínio do link
(via `url_domain()`) e códigos snake_case dos `blockers`. Validado por
3 testes específicos de leak em `tests/test_actionability_readiness.py`.

---

## 2. Decision rules (puras, em `apply_decision`)

| cabin_confirmed | actionable_url | has_price | decision |
|---|---|---|---|
| ✓ | ✓ | ✓ | **`candidate_for_integration`** |
| ✓ | ✗ | * | `validator_only` |
| ✗ | ✓ | * | `insufficient` |
| ✗ | ✗ | * | `not_suitable` |

---

## 3. Avaliação por provider

### 3.1 Kiwi Tequila — `candidate_for_integration` (bloqueado por chave)

> **STATUS PR #63:** marcado como **bloqueado**. O parser e o smoke
> live (PR #62) continuam válidos, mas **não há `KIWI_API_KEY` disponível**
> — Kiwi não respondeu ao contato comercial da Olivia. Enquanto isso,
> Kiwi não é executável e **não pode bloquear o produto**. A frente
> Duffel (§3.5) é prioritária por ser efetivamente testável.



Fixture: `tests/fixtures/kiwi_tequila_business_gru_mia.json`
(reflete payload Tequila Search com `selected_cabins=C`).

```
provider:        kiwi
route:           GRU-MIA
cabin_confirmed: yes
price_amount:    9800.00
price_currency:  BRL
airlines:        LA,AA
actionable_url:  yes
booking_flow:    deep_link
booking_domain:  www.kiwi.com
blockers:        (none)
decision:        candidate_for_integration
```

**Veredito:** **único candidato real à integração** no repositório atual.
- Cabine business confirmada server-side via `selected_cabins=C`.
- `deep_link` direto da Kiwi, navegável no Telegram.
- Preço em BRL (configurável).
- Já existe `KiwiTequilaProvider` em `flight_mapper/providers.py` e é
  consumido pelo `Monitor` quando `KIWI_API_KEY` está setado.

**Pré-condições:**
- Plano Tequila ativo (Kiwi B2B). Existem dois SKUs comerciais: TPF
  (Tequila Public Flights) e Tequila Premium. Confirmar com Kiwi se a
  conta atual cobre Brasil→EUA/Europa em business.
- Verificar se o `KIWI_API_KEY` configurado no Actions Secrets tem
  acesso a `flights/search` com `selected_cabins`.

**Blocker comercial conhecido:** se a conta não tiver Tequila ativo,
nenhuma chamada vai responder e a integração fica inviável sem
pagamento. **O spike não checa o status comercial — só a forma do
payload.**

### 3.2 Amadeus Self-Service — `validator_only`

Fixture: `tests/fixtures/amadeus_business.json` (existing, PR #36).

```
provider:        amadeus
route:           GRU-MIA
cabin_confirmed: yes
price_amount:    1850.20
price_currency:  USD
airlines:        LH
actionable_url:  no
booking_flow:    amadeus_pricing_required
booking_domain:  (n/a)
blockers:        no_booking_link_in_payload,requires_amadeus_pricing_orders_api_for_booking
decision:        validator_only
```

**Veredito:** valida cabine + preço, mas **não devolve link de compra**.

- `Flight Offers Search` retorna ofertas com `travelerPricings.fareDetailsBySegment.cabin` por segmento → permite checar `cabin_confirmed`.
- Para gerar URL/booking, precisaria de `Flight Offers Price` (confirma preço) + `Flight Create Orders` (cria reserva). Isso vira fluxo transacional — não temos contrato comercial e não cabe no produto atual.
- Mesmo se tivéssemos, ainda não seria uma URL que o usuário abre no Telegram — seria uma reserva PNR de servidor a servidor.

**Conclusão:** Amadeus serve como segundo signal de confirmação (sanity
de cabine + preço), exatamente como o `provider-readiness` smoke já usa
hoje. Não promovê-lo a fonte de alerta.

### 3.3 SerpApi Google Flights — `validator_only` (caso real do bug)

Fixtures: `serpapi_google_flights.json` + `serpapi_booking_google_post_only.json`.

```
provider:        serpapi
route:           GRU-MIA
cabin_confirmed: yes
price_amount:    1820.00
price_currency:  USD
airlines:        LATAM
actionable_url:  no
booking_flow:    google_post
booking_domain:  www.google.com
blockers:        booking_google_post_only
decision:        validator_only
```

**Veredito:** valida cabine + preço, mas o booking final é tipicamente
`google.com/travel/clk/redirect?token=...` com `method=POST` e `post_data`
opaco. Não vira hyperlink simples no Telegram.

- Já documentado em `docs/radar-operational-policy.md` (PR #57/#60).
- Bug de produção em 26/05/2026 (Travelpayouts US$208 + SerpApi
  US$1137 com `google_post_only`) confirmou que SerpApi tem outra
  limitação além do booking: o preço encontrado pode ser de **outra**
  oferta business, não validando a tarifa original. Esse risco é
  mitigado em produção pelo PR #60 (`price_is_compatible`).
- Em CASOS pontuais (fixture `serpapi_booking_options.json` mostra um
  Kissandfly OTA com URL simples), o resultado fica `candidate_for_integration`
  — mas é raro e não confiável. Não promover.

**Conclusão:** SerpApi continua como validador opcional + observabilidade.
Não vira fonte de alerta executivo.

### 3.4 Travelpayouts — `not_suitable`

Fixture: `tests/fixtures/travelpayouts_cache_no_cabin.json`.

```
provider:        travelpayouts
route:           GRU-MIA
cabin_confirmed: no
price_amount:    (n/a)
actionable_url:  no
booking_flow:    none
blockers:        no_cabin_confirmation_from_provider,no_actionable_deep_link
decision:        not_suitable
```

**Veredito:** o endpoint de cache do Aviasales **ignora `trip_class`**
— retorna o preço mais barato disponível, qualquer cabine. Não confirma
business. Aviasales deep_link foi bloqueado por completo em PR
anterior (redirecionamento para experiência russa). Útil apenas como
**sinal cru de preço** para alimentar a banda econômica — fora do
escopo deste spike.

### 3.5 Duffel — `candidate_for_integration` (em fixture; live pendente de token)

Fixtures: `tests/fixtures/duffel_business_gru_mia.json`,
`tests/fixtures/duffel_economy_gru_mia.json`,
`tests/fixtures/duffel_empty.json`.

```
provider:        duffel
route:           GRU-MIA
trip_type:       one_way
outbound_date:   2026-09-10
return_date:     (n/a)
cabin_confirmed: yes
price_amount:    4321.50
price_currency:  USD
airlines:        LA
actionable_url:  no
booking_flow:    order_flow
booking_domain:  (n/a)
blockers:        no_clickable_deep_link_in_payload,requires_duffel_orders_api_for_booking
decision:        candidate_for_integration
```

**Veredito:** **candidato executável** — promovido em PR #63 enquanto
Kiwi segue bloqueado.

- Endpoint `/air/offer_requests` (POST) retorna lista de `offers` com
  `cabin_class` por passageiro/segmento, `total_amount`/`total_currency`,
  `owner.iata_code` e segmentos com `marketing_carrier`. O parser exige
  que **todos os passageiros do 1º segmento** tenham
  `cabin_class == "business"` para marcar `cabin_confirmed=yes` —
  conservador, evita falso positivo de "mixed cabin".
- **Não há deep_link público** (`actionable_url=no`). Booking acontece
  por `/air/orders` (server-to-server) — fluxo de API, não URL
  clicável. Esse é o `booking_flow: order_flow`.
- Regra de decisão **específica de Duffel** (diferente das regras
  puras de `apply_decision`, que exigem `actionable_url=yes`): o goal
  PR #63 estabelece que **`order_flow` documentado conta como fluxo
  de ação**, então:
  - cabin + price → `candidate_for_integration`
  - cabin sem price → `validator_only`
  - sem cabin → `not_suitable`
- Cobertura Brasil → EUA/Europa: Duffel agrega mais de 350 cias (LATAM,
  AA, BA, IB, AF, LH etc.); cobertura existe **comercialmente em
  teoria**, mas só o smoke real (§9) confirma para a chave da Olivia.
- Custo do spike: 1 query `offer_requests` por disparo manual. NÃO cria
  order. NÃO cria payment.

**Pré-condições para alerta executivo:**
1. `DUFFEL_ACCESS_TOKEN` ativo em `secrets`.
2. Smoke real (§9) devolve `decision: candidate_for_integration` na
   rota testada.
3. Decisão de produto: aceitar que o "clique" da Olivia abre o app/site
   da própria cia (não o Duffel) — ou abrir frente futura de
   `/air/orders` com pagamento (fora deste spike).

---

## 4. Outros candidatos externos avaliados

Frente a Duffel já promovido (§3.5) e Kiwi bloqueado (§3.1), **nenhum
outro candidato externo está em avaliação ativa**. Possibilidades
descartadas ou parqueadas:

- **Scraping de OTAs / Google Flights:** descartado por ToS e por já
  termos SerpApi cobrindo o canal Google quando útil.
- **APIs de cias diretas (LATAM, BA, AA NDC):** cada uma exige contrato
  individual; custo de manter parsers separados é alto e o benefício
  vs. Duffel agregador é baixo. Parqueado.
- **Travel Coordinator / fornecedores brancos:** sem APIs públicas
  testáveis nesta janela.

Critério de retomada: se Duffel **também** não devolver
`candidate_for_integration` em rota real, a decisão é **§7 — matar
a frente** (radar segue como observação, não como alerta de compra).

---

## 5. Recomendação operacional

Em ordem de prioridade (revisada PR #63):

1. **Rodar smoke real do Duffel (§9)** com `DUFFEL_ACCESS_TOKEN` na rota
   GRU-MIA business para confirmar `decision: candidate_for_integration`
   em payload de produção. Custo: 1 query `/air/offer_requests`, sem
   order, sem payment.
2. **Se Duffel devolver `candidate_for_integration` real:** abrir PR
   futuro de integração efetiva — `DuffelProvider` em `providers.py`
   consumindo `/air/offer_requests`, sem ainda criar order. Alerta
   executivo cita preço + cabine + carrier e instrui usuário a comprar
   pela cia (sem clique automático). Decisão de avançar para
   `/air/orders` (booking real) fica para frente comercial separada.
3. **Kiwi destravado depois:** se `KIWI_API_KEY` finalmente liberar,
   `KiwiTequilaProvider` já existe — pode coexistir com Duffel como
   segunda fonte (Kiwi tem deep_link nativo, Duffel não).
4. **Se nem Duffel nem Kiwi confirmarem:** §7 — assumir radar como
   observação informativa e não prometer alerta acionável.
5. **Não integrar SerpApi como fonte de alerta.** Mantém como validador
   opcional + observabilidade. PR #60 já mitiga o risco de
   "validação enganosa".
6. **Não fazer scraping.** Nem Aviasales, nem Google Flights direto,
   nem qualquer site. Sem ToS = sem integração.

---

## 6. O que NÃO foi feito neste spike

- ❌ Nenhuma chamada de rede real (todos os parsers consomem fixtures).
- ❌ Nenhuma alteração em `monitor.py` / `detector.py` / `providers.py` /
  `notifier.py` / `state.py` / `thresholds.py` / `regions.py`.
- ❌ Nenhuma alteração de workflow ou `data/*`.
- ❌ Nenhum secret armazenado nem lido.
- ❌ Nenhuma compra executada, nenhum POST de Google Flights, nenhuma
  URL artificial montada.
- ❌ Nenhum integrador Duffel criado — apenas avaliação documental.

## 7. Critério de "matar a frente"

Se 2 semanas depois deste spike:
- Kiwi não responder comercialmente OU
- Kiwi confirmar que o plano atual não cobre business internacional E
- Duffel não tiver retorno comercial,

então o produto **deve aceitar conscientemente** que o radar funciona
como ferramenta de observação (👀/💸/🟡 informativos) sem prometer
alerta acionável. O honesto pricing já está implementado. Tentar forçar
"alerta acionável" sem provider B2B → produtos enganosos.

---

## 8. Como rodar o readiness real do Kiwi (smoke manual)

Disponível para validar a hipótese do spike (`PR #61`) com a chave
`KIWI_API_KEY` real. Custo: **1 query Tequila por disparo**, manual,
sem efeitos colaterais.

### 8.1 Via GitHub Actions (recomendado)

Workflow: `.github/workflows/kiwi-readiness-smoke.yml`
(workflow_dispatch only, nunca cron / push / PR).

Acesse `Actions → Kiwi readiness smoke → Run workflow` e escolha:

- `route`: `GRU-MIA` | `GRU-JFK` | `GRU-LIS` | `GRU-MAD` | `GRU-LHR`
  (máx. 5 rotas, choice fechado)
- `trip`: `one_way` | `round_trip`
- `departure`: `YYYY-MM-DD` (vazio = hoje+90d)
- `return_date`: `YYYY-MM-DD` (vazio = +7d sobre partida; só round_trip)

Regras invioláveis do workflow:

- `permissions: contents: read` — não toca `data/`, não commita, não
  empurra.
- Único secret consumido: `KIWI_API_KEY`. Sem `TELEGRAM_*`, sem
  `SERPAPI_KEY`, sem `AMADEUS_*`.
- Não dispara `monitor`/`detector`. Não envia Telegram.
- Saída sanitizada: apenas domínio do `deep_link` (`kiwi.com`),
  nunca URL completa nem query string.

### 8.2 Via CLI local (opcional)

```bash
KIWI_API_KEY="<sua_chave>" python -m flight_mapper provider-readiness \
  --provider kiwi --route GRU-MIA --cabin business \
  --trip one_way --real
```

Flags adicionais: `--departure YYYY-MM-DD`, `--return-date YYYY-MM-DD`.

### 8.3 Critério de decisão sobre o output

O output `format_actionability_report(...)` traz `decision:` e
`blockers:` em uma linha curta. Decisões esperadas:

- `candidate_for_integration` → cabine business confirmada (`C`),
  `deep_link` presente e domínio `kiwi.com`, preço acima de zero.
  **Hipótese de PR #61 validada na rota testada.**
- `validator_only` → cabine confirmada mas sem `deep_link` válido /
  preço ausente. Serve só para validar SerpApi, não para alerta.
- `insufficient` → payload pobre (sem itinerário utilizável).
- `not_suitable` → payload sem candidato (rota sem oferta business no
  dia, ou bloqueador transparente: `live_http_429`, `live_network_error`,
  `live_invalid_json_response`).

### 8.4 O que NÃO fazer com o output

- ❌ Não disparar Telegram com o resultado (workflow não tem secret de
  canal; rodar manualmente também não deve copiar payload para
  Telegram).
- ❌ Não persistir o payload em `data/`. O workflow é read-only.
- ❌ Não usar o `deep_link` retornado para "alerta acionável" em
  produção até a validação comercial concluir (plano Tequila atual
  pode não cobrir business internacional comercialmente — ver §5).
- ❌ Não rodar em loop / cron. O workflow é `workflow_dispatch` only
  por design.

---

## 9. Como rodar o readiness real do Duffel (smoke manual — PR #63)

Disponível para validar a hipótese do spike Duffel (§3.5) com
`DUFFEL_ACCESS_TOKEN` real. Custo: **1 query `/air/offer_requests`
por disparo**, manual, **sem criar order, sem payment**.

### 9.1 Via GitHub Actions (recomendado)

Workflow: `.github/workflows/duffel-readiness-smoke.yml`
(workflow_dispatch only, nunca cron / push / PR).

Acesse `Actions → Duffel readiness smoke → Run workflow` e escolha:

- `route`: 1 rota IATA `ORG-DEST` (máx. 1 por disparo, choice fechado:
  `GRU-MIA` | `GRU-JFK` | `GRU-LIS` | `GRU-MAD` | `GRU-LHR`)
- `trip`: `one_way` | `round_trip`
- `departure`: `YYYY-MM-DD` (vazio = hoje+90d)
- `return_date`: `YYYY-MM-DD` (vazio = +7d sobre partida; só round_trip)

Regras invioláveis do workflow:

- `permissions: contents: read` — não toca `data/`, não commita, não
  empurra.
- Único secret consumido: `DUFFEL_ACCESS_TOKEN`. Sem `TELEGRAM_*`, sem
  `SERPAPI_API_KEY`, sem `AMADEUS_*`, sem `KIWI_API_KEY`.
- **Não chama `/air/orders`. Não cria payment. Não toca booking.**
- Não dispara `monitor`/`detector`. Não envia Telegram. Não faz scraping.
- Saída sanitizada: nunca URL completa, nunca token, nunca order/offer
  id, nunca passenger data.

### 9.2 Via CLI local (opcional)

```bash
DUFFEL_ACCESS_TOKEN="<seu_token>" python -m flight_mapper provider-readiness \
  --provider duffel --route GRU-MIA --cabin business \
  --trip one_way --real
```

Flags adicionais: `--departure YYYY-MM-DD`, `--return-date YYYY-MM-DD`.

### 9.3 Critério de decisão sobre o output

O output `format_actionability_report(...)` traz `decision:` e
`blockers:` em uma linha curta. Decisões esperadas para Duffel:

- `candidate_for_integration` → cabine business confirmada (todos os
  passageiros do 1º segmento com `cabin_class == business`), preço
  presente, `booking_flow: order_flow` documentado. **Hipótese PR #63
  validada na rota testada.** Abrir frente de `DuffelProvider` em
  `providers.py`.
- `validator_only` → cabine confirmada mas preço ausente. Útil só
  como segundo signal de cabine, não para alerta.
- `not_suitable` → sem cabine confirmada OU payload vazio OU bloqueador
  de rede (`live_http_429`, `live_network_error`,
  `live_invalid_json_response`). Tenta outra rota/data antes de
  concluir que Duffel não cobre.

### 9.4 O que NÃO fazer com o output

- ❌ Não disparar Telegram com o resultado (workflow não tem secret de
  canal).
- ❌ Não criar order via `/air/orders` (mesmo se o token tiver permissão
  test mode — fora do escopo deste spike).
- ❌ Não persistir o payload em `data/`. O workflow é read-only.
- ❌ Não copiar `offer_id`, `passenger_id` ou `order_id` para logs,
  Telegram ou docs — o parser e o formatter já garantem isso, manter
  no operacional também.
- ❌ Não rodar em loop / cron. O workflow é `workflow_dispatch` only
  por design.

### 9.5 Regra dura de decisão executiva

> Se Duffel real devolver `candidate_for_integration` na rota
> business GRU→US/Europa: **Duffel vira a fonte transacional do
> radar** (PR futuro separado de integração). Se devolver
> `validator_only` ou `not_suitable` em todas as rotas testadas:
> **nenhuma fonte atual resolve alerta executivo acionável** — o
> produto cai no critério §7 (radar como observação, não como
> alerta de compra).
