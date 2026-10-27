import unittest
from unittest.mock import patch

from app import observabilidade
from app.routes.monitor import montar_painel


class MonitorTests(unittest.TestCase):
    def test_previous_block_does_not_block_next_turn_status(self):
        history = ["guardrail_entrada:prompt_injection", "guardrail_entrada:aprovado"]
        self.assertEqual(observabilidade.status_do_estado({
            "agentes_chamados": history, "bloqueio_entrada": False,
        }), "ok")
        self.assertEqual(observabilidade.status_do_estado({
            "agentes_chamados": history, "bloqueio_entrada": True,
        }), "bloqueado")

    def test_block_is_counted_without_increasing_error_rate(self):
        blocked = {"hora": "10:00:00", "session_id": "teste", "latencia_ms": 1,
                   "rota": "fim", "status": "bloqueado", "tokens_total": 0, "custo_usd": 0.0}
        with patch("app.routes.monitor.listar_turnos", return_value=[blocked]), \
                patch("app.routes.monitor.listar_chamadas", return_value=[]):
            panel = montar_painel()
        self.assertEqual(panel["resumo"]["bloqueios"], 1)
        self.assertEqual(panel["resumo"]["taxa_erro"], 0)
        self.assertTrue(panel["resumo"]["slo_ok"])


if __name__ == "__main__":
    unittest.main()
