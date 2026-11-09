import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict


FUSO = ZoneInfo("America/Sao_Paulo")


def serializar(valor):
    if isinstance(valor, BaseModel):
        return valor.model_dump(mode="json")
    if isinstance(valor, (datetime, Decimal)):
        return valor.isoformat() if isinstance(valor, datetime) else str(valor)
    raise TypeError(f"Tipo não serializável: {type(valor).__name__}")


def dados_json(valor):
    return json.loads(json.dumps(valor, default=serializar, ensure_ascii=False))


def horario(valor: str | datetime | None, referencia: datetime) -> datetime:
    data = datetime.fromisoformat(valor) if isinstance(valor, str) else valor
    data = data or referencia
    return data.replace(tzinfo=FUSO) if data.tzinfo is None else data.astimezone(FUSO)


class Operacao(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    referencia: str
    tipo: Literal["add_transaction", "update_transaction", "add_event", "add_google_event"]
    dados: dict
    antes: dict | None = None
    conversao: dict | None = None


@dataclass
class PlanoTurno:
    turno_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    agora: datetime = field(default_factory=lambda: datetime.now(FUSO))
    operacoes: list[Operacao] = field(default_factory=list)
    passos: list[dict] = field(default_factory=list)
    erros: list[dict] = field(default_factory=list)
    congelado: bool = False
    consumido: bool = False
    hash_aprovacao: str = ""

    def adicionar(self, tipo, dados, antes=None, conversao=None):
        if self.congelado:
            raise ValueError("O plano já foi fechado para avaliação.")
        operacao = Operacao(
            referencia=f"op-{len(self.operacoes) + 1}", tipo=tipo,
            dados=dados_json(dados), antes=dados_json(antes) if antes is not None else None,
            conversao=dados_json(conversao) if conversao is not None else None,
        )
        self.operacoes.append(operacao)
        self.passos.append({"sequencia": len(self.passos) + 1, "operacao": operacao.referencia})
        return operacao

    def leitura(self, nome, argumentos, resultado):
        self.passos.append({
            "sequencia": len(self.passos) + 1, "consulta": nome,
            "argumentos": dados_json(argumentos), "resultado": dados_json(resultado),
            "apos_operacoes": [o.referencia for o in self.operacoes],
        })
        return resultado

    def falha(self, mensagem, tecnico=False):
        self.erros.append({"mensagem": str(mensagem), "tecnico": tecnico})
        return {"status": "error", "message": str(mensagem)}

    def conteudo(self):
        return dados_json({
            "turno_id": self.turno_id, "operacoes": self.operacoes,
            "passos": self.passos, "erros": self.erros,
        })

    def hash_atual(self):
        return hashlib.sha256(json.dumps(self.conteudo(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def fechar(self):
        self.congelado = True
        self.hash_aprovacao = self.hash_atual()
        return {**self.conteudo(), "hash": self.hash_aprovacao}

    def projecoes(self, dominio):
        registros = {}
        for op in self.operacoes:
            if dominio == "financeiro" and op.tipo in ("add_transaction", "update_transaction"):
                chave = op.dados.get("referencia_alvo") or op.dados.get("id") or op.referencia
                registros[chave] = {**deepcopy(op.dados["registro"]), "referencia_temporaria": chave if isinstance(chave, str) else None}
            elif dominio == "agenda" and op.tipo == "add_event":
                registros[op.referencia] = {**deepcopy(op.dados), "referencia_temporaria": op.referencia}
        return list(registros.values())


@dataclass
class ContextoTurno:
    user_id: str
    session_id: str
    pergunta: str
    plano: PlanoTurno = field(default_factory=PlanoTurno)


def obter_plano(runtime):
    contexto = getattr(runtime, "context", None)
    if not isinstance(contexto, ContextoTurno):
        raise ValueError("A escrita exige um turno com avaliação do juiz; nenhuma operação foi executada.")
    return contexto.plano


def preparado(op):
    return {
        "status": "preparado", "referencia_temporaria": op.referencia,
        "operacao": op.tipo, "dados": op.dados,
        **({"conversao": op.conversao} if op.conversao else {}),
        "persistido": False,
    }


_controle_sessoes = threading.Lock()
_sessoes = {}


@contextmanager
def sessao_exclusiva(user_id, session_id):
    chave = (user_id, session_id)
    with _controle_sessoes:
        item = _sessoes.setdefault(chave, [threading.RLock(), 0])
        item[1] += 1
    try:
        with item[0]:
            yield
    finally:
        with _controle_sessoes:
            item[1] -= 1
            if item[1] == 0:
                del _sessoes[chave]
