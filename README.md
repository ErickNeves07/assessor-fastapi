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
O fluxo consulta conflitos e prepara o evento. Depois da aprovação do juiz,
executa SQL sem commit, chama o Google e confirma o item local. Se Google falhar,
o item local é cancelado; em timeout, a resposta informa incerteza externa.
Em pedidos com vários eventos, os anteriores já confirmados são preservados.
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

As ferramentas de negócio preparam operações sem escrever. O juiz avalia o plano,
as consultas e a resposta condicionada à execução. Recusa, timeout, ausência de
chave ou veredito inválido impedem a execução das escritas. O juiz continua como
último nó; o coordenador Python executa somente depois da aprovação.

Consultas consideram apenas operações preparadas antes delas. Moedas estrangeiras
são convertidas para BRL com `CurrencyConverter`, usando a data da transação ou a
última cotação anterior disponível. A taxa é congelada durante o turno; não há
coluna nova nem persistência adicional de metadados. Sem cotação, pedir valor em BRL.

A decisão, os motivos das mudanças, os arquivos alterados e os pedidos de teste estão
em [decisao_tecnica.md](decisao_tecnica.md). A implementação não usa wrappers.

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

Os testes usam um schema PostgreSQL temporário e isolado, removido ao final;
não alteram as tabelas de negócio existentes nem criam eventos Google reais.
Configure `TEST_DATABASE_URL` para usar outro banco de teste, se desejar; na
ausência dessa variável usam `DATABASE_URL`. É necessária permissão para criar
schema. Modelos, juiz e Google são simulados nos testes automatizados.

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -t . -v
```

Depois de enviar a bateria manual no chat, exporte o monitor antes de reiniciar:

```powershell
& .\scripts\exportar_metricas.ps1 -Etapa depois
```

Os arquivos são salvos em `Downloads\metricas_assessor`. O monitor guarda somente
50 turnos no processo atual. As medições anteriores estão em
[docs/metricas_antes.json](docs/metricas_antes.json). Os resultados das duas baterias
manuais e dos testes automatizados estão em
[docs/RELATORIO_VALIDACAO_DEPOIS.md](docs/RELATORIO_VALIDACAO_DEPOIS.md).
