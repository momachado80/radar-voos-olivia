# Radar de Voos Olivia — Política Operacional

**Status:** documentação de política, sem fonte normativa externa. Reflete o
estado real do código em `flight_mapper/` após PRs #21-#50 e o que cada
provider entrega hoje.

Este documento explica **o que conta como alerta confirmado**, **o que vira
oportunidade para verificação manual**, e por que algumas frentes
(SerpApi Google POST, AirHint) **não viram link clicável** no Telegram.

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
