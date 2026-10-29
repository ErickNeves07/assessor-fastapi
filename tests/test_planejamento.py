import unittest
from unittest.mock import patch

from app.execucao import executar_plano
from app.planejamento import PlanoTurno


class TestAutorizacao(unittest.TestCase):
    def test_valores_ambiguos_na_autorizacao_nao_disparam_sql(self):
        for valor in (None, False, "false", "true", 1):
            with self.subTest(valor=valor):
                plano = PlanoTurno()
                plano.adicionar("add_transaction", {"registro": {"amount": "15.00"}})
                snapshot = plano.fechar()
                veredito = {"aprovado": valor, "turno_id": plano.turno_id, "hash_plano": snapshot["hash"]}
                with patch("app.execucao.get_conn") as conexao:
                    self.assertEqual(executar_plano(plano, veredito).status, "recusado")
                    conexao.assert_not_called()
