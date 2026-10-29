from datetime import date
from decimal import Decimal
from typing import Optional

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.cambio import converter_para_reais, validar_moeda
from app.planejamento import ContextoTurno, PlanoTurno, dados_json, horario, obter_plano, preparado
from app.tools.db import get_conn


COLUNAS = "id, amount, type, category_id, description, payment_method, occurred_at, source_text"


class AddTransactionArgs(BaseModel):
    amount: Decimal = Field(..., gt=0, description="Valor na moeda original informada pelo usuário.")
    currency: str = Field(default="BRL", description="Moeda original ISO: BRL, USD, EUR etc.; nunca converta mentalmente.")
    source_text: str = Field(..., description="Pedido original do usuário.")
    occurred_at: Optional[str] = Field(default=None, description="Data/hora ISO; sem fuso, usa São Paulo.")
    type_id: Optional[int] = None
    type_name: Optional[str] = Field(default=None, description="INCOME | EXPENSES | TRANSFER")
    category_name: Optional[str] = Field(default=None, description="comida, besteira, estudo, férias, transporte, moradia, saúde, lazer, contas, investimento, presente, outros")
    category_id: Optional[int] = None
    description: Optional[str] = None
    payment_method: Optional[str] = None


class QueryTransactionsArgs(BaseModel):
    search_text: Optional[str] = None
    type_name: Optional[str] = None
    date_from_local: Optional[str] = None
    date_to_local: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=500)


class UpdateTransactionArgs(BaseModel):
    id: Optional[int] = None
    referencia_temporaria: Optional[str] = Field(default=None, description="Referência de uma criação preparada neste turno, como op-1.")
    match_text: Optional[str] = None
    date_local: Optional[str] = None
    amount: Optional[Decimal] = Field(default=None, ge=0)
    currency: str = Field(default="BRL", description="Moeda do novo valor; BRL, USD, EUR etc.")
    type_id: Optional[int] = None
    type_name: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    description: Optional[str] = None
    payment_method: Optional[str] = None
    occurred_at: Optional[str] = None


def _contexto_leitura(runtime):
    contexto = getattr(runtime, "context", None)
    return contexto.plano if isinstance(contexto, ContextoTurno) else PlanoTurno()


def _ler(sql, parametros):
    conn = get_conn()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(sql, parametros)
            nomes = [d[0] for d in cur.description]
            return [dict(zip(nomes, r)) for r in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()


def _resolver(tabela, campo, nome=None, identificador=None, padrao=None):
    if nome is None and identificador is None:
        return padrao
    coluna, valor = (campo, nome) if nome is not None else ("id", identificador)
    comparacao = f"LOWER({coluna})=LOWER(%s)" if nome is not None else "id=%s"
    linhas = _ler(f"SELECT id FROM {tabela} WHERE {comparacao}", (valor,))
    if not linhas:
        raise ValueError(f"{tabela}: valor não encontrado no cadastro.")
    return linhas[0]["id"]


def _tipo(type_id=None, type_name=None, padrao=None):
    nome = type_name.strip().upper() if type_name else None
    nome = "EXPENSES" if nome == "EXPENSE" else nome
    return _resolver("transaction_types", "type", nome, type_id, padrao)


def _visao(plano):
    import json
    projecoes = plano.projecoes("financeiro")
    sql = """WITH p AS (
        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(
            id bigint, amount numeric, type integer, category_id integer,
            description text, payment_method varchar, occurred_at timestamptz,
            source_text text, referencia_temporaria text)
    ), visao AS (
        SELECT t.id,t.amount,t.type,t.category_id,t.description,t.payment_method,
               t.occurred_at,t.source_text,NULL::text AS referencia_temporaria,false AS projetado
        FROM transactions t WHERE NOT EXISTS (SELECT 1 FROM p WHERE p.id=t.id)
        UNION ALL
        SELECT p.*,true AS projetado FROM p
    ) """
    return sql, [json.dumps(projecoes, ensure_ascii=False)]


def _falha(plano, exc):
    return plano.falha(str(exc), tecnico=getattr(exc, "tecnico", not isinstance(exc, ValueError)))


@tool("add_transaction", args_schema=AddTransactionArgs)
def add_transaction(amount: Decimal, source_text: str, currency: str = "BRL", occurred_at: Optional[str] = None,
                    type_id: Optional[int] = None, type_name: Optional[str] = None,
                    category_name: Optional[str] = None, category_id: Optional[int] = None,
                    description: Optional[str] = None, payment_method: Optional[str] = None,
                    runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Prepara uma criação financeira. Não grava nem retorna ID do banco; aguarda o juiz."""
    plano = obter_plano(runtime)
    try:
        instante = horario(occurred_at, plano.agora)
        moeda = validar_moeda(currency, runtime.context.pergunta)
        valor, conversao = converter_para_reais(amount, moeda, instante.date(), plano.agora.date())
        registro = dict(id=None, amount=valor, type=_tipo(type_id, type_name, 2),
                        category_id=_resolver("categories", "name", category_name, category_id),
                        description=description, payment_method=payment_method, occurred_at=instante,
                        source_text=runtime.context.pergunta)
        return preparado(plano.adicionar("add_transaction", {"registro": registro}, conversao=conversao))
    except Exception as exc:
        return _falha(plano, exc)


@tool("query_transactions", args_schema=QueryTransactionsArgs)
def query_transactions(search_text: Optional[str] = None, type_name: Optional[str] = None,
                       date_from_local: Optional[str] = None, date_to_local: Optional[str] = None,
                       limit: int = 50, runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Consulta transações e as operações preparadas ANTES desta consulta, respeitando filtros e limite."""
    plano = _contexto_leitura(runtime)
    try:
        prefixo, parametros = _visao(plano)
        condicoes = []
        if search_text:
            condicoes.append("(t.source_text ILIKE %s OR t.description ILIKE %s)")
            parametros.extend([f"%{search_text}%"] * 2)
        if type_name:
            condicoes.append("t.type=%s")
            parametros.append(_tipo(type_name=type_name))
        for valor, operador in [(date_from_local, ">="), (date_to_local, "<=")]:
            if valor:
                date.fromisoformat(valor)
                condicoes.append(f"(t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date {operador} %s::date")
                parametros.append(valor)
        where = " WHERE " + " AND ".join(condicoes) if condicoes else ""
        ordem = "ASC" if date_from_local or date_to_local else "DESC"
        parametros.append(limit)
        nulos = "LAST" if ordem == "ASC" else "FIRST"
        linhas = _ler(prefixo + f"""SELECT t.id,t.amount,tt.type AS type_name,t.description,t.payment_method,
            t.occurred_at AT TIME ZONE 'America/Sao_Paulo' AS occurred_at_local,t.source_text,
            t.referencia_temporaria,t.projetado,'BRL' AS currency
            FROM visao t JOIN transaction_types tt ON tt.id=t.type
            {where} ORDER BY t.occurred_at {ordem},t.id {ordem} NULLS {nulos},t.referencia_temporaria LIMIT %s""", parametros)
        resultado = dados_json({"status": "ok", "count": len(linhas), "transactions": linhas,
                               "projecao": bool(plano.projecoes("financeiro"))})
        return plano.leitura("query_transactions", dict(search_text=search_text, type_name=type_name,
                              date_from_local=date_from_local, date_to_local=date_to_local, limit=limit), resultado)
    except Exception as exc:
        return _falha(plano, exc)


def _saldo(plano, dia=None):
    prefixo, parametros = _visao(plano)
    filtro = ""
    if dia:
        date.fromisoformat(dia)
        filtro = " AND (t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date=%s::date"
        parametros.append(dia)
    linhas = _ler(prefixo + """SELECT
        COALESCE(SUM(CASE WHEN tt.type='INCOME' THEN amount ELSE 0 END),0) AS total_income,
        COALESCE(SUM(CASE WHEN tt.type='EXPENSES' THEN amount ELSE 0 END),0) AS total_expenses,
        COALESCE(SUM(CASE WHEN tt.type='INCOME' THEN amount ELSE -amount END),0) AS balance
        FROM visao t JOIN transaction_types tt ON tt.id=t.type
        WHERE tt.type IN ('INCOME','EXPENSES')""" + filtro, parametros)
    return dados_json({"status": "ok", **linhas[0], "currency": "BRL", "projecao": bool(plano.projecoes("financeiro")),
                       **({"date": dia} if dia else {})})


@tool("total_balance")
def total_balance(runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Retorna saldo total em BRL, incluindo somente operações preparadas anteriormente neste turno."""
    plano = _contexto_leitura(runtime)
    try:
        return plano.leitura("total_balance", {}, _saldo(plano))
    except Exception as exc:
        return _falha(plano, exc)


@tool("daily_balance")
def daily_balance(date_local: str, runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Retorna saldo do dia de São Paulo em BRL, incluindo operações preparadas anteriormente."""
    plano = _contexto_leitura(runtime)
    try:
        return plano.leitura("daily_balance", {"date_local": date_local}, _saldo(plano, date_local))
    except Exception as exc:
        return _falha(plano, exc)


@tool("update_transaction", args_schema=UpdateTransactionArgs)
def update_transaction(id: Optional[int] = None, referencia_temporaria: Optional[str] = None,
                       match_text: Optional[str] = None, date_local: Optional[str] = None,
                       amount: Optional[Decimal] = None, currency: str = "BRL", type_id: Optional[int] = None,
                       type_name: Optional[str] = None, category_id: Optional[int] = None,
                       category_name: Optional[str] = None, description: Optional[str] = None,
                       payment_method: Optional[str] = None, occurred_at: Optional[str] = None,
                       runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Prepara alteração de transação por ID, referência temporária ou texto/data; não executa UPDATE."""
    plano = obter_plano(runtime)
    try:
        if all(v is None for v in (amount, type_id, type_name, category_id, category_name, description, payment_method, occurred_at)):
            raise ValueError("Informe pelo menos um campo para alterar.")
        prefixo, params = _visao(plano)
        if referencia_temporaria:
            where = "referencia_temporaria=%s"; params.append(referencia_temporaria)
        elif id is not None:
            where = "id=%s"; params.append(id)
        else:
            if not match_text or not date_local:
                raise ValueError("Informe ID, referência temporária ou descrição e data local.")
            date.fromisoformat(date_local)
            where = "(source_text ILIKE %s OR description ILIKE %s) AND (occurred_at AT TIME ZONE 'America/Sao_Paulo')::date=%s::date"
            params.extend([f"%{match_text}%", f"%{match_text}%", date_local])
        linhas = _ler(prefixo + "SELECT " + COLUNAS + ",referencia_temporaria FROM visao WHERE " + where + " ORDER BY occurred_at DESC LIMIT 1", params)
        if not linhas:
            raise ValueError("Nenhuma transação encontrada para os filtros fornecidos.")
        antes = linhas[0]
        registro = {k: antes[k] for k in COLUNAS.split(", ")}
        conversao = None
        if occurred_at is not None:
            registro["occurred_at"] = horario(occurred_at, plano.agora)
        if amount is not None:
            moeda = validar_moeda(currency, runtime.context.pergunta)
            instante = horario(registro["occurred_at"], plano.agora)
            registro["amount"], conversao = converter_para_reais(amount, moeda, instante.date(), plano.agora.date())
        if type_id is not None or type_name is not None:
            registro["type"] = _tipo(type_id, type_name)
        if category_id is not None or category_name is not None:
            registro["category_id"] = _resolver("categories", "name", category_name, category_id)
        for campo, valor in (("description", description), ("payment_method", payment_method)):
            if valor is not None:
                registro[campo] = valor
        dados = {"id": antes["id"], "referencia_alvo": antes["referencia_temporaria"], "registro": registro}
        return preparado(plano.adicionar("update_transaction", dados, antes=antes, conversao=conversao))
    except Exception as exc:
        return _falha(plano, exc)


TOOLS = [add_transaction, query_transactions, total_balance, daily_balance, update_transaction]
