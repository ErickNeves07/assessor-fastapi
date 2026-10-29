# Juiz tardio e conversão para reais — decisão técnica

Branch: `feat/juiz-tardio`. Implementação da prática livre da Etapa 8, sem wrappers e sem novo agente.

## 1. Problema, evidências anteriores e objetivo

Na versão anterior, `add_transaction`, `update_transaction` e `add_event` faziam `commit` antes do orquestrador, do guardrail de saída e do juiz. Uma resposta recusada não desfazia a alteração já confirmada.

A bateria real de 29/10/2026 teve sete turnos, incluindo uma repetição de atualização e uma consulta adicional. Os dados consolidados estão em [docs/metricas_antes.json](docs/metricas_antes.json). Os retornos de ferramentas e a leitura posterior do banco confirmaram:

| Caso anterior | Juiz | Efeito que permaneceu |
|---|---|---|
| Criar `ANTES-CAMISETA` | `apoiada:abaixo_do_limiar`, 0,46 | INSERT ID 21, valor 15 |
| Atualizar `ANTES-CAMISETA` | `sem_apoio`, 0,52 | ID 21 alterado de 15 para 18 |
| Criar `ANTES-CAMBIO`, pedido de 10 USD | `sem_apoio`, 0,36 | INSERT ID 23, valor 10 sem conversão |

Isso comprovou criação e atualização financeiras mantidas após recusa. Não houve `add_event` recusado naquela amostra; não se atribui a ela uma evidência que não foi coletada. O teste de agenda mostrou outro problema: A e B foram inseridos localmente, mas somente A teve chamada Google. Após falhas de quota Gemini e fallback Groq, o especialista terminou vazio; o usuário recebeu `(sem resposta)`, aprovada pelo juiz em 0,80.

Também se confirmou um defeito de fuso: `2026-10-29T00:00:00` sem offset era interpretado em GMT e passava a 28/10 às 21h em São Paulo. O ajuste pontual da conexão foi feito depois de preservar essa coleta. Nenhuma linha antiga foi corrigida retroativamente.

O HTTP 503 da rota de chat trata exceções dos provedores de modelo; não é o veredito do juiz. Na arquitetura anterior, uma falha de modelo posterior à tool podia coexistir com efeito já gravado. Agora, falha antes da aprovação não executa o plano. Respostas vazias também não autorizam escrita. O roteador passou a receber histórico de falas, sem chamadas e resultados técnicos dos especialistas, e seu prompt limita ferramentas à busca de histórico. Isso evita reproduzir uma chamada financeira que pertence a outro agente. O limite e as quotas dos provedores continuam sendo restrições externas.

Regra principal da solução: **se o juiz recusar, nenhuma criação ou alteração de negócio desse turno será executada no Postgres ou no Google**. Erro, timeout, chave ausente ou veredito inválido também não autorizam execução. Aprovação permite tentar executar; não comprova que a infraestrutura concluiu a operação.

## 2. Decisão: adiar nas próprias tools

Antes:

```text
especialista -> tool executa e confirma -> orquestrador -> guardrail -> juiz -> resposta
```

Depois:

```text
especialista -> tools preparam operações e consultas projetadas
             -> orquestrador prepara rascunho -> guardrail -> juiz -> fim do grafo
             -> coordenador Python executa plano aprovado -> confirma resultado -> resposta e histórico
```

O juiz continua sendo o último nó/agente do grafo. O executor é código Python chamado pelo coordenador depois de `fluxo_agentes.invoke`, sem outro modelo e sem alterar `POST /chat` ou `ChatResponse(resposta=...)`.

As próprias tools preparam objetos validados, com referência temporária e status `preparado`. Não abrem transação de escrita nem geram IDs fictícios. As funções SQL de escrita ficam no executor. Não há registro provisório nas tabelas de negócio.

O plano é exclusivo da requisição e propagado por contexto explícito do runtime. Sua representação serializável chega ao orquestrador e ao juiz. Um hash vincula o veredito ao turno e ao conteúdo aprovado; alteração posterior do plano invalida a autorização. O executor exige `aprovado is True`, plano congelado e ainda não consumido. Planos e aprovações anteriores são limpos no histórico e na inicialização de cada turno.

Requisições da mesma sessão são serializadas, e o histórico do grafo é separado pela combinação usuário/sessão. Isso isola os planos; não cria um sistema novo de segregação de dados financeiros por usuário. O esquema original não tem coluna de propriedade por usuário em `transactions` ou `events`, e não foi alterado para multitenancy.

### Consultas e ordem

`query_transactions`, `total_balance`, `daily_balance` e `query_events` continuam lendo o Postgres. Uma visão virtual SQL combina registros persistidos e apenas as operações preparadas anteriormente. Filtros, agregações, ordenação e limite são aplicados depois dessa combinação, evitando carregar o histórico inteiro em Python ou anexar uma linha a uma lista já limitada.

Cada resultado de consulta é guardado como fotografia do passo em que ocorreu:

- Inserir e depois consultar: inclui a inserção projetada.
- Consultar e depois inserir: a consulta anterior não inclui a inserção posterior.
- Alterar e consultar: usa o novo valor projetado, sem duplicar a linha.
- Consultar, inserir e consultar: preserva duas fotografias diferentes.
- Inserir e alterar no mesmo turno: usa referência temporária, resolvida para ID real pelo executor.

As ferramentas dos especialistas executam em sequência, inclusive quando o modelo entrega várias chamadas no mesmo lote. O contexto do turno torna o preparo anterior visível à consulta seguinte. O juiz recebe os passos para verificar compatibilidade com a ordem solicitada pelo usuário.

### Execução financeira

Os lançamentos e alterações financeiros do plano compartilham uma transação curta após aprovação. Qualquer falha antes do commit cancela esse grupo. UPDATE revalida a linha sob bloqueio e compara com o estado usado no preparo; não sobrescreve silenciosamente uma alteração concorrente. A moeda, os valores e os alvos aprovados não são reescritos para contornar um conflito.

### Execução de agenda e Google

Cada evento aprovado tem sua própria confirmação:

```text
SQL do evento, sem commit -> chamada de criação Google -> commit do evento local
```

O preparo de `add_event` inclui automaticamente a intenção Google, salvo pedido explícito somente local. Isso impede que a criação externa dependa de o modelo lembrar de chamar outra tool para cada item. `add_google_event` permanece como preparação compatível da mesma intenção; não cria duplicata dentro do turno nem aceita evento órfão ou dados divergentes.

O executor revalida conflito de horário e usa um bloqueio transacional consultivo para serializar as gravações de agenda realizadas por este código. Não há conexão de escrita aberta durante as chamadas dos agentes ou do juiz. A transação de agenda aguarda a chamada Google, com o custo correspondente de conexão e bloqueio.

Num lote, eventos anteriores já confirmados são preservados se o atual falhar. A execução para e informa os confirmados, o item falho/incerto e a não execução dos seguintes. A resposta integral do orquestrador não é liberada nesse caso.

## 3. Decisões de negócio fechadas no chat

| Tema | Decisão implementada |
|---|---|
| Ordem após o juiz | SQL sem commit, Google, confirmação local por evento |
| Falha explícita Google | Cancelar o registro local do item e informar o erro |
| Timeout/resultado externo desconhecido | Cancelar local, informar incerteza e pedir conferência do calendário antes de repetir |
| Google confirmou e banco falhou | Informar o que foi confirmado; permitir pedido posterior somente de inserção local |
| Reparo somente local | Não chamar Google; conferir existência local/conflito antes de preparar outra inserção |
| Vários eventos | Preservar sucessos anteriores, parar na falha e relatar resultado parcial |
| Reenvio de requisição | Sem idempotência durável adicional; API atual preservada; novo POST é nova solicitação |
| Google | Tentado por padrão em todo agendamento normal; somente inserção, sem consulta, alteração ou exclusão de eventos |
| Novas tentativas posteriores | Nenhuma fila, pendência para sincronização ou reexecução automática posterior |
| Compensação | Excluída: não excluir/criar efeito inverso após gravação permanente |
| Novo agente | Nenhum |
| Wrapper de ferramentas | Excluído por escolha do usuário; preparo nas tools |
| Limiar do juiz | 0,55, com `choice=apoiada` obrigatório; reduzir score não substitui corrigir fatos |
| Data dinâmica por middleware | Não reintroduzida |
| Evidências da agenda | Sem `evidencias_agenda.py` ou camada nova de normalização antes do juiz |
| Metadados de câmbio no banco | Não criar coluna, tabela ou persistência adicional; manter `source_text` original já existente |

Uma recusa legítima do juiz e uma falha técnica depois de sua aprovação são situações diferentes. O executor não reapresenta o resultado como recusa posterior do juiz, nem afirma que um Google confirmado foi desfeito.

## 4. Conversão de moedas

Biblioteca escolhida: `CurrencyConverter==0.18.22`. Dados históricos oficiais do Banco Central Europeu são baixados com timeout e cache local de dados públicos por data, fora do repositório. BRL não consulta cotação. Não se usa silenciosamente a base embutida possivelmente desatualizada nem interpolação de taxas.

O cálculo usa `Decimal`, arredondamento `ROUND_HALF_UP` para centavos e validação do limite de `NUMERIC(14,2)`. A taxa é da data da transação ou da última data disponível anterior. A fonte e a data efetiva aparecem na evidência temporária para o juiz. A conversão é congelada no plano e não é refeita após o veredito.

Sem moeda suportada ou cotação confiável, não preparar número estrangeiro como reais: pedir o valor correspondente em BRL. Símbolo ambíguo pede esclarecimento. Não há cotação futura inventada. Alterar somente data/descrição não reconverte um valor antigo automaticamente.

`amount` continua em BRL. Não foram criados `fx_metadata` nem migração de banco. A evidência de conversão existe no contexto e nos retornos do turno; não foi criado registro durável específico de câmbio. O campo existente `source_text` continua com o pedido original. Por isso, consultas futuras podem informar o valor BRL registrado, mas não reconstruir a taxa específica de uma conversão passada.

Um teste de integração de leitura obteve cotação real para 10 USD em 28/10/2026 e calculou R$ 53,27. Os testes automatizados usam taxas determinísticas para verificar cálculo e arredondamento; não dependem desse número fixo nem da disponibilidade da internet.

## 5. Alternativas analisadas e motivos para não adotá-las

| Alternativa | Como funcionaria | Vantagem real | Custo/limite e decisão |
|---|---|---|---|
| Interceptar e julgar cada escrita | Avaliar uma ação antes de chamar sua tool | Pode barrar uma ação cedo | Aprovação individual não cobre recusa final posterior do turno; acrescentaria avaliações e não atenderia sozinha à regra |
| Wrapper de adiamento | `wrap_tool_call` guardaria argumentos e retornaria preparado | Centraliza a fronteira para várias tools | Continua exigindo plano, validação, projeções e executor; usuário preferiu alterar as tools diretamente |
| Transação aberta desde a primeira tool | SQL provisório compartilhado enquanto modelos e juiz respondem | Banco executa SQL e enxerga alterações próprias antes do juiz | Conexão e possíveis bloqueios ficam presos durante a latência de IA; preferido adiamento com transação posterior curta |
| Linha com status provisório | Inserir antes e confirmar status depois | Facilita guardar intenção de forma durável | Já grava antes da decisão, exige esquema e filtros novos; não escolhido |
| Compensar | Gravar e executar operação inversa na recusa | Pode reaproveitar tools que confirmam cedo | Segunda operação também pode falhar; Google não tem exclusão/alteração no escopo; excluído pelo usuário |
| Comprovante que mantém escrita apesar da recusa final | Mostrar resultado persistido mesmo quando a redação foi recusada | Esclarece um efeito que já ocorreu | Contraria a invariável do HTML; proposta anterior corrigida e descartada |
| Google antes de qualquer SQL | Criar externamente e só então executar INSERT | Evita espera Google com transação local aberta | Erros SQL previsíveis podem deixar efeito só externo; executamos SQL sem commit primeiro |
| Commit local antes de Google | Confirmar banco e tentar Google depois | Transação local mais curta | Deixaria item local em falha Google; contrário à regra escolhida |
| Outbox e novas tentativas | Confirmar evento e tarefa durável de sincronização | Recupera falhas entre banco e API | Exigiria aceitar evento Google pendente; usuário retirou essa opção |
| ID estável no Google + recuperação de pedido | Reusar identificador e guardar resultado para reconhecer repetição | Reduz duplicação quando resposta se perde | MCP `create_event` atual não expõe ID de evento; exigiria rever integração e identificação entre cliente/API; fora desta entrega |
| Transação distribuída/2PC | Coordenar confirmação de ambos os destinos | Atomicidade se todos participassem | Calendar não participa da transação Postgres; `PREPARE TRANSACTION` isolado não resolve o resultado externo desconhecido |

## 6. Falhas, quedas, histórico e garantias restantes

| Situação | Comportamento |
|---|---|
| Juiz recusa ou não autoriza validamente | Não chamar executor SQL/Google |
| Erro de preparo | Nenhuma execução do plano incompleto; esclarecer impedimento |
| Plano alterado após avaliação | Hash diverge, execução impedida |
| SQL financeiro falha antes do commit | Cancelar o grupo financeiro, sem publicar consultas que dependiam dele |
| Google rejeita | Cancelar SQL do evento atual; preservar eventos anteriores confirmados |
| Google timeout, resposta inválida ou sem ID | Resultado externo incerto; cancelar item local e orientar conferência |
| Google confirmado e commit local rejeitado | Google permanece; informar e oferecer pedido somente local |
| Conexão perdida durante commit | Pode ter confirmado; informar incerteza, não afirmar ausência de escrita |
| Processo cai antes do commit | Transação aberta é cancelada; Google já criado pode permanecer |
| Processo cai depois do commit e antes da resposta | Efeito pode existir sem resposta recebida; não há garantia de reenvio idempotente entre novos pedidos |
| Vários eventos com falha intermediária | Preservar anteriores; informar parcial, não tentar seguintes |
| Falha Mongo após commit | Preservar resposta de negócio conhecida, registrar erro técnico; não repetir operação |
| Mesma requisição lógica reenviada como novo POST | Pode gerar outra operação; não deduplicar silenciosamente por texto, título ou horário |

Não há atomicidade perfeita entre Calendar e Postgres. O conjunto atual não consulta, altera ou exclui eventos Google para reconciliar. Identificar resultado incerto é parte do comportamento, não prova de que nada ocorreu.

O plano é executado uma vez dentro do turno. Isso não é idempotência durável entre processos ou requisições. Reiniciar perde contexto em RAM. Não se prometem garantias adicionais sem identidade estável e registro durável de execução.

`salvar_mensagem` no Mongo permanece como histórico/auditoria, fora das tabelas de negócio protegidas pela invariável. A mensagem humana pode ser registrada antes do veredito; a resposta assistente salva é a efetivamente selecionada depois da execução. O histórico do grafo substitui o rascunho pelo resultado real. Falhas de histórico após confirmação são registradas, sem compensar ou negar um efeito conhecido.

O MCP financeiro independente foi adaptado para preparar e passar pelo mesmo juiz/executor, mantendo suas ferramentas e parâmetros. Não recebeu wrapper ou agente adicional. O saldo diário sem argumento usa o dia atual de São Paulo. Esse ajuste evita deixar as escritas MCP incompatíveis com tools que agora exigem contexto de turno.

## 7. Métricas SRE e comparação

| Métrica real anterior | Valor |
|---|---:|
| Turnos | 7 |
| Latência média | 27,476 s |
| Latência p95 | 77,129 s |
| Tokens entrada | 152.791 |
| Tokens saída | 14.939 |
| Tokens total | 167.730 |
| Custo estimado | US$ 0,062414 |
| Fallbacks | 3 |
| Status `ok` no monitor | 7 |
| Recusas do juiz com escrita mantida | 3/3 nos casos observados |

O monitor anterior mostrava 0% de erro, apesar de tarefas incompletas e escritas após recusa. Status técnico, veredito e resultado funcional precisam ser comparados separadamente. Não houve captura de status HTTP recebido pelo navegador; não se inventou 200 nos registros.

Na solução escolhida, a latência inclui serialização por sessão, preparo, consultas, câmbio, modelos, juiz, execução e histórico. O wrapper não foi adotado; não há chamada extra de modelo para executar ou montar erro. O plano completo aumenta a entrada do orquestrador e do juiz. Consultas/resoluções podem acrescentar acessos curtos ao banco. Cache de câmbio reduz downloads repetidos. Executar tools em sequência pode aumentar a duração de lotes antes paralelos, mas preserva a ordem solicitada.

Na alternativa de transação longa, o número de chamadas de modelo poderia ser o mesmo, mas a conexão e os bloqueios durariam durante essas chamadas. Na alternativa de interceptar/julgar individualmente, haveria mais chamadas de avaliação, latência e custo por tool. Compensar acrescentaria outra operação de infraestrutura e mais pontos de falha.

O SLO continua p95 até 8 s e taxa de erro até 5%. Bloqueio de entrada é `bloqueado`, fora do numerador de erro. Recusa legítima do juiz sem falha técnica é resposta segura, sem contar como erro de infraestrutura. Falha técnica de juiz, preparo, SQL, Google ou histórico é `erro`, mesmo se houver resposta compreensível. Fallback recuperado pode terminar em `ok`; sua ocorrência é registrada separadamente.

Limites de medição: painel guarda somente 50 turnos na RAM; reiniciar zera. Custo é estimativa da tabela fixa. Tokens/custo JEV, chamado diretamente por HTTP, não entram no coletor de chat models; espera JEV entra na latência. Chamadas de modelo com erro não têm contabilização completa de tokens/custo. Não interpretar ausência como custo provadamente zero.

### O que já foi verificado depois

Foram aprovados 37 testes da suíte SQL/grafo/câmbio, um teste dirigido de autorização estrita e um de classificação técnica no monitor: **39 testes, sem falhas**, entre a execução da suíte e verificações dirigidas posteriores. Usaram schema Postgres isolado, removido ao final, Google simulado e modelos/juiz determinísticos. Verificam recusa sem efeito nas três tools, confirmação uma vez, ordem de consultas, UPDATE projetado, câmbio, falha intermediária, timeout, commit incerto, histórico, MCP, plano antigo/mutado e falha técnica de preparo seguida de recusa.

Isso comprova os comportamentos nas condições controladas. Não é uma medição de latência, tokens ou custo dos provedores reais. As métricas SRE reais posteriores permanecem pendentes da bateria manual abaixo. Não se preencheu uma coluna de desempenho posterior com tempos de testes simulados nem se prometeu cumprir o SLO antes de medir.

## 8. Mensagens para o teste manual

Instalar as dependências no mesmo Python do servidor e iniciar/reiniciar a API. Manter limiar 0,55 nos dois lados da comparação. Usar sessão nova por cenário independente e o mesmo histórico para cenários encadeados. Os registros ANTES não foram corrigidos nem removidos; use os marcadores DEPOIS para localizar os novos. Verifique horários livres para eventos reais.

Os resultados de sucesso abaixo pressupõem aprovação do juiz. Em qualquer recusa, conferir que o marcador não foi inserido e que uma atualização manteve o valor anterior. Se repetir a bateria, trocar os marcadores e horários para distinguir rodadas e evitar conflitos com eventos já criados.

1. **Inserir e consultar:**

   > Registre uma compra de camiseta de 15 reais em 29/10/2026, com descrição DEPOIS-CAMISETA, e depois liste todas as transações desse dia.

   Esperado: lista inclui a camiseta projetada; só liberar resposta de sucesso depois do commit. Conferir `amount=15` e dia local 29/10.

2. **Atualizar o lançamento do teste 1:**

   > Altere o valor da compra DEPOIS-CAMISETA de 29/10/2026 para 18 reais.

   Esperado: atualizar o mesmo ID para 18, sem nova linha e sem trocar a data por 28/10.

3. **Consultar antes de inserir:**

   > Primeiro liste todas as transações de 29/10/2026 e só depois registre uma compra de calça de 30 reais nesse dia, com descrição DEPOIS-CALCA.

   Esperado: a lista anterior não inclui DEPOIS-CALCA; depois do sucesso, uma nova consulta deve encontrá-la no dia local 29/10.

4. **Converter USD:**

   > Registre uma compra de 10 dólares americanos em 28/10/2026, com descrição DEPOIS-CAMBIO.

   Esperado: preparar conversão com taxa da data ou última anterior e gravar o valor resultante em BRL, sem coluna nova. Não gravar simplesmente 10 como reais. Se a fonte falhar e não houver cache válido, pedir o valor em BRL e não gravar.

5. **Dois eventos Google:**

   > Crie o evento DEPOIS-AGENDA-A em 10/11/2026 das 20h às 21h e o evento DEPOIS-AGENDA-B em 11/11/2026 das 20h às 21h.

   Esperado: ambos preparados e avaliados; após aprovação, confirmar cada item no banco e no Google. Não depende de o agente chamar `add_google_event` para B. Se um falhar, informar o parcial, preservando anteriores confirmados. Os horários foram deslocados para não colidir com os testes ANTES das 18h.

Somente se houver sucesso Google com falha local, pode usar:

> O Google já confirmou o evento [título] em [data e horário]. Registre somente no banco local, sem criar novamente no Google.

Não usar esse reparo para gerar outro evento sem necessidade. Se houver registro local ou resultado de commit incerto, verificar primeiro. Não existe frase que garanta, por si só, um veredito específico do modelo; a recusa e falhas de infraestrutura são verificadas deterministicamente nos testes automatizados, sem provocar erros no calendário pessoal.

## 9. Como medir depois e comparar

Após concluir os pedidos e antes de reiniciar ou ultrapassar 50 turnos:

```powershell
& .\scripts\exportar_metricas.ps1 -Etapa depois
```

O script somente faz GET no monitor e salva JSON, resumo, turnos, modelos, rotas, ficha funcional e contexto (commit, hashes, limiar) em `Downloads\metricas_assessor`. Não envia mensagens nem escreve no banco/Google. Preencher a ficha com rótulo `[juiz]`, resposta recebida, IDs e estado antes/depois. Não somar novamente janelas sobrepostas.

Comparar os mesmos cenários e quantidade de repetições, separando criação, atualização, consulta, moeda e agenda. A amostra ANTES teve sete turnos e não é prova estatística de confiabilidade em produção. P95 pode ficar perto do máximo em amostras pequenas. Comparar custo/tokens médios por pedido, não somente total de baterias de tamanhos diferentes. Controlar quotas e fallbacks: a falha Gemini influenciou a bateria anterior.

| Indicador | Antes observado | Depois |
|---|---|---|
| Efeito após recusa | 3/3 nas recusas financeiras observadas | Nenhum nos testes controlados das três tools; conferir também os turnos reais |
| Ordem consulta/inserção | Lote paralelo e efeitos de fuso observados | Verificada em testes reais SQL com modelo simulado; validar respostas reais |
| Dois eventos | A/B locais; somente A Google; resposta vazia | Dois destinos por item verificados com Google simulado; conferir calendário no teste manual |
| Conversão | 10 USD gravados como número 10 | Decimal e data anterior verificados; confirmar novo registro real |
| Latência, tokens, custo, p95, erro real | Tabela da seção 7 | Preencher com a exportação posterior; não estimado como resultado medido |

A coleta anterior foi feita antes do ajuste pontual de fuso. Compará-la diretamente com a solução final mede o conjunto das correções. Para atribuir diferenças exclusivamente ao adiamento, seria necessária uma coleta intermediária com fuso já corrigido. Falhas que antes apareciam como `ok` podem agora ser classificadas corretamente; distinguir melhoria na medição de mudança na frequência real de falhas.

## 10. Arquivos criados

| Arquivo | Função |
|---|---|
| `app/planejamento.py` | Operações validadas, contexto por turno, passos/fotografias, hash e exclusão mútua por sessão |
| `app/cambio.py` | CurrencyConverter, cache público, datas, Decimal e evidência temporária |
| `app/execucao.py` | Executor autorizado, transação financeira, confirmação por evento e mensagens de falha/parcial/incerto |
| `tests/__init__.py` | Pacote de testes |
| `tests/db_isolado.py` | Schema temporário próprio e conexões de teste sem alterar tabelas existentes |
| `tests/test_cambio.py` | Moedas, taxa anterior, arredondamento e indisponibilidade |
| `tests/test_juiz_tardio.py` | Invariáveis SQL, Google simulado, falhas, referências, MCP e projeções |
| `tests/test_fluxo.py` | Grafo completo com modelos determinísticos, ordem, execução e histórico |
| `tests/test_planejamento.py` | Autorização estrita: valores ambíguos não liberam SQL |
| `scripts/exportar_metricas.ps1` | Exportação comparável antes/depois sem executar operações |
| `docs/metricas_antes.json` | Dados consolidados reais da linha de base, sem credenciais |
| `decisao_tecnica.md` | Este registro de decisão, evidências, alternativas, arquivos e testes |

## 11. Arquivos modificados

| Arquivo | Motivo |
|---|---|
| `app/tools/financeiro.py` | Substituir INSERT/UPDATE imediatos por preparo; projeções SQL, Decimal, moeda e referências temporárias |
| `app/tools/agenda.py` | Preparar eventos e intenção Google; consultas projetadas e checagem de conflitos sem escrita |
| `app/tools/calendario_google.py` | Separar preparo de criação real autorizada; distinguir erro conhecido de resultado incerto |
| `app/graph.py` | Propagar contexto, serializar ferramentas, fechar plano, executar depois do juiz e substituir rascunho pelo resultado real |
| `app/juiz.py` | Veredito explícito ligado ao plano, regras de preparo/projeção/conversão, falha fechada e evidência sem corte silencioso |
| `app/mcp_server.py` | Manter ferramentas financeiras independentes funcionais com preparo, juiz e executor; corrigir data do saldo diário |
| `app/observabilidade.py` | Distinguir erro técnico explícito mesmo quando a resposta ao usuário é tratada |
| `app/prompts.py` | Contratos de preparado, sequência, moeda, vários eventos e rascunho condicionado |
| `requirements.txt` | Fixar a biblioteca CurrencyConverter |
| `README.md` | Explicar o comportamento implementado, dependência, testes isolados e exportação |

Correções anteriores preservadas em commits próprios na mesma branch:

- `41dbf01`: `app/graph.py` e `app/prompts.py`, isolamento do contexto técnico no roteador e ferramenta única de histórico.
- `6e04fe8`: `app/juiz.py` e `README.md`, limiar 0,55 conforme pedido do usuário.
- `a1a05a7`: `app/tools/db.py`, conexão São Paulo para datas sem offset; não altera dados antigos.

Rotas, esquema público de chat, tabelas de negócio, telas, FAQ, cadastro de perfil e autenticação Google não foram redesenhados. Não foram versionados `.env`, JSON OAuth, tokens ou URLs com senha. A cópia inicial do código e os registros brutos de coleta permaneceram fora do repositório.

## 12. Verificação e referências

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

Os testes SQL criam um schema de nome aleatório `test_juiz_...`, sem modificar `public.transactions` ou `public.events`, e o removem ao final. Pode-se definir `TEST_DATABASE_URL` para um banco separado; sem essa variável usam a conexão configurada e precisam de permissão CREATE SCHEMA. Google, modelos e JEV são simulados nessa suíte; não consomem cotas de modelo nem criam eventos pessoais.

- [PostgreSQL — transações](https://www.postgresql.org/docs/current/tutorial-transactions.html)
- [PostgreSQL — interpretação de fuso](https://www.postgresql.org/docs/current/datatype-datetime.html)
- [LangChain — runtime e contexto](https://docs.langchain.com/oss/python/langchain/runtime)
- [CurrencyConverter — dados, datas e Decimal](https://pypi.org/project/CurrencyConverter/)
- [Google Calendar — criação e identificadores](https://developers.google.com/workspace/calendar/api/guides/create-events)
- [Google Calendar MCP — contrato create_event](https://developers.google.com/workspace/calendar/api/v3/reference/mcp/tools_list/create_event)
