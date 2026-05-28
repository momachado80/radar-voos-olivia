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

**Resumo executivo:** após o spike, **Kiwi Tequila é o único candidato real**
no repositório atual — desde que `KIWI_API_KEY` esteja ativo e o plano
Tequila suporte as rotas necessárias. Amadeus e SerpApi viram
**validator_only**. Travelpayouts é **not_suitable** para alerta executivo.
Duffel é candidato externo a avaliar comercialmente.

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

### 3.1 Kiwi Tequila — `candidate_for_integration`

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

---

## 4. Candidato externo: Duffel

Não está no repo. Avaliação externa apenas (não testado neste spike):

- API transacional moderna (booking flow completo: offers → confirm
  → order → tickets).
- Cobertura Brasil → EUA/Europa: precisa confirmar comercialmente.
- Modelo: B2B com contrato. Não há free tier acessível para teste
  individual.
- Vantagem teórica: payload de offer já trazendo `cabin_class=business`
  e `links` para o booking — se confirmado, viraria `candidate_for_integration`
  como Kiwi.
- Desvantagem: outro contrato comercial pago a perseguir, em paralelo
  ao Tequila. Recomendação: **só investigar se Kiwi Tequila falhar
  comercialmente** (caso a conta Olivia não tenha plano que cubra
  Brasil→EUA/Europa em business).

Esse spike não cria fixture Duffel nem implementa parser — fica para
um PR futuro condicionado ao Kiwi falhar.

---

## 5. Recomendação operacional

Em ordem de prioridade:

1. **Confirmar comercialmente o acesso Tequila/Kiwi atual.** O parser
   já existe. A peça que falta é se o plano cobre rotas business
   Brasil→US/Europa com `selected_cabins=C`. Resposta esperada do
   suporte Kiwi: lista de mercados/cabines incluídas no SKU contratado.
2. **Se Kiwi cobrir:** abrir PR de integração efetiva. O
   `KiwiTequilaProvider` em `providers.py` já está pronto; o que precisa
   é (a) garantir que rotas business sejam consultadas com
   `selected_cabins=C`, (b) verificar que `deep_link` está no payload
   real (não só fixture), (c) confiar no `_actionable_link_from_history`
   e nas regras de gate atuais (PR #51/#60).
3. **Se Kiwi não cobrir** (plano TPF mais barato sem business
   internacional): abrir frente Duffel ou voltar ao status quo, mantendo
   o radar como "honesto mas não acionável" e usando Travelpayouts +
   SerpApi como sinais sem alerta automático.
4. **Não integrar SerpApi como fonte de alerta.** Mantém como validador
   opcional + observabilidade. PR #60 já mitiga o risco de
   "validação enganosa".
5. **Não fazer scraping.** Nem Aviasales, nem Google Flights direto,
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
