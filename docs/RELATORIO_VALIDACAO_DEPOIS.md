# Validação posterior — 29/10/2026

## Conclusão

A proteção contra escrita após recusa foi comprovada em um pedido real: a camiseta foi preparada, consultada por projeção, recusada e não gravada. A bateria completa ainda não passou. Há falhas de quota, um erro de parsing no roteador e uma resposta inadequada depois de um INSERT aprovado.

Coleta do commit `cc5b915`, sem alterar código de aplicação ou repetir solicitações de negócio. Dados por turno, respostas completas, planos, consultas e vereditos: [metricas_depois.json](metricas_depois.json). Os registros brutos ficaram em `Downloads/metricas_assessor/depois_20261029_123426_985`.

## Resultados por pedido

| Pedido | Resultado observado | Evidência |
|---|---|---|
| Camiseta 15 BRL + consulta | Recusado, sem gravação | Tool preparado; consulta count=1 projetado; juiz sem_apoio 0,79; marcador ausente do Postgres |
| Atualizar camiseta para 18 | Não encontrada | A criação anterior foi recusada; nenhum UPDATE preparado ou executado |
| Consultar e depois inserir calça 30 | Ordem e INSERT corretos; resposta inadequada | Consulta count=0 antes do preparo; juiz apoiada 0,92; ID 24 gravado uma vez; resposta recomenda registrar o que já foi registrado |
| Compra 10 USD | Conversão preparada, turno interrompido | 53,27 BRL, taxa da data; Gemini 429 diário e Groq 429 TPM; não chegou ao juiz; nenhuma linha |
| Dois eventos | Falhou no roteador | Groq output_parse_failed; nenhuma tool de agenda/Google foi chamada; nenhum evento local |

A recusa da camiseta foi por `choice=sem_apoio`, mesmo com `noul=0,79`; baixar o limiar não resolve esse caso. O contrato atual do JEV não devolve justificativa textual, então não há evidência para atribuir a recusa a um fato específico. A resposta candidata e o plano integral foram preservados.

O fuso foi validado no registro ID 24: `2026-10-29 00:00:00-03:00`. As linhas antigas ANTES permanecem iguais; não foram corrigidas ou excluídas pela coleta. Uma nova sessão reduz histórico, mas não elimina a necessidade de corrigir a redação de confirmação e investigar o parsing.

## Métricas reais

| Medida | Antes | Depois |
|---|---:|---:|
| Turnos | 7 | 5 |
| Latência média | 27,476 s | 68,085 s |
| Latência p95 | 77,129 s | 115,512 s |
| Erro do monitor | 0% | 40% |
| Tokens de entrada | 152.791 | 85.580 |
| Tokens de saída | 14.939 | 7.791 |
| Tokens total | 167.730 | 93.371 |
| Tokens por turno | 23.961,43 | 18.674,20 |
| Custo estimado total | US$ 0,062414 | US$ 0,026932 |
| Custo estimado por turno | US$ 0,008916 | US$ 0,005386 |
| Fallbacks registrados | 3 | 5 |
| Tentativas LLM nos traces | 51 | 35 |
| Tentativas LLM com erro | 3 | 8 |

A média aumentou 147,8% e o p95 49,8%. Tokens por turno caíram 22,1% e custo estimado por turno 39,6%, mas isso não demonstra ganho de eficiência: dois pedidos abortaram e a composição/amostra mudou. Os SLOs de p95 até 8 s e erro até 5% não foram atendidos. Não atribuir toda a piora ao adiamento: quotas, repetição de chamadas, fallback e crescimento do histórico também influenciaram. JEV não entra nos tokens/custo do coletor, e tentativas falhas têm custo incompleto.

## Cobertura e pendências

- Antes real: INSERT e UPDATE financeiros persistiram após recusa; não houve caso real anterior de add_event recusado.
- Depois real: INSERT recusado sem efeito; consulta antes/depois do preparo; aprovação com INSERT único e data local correta; preparo real de conversão.
- Suíte automatizada anterior: 39 testes passaram com SQL isolado e modelos/Google/juiz simulados; inclui as três tools, erros e confirmação uma vez.
- Auditoria complementar concluída: nove cenários passaram; ver [validacao_controlada.json](validacao_controlada.json). Compara as tools antigas do ZIP com as atuais, em schema próprio, com marcador único, estado antes/depois e juiz controlado. Não é execução dos modelos reais.
- Ainda sem ponta a ponta real bem-sucedida nesta bateria: UPDATE de registro existente, conversão seguida de commit e criação dos eventos Google. Há ainda ajuste necessário na confirmação textual da calça e investigação da recusa/parsing.

## Próximos testes mínimos

Não repetir a calça: ID 24 já existe. Quando houver cota, usar uma sessão nova por cenário e registrar a resposta antes de exportar novamente:

1. `Altere o valor da transação de ID 24, DEPOIS-CALCA, para 35 reais.` Esperado: mesmo ID, valor 35, sem linha nova. Se recusado, permanecer em 30.
2. `Registre uma compra de 10 dólares americanos em 28/10/2026, com descrição DEPOIS-CAMBIO-RETESTE.` Esperado: conversão e commit após aprovação; valor em BRL e mensagem de confirmação coerente.
3. `Crie o evento DEPOIS-AGENDA-A em 10/11/2026 das 20h às 21h e o evento DEPOIS-AGENDA-B em 11/11/2026 das 20h às 21h.` Esperado: dois eventos locais e Google depois da aprovação; conferir horários livres.

Esses pedidos não garantem resolver falhas de modelo nem obter aprovação. Não provocar timeout/erro no calendário pessoal: os testes controlados cobrem esses cenários.

## Limites dos provedores

O log mostrou Gemini com cota diária de 20 requisições por projeto/modelo e Groq com TPM 8.000. Trocar chave no mesmo projeto Google não renova a cota. Aguardar reset ou utilizar projeto/plano autorizado com cota disponível. Para Groq, aguardar a janela indicada, reduzir histórico e enviar um cenário por vez. Não se configurou faturamento nem alteraram-se chaves na coleta.

Fontes: [limites Gemini](https://ai.google.dev/gemini-api/docs/rate-limits), [limites Groq](https://console.groq.com/docs/rate-limits).
