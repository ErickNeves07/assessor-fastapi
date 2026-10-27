import sys
from pathlib import Path
from typing import Annotated, Any, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from app.tools.financeiro import (
    add_transaction as _add_transaction,
    daily_balance as _daily_balance,
    query_transactions as _query_transactions,
    total_balance as _total_balance,
    update_transaction as _update_transaction,
)

mcp = MCPServer(
    name="assessor-financeiro",
    version="1.0.0",
    instructions="Ferramentas financeiras do Assessor.AI...",
)

LEITURA = ToolAnnotations(readOnlyHint=True)
ESCRITA = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

@mcp.tool(
    name="total_balance",
    title="Saldo total",
    description="Saldo de todo o histórico: receitas menos despesas.",
    annotations=LEITURA,
)
def total_balance() -> dict[str, Any]:
    return _total_balance.invoke({})


@mcp.tool(
    name="add_transaction",
    title="Registrar transação",
    description="Registra uma transação na tabela transactions.",
    annotations=ESCRITA,
)
def add_transaction(
    amount: Annotated[float, Field(description="Valor (sempre positivo).")],
    source_text: Annotated[str, Field(description="Texto original.")],
    type_name: Annotated[Optional[str], Field(description="INCOME | EXPENSES | TRANSFER")] = None,
    category_name: Annotated[Optional[str], Field(description="Categoria em pt-BR.")] = None,
    description: Annotated[Optional[str], Field(description="Descrição da transação.")] = None,
    payment_method: Annotated[Optional[str], Field(description="Método de pagamento.")] = None,
    occurred_at: Annotated[Optional[str], Field(description="Data e hora da ocorrência.")] = None,
    type_id: Annotated[Optional[int], Field(description="ID do tipo.")] = None,
    category_id: Annotated[Optional[int], Field(description="ID da categoria.")] = None
) -> dict[str, Any]:
    return _add_transaction.invoke({
        "amount": amount,
        "source_text": source_text,
        "type_name": type_name,
        "category_name": category_name,
        "description": description,
        "payment_method": payment_method,
        "occurred_at": occurred_at,
        "type_id": type_id,
        "category_id": category_id
    })

@mcp.tool(
    name="update_transaction",
    title="Atualizar transação",
    description="Atualiza uma transação na tabela transactions.",
    annotations=ESCRITA,
)
def update_transaction(
    id: Annotated[Optional[int], Field(description="ID da transação.")] = None,
    match_text: Annotated[Optional[str], Field(description="Texto de correspondência.")] = None,
    date_local: Annotated[Optional[str], Field(description="Data local.")] = None,
    amount: Annotated[Optional[float], Field(description="Valor (sempre positivo).")] = None,
    type_id: Annotated[Optional[int], Field(description="ID do tipo.")] = None,
    type_name: Annotated[Optional[str], Field(description="Nome do tipo.")] = None,
    category_id: Annotated[Optional[int], Field(description="ID da categoria.")] = None,
    category_name: Annotated[Optional[str], Field(description="Nome da categoria.")] = None,
    description: Annotated[Optional[str], Field(description="Descrição da transação.")] = None,
    payment_method: Annotated[Optional[str], Field(description="Método de pagamento.")] = None,
    occurred_at: Annotated[Optional[str], Field(description="Data e hora da ocorrência.")] = None,
) -> dict[str, Any]:
    return _update_transaction.invoke({
        "id": id,
        "match_text": match_text,
        "date_local": date_local,
        "amount": amount,
        "type_id": type_id,
        "type_name": type_name,
        "category_id": category_id,
        "category_name": category_name,
        "description": description,
        "payment_method": payment_method,
        "occurred_at": occurred_at
    })

@mcp.tool(
    name="query_transactions",
    title="Consultar transações",
    description="Consulta transações na tabela transactions.",
    annotations=LEITURA,
)
def query_transactions(
    search_text: Annotated[Optional[str], Field(description="Texto de busca.")] = None,
    date_from_local: Annotated[Optional[str], Field(description="Data inicial (formato local).")] = None,
    date_to_local: Annotated[Optional[str], Field(description="Data final (formato local).")] = None,
    limit: Annotated[int, Field(description="Limite de resultados.")] = 50,
    type_name: Annotated[Optional[str], Field(description="INCOME | EXPENSES | TRANSFER")] = None,
) -> dict[str, Any]:
    return _query_transactions.invoke({
        "type_name": type_name,
        "search_text": search_text,
        "date_from_local": date_from_local,
        "date_to_local": date_to_local,
        "limit": limit,
    })

@mcp.tool(
    name="daily_balance",
    title="Saldo diário",
    description="Saldo diário: receitas menos despesas.",
    annotations=LEITURA,
)
def daily_balance() -> dict[str, Any]:
    return _daily_balance.invoke({})

if __name__ == "__main__":
    from app.config import DATABASE_URL

    if not DATABASE_URL:
        print("[assessor-financeiro] DATABASE_URL ausente no .env", file=sys.stderr)

    mcp.run(transport="stdio")