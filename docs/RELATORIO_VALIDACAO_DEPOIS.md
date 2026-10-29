# Resultados dos testes — 29/10/2026

## O que verificamos

Fizemos oito pedidos manuais, divididos em duas baterias, e conferimos as respostas no Mongo, os vereditos nos traces e os registros no Postgres. Também consultamos o Google Calendar para confirmar os dois eventos criados.

A principal mudança funcionou nos casos de recusa: a camiseta não foi inserida, a calça manteve o valor anterior e a compra em dólares não foi gravada. Na agenda, os dois eventos aprovados foram criados tanto no banco quanto no Google.

Os testes também apontaram problemas na resposta final. Em um caso, o sistema recomendou registrar a calça depois de já tê-la salvo. Em outro, informou que os eventos aguardavam confirmação, embora a criação já tivesse terminado. Ajustamos as orientações do orquestrador e do juiz para deixar claro que o rascunho de sucesso é publicado somente depois da confirmação do executor.

## Primeira bateria

| Pedido | Resposta e resultado |
|---|---|
| Registrar camiseta de R$ 15 e consultar o dia | O juiz recusou. A consulta projetada incluía a camiseta, mas nenhuma linha foi gravada. |
| Alterar a camiseta para R$ 18 | O registro não foi encontrado, pois a criação anterior tinha sido recusada. |
| Consultar o dia e depois registrar calça de R$ 30 | A consulta veio antes do preparo e retornou zero. Depois da aprovação, a calça foi salva como ID 24, no dia local correto. A recomendação de registrá-la novamente estava inadequada. |
| Registrar compra de 10 USD | A tool calculou R$ 53,27. Limites do Gemini e do Groq interromperam o turno antes do juiz, sem INSERT. |
| Criar dois eventos | O modelo retornou erro de parsing no roteador. O pedido não chegou às tools de agenda. |

A recusa da camiseta foi `choice=sem_apoio`, com `noul=0,79`. Portanto, reduzir o limiar de 0,55 não resolveria esse caso: a aprovação depende também do choice. O JEV não fornece uma justificativa textual nesse contrato.

## Segunda bateria

| Pedido | Juiz | Resultado conferido |
|---|---|---|
| Alterar ID 24 de R$ 30 para R$ 35 | `sem_apoio`, 0,49 | O valor permaneceu em R$ 30. |
| Registrar 10 USD com marcador DEPOIS-CAMBIO-RETESTE | `sem_apoio`, 0,61 | Conversão preparada para R$ 53,27, sem gravação. |
| Criar DEPOIS-AGENDA-A e DEPOIS-AGENDA-B | `apoiada`, 0,91 | Eventos locais IDs 7 e 8, ambos confirmados no Google. |

O evento A ficou em 10/11/2026, das 20h às 21h; o B, em 11/11/2026, no mesmo horário. A consulta ao Google retornou os dois eventos como `confirmed`, com fuso de São Paulo.

O registro da calça também confirmou a correção do fuso: `2026-10-29 00:00:00-03:00`. Os registros ANTES foram preservados para comparação.

## Métricas

A última captura do monitor contém os oito pedidos posteriores. Ela inclui os cinco da primeira bateria e não deve ser somada à captura anterior.

| Medida | Antes: 7 pedidos | Depois: 8 pedidos |
|---|---:|---:|
| Latência média | 27,476 s | 69,692 s |
| P95 | 77,129 s | 170,056 s |
| Erro técnico no monitor | 0% | 25% |
| Tokens de entrada | 152.791 | 140.844 |
| Tokens de saída | 14.939 | 12.213 |
| Tokens totais | 167.730 | 153.057 |
| Custo estimado | US$ 0,062414 | US$ 0,038744 |
| Fallbacks | 3 | 13 |

O SLO de p95 até 8 s e erro até 5% não foi atingido. As quotas dos provedores, as chamadas repetidas e os fallbacks contribuíram para a demora. Como a quantidade e os resultados dos pedidos mudaram, a redução de tokens e custo não basta para concluir que houve ganho de eficiência.

Os três últimos pedidos terminaram com status técnico `ok`: dois foram recusados pelo juiz e um foi executado. Uma recusa sem falha técnica não entra como erro de infraestrutura. No conjunto dos oito pedidos, houve seis `ok`, dois `erro` e nenhum `bloqueado`.

A espera pelo JEV entra na latência, mas seus tokens e custo não são contabilizados pelo coletor de chat models. Chamadas de modelo que falham também têm contabilização incompleta de custo.

## Testes automatizados e comparação das tools

Além dos pedidos manuais, passaram 39 testes automatizados e nove cenários controlados de antes/depois. Os testes usam SQL em schema isolado, com modelos, juiz e Google simulados para controlar as respostas e as falhas.

Os nove cenários cobrem `add_transaction`, `update_transaction` e `add_event`. Para cada tool, verificamos a escrita mantida após recusa na versão antiga, a recusa sem efeito na versão nova e a aprovação com uma única gravação. Cada caso registra marcador, estado inicial, retorno da tool, veredito e estado final. Reexecutar o mesmo plano aprovado foi bloqueado.

Nos pedidos manuais, temos INSERT e UPDATE recusados sem efeito, INSERT aprovado e dois eventos aprovados em ambos os destinos. A aprovação de UPDATE e o commit da conversão foram exercitados nos testes controlados.

## Arquivos com as evidências

- [metricas_antes.json](metricas_antes.json): dados da linha de base.
- [metricas_depois.json](metricas_depois.json): primeira bateria posterior, com respostas, planos e vereditos.
- [metricas_reteste_final.json](metricas_reteste_final.json): segunda bateria, monitor cumulativo, banco e confirmação Google.
- [validacao_controlada.json](validacao_controlada.json): nove cenários nas três tools.
- [decisao_tecnica.md](../decisao_tecnica.md): decisão, alternativas e relação dos arquivos alterados.

Os registros brutos ficaram em `Downloads/metricas_assessor`, nas pastas `depois_20261029_123426_985` e `depois_20261029_125042_987`. Os códigos HTTP do navegador não foram capturados; as respostas foram conferidas no histórico e nos traces.

## Quotas encontradas

Os logs mostraram limite diário de 20 requisições no Gemini e limite de 8.000 tokens por minuto no Groq. No Gemini, gerar outra chave no mesmo projeto não renova a cota. No Groq, uma sessão nova reduz o histórico enviado, mas cada pedido ainda pode fazer várias chamadas ao modelo.

Referências: [limites do Gemini](https://ai.google.dev/gemini-api/docs/rate-limits) e [limites do Groq](https://console.groq.com/docs/rate-limits).
