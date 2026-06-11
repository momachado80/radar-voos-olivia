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

**`booking_flow=order_flow` ⇒ Duffel não dá checkout clicável; o robô cruza
com o Google Flights (PR #76).** A oferta da Duffel é uma ordem via API
(server-to-server), não uma URL de compra. Em vez de mandar a usuária ao
Duffel Dashboard (painel de dev, sem compra), o robô monta um link de
**busca PRÉ-PREENCHIDA no Google Flights** (rota/datas/cabine) a partir da
oferta confirmada. Por isso o alerta Duffel:
- lidera com **"🟡 Oferta confirmada — Executiva/Econômica — buscar no Google
  Flights"** (🟡, NUNCA 🟢; NUNCA "EXECUTIVA CONFIRMADA" nem "clique para
  comprar" — não é a oferta travada);
- exibe **"🛒 Fonte: Duffel (Offer Request, cabine ... confirmada)"**, a
  companhia, o `Score operacional`, e o link
  **🔎 "Buscar esta oferta no Google Flights"**;
- fecha com o aviso honesto **"Busca pré-preenchida a partir da oferta
  confirmada pela Duffel. Preço e disponibilidade podem variar; confira
  antes de comprar."**;
- mantém `link_status: order_flow` — o link do Google Flights é um **atalho
  de busca**, não a oferta Duffel travada nem um checkout direto.

**Sanitização do link (PR #76):** a URL do Google Flights contém APENAS
dados públicos — origem/destino (IATA), datas, cabine e **nome da companhia
operadora** (PR #83, ver abaixo). NUNCA offer_id, token, preço, payload ou
dado de passageiro. O único host clicável no alerta Duffel é
`www.google.com`.

**Filtro de companhia (PR #83).** A Duffel devolve o IATA da cia operadora
(`AF`, `LA`, `TP`, …); o alerta passou a apender `on {Nome}` à query do
Google Flights quando o IATA é mapeado em `flight_mapper/airlines.py` (ex.:
`AF` → `Air France`, `LA` → `LATAM`, `TP` → `TAP Air Portugal`). Resultado:
a página de resultados abre **filtrada pela cia da oferta**, em vez de
"todas as opções da rota" — muito mais próximo da oferta exata que a Duffel
confirmou. Para cias não mapeadas, a query fica sem filtro (compat PR #76).

**Número do voo (PR #84).** A Duffel também devolve
`marketing_carrier_flight_number` em cada segmento; o parser extrai e
combina com o IATA → `AF447` (direto) ou `("AF447", "KL1234")` (conexão,
voos do **outbound** slice — o return é ignorado pra não poluir a busca,
que já cobre o sentido pelo filtro de cabine). A query do Google Flights
apende esses números após o filtro de cia (ex.:
`... business class on Air France AF447`), e o Google interpreta
semanticamente — a página abre **literalmente no voo exato**, em vez de
"todos os voos da rota nesta data". Quando a Duffel não devolve
flight_number (payload incompleto, codeshare obscuro), degrada
silenciosamente pra busca com filtro só de cia (comportamento PR #83).

A linha 🛫 do alerta foi reformatada:
- Com voo: `🛫 Companhia: Air France — voo AF447`
- Com conexão: `🛫 Companhia: Air France — voos AF447 → KL1234`
- Sem voo conhecido: `🛫 Companhia: Air France (AF)` (formato PR #83)

Você confere visualmente o voo antes de clicar; e clica direto pra ele no
Google Flights. O número do voo é **informação pública** (consta em
boarding pass, app da cia, e-mail de confirmação) — usar no alerta e na
query de busca é alinhado ao mesmo propósito do filtro de cia.

**Limite estrutural (sem mudança de natureza).** O link continua sendo um
**atalho de busca**, não checkout da oferta travada: `link_status` segue
`order_flow`. Os PRs #83/#84 reduzem drasticamente o atrito ("achei a
oferta?" / "qual voo era mesmo?"), mas a única forma de transformar em
"click → comprar A OFERTA" seria atravessar o hosted checkout da Duffel —
investigação fechada (Duffel Links existe e funciona, mas exige KYC + plano
pago + lida com `expires_at` ~30min da oferta; ver decisão da Olivia em
junho/2026).

**Preservação do trip_type, rota, datas e cabine (PR #79).** A query do
Google Flights é construída de forma **EXPLÍCITA**:
- **one-way** → `one way flight from <ORG> to <DST> on YYYY-MM-DD <cabin> class`
- **round-trip** → `round trip flight from <ORG> to <DST> departing YYYY-MM-DD return YYYY-MM-DD <cabin> class`

Por que: sem o token `one way` explícito, o Google escolhia round-trip por
default e inventava uma data de retorno (bug real de produção: GRU-MIA
one-way de 978 EUR virava round-trip de R$ 10.212). Em todo alerta o robô
adiciona ainda uma linha-label imediatamente abaixo do link, ex.:
`Busca Google Flights: somente ida, cabine executiva.` /
`Busca Google Flights: ida e volta, cabine econômica.` — assim a Olivia lê
o que o link vai abrir antes de clicar. O disclaimer honesto continua
("Preço e disponibilidade podem variar; confira antes de comprar.") e
`link_status` segue `order_flow`.

**Verde acionável (`direct_link`) exige caminho de compra real.** Só um
provider que devolve um `deep_link` clicável da própria oferta (ex.: Kiwi)
gera alerta verde acionável (`link_status: direct_link`, com link "Conferir
busca"). Se a `KIWI_API_KEY` for liberada, a Kiwi vence o Duffel nessas
rotas. Até lá, o cruzamento Duffel → Google Flights é o melhor caminho
honesto: leva a usuária ao lugar certo, com os filtros prontos.

**Semântica do relatório diário (PR #78).** O rodapé final, as três linhas
Duffel no 🧭 e o bloco de mudanças SerpApi precisam refletir o que de fato
aconteceu no ciclo — sem contradição:

- **"Oferta confirmada com busca Google Flights" ≠ "Alerta acionável com
  link direto enviado".** O rodapé final diferencia explicitamente:
  - `🔥 Alerta acionável com link direto enviado (N no ciclo).` quando há
    `direct_link` real (Kiwi);
  - `ℹ️ Sem link direto de compra. Há X oferta(s) Duffel confirmada(s) com
    busca Google Flights.` quando o Duffel confirmou mas o `link_status`
    segue `order_flow` (busca pré-preenchida, não checkout).
- **Nunca "nenhuma cabine confirmada" no rodapé quando o Duffel acabou
  de confirmar uma oferta.** O bloqueio do Travelpayouts no Monitor
  primário não anula o sinal positivo do Duffel.
- **Três linhas Duffel sem se contradizer:** `Duffel genérico: ...`
  (pass GRU-MIA + radar), `Duffel broad scan: ...` (pool broad), e
  `Duffel total: X com busca Google Flights; link_status=order_flow.`
  Quando o broad não acha nada mas o genérico achou, o broad diz
  *"sem novas ofertas neste bloco"* em vez de "0 confirmadas" — para não
  parecer estado conflitante.
- **SerpApi com orçamento mensal esgotado** NÃO diz "gastou X queries
  neste ciclo" (o delta entre snapshots era só ruído de estado anterior).
  Frase usada: `🔎 SerpApi já consumiu 90/90 queries no mês; validação
  pausada.` O sinalizador `serpapi_budget_exhausted` foi adicionado ao
  `CycleSnapshot` (schema fechado preservado).

**Moeda do alerta (PR #66):** alertas Duffel mostram a moeda original (ex.:
EUR) mais a estimativa em BRL usando o câmbio configurado (`EUR_BRL_RATE`),
ex.: `964 EUR ≈ R$ 5.784 (câmbio EUR_BRL_RATE=6.00; alvo R$ 6.000)`. A moeda
estrangeira é confirmada; só a conversão BRL é estimada — nunca "moeda não
confirmada". O preço/alvo e o `Score operacional: N/100` (linha secundária)
seguem visíveis no corpo, então o **sinal de valor não se perde** — só não é
rotulado como compra acionável.

**Escala do teto (correção de moeda):** os tetos em `thresholds.py` são
denominados em **USD**. Duas conversões INDEPENDENTES acontecem no pass
Duffel e não devem se misturar:
1. **Preço da oferta → BRL** usa a taxa da MOEDA DA OFERTA (`EUR_BRL_RATE`
   para ofertas EUR), via `amount_brl_estimated`.
2. **Teto USD → BRL** usa SEMPRE a taxa **USD→BRL** (`get_usd_brl_rate()`),
   nunca a taxa da oferta.

Antes, o pass Duffel escalava o teto USD pela `quote.fx_rate` (que para
ofertas EUR é a taxa EUR→BRL), inflando o teto em ~`(eur_brl/usd_brl)` e
afrouxando o alerta. Agora o teto é escalado por USD→BRL, alinhado ao pass
genérico (Travelpayouts). Consequência: sem `USD_BRL_RATE` confiável o teto
não é escalável com honestidade ⇒ o `evaluate_ceiling` bloqueia (política
"não inventamos câmbio"); o detector de queda legado segue avaliando.
Regressão coberta por
`test_duffel_ceiling_scales_threshold_by_usd_rate_not_offer_eur_rate`.

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

**Observabilidade no 🧭 Status das fontes (PR #65, wording revisado no PR
#74):** todo ciclo o relatório diário inclui linhas de status do Duffel,
derivadas de resumos SANITIZADOS (só contadores + código de resultado,
NUNCA offer_id/token/URL/payload/order_id/passageiro). **PR #74** separou as
três linhas com prefixos distintos — antes três linhas começavam com
`Duffel:` e pareciam estados conflitantes. A linha do **pass genérico**
(`DuffelStatusSummary`) usa o prefixo **`Duffel genérico:`**. Estados
possíveis:
- `Duffel genérico: inativo (token ausente ou flag desligada).`
- `Duffel genérico: 1 oferta confirmada (compra pendente).`
- `Duffel genérico: ativo, mas bloqueado por câmbio EUR→BRL ausente.`
- `Duffel genérico: ativo, sem oferta abaixo do teto neste ciclo.`
- `Duffel genérico: ativo; N consulta(s) neste ciclo; sem oferta confirmada.`
- `Duffel genérico: ativo, mas cabine não confirmada.` / `...preço economicamente suspeito.`

## 2.1.1 Pool broad de candidatos Duffel (PR #77; escopo ampliado PR #81) — DEFAULT atual

O foco exclusivo Londres/Paris setembro (PR #67) **não é mais o default** —
travou a observação porque, sem oferta nas 16 combinações específicas, o
robô ficava em silêncio e o link novo do Google Flights (PR #76) não dava
para ser testado. Em vez disso, o robô varre um pool **broad** de muitos
destinos × duas cabines × dois trip_types, com datas dinâmicas (hoje+90d),
maximizando a chance de achar qualquer oferta Duffel-confirmada genuinamente
útil.

**PR #81 — escopo ampliado a pedido da Olivia:** o pool deixou de ser só
Europa/EUA executiva. A Olivia pediu **promoções de econômica E executiva**
em várias regiões — América do Sul, América Central/Caribe, América do
Norte, Canadá, Europa e Ásia (China/Japão). O pool passou de 8 para **24
destinos** (96 entradas = 24 × 2 cabines × 2 trip_types). A ordem começa
pela América do Sul (não por Londres/Paris), e a rotação intercala regiões.

Calibração **"só promoções (poucas, boas)"**: os tetos de cada destino (em
`flight_mapper/thresholds.py`, namespaces `-business` / `-economy` /
`-one_way-*`) foram afinados para disparar **só em preço de promoção real**,
não em tarifa de tabela. Cada uma das 96 entradas tem teto correspondente —
um destino sem teto nunca alertaria (`levels_for` → None →
`evaluate_ceiling` devolve `alert=False`, falha silenciosa), invariante
coberta pelo teste `test_every_broad_entry_has_a_threshold`.

Configurável por `DUFFEL_ROUTE_MODE` (default `broad`; valor inválido cai
em `broad`):
- **`broad`** — pool de **96 entradas**, 24 destinos: América do Sul (`EZE`,
  `SCL`, `BOG`, `LIM`); EUA (`MIA`, `JFK`, `ORD`); América Central (`CUN`,
  `PTY`, `SJO`); Canadá (`YYZ`, `YUL`); Europa (`LHR` Londres, `CDG` Paris,
  `MAD`, `LIS`, `FCO`, `AMS`, `FRA`); Ásia (`NRT`, `HND`, `PVG`, `PEK`,
  `HKG`). Londres e Paris continuam no pool, **mas não como primeiras slots
  exclusivas** — entram intercaladas, depois das Américas, na rotação.
  Cabines: `business` e `economy`. Trip types: `one_way` e `round_trip`
  (10 noites).
- **`watchlist`** (opt-in) — pool fixo Londres/Paris setembro do PR #67/#68.
- **`disabled`** — pool vazio (sem Offer Requests).

A rotação usa o mesmo `data/duffel_watchlist_state.json` (offset rotativo),
respeita o cap `DUFFEL_WATCHLIST_MAX_REQUESTS_PER_CYCLE` (produção: **12** —
com 96 entradas e rotação, cobre o pool inteiro em ~8 ciclos ≈ 2h) e
preserva integralmente o cruzamento Duffel → Google Flights (PR #76): toda
oferta confirmada gera o link de busca pré-preenchida `🔎 Buscar esta oferta
no Google Flights`, mantendo `link_status: order_flow` (atalho de busca, não
checkout direto). `direct_link` (ex.: Kiwi) segue superior e imediato.

Linha do 🧭 no modo broad (frase do goal):
- `Duffel broad scan: X rotas consultadas; Y ofertas confirmadas; Z com link Google Flights.`

Invariantes: nenhum `/air/orders`, nenhum order/payment, nenhum dado de
passageiro, nenhum link auxiliar além do único cruzamento Google Flights;
detector, thresholds e provider Duffel intactos.

## 2.2 Watchlist premium Londres/Paris setembro (PR #67, opt-in no PR #77)

Watchlist TEMPORÁRIA de alta prioridade para confirmar executiva nas
combinações exatas pedidas pela Olivia. O pass Duffel consulta estas
entradas **ANTES** da rota genérica (GRU-MIA one_way), respeitando um cap
dedicado e conservador.

Origem São Paulo = `GRU`. São **8 combinações** round-trip business:
`GRU-LHR` (Londres) e `GRU-CDG` (Paris), ida `2026-09-02` ou `2026-09-03`,
volta `2026-09-12` ou `2026-09-13`.

- Controlada por `DUFFEL_WATCHLIST_MAX_REQUESTS_PER_CYCLE` (default **0** —
  opt-in seguro). No workflow ligamos com **2**: 2 combinações por ciclo,
  com **rotação** (offset persistido em `data/duffel_watchlist_state.json`,
  só `{"offset": N}`) cobrindo as 8 combinações ao longo dos ciclos.
- Histórico/dedup **isolado por combinação de datas** (chave
  `GRU-LHR-business::duffel::2026-09-02_2026-09-12`), nunca polui o store
  principal nem o pass genérico.
- Mesmas garantias do Duffel: **read-only** (só Offer Requests, nunca
  `/air/orders`, nunca order/payment), mesmos gates (moeda/cabine/sanidade/
  teto/dedup), alerta 🟢 com Londres/Paris + datas ida e volta + cabine
  business confirmada + companhia + moeda original + estimativa BRL + alvo
  + `order_flow` + "sem compra automática". NUNCA expõe offer_id, token,
  URL, payload ou dado de passageiro.
- O relatório diário mostra a linha da watchlist no 🧭 com prefixo próprio
  **`Duffel watchlist Londres/Paris:`** (PR #74), distinto do pass genérico,
  ex.:
  `Duffel watchlist Londres/Paris: consultada neste ciclo; 0 ofertas confirmadas.` ou
  `Duffel watchlist Londres/Paris: 1 oferta executiva confirmada, compra pendente; sem link direto.`
- O comportamento Duffel GRU-MIA genérico é **preservado** e roda logo
  após a watchlist.

## 2.3 Econômica também monitorada + `link_status` explícito (PR #68)

O radar monitora **business E econômica**. A watchlist premium agora cobre
as mesmas combinações Londres/Paris em **duas cabines** (16 entradas:
business primeiro, depois economy), com `build_september_watchlist(("business","economy"))`.

- **Thresholds de econômica SEPARADOS** dos de business (namespace
  `-economy` / `-one_way-economy` em `thresholds.py`): ex.
  `GRU-LHR-economy` excelente 550 / bom 750 (USD, escalados em runtime).
  Os tetos de business ficam intactos.
- **Headline (atualizado PR #69):** como o Duffel é `order_flow` (sem
  caminho de compra direto), tanto business quanto econômica lideram com
  `🟡 Oferta confirmada, compra pendente — Executiva/Econômica` — a cabine
  e o alvo seguem visíveis no corpo. As variantes verdes
  (`🟢 EXECUTIVA CONFIRMADA` / `💸 ECONÔMICA MUITO BOA`) ficam reservadas
  para quando houver caminho de compra (`direct_link`).
- Histórico/teto de econômica isolados por cabine
  (`GRU-LHR-economy::duffel::...`), nunca misturam com business.

**`link_status` em TODO alerta** — torna a acionabilidade do link explícita:
- `direct_link` — provider deu deep_link clicável real (ex.: Kiwi);
- `order_flow` — Duffel: ordem via API, **sem link direto de compra**;
- `auxiliary_search` — URL de busca gerada (não é checkout confirmado);
- `none` — sem link.

Regras de honestidade do link:
- **Duffel é SEMPRE `order_flow`** — nunca afirmamos que tem link direto de
  compra. O alerta diz `booking_flow: order_flow; sem link direto de compra;
  verificar no Duffel Dashboard`.
- Um provider só é descrito como **link direto** quando devolve um
  `deep_link` clicável real (`direct_link`).
- Travelpayouts/SerpApi **não** são descritos como compra direta confirmada
  a menos que forneçam link real; o fallback manual aparece como
  `auxiliary_search` ("links auxiliares de pesquisa, não oferta confirmada").
- **Compra/booking continua sendo projeto futuro separado** (Orders API),
  com aprovação explícita: o radar nunca chama `/air/orders`, nunca cria
  order/payment, nunca armazena dado de passageiro.

## 2.4 Política de push das ofertas Duffel order_flow (PR #71 → PR #73)

Regra de produto: **só ofertas com caminho de compra DIRETO (`direct_link`)
disparam push standalone imediato no Telegram.** Como o Duffel é
`order_flow` (sem checkout direto — a compra é uma ordem via API, projeto
futuro, ver PR #70), suas ofertas confirmadas **não** geram push standalone
por padrão; ficam visíveis apenas no relatório/status diário.

- **`direct_link` ⇒ alerta standalone imediato** (ex.: Kiwi). Único push de
  compra imediato. Não é agrupado nem atrasado, nem afetado pelo modo abaixo.
- **Duffel `order_flow` ⇒ controlado por `DUFFEL_ORDER_FLOW_ALERT_MODE`**.
  **Produção usa `grouped_push` (PR #80):** a Olivia quer as ofertas em
  TEMPO REAL, não um resumo 1x/dia. O code default seguro do `Config`
  continua `daily_only`, mas o workflow de produção define `grouped_push`
  explicitamente.

| Modo | Push standalone agrupado | Conteúdo |
|------|--------------------------|----------|
| `grouped_push` *(produção, PR #80)* | **Sim** — UMA mensagem agrupada por ciclo (15 min) com as ofertas que batem o alvo + link Google Flights por oferta; cooldown 6h por combo, furado por queda de preço ≥5% | tempo real |
| `daily_only` *(code default)* | **Não** | resumo 1x/dia com top 3 |
| `disabled` | **Não** | nada (suprime do Telegram; só logs) |

- **`daily_only` (default):** nenhum push agrupado. O relatório diário mostra
  a linha-resumo e, opcionalmente, até 3 melhores ofertas
  (rota/cidade · cabine · datas · preço+estimativa BRL · cia), fechando com
  `Sem link direto de compra. Verificar no Duffel Dashboard.`
- **`grouped_push` (opt-in, debug/manual):** preserva o comportamento do
  PR #71 — coleta as ofertas "compra pendente" do ciclo e envia **no máximo
  UMA** mensagem `🟡 Ofertas confirmadas pela Duffel — compra pendente`,
  listando até 5 (com `+N outras ofertas confirmadas no ciclo.` e
  `link_status=order_flow`). Respeita o **cooldown de 6h** por combo
  (provider·rota·cabine·trip·datas), furado por melhora de preço ≥5% (estado
  em `data/duffel_alert_cooldown.json` — só identidade + timestamp + preço
  arredondado/moeda; NUNCA offer_id/token/payload/passageiro).
- **`disabled`:** suprime totalmente o conteúdo "compra pendente" do
  Telegram (push e relatório); permanece apenas nos logs/notas do ciclo.
- Invariantes preservadas: nunca `/air/orders`, sem order/payment, sem
  dado de passageiro, sem leak; thresholds, detector, provider logic e o
  comportamento `direct_link` intactos.

**Separação de wording no 🧭 (PR #74):** as três linhas Duffel têm prefixos
distintos para não parecerem estados conflitantes — `Duffel genérico:`
(pass GRU-MIA + radar), `Duffel watchlist Londres/Paris:` (watchlist
premium) e `Duffel order_flow (resumo do ciclo):` (roll-up das ofertas
order_flow). Ex.: o genérico pode dizer "ativo, sem oferta abaixo do teto
neste ciclo" enquanto a watchlist diz "1 oferta executiva confirmada,
compra pendente; sem link direto" — leituras coerentes de passes diferentes,
não contradição. Mudança só de copy: nenhuma alteração de detecção,
threshold, chamada de provider, workflow ou comportamento de alerta.

## 2.5 Heartbeat diário desligado em produção (PR #85)

O "Radar de Voos Olivia — relatório diário" — heartbeat de 24h, ~200 linhas
— virou ruído para a Olivia. Os alertas em tempo real do `grouped_push`
(PR #80) já entregam as ofertas confirmadas pela Duffel **na hora**, e o
resto do relatório (contadores internos do ciclo, "👀 Sinais em observação",
"🛡️ Bloqueios de segurança", explicações verbosas por sinal, blocos
vazios "Nenhuma executiva confirmada agora") era debug operacional, não
informação acionável.

Controle: env `DAILY_REPORT_ENABLED`. Default `true` (mantém compat com a
suíte de testes e qualquer outro uso fora da Olivia). O workflow de
produção (`flight-mapper.yml`) define `DAILY_REPORT_ENABLED: "false"`.
Qualquer valor diferente de `"false"` (case-insensitive) mantém o
heartbeat ligado — fail-safe contra typo no env.

Em `__main__.py`, `cmd_cycle` faz o gate antes de chamar `maybe_send_status`:
quando desligado, imprime `status action=skipped reason=daily_report_disabled`
e não toca a `StatusState` nem o `notifier`. **Não afeta NENHUM alerta em
tempo real:** `grouped_push`, alertas executivos `direct_link` e
notificações de quedas reais seguem inteiramente pela mesma rota
(`notifier.send`/`notifier.send_alert`), independente do heartbeat.

Re-ligar é uma linha no workflow YAML (trocar `"false"` por `"true"` ou
remover a env). Cobertura: `tests/test_daily_report_disable.py` (sentinela
que faz o ciclo explodir se `maybe_send_status` for chamado com a flag
desligada + asserção de que o YAML de produção mantém a flag em `"false"`).

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
