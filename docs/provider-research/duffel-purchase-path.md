# Spike: caminho de compra Duffel (sem pagamento/bilhete) — PR #70

**Status:** spike read-only/sandbox. NÃO integra compra em produção, NÃO cria
ordem, NÃO cria pagamento, NÃO emite bilhete, NÃO usa nem armazena dado de
passageiro. Só cria **Offer Request** (busca) em modo de teste e inspeciona
campos sanitizados.

## Como rodar

```bash
# Offline (sem rede, sem token) — análise sobre fixture:
python -m flight_mapper duffel-purchase-spike --test-mode \
  --route GRU-MIA --cabin business --trip one_way \
  --mock-file tests/fixtures/duffel_order_fields_present.json

# Live-test (sandbox) — exige DUFFEL_ACCESS_TOKEN de TESTE (duffel_test_*):
DUFFEL_ACCESS_TOKEN="duffel_test_..." python -m flight_mapper \
  duffel-purchase-spike --test-mode --route GRU-MIA --cabin business --trip one_way
```

Gates de segurança do comando:
- recusa sem `--test-mode`;
- recusa sem `DUFFEL_ACCESS_TOKEN` no modo live-test;
- recusa token **live** (`duffel_live_*`) e qualquer token sem prefixo
  `duffel_test_*` — sem nunca ecoar o valor do token;
- só chama `POST /air/offer_requests` (busca). **NUNCA** `/air/orders`,
  `/air/payments`, nem qualquer endpoint que cobre/reserve/emita.

## Saída (sanitizada)

`provider, environment, route, cabin, trip_type, offer_found, cabin_confirmed,
price, currency, airline, dashboard_recovery, order_creation_requires_passenger_data,
dry_run_available, safe_next_step, blockers, recommendation`.

Nunca contém token, offer_id, order_id, payment_id, URL completa, payload cru
nem dado de passageiro (apenas presença booleana dos campos estruturais).

## Respostas às perguntas do spike

1. **Recuperar a oferta do Offer Request?** Sim — o Offer Request com
   `return_offers=true` devolve `offers[]` com `id`, `passengers[].id`,
   `slices`, `total_amount` e `payment_requirements`. O spike confirma a
   presença desses campos sem expor os valores.
2. **Dashboard URL / recuperação manual segura?** **Não** no payload. As
   ofertas Duffel não trazem URL de recuperação para o usuário final; o
   Dashboard é do desenvolvedor (mostra ordens criadas, não ofertas
   compartilháveis), e ofertas expiram rápido. `dashboard_recovery = no`.
3. **Payload mínimo para criar ordem?** `POST /air/orders` exige
   `selected_offers` (id da oferta), `passengers` com **PII real**
   (given_name, family_name, born_on, gender, title, email, phone) e um
   objeto `payments`. Nada disso é coberto por este spike.
4. **Criar ordem exige dado de passageiro real?** **Sim.**
5. **Validar o payload sem criar ordem?** **Não.** O Duffel **não tem**
   endpoint de validação/dry-run de ordem (`dry_run_available = no`).
6. **Sandbox cria ordem de teste não-emissora com segurança?** Mesmo com
   token de teste, `POST /air/orders` cria uma **ordem real de teste**
   (reserva/segura inventário; com instant payment, "paga" em teste). Não é
   um no-op inócuo, então o spike **não** o chama.
7. **Onde pagamento/emissão se tornam possíveis/obrigatórios?** Exatamente
   no `POST /air/orders` (instant payment) ou no fluxo hold→pay — sempre uma
   ordem real. Antes disso (Offer Request) não há cobrança nem reserva.
8. **Token de teste vs live?** Teste: cria Offer Requests/ordens de teste,
   sem cobrança/bilhete real. Live: cobra e emite de verdade. Este spike só
   aceita token de teste e ainda assim não cria ordem.
9. **"Próximo passo" acionável seguro sem executar compra?** Sim, de forma
   honesta: reportar a oferta confirmada como **"compra pendente"** e
   apontar o bloqueio (Orders API exige PII + pagamento + aprovação). Sem
   botão de compra automático.

## Conclusão

> **B. A Orders API pode ser implementada depois, mas exige dados de
> passageiro reais + pagamento no momento da criação da ordem, e requer
> aprovação explícita futura.**

Não existe endpoint de validação/dry-run de ordem no Duffel
("**No safe validation-only order endpoint found; order creation requires
explicit future approval.**"). Também não há caminho de recuperação manual
por dashboard para o usuário final. Portanto, por ora, **não há caminho de
compra automático seguro pelo robô**: o alerta Duffel permanece como
**"oferta confirmada, compra pendente"** (ver PR #69). A criação de ordens
(booking real) segue sendo um **projeto futuro separado**, condicionado a
aprovação explícita, tratamento de PII e desenho próprio de pagamento.
