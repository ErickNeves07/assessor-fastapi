import hashlib
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app import graph, juiz
from app.planejamento import ContextoTurno
from app.tools.agenda import TOOLS_AGENDA
from app.tools.financeiro import TOOLS
from tests.db_isolado import BancoIsolado


def tearDownModule():
    from app import memory, perfil
    memory._mongo.close()
    perfil._mongo.close()


class ModeloRoteiro(BaseChatModel):
    respostas: list
    indice: int = 0

    @property
    def _llm_type(self):
        return "modelo-isolado"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.indice >= len(self.respostas):
            raise AssertionError("Modelo de teste recebeu chamada inesperada.")
        resposta = self.respostas[self.indice]
        self.indice += 1
        return ChatResult(generations=[ChatGeneration(message=resposta)])


class TestFluxo(BancoIsolado):
    def setUp(self):
        super().setUp()
        self.sessao = uuid.uuid4().hex
        self.historico = Mock()
        self.fim_turno = Mock()
        for nome, valor in {
            "salvar_mensagem": self.historico,
            "guardrail_entrada": lambda _: {"bloqueado": False},
            "guardrail_saida": lambda resposta, _: {"conteudo": resposta},
            "fechar_turno": self.fim_turno,
        }.items():
            remendo = patch.object(graph, nome, valor)
            remendo.start(); self.addCleanup(remendo.stop)
        remendo = patch.object(juiz, "_julgar_jev", side_effect=lambda p, r, e: juiz._publicar(r, "apoiada noul=0.99", r, aprovado=True))
        self.julgamento = remendo.start(); self.addCleanup(remendo.stop)

    def agente(self, dominio, respostas, texto_final):
        tools = TOOLS if dominio == "financeiro" else TOOLS_AGENDA
        agente = create_agent(model=ModeloRoteiro(respostas=respostas), tools=tools, context_schema=ContextoTurno)
        for nome, valor in {
            dominio + "_app": agente,
            "router_app": SimpleNamespace(invoke=lambda *a, **k: {"messages": [AIMessage(content="ROUTE=" + dominio)]}),
            "orquestrador_app": SimpleNamespace(invoke=lambda *a, **k: {"messages": [AIMessage(content=texto_final)]}),
        }.items():
            remendo = patch.object(graph, nome, valor)
            remendo.start(); self.addCleanup(remendo.stop)

    def chamada_financeira(self, valor="15"):
        return {"name": "add_transaction", "id": "t-insert", "args": {
            "amount": valor, "currency": "BRL", "source_text": "Registre uma compra em reais",
            "description": "teste-fluxo", "occurred_at": "2026-10-29T10:00:00-03:00"}}

    def resumo(self):
        return AIMessage(content=json.dumps({"dominio": "financeiro", "intencao": "inserir", "resposta": "Operação preparada."}))

    def test_batch_de_tools_respeita_preparo_antes_da_consulta(self):
        consulta = {"name": "query_transactions", "id": "t-query", "args": {"date_from_local": "2026-10-29", "date_to_local": "2026-10-29"}}
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira(), consulta]), self.resumo()], "Registrei 15 reais; a lista deste dia inclui a compra de 15 reais.")
        resposta = graph.executar_fluxo_assessor("Registre uma compra de 15 reais e depois consulte o dia", self.sessao, "teste")
        self.assertIn("Registrei", resposta)
        evidencia = self.julgamento.call_args.args[2]
        self.assertIn('"count": 1', evidencia)
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)
        self.assertEqual(self.historico.call_args.args[2], resposta)

    def test_batch_de_tools_consulta_anterior_nao_inclui_insercao(self):
        consulta = {"name": "query_transactions", "id": "t-query", "args": {"date_from_local": "2026-10-29", "date_to_local": "2026-10-29"}}
        self.agente("financeiro", [AIMessage(content="", tool_calls=[consulta, self.chamada_financeira()]), self.resumo()], "Antes não havia transações; depois registrei 15 reais.")
        graph.executar_fluxo_assessor("Primeiro consulte e depois registre uma compra de 15 reais", self.sessao, "teste")
        evidencia = self.julgamento.call_args.args[2]
        self.assertIn('"count": 0', evidencia)
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)

    def test_recusa_no_grafo_nao_executa_operacao(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira()]), self.resumo()], "Registrei 15 reais.")
        self.julgamento.side_effect = lambda p, r, e: juiz._publicar(juiz.FRASE_SEGURA, "sem_apoio noul=0.30", r)
        resposta = graph.executar_fluxo_assessor("Registre 15 reais", self.sessao, "teste")
        self.assertEqual(resposta, juiz.FRASE_SEGURA)
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)

    def test_recusa_nao_oculta_falha_tecnica_do_preparo_no_monitor(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira()]), self.resumo()], "Não consegui preparar o lançamento.")
        self.julgamento.side_effect = lambda p, r, e: juiz._publicar(juiz.FRASE_SEGURA, "sem_apoio noul=0.30", r)
        with patch("app.tools.financeiro._resolver", side_effect=RuntimeError("Banco indisponível no preparo")):
            resposta = graph.executar_fluxo_assessor("Registre 15 reais", self.sessao, "teste")
        self.assertEqual(resposta, juiz.FRASE_SEGURA)
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)
        self.assertEqual(self.fim_turno.call_args.kwargs["status"], "erro")

    def test_resposta_vazia_do_especialista_usa_plano_no_orquestrador(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira()]), AIMessage(content="")], "Registrei 15 reais.")
        resposta = graph.executar_fluxo_assessor("Registre 15 reais", self.sessao, "teste")
        self.assertEqual(resposta, "Registrei 15 reais.")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)

    def test_resposta_vazia_do_orquestrador_impede_escrita(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira()]), self.resumo()], "")
        resposta = graph.executar_fluxo_assessor("Registre 15 reais", self.sessao, "teste")
        self.assertNotIn("Registrei", resposta)
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)

    def test_erro_sql_substitui_rascunho_no_historico(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira("999")]), self.resumo()], "Registrei 999 reais.")
        resposta = graph.executar_fluxo_assessor("Registre 999 reais", self.sessao, "teste")
        chave = hashlib.sha256(f"teste\0{self.sessao}".encode()).hexdigest()
        estado = graph.fluxo_agentes.get_state({"configurable": {"thread_id": chave}}).values
        self.assertEqual(estado["messages"][-1].text, resposta)
        self.assertEqual(self.historico.call_args.args[2], resposta)
        self.assertNotEqual(resposta, "Registrei 999 reais.")
        self.assertEqual(self.fim_turno.call_args.kwargs["status"], "erro")

    def test_falha_mongo_posterior_nao_invalida_commit(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira()]), self.resumo()], "Registrei 15 reais.")
        self.historico.side_effect = [None, RuntimeError("Falha Mongo simulada")]
        with self.assertLogs(level="ERROR"):
            resposta = graph.executar_fluxo_assessor("Registre 15 reais", self.sessao, "teste")
        self.assertEqual(resposta, "Registrei 15 reais.")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)
        self.assertEqual(self.fim_turno.call_args.kwargs["status"], "erro")

    def test_plano_nao_e_reexecutado_na_pergunta_seguinte(self):
        self.agente("financeiro", [AIMessage(content="", tool_calls=[self.chamada_financeira()]), self.resumo(), self.resumo()], "Registrei 15 reais.")
        graph.executar_fluxo_assessor("Registre 15 reais", self.sessao, "teste")
        graph.executar_fluxo_assessor("Obrigado pelo registro", self.sessao, "teste")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)

    def test_dois_eventos_execucao_independe_de_segunda_tool_google(self):
        chamadas = [{"name": "add_event", "id": f"t-{i}", "args": {"title": nome, "source_text": "Crie A e B", "start_time": f"2030-01-{10+i}T18:00:00-03:00"}} for i, nome in enumerate(("A", "B"))]
        final = AIMessage(content=json.dumps({"dominio": "agenda", "resposta": "Dois eventos preparados."}))
        self.agente("agenda", [AIMessage(content="", tool_calls=chamadas), final], "Criei A e B no banco e no Google.")
        with patch("app.execucao.criar_google_aprovado", side_effect=[{"status": "ok", "id": "ga"}, {"status": "ok", "id": "gb"}]) as google:
            resposta = graph.executar_fluxo_assessor("Crie A e B", self.sessao, "teste")
            self.assertEqual(google.call_count, 2)
        self.assertEqual(resposta, "Criei A e B no banco e no Google.")
        self.assertEqual(self.sql("SELECT title FROM events ORDER BY id"), [("A",), ("B",)])
