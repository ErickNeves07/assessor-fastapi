import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from currency_converter import RateNotFoundError

from app import cambio


class TestCambio(unittest.TestCase):
    def conversor(self):
        c = Mock()
        c.currencies = {"USD", "EUR", "BRL"}
        c.bounds = {m: SimpleNamespace(first_date=date(2026, 1, 1), last_date=date(2026, 10, 30)) for m in c.currencies}
        return c

    def test_brl_nao_busca_cotacao(self):
        with patch.object(cambio, "_obter_conversor") as obter:
            valor, evidencia = cambio.converter_para_reais(Decimal("15"), "BRL", date(2026, 10, 29), date(2026, 10, 29))
            obter.assert_not_called()
        self.assertEqual(valor, Decimal("15.00"))
        self.assertIsNone(evidencia)

    def test_data_sem_taxa_usa_anterior_e_congela_valor(self):
        c = self.conversor()
        c.convert.side_effect = [RateNotFoundError(), Decimal("5.12345")]
        with patch.object(cambio, "_obter_conversor", return_value=c):
            valor, evidencia = cambio.converter_para_reais(Decimal("10"), "USD", date(2026, 10, 29), date(2026, 10, 29))
        self.assertEqual(valor, Decimal("51.23"))
        self.assertEqual(evidencia["data_cotacao"], "2026-10-28")
        self.assertEqual(c.convert.call_args.kwargs["date"], date(2026, 10, 28))

    def test_arredondamento_e_euro(self):
        c = self.conversor()
        c.convert.return_value = Decimal("6.005")
        with patch.object(cambio, "_obter_conversor", return_value=c):
            valor, _ = cambio.converter_para_reais(Decimal("1"), "EUR", date(2026, 10, 29), date(2026, 10, 29))
        self.assertEqual(valor, Decimal("6.01"))

    def test_moeda_nao_suportada_pede_reais(self):
        with patch.object(cambio, "_obter_conversor", return_value=self.conversor()):
            with self.assertRaisesRegex(cambio.CambioIndisponivel, "reais"):
                cambio.converter_para_reais(10, "ARS", date(2026, 10, 29), date(2026, 10, 29))

    def test_moeda_original_nao_pode_ser_trocada_por_brl(self):
        with self.assertRaises(cambio.CambioIndisponivel):
            cambio.validar_moeda("BRL", "Registre 10 dólares americanos")
        self.assertEqual(cambio.validar_moeda("USD", "Registre 10 dólares americanos"), "USD")

    def test_moeda_ambigua_e_valores_invalidos(self):
        with self.assertRaises(cambio.CambioIndisponivel):
            cambio.validar_moeda("USD", "Registre $10")
        for valor in ("NaN", "Infinity", "-1", "1000000000000"):
            with self.assertRaises(ValueError):
                cambio.valor_monetario(valor)

    def test_futuro_nao_inventa_cotacao(self):
        with self.assertRaisesRegex(cambio.CambioIndisponivel, "reais"):
            cambio.converter_para_reais(10, "USD", date(2030, 1, 1), date(2026, 10, 29))
