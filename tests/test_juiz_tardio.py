from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from psycopg2 import OperationalError

from app import execucao, juiz
from app.planejamento import ContextoTurno, horario
from app.tools import agenda, financeiro
from app.tools.calendario_google import add_google_event
from tests.db_isolado import BancoIsolado


class TestJuizTardio(BancoIsolado):
    def runtime(self, pergunta="Registre uma compra em reais"):
        return SimpleNamespace(context=ContextoTurno("teste", "sessao", pergunta))

    def aprovar(self, runtime):
        plano = runtime.context.plano
        snapshot = plano.fechar()
        return {"aprovado": True, "turno_id": plano.turno_id, "hash_plano": snapshot["hash"]}

    def criar(self, runtime, valor="15", **kwargs):
        return financeiro.add_transaction.func(amount=Decimal(valor), source_text=runtime.context.pergunta,
            description="teste", occurred_at="2026-10-29T10:00:00-03:00", runtime=runtime, **kwargs)

    def evento(self, runtime, titulo="A", dia="2030-01-10"):
        return agenda.add_event.func(title=titulo, source_text=runtime.context.pergunta,
            start_time=dia + "T18:00:00-03:00", end_time=dia + "T19:00:00-03:00", runtime=runtime)

    def test_criacao_recusada_nao_grava_aprovada_grava_uma_vez(self):
        runtime = self.runtime()
        self.assertEqual(self.criar(runtime)["status"], "preparado")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)
        veredito = self.aprovar(runtime)
        recusado = {**veredito, "aprovado": False}
        self.assertEqual(execucao.executar_plano(runtime.context.plano, recusado).status, "recusado")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)
        self.assertEqual(execucao.executar_plano(runtime.context.plano, veredito).status, "ok")
        self.assertEqual(execucao.executar_plano(runtime.context.plano, veredito).status, "erro")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)

    def test_update_recusado_preserva_original_e_aprovado_atualiza(self):
        identificador = self.sql("INSERT INTO transactions(amount,type,occurred_at,source_text) VALUES (10,2,'2026-10-29 10:00-03','teste') RETURNING id")[0][0]
        runtime = self.runtime("Altere para 18 reais")
        resultado = financeiro.update_transaction.func(id=identificador, amount=Decimal("18"), runtime=runtime)
        self.assertEqual(resultado["status"], "preparado")
        veredito = self.aprovar(runtime)
        execucao.executar_plano(runtime.context.plano, {**veredito, "aprovado": False})
        self.assertEqual(self.sql("SELECT amount FROM transactions")[0][0], Decimal("10"))
        self.assertEqual(execucao.executar_plano(runtime.context.plano, veredito).status, "ok")
        self.assertEqual(self.sql("SELECT amount FROM transactions")[0][0], Decimal("18"))

    def test_evento_recusado_nao_grava_nem_chama_google(self):
        runtime = self.runtime("Crie um evento")
        self.evento(runtime)
        veredito = self.aprovar(runtime)
        with patch.object(execucao, "criar_google_aprovado") as google:
            execucao.executar_plano(runtime.context.plano, {**veredito, "aprovado": False})
            google.assert_not_called()
        self.assertEqual(self.sql("SELECT count(*) FROM events")[0][0], 0)

    def test_evento_aprovado_google_automatico_e_uma_gravacao(self):
        runtime = self.runtime("Crie um evento")
        self.evento(runtime)
        self.assertEqual(len(runtime.context.plano.operacoes), 2)
        with patch.object(execucao, "criar_google_aprovado", return_value={"status": "ok", "id": "g1"}) as google:
            self.assertEqual(execucao.executar_plano(runtime.context.plano, self.aprovar(runtime)).status, "ok")
            self.assertEqual(google.call_count, 1)
        self.assertEqual(self.sql("SELECT count(*) FROM events")[0][0], 1)

    def test_google_rejeita_e_cancela_local(self):
        runtime = self.runtime("Crie um evento")
        self.evento(runtime)
        with patch.object(execucao, "criar_google_aprovado", return_value={"status": "error", "message": "Sem permissão"}):
            resultado = execucao.executar_plano(runtime.context.plano, self.aprovar(runtime))
        self.assertEqual(resultado.status, "erro")
        self.assertIn("cancelado", resultado.mensagem)
        self.assertEqual(self.sql("SELECT count(*) FROM events")[0][0], 0)

    def test_timeout_google_cancela_local_e_nao_repete(self):
        runtime = self.runtime("Crie um evento")
        self.evento(runtime)
        with patch.object(execucao, "criar_google_aprovado", side_effect=TimeoutError) as google:
            resultado = execucao.executar_plano(runtime.context.plano, self.aprovar(runtime))
            self.assertEqual(google.call_count, 1)
        self.assertEqual(resultado.status, "incerto")
        self.assertIn("Confira o calendário", resultado.mensagem)
        self.assertEqual(self.sql("SELECT count(*) FROM events")[0][0], 0)

    def test_lote_de_eventos_preserva_sucesso_e_para_apos_falha(self):
        runtime = self.runtime("Crie três eventos")
        for nome, dia in (("A", "2030-01-10"), ("B", "2030-01-11"), ("C", "2030-01-12")):
            self.evento(runtime, nome, dia)
        with patch.object(execucao, "criar_google_aprovado", side_effect=[{"status": "ok", "id": "g1"}, {"status": "error"}]) as google:
            resultado = execucao.executar_plano(runtime.context.plano, self.aprovar(runtime))
            self.assertEqual(google.call_count, 2)
        self.assertEqual(resultado.status, "parcial")
        self.assertEqual(self.sql("SELECT title FROM events"), [("A",)])
        self.assertIn("não foram tentados", resultado.mensagem)

    def test_somente_local_nao_chama_google(self):
        runtime = self.runtime("Salve apenas no banco local; o Google já confirmou")
        self.evento(runtime)
        with patch.object(execucao, "criar_google_aprovado") as google:
            self.assertEqual(execucao.executar_plano(runtime.context.plano, self.aprovar(runtime)).status, "ok")
            google.assert_not_called()

    def test_sql_segundo_insert_falha_e_cancela_primeiro(self):
        runtime = self.runtime()
        self.criar(runtime, "10")
        self.criar(runtime, "999")
        self.assertEqual(execucao.executar_plano(runtime.context.plano, self.aprovar(runtime)).status, "erro")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)

    def test_consultas_sao_fotografias_da_ordem(self):
        runtime = self.runtime()
        antes = financeiro.query_transactions.func(date_from_local="2026-10-29", date_to_local="2026-10-29", runtime=runtime)
        self.criar(runtime)
        depois = financeiro.query_transactions.func(date_from_local="2026-10-29", date_to_local="2026-10-29", runtime=runtime)
        self.assertEqual(antes["count"], 0)
        self.assertEqual(depois["count"], 1)
        self.assertIsNone(depois["transactions"][0]["id"])
        self.assertTrue(depois["transactions"][0]["projetado"])
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)
        self.assertEqual(runtime.context.plano.passos[0]["resultado"]["count"], 0)

    def test_atualiza_criacao_temporaria_e_total(self):
        runtime = self.runtime("Registre e altere compra em reais")
        ref = self.criar(runtime)["referencia_temporaria"]
        atualizado = financeiro.update_transaction.func(referencia_temporaria=ref, amount=Decimal("18"), runtime=runtime)
        self.assertEqual(atualizado["status"], "preparado")
        saldo = financeiro.daily_balance.func("2026-10-29", runtime=runtime)
        self.assertEqual(Decimal(saldo["total_expenses"]), Decimal("18"))
        resultado = execucao.executar_plano(runtime.context.plano, self.aprovar(runtime))
        self.assertEqual(resultado.status, "ok", resultado.mensagem)
        self.assertEqual(self.sql("SELECT amount FROM transactions"), [(Decimal("18"),)])

    def test_update_projeta_filtro_nova_data_e_limite(self):
        identificador = self.sql("INSERT INTO transactions(amount,type,description,occurred_at,source_text) VALUES (10,2,'antiga','2026-10-27 10:00-03','origem') RETURNING id")[0][0]
        runtime = self.runtime("Altere a compra para 30 reais em 29/10")
        financeiro.update_transaction.func(id=identificador, amount=Decimal("30"), description="camiseta", occurred_at="2026-10-29T10:00:00", runtime=runtime)
        consulta = financeiro.query_transactions.func(search_text="camiseta", date_from_local="2026-10-29", date_to_local="2026-10-29", limit=1, runtime=runtime)
        self.assertEqual(consulta["count"], 1)
        self.assertEqual(Decimal(consulta["transactions"][0]["amount"]), Decimal("30"))
        self.assertEqual(self.sql("SELECT amount FROM transactions")[0][0], Decimal("10"))

    def test_aprovacao_antiga_ou_plano_mutado_nao_executa(self):
        runtime = self.runtime()
        self.criar(runtime)
        aprovacao = self.aprovar(runtime)
        runtime.context.plano.operacoes[0].dados["registro"]["amount"] = "800"
        self.assertEqual(execucao.executar_plano(runtime.context.plano, aprovacao).status, "erro")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)

    def test_commit_incerto_preserva_confirmacao_google_e_orienta_local(self):
        runtime = self.runtime("Crie um evento")
        self.evento(runtime)
        conectar = self.conectar
        class Conexao:
            def __init__(self): self.real = conectar()
            def cursor(self): return self.real.cursor()
            def commit(self): raise OperationalError("Conexão perdida durante commit")
            def rollback(self): return self.real.rollback()
            def close(self): return self.real.close()
        with patch.object(execucao, "get_conn", Conexao), patch.object(execucao, "criar_google_aprovado", return_value={"status": "ok", "id": "g1"}) as google:
            resultado = execucao.executar_plano(runtime.context.plano, self.aprovar(runtime))
            self.assertEqual(google.call_count, 1)
        self.assertEqual(resultado.status, "incerto")
        self.assertEqual(resultado.itens[0]["google"], "confirmado")
        self.assertIn("somente a inserção local", resultado.mensagem)

    def test_juiz_sem_chave_ou_excecao_impede_escrita(self):
        runtime = self.runtime()
        self.criar(runtime)
        snapshot = runtime.context.plano.fechar()
        estado = {"messages": [HumanMessage(content="Registre 15 reais"), AIMessage(content="Registrei 15 reais.")], "plano_turno": snapshot}
        with patch.object(juiz, "TYPESAFE_API_KEY", None):
            veredito = juiz.no_juiz(estado)["veredito_juiz"]
        self.assertFalse(veredito["aprovado"])
        self.assertTrue(veredito["tecnico"])
        execucao.executar_plano(runtime.context.plano, veredito)
        with patch.object(juiz, "_julgar_jev", side_effect=TimeoutError):
            veredito = juiz.no_juiz(estado)["veredito_juiz"]
        self.assertFalse(veredito["aprovado"])
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)

    def test_agenda_consultas_preservam_passos_e_dias(self):
        runtime = self.runtime("Crie eventos")
        antes = agenda.query_events.func(date_local="2030-01-10", runtime=runtime)
        self.evento(runtime)
        depois = agenda.query_events.func(date_local="2030-01-10", runtime=runtime)
        outro_dia = agenda.query_events.func(date_local="2030-01-11", runtime=runtime)
        self.assertEqual(antes["count"], 0)
        self.assertEqual(depois["count"], 1)
        self.assertEqual(outro_dia["count"], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM events")[0][0], 0)

    def test_preparo_google_repetido_nao_duplica_intencao(self):
        runtime = self.runtime("Crie evento")
        self.evento(runtime)
        resultado = add_google_event.func(title="A", start_time="2030-01-10T18:00:00-03:00", end_time="2030-01-10T19:00:00-03:00", runtime=runtime)
        self.assertEqual(resultado["status"], "preparado")
        self.assertEqual(len(runtime.context.plano.operacoes), 2)

    def test_alteracao_concorrente_impede_sobrescrita(self):
        identificador = self.sql("INSERT INTO transactions(amount,type,occurred_at,source_text) VALUES (10,2,'2026-10-29 10:00-03','teste') RETURNING id")[0][0]
        runtime = self.runtime("Altere para 18 reais")
        financeiro.update_transaction.func(id=identificador, amount=Decimal("18"), runtime=runtime)
        self.sql("UPDATE transactions SET amount=12 WHERE id=%s", (identificador,))
        resultado = execucao.executar_plano(runtime.context.plano, self.aprovar(runtime))
        self.assertEqual(resultado.status, "erro")
        self.assertEqual(self.sql("SELECT amount FROM transactions")[0][0], Decimal("12"))

    def test_conversao_temporaria_grava_somente_brl_sem_metadados(self):
        runtime = self.runtime("Registre 10 dólares americanos em 28/10/2026")
        evidencia = {"moeda_original": "USD", "valor_original": "10", "taxa": "5", "data_cotacao": "2026-10-28", "valor_brl": "50.00"}
        with patch.object(financeiro, "converter_para_reais", return_value=(Decimal("50.00"), evidencia)) as converter:
            retorno = financeiro.add_transaction.func(amount=Decimal("10"), currency="USD", source_text=runtime.context.pergunta, occurred_at="2026-10-28T00:00:00", runtime=runtime)
        self.assertEqual(retorno["conversao"]["moeda_original"], "USD")
        self.assertEqual(converter.call_args.args[2].isoformat(), "2026-10-28")
        self.assertEqual(execucao.executar_plano(runtime.context.plano, self.aprovar(runtime)).status, "ok")
        self.assertEqual(self.sql("SELECT amount FROM transactions")[0][0], Decimal("50"))
        self.assertIn("dólares", self.sql("SELECT source_text FROM transactions")[0][0])

    def test_mcp_financeiro_preserva_escrita_com_mesma_fronteira(self):
        from app import mcp_server
        with patch.object(juiz, "_julgar_jev", side_effect=lambda p, r, e: juiz._publicar(r, "apoiada", r, aprovado=True)):
            retorno = mcp_server._concluir_escrita(financeiro.add_transaction, {"amount": 15, "source_text": "Registre 15 reais"})
        self.assertEqual(retorno["status"], "ok", retorno)
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 1)

    def test_mcp_recusado_nao_faz_insert(self):
        from app import mcp_server
        with patch.object(juiz, "_julgar_jev", side_effect=lambda p, r, e: juiz._publicar(juiz.FRASE_SEGURA, "sem_apoio", r)):
            retorno = mcp_server._concluir_escrita(financeiro.add_transaction, {"amount": 15, "source_text": "Registre 15 reais"})
        self.assertEqual(retorno["status"], "error")
        self.assertEqual(self.sql("SELECT count(*) FROM transactions")[0][0], 0)
