# Spike: provedor de direct booking / deep_link (PR #72)

**Status:** pesquisa + spike read-only. NÃO integra nenhum provider em
produção. NÃO toca Duffel/detector/thresholds/monitor/data/workflows/Telegram.
NÃO faz scraping.

## Requisito duro

> **Sem deep_link = not suitable.** O provider precisa devolver uma URL que
> abre a **oferta exata** ou um **fluxo de booking real clicável** — não uma
> página de busca genérica. Links auxiliares de busca NÃO contam.

## Como rodar (offline)

```bash
python -m flight_mapper direct-link-readiness \
  --provider <name> --route GRU-LHR --trip round_trip --cabin economy \
  --mock-file tests/fixtures/directlink_<case>.json
```

Saída (sanitizada — só domínio + tipo, nunca token/URL/query secret):

```
provider / route / trip_type / dates
cabin_available yes/no
price_available yes/no
deep_link_available yes/no
deep_link_type   exact_offer | booking_flow | generic_search | none
deep_link_domain <host>
decision         candidate_for_integration | not_suitable | blocked_commercially
blockers / next_step
```

## Critério de decisão

- `candidate_for_integration` — `deep_link_type == exact_offer` (URL abre a
  oferta exata; rota/data + preço + cabine/fare quando disponível + cia;
  API utilizável ou affiliate documentado; sem scraping).
- `not_suitable` — `generic_search` (só busca), `none` (sem link) ou
  `booking_flow` (order_flow não-clicável, ex.: Duffel — fora deste goal).
- `blocked_commercially` — exige aprovação comercial/affiliate manual sem
  caminho self-service.

## Avaliação por candidato

| # | Candidato | deep_link? | Tipo | Decisão | Observação |
|---|-----------|-----------|------|---------|------------|
| 1 | **Kiwi / Tequila** | **sim** | `exact_offer` (`*.kiwi.com/deep?...`) | **candidate_for_integration** | Tequila Search devolve `deep_link` real da oferta. **Bloqueio prático:** sem `KIWI_API_KEY` (Kiwi não respondeu ao contato — ver `actionable-flight-provider.md` §3.1). Tecnicamente candidato; comercialmente travado até a chave. |
| 2 | **Travelpayouts / Aviasales deeplink** | parcial | `generic_search` / bloqueado | **not_suitable** | O endpoint de cache não devolve deep_link de oferta; o deeplink Aviasales foi **bloqueado por completo** (redireciona p/ experiência russa mesmo com `locale=en-us`) — ver `is_actionable_url`. Não usar. |
| 3 | **Skyscanner partner API** | não (self-service) | `generic_search` | **blocked_commercially** | A Travel/Affiliate API exige aprovação de parceria; o B2B "Flights Live Prices" não é self-service e os redirects são para páginas de busca/parceiro, não oferta exata clicável garantida. Sem caminho self-service → parquear. |
| 4 | **Kayak / Momondo (metasearch affiliate)** | não | `generic_search` | **not_suitable** | Affiliate gera **deeplink de busca** (página de resultados), não URL de oferta exata. Falha no requisito duro. |
| 5 | **RapidAPI flight providers** | varia | depende | **not_suitable** (na maioria) | A maioria devolve dados de preço/itinerário **sem** booking URL real; os que prometem "booking link" tipicamente caem em busca genérica ou exigem contrato. Só vira candidato se um provider específico devolver `exact_offer` documentado e self-service. |
| 6 | **APIs diretas de cia (NDC)** | sim (teórico) | `exact_offer` / `booking_flow` | **blocked_commercially** | LATAM/IB/AF/BA NDC podem devolver fluxo de booking, mas exigem contrato individual + certificação; sem self-service e alto custo de manter parsers por cia. Parquear (mesma conclusão do `actionable-flight-provider.md` §4). |
| 7 | **Outros affiliates com deep_link documentado** | — | — | — | Nenhum self-service com `exact_offer` documentado encontrado nesta janela além do Kiwi. |

## Conclusão

- **Único candidato técnico real: Kiwi/Tequila** (`deep_link` de oferta
  exata). Está **comercialmente bloqueado** hoje pela ausência de
  `KIWI_API_KEY` (Kiwi sem resposta). Se a chave for liberada, é o caminho
  natural para o **primeiro alerta verde com link direto** (o radar já
  trata `direct_link` como alerta standalone imediato — PR #71).
- **Travelpayouts/Aviasales, Kayak/Momondo, metasearch genérico:**
  `not_suitable` — entregam busca genérica, não oferta exata. Não adicionar
  links auxiliares de busca (decisão de produto reforçada).
- **Skyscanner partner / NDC de cia:** `blocked_commercially` — sem caminho
  self-service; parquear até aprovação comercial explícita.
- **Duffel:** continua confirmando ofertas reais, mas `order_flow`
  (`booking_flow`) — `not_suitable` para ESTE goal (sem link clicável). A
  Orders API segue como projeto futuro separado (ver
  `duffel-purchase-path.md`).

**Próximo passo recomendado:** destravar comercialmente o Kiwi/Tequila
(obter `KIWI_API_KEY`). Enquanto isso, nenhum provider self-service oferece
deep_link de oferta exata, então o radar permanece honesto: Duffel como
"oferta confirmada, compra pendente" (agrupada), sem prometer link de compra.

## O que NÃO foi feito

- ❌ Nenhuma integração de produção, nenhuma mudança em
  Duffel/detector/thresholds/monitor/notifier/data/workflows.
- ❌ Nenhum link auxiliar de busca adicionado.
- ❌ Nenhuma chamada à Duffel Orders API.
- ❌ Nenhum scraping. Nenhuma chamada de rede no spike (parser consome
  fixtures/dicts sanitizados).
