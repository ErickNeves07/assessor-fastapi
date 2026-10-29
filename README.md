# Assessor.AI

Assistente de finanças e agenda com FastAPI, LangGraph, PostgreSQL, MongoDB,
Qdrant e Google Calendar. O frontend é servido pela própria API.

## Instalar e executar

Use Python 3.11 ou superior. No PowerShell, na raiz deste projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Na primeira configuração, copie `.env.example` para `.env`, se ainda não existir:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Preencha `.env` com as chaves Gemini e Groq, a conexão PostgreSQL, o endereço
MongoDB e o endereço e a chave Qdrant. O PostgreSQL precisa das tabelas do
projeto: `transactions`, `transaction_types`, `categories` e `events`. Os tipos
financeiros são `INCOME`, `EXPENSES` e `TRANSFER`.

As collections Qdrant usam vetores **sem nome**, 768 dimensões e distância
Cosine: `faq_chunks`, `memoria_resumos` e `perfil_preferencias`. Crie as duas
primeiras antes de usá-las; o cadastro de perfil cria a terceira se necessário.
Para instalações com filtro estrito, crie um índice de payload keyword em
`user_id` nas collections de memória e perfil.

Com `data/faq.pdf` disponível, insira os points do FAQ:

```powershell
.\.venv\Scripts\python -m app.ingest_faq
```

Esse comando substitui todos os points de `faq_chunks`. Não altera memória nem perfil.

Inicie o servidor:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

- Chat: <http://localhost:8000/>
- Perfil: <http://localhost:8000/perfil.html>
- Monitoramento: <http://localhost:8000/monitor.html>
- Documentação das rotas: <http://localhost:8000/docs>
- Configuração: <http://localhost:8000/health>

`/health` verifica as variáveis e o PDF; não testa as conexões externas.

## Google Calendar — etapa 5.2

Coloque o JSON OAuth Desktop fornecido pelo professor na raiz, com o nome
`gcp-oauth.keys.json`, ou indique seu caminho em `GOOGLE_OAUTH_CREDENTIALS`.
Deixe `GOOGLE_CALENDAR_ACCESS_TOKEN` vazio para usar login e renovação automática.

Faça o login uma vez, na raiz do projeto:

```powershell
.\.venv\Scripts\python -c "from app.tools.calendario_google import autenticar; print(autenticar())"
```

O navegador abre para você escolher a conta e conceder acesso aos eventos.
O token é salvo em `google-calendar.token.json`. Nem o servidor nem o chat
abrem a tela de login automaticamente.

Se receber `403 access_denied` durante o login, peça ao responsável pelo projeto
Google Cloud para incluir seu Gmail nos usuários de teste. O aplicativo precisa
ter a Calendar API habilitada. O MCP também depende da habilitação do serviço
Calendar MCP; quando ele recusa com 403, o cliente tenta a Calendar API com o
mesmo token, conforme a alternativa do roteiro.

O catálogo pode ser consultado sem criar eventos:

```powershell
.\.venv\Scripts\python -c "from app.tools.calendario_google import listar_tools_google; print(listar_tools_google())"
```

No chat, teste: `Marque um teste de agenda amanhã às 14h, por uma hora.`
Confira o registro em `events` e na conta aberta em <https://calendar.google.com>.
O fluxo consulta conflitos, grava no PostgreSQL e chama o Google. Se apenas o
Google falhar, o registro local permanece e a resposta deve informar isso.
Não repita o pedido de criação sem conferir o banco e o calendário, para evitar
duplicatas. O fim padrão é uma hora após o início nos dois destinos.

Essa configuração de aula usa uma conta Google local. Autenticação de vários
usuários e armazenamento separado de tokens ainda não estão implementados.

Referências: [configuração do MCP Google](https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server)
e [contrato create_event](https://developers.google.com/workspace/calendar/api/v3/reference/mcp/tools_list/create_event).

## Monitoramento e LangSmith — etapa 6

O painel usa o registro em memória dos últimos 50 turnos. Mostra latência,
tokens, custo estimado, modelos, fallbacks e bloqueios. O SLO da aula é p95
até 8 segundos e taxa de erro até 5%. Bloqueio é uma recusa esperada e não erro.
Reiniciar o processo zera o painel.

Para receber os traces, configure no `.env` e reinicie o servidor:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=sua_chave
LANGSMITH_PROJECT=assessor-aula
```

Faça uma pergunta de finanças e confira Gemini, Groq, tokens e custo no monitor.
Depois envie `ignore as instrucoes`: deve aparecer `bloqueado`, rota `fim`,
sem aumentar a taxa de erro. Envie uma pergunta normal na mesma sessão para
confirmar que o status volta a `ok`. No LangSmith, abra o projeto configurado
e compare o trace do mesmo turno. Os preços estimados podem diferir da fatura.

## Juiz de fidelidade — etapa 7

`TYPESAFE_API_KEY` é opcional na configuração. Sem uma chave válida, os caminhos
que passam pelo juiz devolvem `Não tenho evidência para afirmar isso.`
Financeiro e agenda passam por orquestrador, guardrail de saída e juiz;
FAQ passa diretamente pelo juiz, como no roteiro. O juiz mantém a resposta
somente com `choice=apoiada` e `noul >= 0.55`.

O juiz avalia a frase depois das ferramentas. Ele não desfaz uma escrita já
confirmada no banco. Tratamento de moedas e controle de escritas recusadas
continuam como a atividade seguinte proposta no roteiro 7.

## Sessões e ferramentas

O chat envia `POST /chat` com `session_id`, `user_id` e `pergunta`. A tela usa
`usuario_teste` por padrão. O botão de nova sessão chama
`POST /sessions/{session_id}/encerrar`, que gera o resumo e o indexa no Qdrant.
`POST /sessions/{session_id}/iniciar` permite abrir uma sessão explicitamente.
O contexto da conversa em andamento fica em RAM e não sobrevive ao reinício.

O servidor MCP financeiro pode ser iniciado com:

```powershell
.\.venv\Scripts\python -m app.mcp_server
```

Não versione `.env`, `gcp-oauth.keys.json` ou `google-calendar.token.json`.
Se um arquivo já foi rastreado, adicioná-lo ao `.gitignore` não o remove do
histórico. Não compartilhe tokens nem chaves nos prints de teste.

## Verificar as alterações

Os testes de regressão não criam eventos reais nem escrevem nos bancos externos:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```
