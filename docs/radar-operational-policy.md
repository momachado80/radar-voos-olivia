# Radar de Voos Olivia — Política Operacional

**Status:** documentação de política, sem fonte normativa externa. Reflete o
estado real do código em `flight_mapper/` após PRs #21-#51 e o que cada
provider entrega hoje.

Este documento explica **o que conta como alerta confirmado**, **o que vira
oportunidade para verificação manual**, e por que algumas frentes
(SerpApi Google POST, AirHint) **não viram link clicável** no Telegram.

**Aplicação (PR #51):** o relatório diário em `flight_mapper/status.py`
agora apresenta as ofertas em 6 blocos decisórios alinhados com a tabela
da seção 1 deste doc. Cabine confirmada com `deep_link` acionável vai
para "🟢 Executiva confirmada"; cabine confirmada sem link vai para
"🟡 Verificação manual" com dica humana ao usuário ("verificar
manualmente no Google Flights ou na companhia"). Sinais sem cabine
seguem em "💸 Econômica possível" (preço bom) ou "👀 Sinais em
observação" (sem grade). Os gates de segurança (`cabine bloqueada`,
`preço suspeito`, `câmbio ausente`, `entradas legadas sem moeda
comprovada`) permanecem no bloco "🛡️ Bloqueios de segurança".

**Compatibilidade de preço na validação SerpApi (PR #60):** o teste
de elevação para 🟡 Verificação manual agora exige **DOIS** sinais
simultâneos:

1. SerpApi confirma cabine business para a rota.
2. O preço retornado pela SerpApi é compatível com o preço do
   sinal original Travelpayouts.

Critério de compatibilidade (puro, em `serpapi_validation.price_is_compatible`):

> SerpApi ≤ expected × 1.25 **OU** |Δ| ≤ USD 100.

Faltando o `expected_usd` ou o `price_usd` SerpApi → conservador,
considera incompatível para elevação.

Caso incompatível: o candidato **permanece** em 💸/👀 e ganha uma nota
informativa do tipo "*SerpApi encontrou executiva na rota por ~USD X,
mas não confirmou a tarifa original de US$ Y*". O bloco 🧭 Status das
fontes distingue claramente: `validado e movido` vs `encontrou
executiva em preço diferente` vs `tentou e não confirmou`. A frase
final do relatório (sem alerta) também é coerente — quando há manual
check existente diz "há verificação manual"; quando há só price-mismatch
diz que a tarifa original não foi confirmada.

Justificativa: SerpApi achar business em US$ 1137 enquanto o sinal
Travelpayouts diz US$ 208 NÃO é "validar a tarifa US$ 208 como business".
É evidência de que existe executiva na rota, mas em outro nível de preço.
Tratar como "validação" induziria o usuário a achar que o sinal barato
foi confirmado.

**Resumo executivo do ciclo (PR #58):** o topo do relatório agora
começa com dois blocos curtos que respondem "o que aconteceu nesse
ciclo?" em segundos:

- **🧠 Leitura do ciclo** — frase humana com até 3 sentenças:
  - quantas oportunidades executivas acionáveis;
  - quantos candidatos em Verificação manual;
  - melhor sinal bruto/econômica (rota + preço + status de cabine);
  - linha SerpApi (reuso da PR #57);
  - gargalo principal (maior contador de bloqueio do ciclo).
- **📈 Mudanças desde o último ciclo** — até 5 linhas, prioridade
  decrescente:
  1. novos candidatos em 🟡 Verificação manual;
  2. quedas de preço > 5%;
  3. altas de preço > 5%;
  4. novas rotas cotadas;
  5. delta de consumo SerpApi (confirmou / só tentou).

Persistência: `data/cycle_snapshot.json` com schema fechado
`{snapshot_at, latest_prices, manual_check_keys, serpapi_used,
serpapi_elevated}`. NUNCA contém token, URL, post_data, payload,
carriers nem rota de SerpApi — apenas `route_key → price_brl` (chaves
já são públicas — `GRU-MIA-business`, etc.) + lista de chaves em 🟡 +
2 contadores agregados. Defensivo: arquivo ausente ou corrompido →
`empty()` (sem crash, sem regressão visual). Primeiro ciclo registrado
(sem snapshot prévio) → "Sem mudança relevante desde o último ciclo".

**Observabilidade SerpApi (PR #57):** o relatório diário do Telegram
inclui uma linha **"SerpApi: ..."** dentro do bloco **🧭 Status das
fontes** mostrando o estado da validação no ciclo:

- `SerpApi: validação desativada.` — env `SERPAPI_VALIDATION_ENABLED=false` ou ausente.
- `SerpApi: configurada, mas sem chave disponível nos Actions Secrets.` — env ligada mas `SERPAPI_API_KEY` ausente.
- `SerpApi: ativa; N/90 queries usadas no mês. Nenhum candidato forte elegível neste ciclo.` — env+key OK mas nenhum sinal qualificou (region_band não é "forte" nem "boa").
- `SerpApi: ativa; N/90 queries usadas no mês. Validação tentou X candidato(s) neste ciclo, mas não confirmou executiva.` — SerpApi rodou mas não confirmou cabine business / actionability útil.
- `SerpApi: ativa; N/90 queries usadas no mês. X candidato(s) validado(s) e movido(s) para Verificação manual.` — SerpApi confirmou e elevou para 🟡.
- `SerpApi: orçamento mensal esgotado (N/90 queries usadas); validação pausada até a virada do mês UTC.` — `remaining < 3` (custo estimado).

A linha consome apenas dados sanitizados (contadores agregados +
booleanos) — `SerpApiValidationSummary` em `serpapi_validation.py`
documenta o schema fechado. Garantia testada: o relatório nunca
contém token, URL completa, query string, post_data ou payload bruto
do SerpApi.

**Validação SerpApi opcional (PR #52):** `flight_mapper/serpapi_validation.py`
adiciona uma camada read-only que consulta SerpApi para sinais brutos
com USD em banda "forte". Quando a validação confirma cabine business +
booking option (mesmo que `google_post_only`), o sinal é elevado de
"👀 Sinais em observação" para "🟡 Verificação manual" com nota indicando
que SerpApi validou. Princípios estritos:
- **SerpApi NUNCA gera link clicável.** Mesmo se `actionability` for
  `airline_simple_link`, a sugestão é `CONFIRMED_MANUAL_CHECK` (🟡), nunca
  `CONFIRMED_ACTIONABLE` (🟢). Motivo: SerpApi encapsula links via Google
  e não podemos garantir que o link clicável funcione direto.
- **Opt-in por env:** `SERPAPI_VALIDATION_ENABLED=true` (default false) +
  `SERPAPI_API_KEY` obrigatórios. Sem qualquer um deles, pipeline atual
  segue idêntico.
- **Cap por ciclo:** `SERPAPI_VALIDATION_MAX_PER_CYCLE` default 1, máximo 3.
- **Custo:** 2 queries por candidato one-way; 3 por round-trip.
- **Falha silenciosa:** qualquer erro de rede/parse → resultado vazio com
  reason code, sinal continua em observação. Relatório nunca quebra.
- **Sem leak:** resultado nunca contém token bruto, URL completa, query
  string ou post_data — só estrutura sanitizada (presença, domínio,
  método, `post_data_presente=True/False`).

## 1. Estados decisórios

O Radar agora classifica cada sinal em um de seis estados, computados
pelo helper puro `compute_decision` em `flight_mapper/booking_actionability.py`.

| Estado | Quando ocorre | O que o Telegram mostra |
|---|---|---|
| **`confirmed_actionable`** | cabine business confirmada + preço bom (forte/boa) + link clicável simples (airline direto OU OTA conhecida) | "Executiva confirmada" — alerta com hyperlink |
| **`confirmed_manual_check`** | cabine business confirmada + preço bom + sem link aproveitável (Google POST only / sem URL / erro) | "Oportunidade para verificação manual" + dica de onde olhar |
| **`possible_economy`** | sem cabine confirmada + preço dentro de banda econômica (Travelpayouts cru) | "Econômica possível" — sinal informativo, sem rótulo de business |
| **`watch_only`** | preço fraco OU histórico repetitivo (baseline_weak) | "Sinal em observação" — não dispara, ajuda calibragem |
| **`raw_signal`** | sem cabine + sem grading bom | "Sinal bruto de preço" — apenas registrado |
| **`blocked`** | preço economicamente suspeito OU moeda desconhecida | Não envia. Aparece só no relatório técnico ("bloqueios por segurança"). |

A ordem de avaliação é **gates duros primeiro** (suspicious / currency_unknown
sempre bloqueiam), depois cabine + preço, e por último actionability do
booking option.

## 2. O que conta como alerta confirmado (`confirmed_actionable`)

Três condições simultâneas:

1. **Cabine business confirmada** pelo provider (Kiwi devolve `cabin=business`;
   Travelpayouts não confirma → nunca alcança esse estado pela rota cru de TP).
2. **Preço bom** segundo `deal_intelligence.usd_band`: USD abaixo do piso "forte"
   ou da faixa "boa" para a região (EUA/Europa/Ásia) e trip_type (one-way /
   round-trip).
3. **Booking option utilizável**, classificado por `classify_actionability`
   como um dos:
   - `airline_simple_link` — domínio da cia direto (`latam.com`, `aa.com`,
     `copaair.com`, etc.), sem `post_data`;
   - `ota_simple_link` — OTA reconhecida sem `post_data`;
   - `mixed_simple_and_post` — pelo menos uma opção simples coexiste com POST.

Quando todas batem, o Telegram pode enviar o alerta com hyperlink para a
companhia/OTA — porque há link clicável real, não só preço.

## 2.1 Duffel — fonte read-only de oferta business CONFIRMADA (PR #64)

Duffel é integrado como **fonte read-only de oferta confirmada**, num pass
ADITIVO e isolado (`Monitor.run_duffel_confirmations`). Não substitui nem
altera Travelpayouts/SerpApi/Kiwi — roda em paralelo, com história própria
(`data/duffel_history.json`) que nunca polui os painéis de status/ciclo.

**O que Duffel entrega:**
- Cabine business **confirmada** (parser só promove `candidate_for_integration`
  quando todos os passageiros do 1º segmento têm `cabin_class=business`).
- Preço final + moeda (tipicamente EUR; convertido para BRL via `EUR_BRL_RATE`,
  ou USD via `USD_BRL_RATE`). Sem taxa confiável ⇒ alerta **bloqueado**.
- Companhia aérea (código IATA).
- Rota + datas + trip_type.

**`booking_flow=order_flow` ⇒ NÃO há link direto.** O fluxo de compra do
Duffel é uma ordem via API (server-to-server), não uma URL clicável. Por
isso o alerta Duffel:
- exibe o selo **"🟢 Oferta confirmada por Duffel; sem compra automática."**
- exibe **"🛒 Fonte: Duffel (Offer Request, cabine business confirmada)"**,
  a companhia, e **"Ação: verificar no Duffel Dashboard."**
- **não** mostra hyperlink de compra (não existe), e o pass Duffel **não**
  passa pela resolução de link comercial (`_resolve_actionable_link`).

**Moeda do alerta (PR #66):** alertas Duffel mostram a moeda original (ex.:
EUR) mais a estimativa em BRL usando o câmbio configurado (`EUR_BRL_RATE`),
ex.: `964 EUR ≈ R$ 5.784 (câmbio EUR_BRL_RATE=6.00; alvo R$ 6.000)`. A moeda
estrangeira é confirmada; só a conversão BRL é estimada — nunca "moeda não
confirmada". O título lidera com **"🟢 EXECUTIVA CONFIRMADA — abaixo do alvo"**
e o score vai em linha secundária (`Score operacional: N/100`).

**Compra automática: NUNCA.** O radar **não** chama `/air/orders`, **não**
cria order, **não** cria payment, **não** armazena `offer_id`, token, payload
cru ou dado de passageiro. A criação de orders (booking real) é um **projeto
futuro separado**, que exige aprovação explícita e desenho próprio — não está
neste escopo e não deve ser inferido como habilitado.

**Gates aplicados (mesma barra de qualidade do radar):** o alerta Duffel só
sai se passar moeda (BRL confiável) + cabine (business confirmada) + sanidade
(piso de preço plausível) + teto (`evaluate_ceiling` na mesma régua de
Excelente/Bom) + dedup. Quantidade controlada por:
- `DUFFEL_PROVIDER_ENABLED` (default `false`; só liga no workflow);
- `DUFFEL_MAX_REQUESTS_PER_CYCLE` (default `1` — cap conservador de 1 Offer
  Request por ciclo);
- falha silenciosa e segura se `DUFFEL_ACCESS_TOKEN` ausente.

**Rota priorizada (PR #65):** o pass consulta PRIMEIRO a rota provada pelo
readiness smoke — **GRU-MIA one_way business** — porque foi a única
confirmada end-to-end (`cabin_confirmed=yes`, `decision=candidate_for_integration`).
Com cap=1, é a única consultada por ciclo; as demais priority entram só se o
cap subir.

**Observabilidade no 🧭 Status das fontes (PR #65):** todo ciclo o relatório
diário inclui UMA linha de status do Duffel, derivada de um resumo
SANITIZADO (`DuffelStatusSummary`: só contadores + código de resultado,
NUNCA offer_id/token/URL/payload/order_id/passageiro). Estados possíveis:
- `Duffel: inativa (token ausente ou flag desligada).`
- `Duffel: ativa; 1 oferta confirmada enviada como alerta.`
- `Duffel: ativa, mas bloqueada por câmbio EUR→BRL ausente.`
- `Duffel: ativa, mas preço acima do teto.`
- `Duffel: ativa; N consulta(s) neste ciclo; 0 alertas; motivo: sem oferta confirmada.`
- `Duffel: ativa, mas cabine não confirmada.` / `...preço economicamente suspeito.`

## 3. O que conta como oportunidade para verificação manual

Mesmas duas primeiras condições do confirmado (cabine + preço), mas
`classify_actionability` devolve uma das seguintes:

- `google_post_only` — todos os booking_options apontam para
  `google.com/travel/clk/...` exigindo POST. Não é hyperlink simples.
  É o caso observado no run #11 do `Provider readiness smoke`.
- `no_clickable_url` — booking_token foi expandido mas as opções não
  trouxeram URL aproveitável.
- `empty_booking_options` — `booking_token` existe mas a expansão devolveu
  lista vazia.
- `error` ou `unknown` — não foi possível classificar com segurança.

Nesses casos o Radar pode mostrar a oferta com selo **"Cabine confirmada
por SerpApi"** + **"Booking encontrado, mas sem link simples"** +
**"Ação sugerida: verificar manualmente no Google Flights ou na
companhia"**. A informação só aparece quando o fluxo realmente produziu
esses dados — não inventa.

## 4. O que fica só como econômica possível (`possible_economy`)

Quando **a cabine não foi confirmada** mas o preço (em USD) está dentro
de banda econômica:

- Travelpayouts entrega preço cru sem `cabin=business` confirmado.
- O Radar mostra como **"Econômica possível"** com classificação
  `muito forte` / `boa` baseada em `usd_band` por região e trip_type.
- **Nunca** rotula como "Executiva". Nunca usa selo de business.
- Se o histórico interno for `baseline_weak` (poucas amostras OU
  variação muito baixa), em vez de "Desconto: 0% vs mediana" exibe
  "Histórico interno ainda fraco para estimar desconto real."

## 5. O que fica como sinal bruto (`raw_signal`)

Sem cabine confirmada **e** sem preço dentro de banda econômica.
Aparece no bloco técnico do relatório para fins de auditoria, mas
não vira "promoção" nem dispara Telegram.

## 6. Sinal em observação (`watch_only`)

Acionado por `baseline_weak`. Significa:

- Provider está repetindo valores muito parecidos (variação <2% da
  mediana na janela recente, OU ≤2 valores únicos nos últimos N
  registros, OU amostras insuficientes).
- O texto humano no Telegram usa frases como:
  > A fonte vem repetindo valores muito parecidos.
  >
  > Ainda não há variação suficiente para confirmar promoção real.
  >
  > Preço forte, mas sem sinal claro de movimento real.
- O termo técnico `baseline_weak` / "cache repetitivo" fica em
  `reason_codes` internos e nesta documentação — **nunca no Telegram
  humano**.

## 7. Bloqueado por segurança (`blocked`)

Dois gates duros que sempre bloqueiam, antes de qualquer avaliação:

1. **`is_suspicious_price`** (em `sanity.py`): preço absurdamente baixo
   para business internacional (ex.: US$ 232 GRU-MIA business).
2. **`currency_known == False`**: cotação sem `currency` ou `fx_rate`
   confiável.

Esses sinais não aparecem em "Oportunidades confirmadas" nem em
"Possíveis promoções". São registrados apenas no bloco técnico
"🛡️ Alertas bloqueados por segurança".

## 8. Por que SerpApi Google POST não vira link clicável

O fluxo completo SerpApi para round-trip, hoje documentado em
`flight_mapper/serpapi_client.py` (PRs #40 / #46 / #48):

```
1. search → offers com departure_token
2. departure_token follow-up → return_offers com booking_token (round-trip)
3. booking_options com booking_token → opções de compra
```

No run #11 (real, GRU-MIA business, 2026-09-10 → 2026-09-17), o passo 3
devolveu opções **todas** no formato:

```
domínio=www.google.com | POST — não é hyperlink simples
```

O Radar reconhece isso como `BookingActionability.GOOGLE_POST_ONLY`.
Como o Telegram não consegue replicar `POST` form-data num hyperlink:

- **Não montamos a URL** de `google.com/travel/flights/...` artificialmente.
- **Não enviamos o token** no link.
- **Não imprimimos o `post_data`**.
- **Não usamos proxy/redirect** para forjar acionabilidade.

Em vez disso, o Radar diz claramente "Booking encontrado, mas sem link
simples — verificar manualmente". É honesto: o SerpApi valida que a
oferta existe, valida cabine e preço, mas não entrega um link clicável
desse provider específico.

## 9. Por que AirHint foi descartado por ora

Documentado em `docs/provider-research/airhint-price-prediction.md`:

- Não há API self-service documentada publicamente.
- B2B é canal comercial fechado (formulário "fale conosco").
- Scraping não é opção — viola provavelmente ToS, frágil, expõe credencial
  pessoal se autenticado.

Critério explícito de "matar a frente" registrado no doc de pesquisa:
~2 semanas sem retorno técnico acionável após contato comercial → encerra.

## 10. O que falta para uma versão com alerta executivo 100% acionável

A barreira atual não é técnica do nosso lado — é **distribuição de
booking_options dos provedores**:

| Caminho | Status |
|---|---|
| Kiwi Tequila com deep_link próprio | **funciona** quando `KIWI_API_KEY` está setado |
| SerpApi com `airline_simple_link` (LATAM/AA/COPA direto) | depende do payload retornado para cada rota — observado em fixtures mas não no run #11 real GRU-MIA |
| SerpApi com `google_post_only` | **não vira hyperlink** — só verificação manual |
| Amadeus | bom para benchmark de cabine business confirmada, **não traz link de compra** |
| Travelpayouts cru | preço só, sem cabine confirmada |

Para "alerta executivo 100% acionável" de forma consistente:

1. **Cobertura mais ampla de cias com `KIWI_API_KEY`** — exige plano pago Tequila.
2. **Ou** alternativa de provider com deep_link próprio (Aviasales está bloqueado
   para nosso uso por redirect russo; outras OTAs comerciais exigem contratos).
3. **Ou** aceitar `confirmed_manual_check` como estado final em parte das rotas
   e fazer o Telegram comunicar isso claramente — caminho deste PR (#50).

## 11. Princípios invioláveis

- Nunca rotular como "Executiva" sem cabine confirmada.
- Nunca usar termo técnico no Telegram humano (sem "cache repetitivo",
  sem "baseline_weak", sem "actionability=...").
- Nunca montar URL para o usuário sem ter recebido essa URL acionável
  de um provider.
- Nunca executar POST do Google.
- Nunca enviar token no link.
- Nunca imprimir URL completa, post_data, query string ou path com
  payload sensível.
- Nunca promete compra automática.

Esses princípios são validados por testes (`tests/test_booking_actionability.py`,
`tests/test_provider_readiness.py`, `tests/test_economy_grading_clarity.py`,
entre outros). Qualquer mudança que os viole deve ser rejeitada no review.
