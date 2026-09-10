# Justificativa da arquitetura do perfil do assessor

---
## 1. Quais arquivos você criou ou modificou? Indique o caminho de cada um.
Arquivos criados: criei app/perfil.py, app/routes/perfil.py, app/tools/perfil.py, JUSTIFICATIVA.md; 

Arquivos modificados: app/schemas.py, app/vectorstore.py, app/main.py, app/graph.py, app/prompts.py.

---

## 2. Onde o perfil entra e como é gravado

O perfil entra pela rota `POST /perfil`, acionada pela tela de cadastro/perfil do usuário. A ideia foi manter o ponto de escrita único e explícito, para que todo dado sensível passe por um mesmo contrato de validação antes de ser persistido.

A estrutura do perfil é salva em dois armazenamentos distintos:

- no MongoDB, em uma collection chamada `perfis`, para guardar as informações estruturadas do usuário;
- no Qdrant, em uma collection chamada `perfil_preferencias`, para armazenar as preferências em formato vetorial, prontas para comparação semântica por similaridade.

Tudo isso é disparado pela mesma função `salvar_perfil()` na rota, o que reduz inconsistência entre os dados e facilita rastreio de regras de negócio. Esse desenho também permite separar claramente o que é dado de consulta estruturada do que é dado de contexto semântico.

Em outras palavras: o Mongo guarda o cadastro atual do usuário, enquanto o Qdrant guarda o estado “de orientação” do comportamento e das preferências, em um formato que o modelo pode comparar semanticamente durante a recomendação.

---

## 3. Como as preferencias são guardadas e consultadas, e por que não é busca por palavra?

São guardadas e buscadas por embedding!

A principal razão para guardar preferências como embedding no Qdrant é permitir que o sistema entenda relações semânticas, não apenas coincidência literal de palavras.

Isso é importante porque o assessor financeiro precisa interpretar contexto de forma mais natural. Por exemplo, um usuário pode dizer que não quer algo “agressivo”, “arriscado” ou “muito volátil”, mesmo que a expressão exata não seja “cripto”, “ações”, “renda variável” ou qualquer termo textual diretamente correspondente.

Se a busca fosse por palavra-chave pura, o sistema apenas compararia strings e perderia nuances como:

- “quero algo prudente”;
- “não gosto de investimentos agressivos”;
- “prefiro algo mais conservador”;
- “evito produtos com muita volatilidade”.

Com embeddings, o modelo consegue aproximar esses conceitos sem depender de uma coincidência literal. Por isso, a consulta por semântica é mais robusta para o contexto real do usuário e reduz o risco de a recomendação ignorar preferências importantes por causa de sinônimos ou variações de expressão.

A consulta semântica do Qdrant é então uma camada de interpretação do perfil, não uma substituição do cadastro estruturado. O Mongo continua sendo a fonte de verdade para dados explícitos; o Qdrant complementa isso com representações contextuais e probabilísticas.

---

## 4.	Você criou uma tool ou duas? Por quê?

A solução foi desenhada com uma tool única: `consultar_perfil`.

Esse padrão foi adotado por três motivos principais:

1. Menor latência: uma chamada reúne o contexto relevante em um único ponto, em vez de várias operações fragmentadas.
2. Menor risco de inconsistência: o perfil é consultado como um bloco coeso e controlado, em vez de o modelo montar inferências a partir de pedaços desconectados.
3. Menos invenção pelo modelo: ao receber o contexto completo de forma estruturada e já validado, o modelo tem menos espaço para “preencher lacunas” com suposições.

A tool recebe o `user_id` do contexto da requisição e pode, opcionalmente, disparar uma busca semântica quando o assunto relevante aparece no aconselhamento. Isso significa que o sistema só faz a etapa vetorial quando há necessidade contextual de comparação semântica, e não em toda consulta.

Esse desenho melhora o uso do modelo: ele não precisa adivinhar o perfil do usuário; recebe um contexto explícito, filtrado e encapsulado pela tool.

---

## 5. O que garante que o perfil de um usuário não apareceria para outro, se houvesse mais de um?

A separação entre usuários foi implementada de forma explícita e técnica:

- no Mongo, o filtro é feito com `{"user_id": ...}`;
- no Qdrant, o filtro é feito com `FieldCondition(key="user_id", match=...)`.

O ponto mais importante é que o `user_id` vem do contexto da requisição, e não do modelo ou de qualquer entrada livre. Isso reduz o risco de vazamento de dados entre perfis e evita que um usuário acesse ou seja influenciado por dados de outro usuário.

Esse tipo de isolamento é essencial em qualquer sistema que trabalha com perfil, recomendação e dados pessoais. O contexto do usuário precisa ser sempre um dado de identidade do próprio usuário autenticado, e não um valor inferido pelo modelo.

---

## 6. Sua tool consulta o banco diretamente ou faz uma chamada HTTP na própria API? Por quê?

A tool consulta os bancos diretamente, usando as funções de `app.perfil`, da mesma forma que as outras tools do sistema fazem acesso interno a dependências já disponíveis no processo da API.

Essa decisão foi feita por motivos práticos e de arquitetura:

- evita latência extra de uma chamada HTTP interna;
- evita risco de recursão ou loop de chamadas entre serviços;
- usa a mesma infraestrutura já importada no processo da aplicação;
- reduz complexidade operacional sem sacrificar controle e observabilidade.

Em um backend como este, fazer a tool chamar a própria API por HTTP seria redundante e menos eficiente. O sistema já possui as conexões e as dependências necessárias no mesmo processo; então a melhor opção é consultar diretamente os repositórios de dados.

Além disso, essa abordagem mantém a lógica do perfil próxima ao restante do domínio, com integrações explícitas e sem “camadas artificiais” que aumentam a superfície de erro.

---

## 7. Por que não existe um agente separado para perfil

O perfil não foi modelado como um agente independente porque ele não é um domínio de conversa principal. Ele funciona como um suporte contextual para o agente de aconselhamento financeiro.

Em outras palavras:

- o agente principal é o que aconselha sobre finanças;
- o perfil é um contexto de apoio para esse aconselhamento;
- o perfil não precisa ter roteamento próprio, persona, tools independentes ou fluxo de conversa autônomo.

Seu papel é complementar e de apoio, não argumentativo. O sistema não precisa de uma “inteligência do perfil” separada, porque a lógica do perfil já existe para fornecer condições e restrições ao processo principal de recomendação.

Essa escolha simplifica a arquitetura e evita um problema comum: multiplicar agentes para dados que deveriam apenas alimentar o contexto executivo do sistema.

---

## 8. Por que o chat não altera o cadastro?

O chat não escreve diretamente no cadastro do perfil por design. A única operação de escrita é feita pela tela, que envia os dados para a rota e passa pela validação do backend.

Essa decisão tem duas consequências importantes:

1. o modelo não pode inventar valores de perfil;
2. o contrato do cadastro fica protegido contra manipulação implícita.

Em outras palavras, o agente de conversa pode utilizar o perfil como contexto, mas não pode “forçar” alterações na base de dados sem passar por uma origem explícita e controlada. Isso é essencial para manter integridade do sistema, confiabilidade do dado e previsibilidade do comportamento do assistente.

A regra foi deixar o fluxo de escrita no ponto de entrada do usuário e o fluxo de leitura no ponto de uso do assistente. Assim, o modelo trabalha com dados confiáveis e com uma fronteira clara de responsabilidade.