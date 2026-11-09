import re
import tempfile
import threading
import unicodedata
from zipfile import BadZipFile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import httpx
from currency_converter import CurrencyConverter, ECB_URL, RateNotFoundError


_lock = threading.Lock()
_conversor = None
_dia_carregado = None


class CambioIndisponivel(ValueError):
    def __init__(self, mensagem, tecnico=False):
        super().__init__(mensagem)
        self.tecnico = tecnico


def valor_monetario(valor):
    try:
        numero = Decimal(str(valor))
        if not numero.is_finite() or numero < 0 or numero > Decimal("999999999999.99"):
            raise ValueError
        return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (ValueError, InvalidOperation):
        raise ValueError("Informe um valor monetário válido, não negativo e dentro do limite do banco.") from None


def moedas_explicitas(texto):
    texto = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    padroes = {
        "BRL": r"\bbrl\b|r\$|\breais\b|\breal\b",
        "USD": r"\busd\b|us\$|\bdolares? americanos?\b",
        "EUR": r"\beur\b|€|\beuros?\b",
        "CAD": r"\bcad\b|\bdolares? canadenses?\b",
        "AUD": r"\baud\b|\bdolares? australianos?\b",
        "GBP": r"\bgbp\b|£|\blibras?\b",
        "JPY": r"\bjpy\b|\bienes?\b",
        "ARS": r"\bars\b|\bpesos? argentinos?\b",
        "MXN": r"\bmxn\b|\bpesos? mexicanos?\b",
    }
    moedas = {moeda for moeda, padrao in padroes.items() if re.search(padrao, texto)}
    if re.search(r"\bdolares?\b", texto) and not moedas.intersection({"CAD", "AUD", "USD"}):
        moedas.add("USD")
    if not moedas and ("$" in texto or re.search(r"\bpesos?\b", texto)):
        raise CambioIndisponivel("A moeda está ambígua. Informe a moeda por extenso ou o valor correspondente em reais.")
    return moedas


def validar_moeda(moeda, texto):
    moeda = moeda.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", moeda):
        raise CambioIndisponivel("Informe a moeda com código ISO, como BRL, USD ou EUR.")
    explicitas = moedas_explicitas(texto)
    if len(explicitas) == 1 and moeda not in explicitas:
        raise CambioIndisponivel("A moeda da operação difere da informada no pedido. Confirme a moeda ou informe o valor em reais.")
    return moeda


def _obter_conversor(dia_pedido, hoje):
    global _conversor, _dia_carregado
    with _lock:
        if _conversor is not None and (_dia_carregado == hoje or dia_pedido <= _dia_carregado):
            return _conversor
        pasta = Path(tempfile.gettempdir()) / "assessor-cambio"
        pasta.mkdir(parents=True, exist_ok=True)
        arquivos = sorted(pasta.glob("ecb_????-??-??.zip"), reverse=True)
        arquivo = pasta / f"ecb_{hoje.isoformat()}.zip"
        try:
            if not arquivo.is_file():
                resposta = httpx.get(ECB_URL, timeout=5, follow_redirects=True)
                resposta.raise_for_status()
                with tempfile.NamedTemporaryFile(dir=pasta, suffix=".zip", delete=False) as temporario:
                    temporario.write(resposta.content)
                    caminho_temporario = Path(temporario.name)
                caminho_temporario.replace(arquivo)
            _conversor = CurrencyConverter(str(arquivo), decimal=True)
            _dia_carregado = hoje
        except (httpx.HTTPError, OSError, ValueError, BadZipFile):
            validos = [a for a in arquivos if date.fromisoformat(a.stem[4:]) >= dia_pedido]
            if not validos:
                raise CambioIndisponivel("Não consegui obter a cotação. Informe o valor correspondente em reais para registrar.", tecnico=True) from None
            _conversor = CurrencyConverter(str(validos[0]), decimal=True)
            _dia_carregado = date.fromisoformat(validos[0].stem[4:])
        return _conversor


def converter_para_reais(valor, moeda, dia, hoje):
    original = Decimal(str(valor))
    if not original.is_finite() or original < 0:
        raise ValueError("Informe um valor monetário não negativo e finito.")
    if moeda == "BRL":
        return valor_monetario(original), None
    if dia > hoje:
        raise CambioIndisponivel("Ainda não há cotação para a data futura informada. Informe o valor em reais.")
    conversor = _obter_conversor(dia, hoje)
    if moeda not in conversor.currencies or "BRL" not in conversor.currencies:
        raise CambioIndisponivel("A moeda não é suportada pela fonte de câmbio. Informe o valor correspondente em reais.")
    inicio = max(conversor.bounds[moeda].first_date, conversor.bounds["BRL"].first_date)
    efetivo = min(dia, conversor.bounds[moeda].last_date, conversor.bounds["BRL"].last_date)
    while efetivo >= inicio:
        try:
            taxa = conversor.convert(Decimal("1"), moeda, "BRL", date=efetivo)
            convertido = valor_monetario(original * taxa)
            return convertido, {
                "moeda_original": moeda, "valor_original": str(original), "moeda_destino": "BRL",
                "taxa": str(taxa), "data_solicitada": dia.isoformat(), "data_cotacao": efetivo.isoformat(),
                "valor_brl": str(convertido), "fonte": "Banco Central Europeu",
            }
        except RateNotFoundError:
            efetivo -= timedelta(days=1)
    raise CambioIndisponivel("Não há cotação disponível nessa data ou antes dela. Informe o valor correspondente em reais.")
