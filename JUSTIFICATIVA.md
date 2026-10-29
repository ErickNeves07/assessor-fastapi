# Justificativa da arquitetura do perfil

## 1. Arquivos criados e modificados

Criamos `app/perfil.py`, `app/routes/perfil.py`, `app/tools/perfil.py` e este documento.

Alteramos `app/schemas.py`, `app/vectorstore.py`, `app/main.py`, `app/graph.py` e `app/prompts.py` para incluir o cadastro e a consulta do perfil no fluxo existente.

## 2. Onde o perfil entra e como é salvo

O cadastro vem da tela de perfil, que envia os dados para `POST /perfil`. A rota valida a entrada e chama `salvar_perfil()`.

Usamos dois armazenamentos porque cada um atende a uma necessidade:

- O MongoDB guarda o cadastro estruturado na collection `perfis`.
- O Qdrant guarda os embeddings das preferências em `perfil_preferencias`, para busca semântica.

A gravação fica concentrada na mesma função, em vez de espalhada entre a rota e as tools. Assim, as regras de atualização do perfil ficam em um só lugar.

## 3. Como as preferências são consultadas

As preferências são transformadas em embeddings e consultadas por similaridade no Qdrant. Isso permite relacionar expressões próximas, mesmo quando as palavras são diferentes.

Por exemplo, “prefiro algo conservador” e “não gosto de investimentos muito arriscados” podem indicar uma preferência semelhante. Uma busca apenas por palavra-chave teria dificuldade para reconhecer essa relação.

O Mongo continua guardando os dados explícitos do cadastro. A busca semântica complementa esse cadastro quando o agente precisa de contexto para uma recomendação.

## 4. Por que usamos uma única tool

Criamos `consultar_perfil` para reunir a consulta do cadastro e, quando necessário, das preferências semânticas.

Com uma única tool, o agente recebe o contexto do perfil em uma chamada. Isso evita que ele precise juntar informações de várias ferramentas ou preencher dados ausentes por suposição.

O `user_id` vem do contexto da requisição. A busca vetorial é feita quando existe um assunto relevante para comparar com as preferências, sem repetir essa etapa em toda consulta.

## 5. Como os perfis são separados

As consultas filtram os dados pelo `user_id`:

- No MongoDB, usamos o filtro `{"user_id": ...}`.
- No Qdrant, usamos `FieldCondition(key="user_id", match=...)`.

O modelo não escolhe livremente qual perfil consultar: a tool recebe o identificador pelo contexto. Essa separação diz respeito aos perfis; ela não substitui a autenticação e o controle de acesso da API.

## 6. Por que a tool consulta os bancos diretamente

A tool usa as funções de `app.perfil`, que já estão disponíveis no processo da API. Fazer uma chamada HTTP para a própria aplicação acrescentaria uma etapa sem trazer benefício para esse fluxo.

A consulta direta evita essa latência e mantém a lógica de acesso aos dados concentrada no módulo do perfil.

## 7. Por que não criamos um agente de perfil

O perfil serve de apoio ao aconselhamento financeiro. Ele fornece renda, objetivos e preferências, mas não precisa de uma conversa ou de um roteamento próprio.

Por isso, mantivemos o agente financeiro e acrescentamos uma tool de consulta. Um agente separado aumentaria o número de etapas para buscar dados que o especialista já pode receber diretamente.

## 8. Por que o chat não altera o cadastro

A escrita fica na tela de perfil e na rota `POST /perfil`. Pelo chat, o agente apenas consulta os dados.

Essa divisão evita que uma interpretação do modelo altere o cadastro sem uma ação explícita na tela. O usuário atualiza o perfil pelo formulário, e o agente usa essas informações como contexto para responder.
