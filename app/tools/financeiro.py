from typing import Optional, List
from langchain.tools import tool
from pydantic import BaseModel, Field
from app.tools.db import get_conn

# Essa classe garante que o objeto de Python passe todos esses campos
class AddTransactionArgs(BaseModel):
    amount: float = Field(..., description="Valor da transação (use positivo).")
    source_text: str = Field(..., description="Texto original do usuário.")
    occurred_at: Optional[str] = Field(
        default=None,
        description="Timestamp ISO 8601; se ausente, usa NOW() no banco."
    )
    type_id: Optional[int] = Field(default=None, description="ID em transaction_types (1=INCOME, 2=EXPENSES, 3=TRANSFER).")
    type_name: Optional[str] = Field(default=None, description="Nome do tipo: INCOME | EXPENSES | TRANSFER.")
    category_name: Optional[str] = Field(default=None, description="""
    "Nome da categoria entre as disponíveis: comida, contas, besteiras ,transporte, salário, lazer, saúde, educação, moradia, investimento, presente, outros.
                                         """)
    category_id: Optional[int] = Field(default=None, description="FK de categories (opcional).")
    description: Optional[str] = Field(default=None, description="Descrição (opcional).")
    payment_method: Optional[str] = Field(default=None, description="Forma de pagamento (opcional).")

# Essa classe garante que o objeto de Python passe todos esses campos
class QueryTransactionsArgs(BaseModel):
    search_text: Optional[str] = Field(default=None, description="Texto para busca em source_text ou description.")
    type_name: Optional[str] = Field(default=None, description="INCOME | EXPENSES | TRANSFER.")
    date_from_local: Optional[str] = Field(default=None, description="Data inicial local YYYY-MM-DD (America/Sao_Paulo).")
    date_to_local: Optional[str] = Field(default=None, description="Data final local YYYY-MM-DD (America/Sao_Paulo).")
    limit: int = Field(default=50, description="Máximo de registros retornados.")

#Garante que o campo type da tabela transactions receba um id válido (1=INCOME, 2=EXPENSES, 3=TRANSFER
def _resolve_type_id(cur, type_id: Optional[int], type_name: Optional[str]) -> Optional[int]:
    if type_name:
        t = type_name.strip().upper()
        if t == "EXPENSE":
            t = "EXPENSES"
        cur.execute("SELECT id FROM transaction_types WHERE UPPER(type)=%s LIMIT 1;", (t,))
        row = cur.fetchone()
        return row[0] if row else None
    if type_id:
        return int(type_id)
    return 2

def _get_category_id(cur, category_name: Optional[str]) -> Optional[int]:
    if not category_name:
        return None
    cur.execute(
        "SELECT id FROM categories WHERE LOWER(name) = LOWER(%s) LIMIT 1;",
        (category_name,)
    )
    row = cur.fetchone()
    return row[0] if row else None

def _local_date_filter_sql(field: str = "occurred_at") -> str:
    """
    Retorna um trecho SQL para filtragem por dia local em America/Sao_Paulo.
    Ex.: (occurred_at AT TIME ZONE 'America/Sao_Paulo')::date = %s::date
    """
    return f"(({field} AT TIME ZONE 'America/Sao_Paulo')::date = %s::date)"

# Tool: add_transaction
@tool("add_transaction", args_schema=AddTransactionArgs)
def add_transaction(
    amount: float,
    source_text: str,
    occurred_at: Optional[str] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_name: Optional[str] = None,
    category_id: Optional[int] = None,
    description: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> dict:
    """Insere uma transação financeira no banco de dados Postgres.""" # docstring obrigatório da @tools do langchain (estranho, mas legal né?)
    conn = get_conn()
    cur = conn.cursor()
    try:
        resolved_type_id = _resolve_type_id(cur, type_id, type_name)
        if not resolved_type_id:
            return {"status": "error", "message": "Tipo inválido (use type_id ou type_name: INCOME/EXPENSES/TRANSFER)."}

        if not category_id:
            category_id = _get_category_id(cur, category_name);

        if occurred_at:
            cur.execute(
                """
                INSERT INTO transactions
                    (amount, type, category_id, description, payment_method, occurred_at, source_text)
                VALUES
                    (%s, %s, %s, %s, %s, %s::timestamptz, %s)
                RETURNING id, occurred_at;
                """,
                (amount, resolved_type_id, category_id, description, payment_method, occurred_at, source_text),
            )
        else:
            cur.execute(
                """
                INSERT INTO transactions
                    (amount, type, category_id, description, payment_method, occurred_at, source_text)
                VALUES
                    (%s, %s, %s, %s, %s, NOW(), %s)
                RETURNING id, occurred_at;
                """,
                (amount, resolved_type_id, category_id, description, payment_method, source_text),
            )

        new_id, occurred = cur.fetchone()
        conn.commit()
        return {"status": "ok", "id": new_id, "occurred_at": str(occurred)}

    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

@tool("query_transactions", args_schema=QueryTransactionsArgs)
def query_transactions(
    search_text: Optional[str] = None,
    type_name: Optional[str] = None,
    date_from_local: Optional[str] = None,
    date_to_local: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """
    Consulta transações com filtros por texto (source_text/description), tipo e datas locais (America/Sao_Paulo).
    Os dados devem vir na seguinte ordem:
    - Intervalo (date_from_local/date_to_local): ASC (cronológico).
    - Caso contrário: DESC (mais recentes primeiro).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        # Montar query aos poucos dependendo de cada filtro que vai ser utilizado
        conditions = []  # cada filtro vira um elemento aqui
        params = []      # cada valor do que será procurado do filtro é um item aqui (na mesma ordem do array de cima)

        if search_text:
            conditions.append("(t.source_text ILIKE %s OR t.description ILIKE %s)")
            params.extend([f"%{search_text}%", f"%{search_text}%"])  

        if type_name:
            resolved = _resolve_type_id(cur, type_id=None, type_name=type_name)
            if not resolved:
                return {"status": "error", "message": f"Tipo inválido: {type_name}"}
            conditions.append("t.type = %s")
            params.append(resolved)

        if date_from_local:
            conditions.append(
                "(t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date >= %s::date"
            )
            params.append(date_from_local)

        if date_to_local:
            conditions.append(
                "(t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date <= %s::date"
            )
            params.append(date_to_local)

        # Junta os filtros com AND, não coloca WHERE nenhum se não tiver filtro, ou seja, coloca uma string vazia
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Ordem cronológica se tem filtro de data, se não os mais recente primeiro
        order = "ASC" if (date_from_local or date_to_local) else "DESC"

        params.append(limit)  # smp o ultimo param (pro LIMIT)

        cur.execute(
            f"""
            SELECT
                t.id,
                t.amount,
                tt.type        AS type_name,
                t.description,
                t.payment_method,
                t.occurred_at AT TIME ZONE 'America/Sao_Paulo' AS occurred_at_local,
                t.source_text
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            {where_clause}
            ORDER BY t.occurred_at {order}
            LIMIT %s;
            """,
            params
        )

        columns = [desc[0] for desc in cur.description] #pegar somente o nome da coluna 
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]  #fazer um zip entre os nomes das colunas e os valores de cada linha, e transformar isso em um dicionário para cada linha, e depois juntar tudo numa list

        # transformar em string
        for row in rows:
            if row.get("occurred_at_local"):
                row["occurred_at_local"] = str(row["occurred_at_local"])

        return {"status": "ok", "count": len(rows), "transactions": rows}

    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
    
@tool("total_balance")
def total_balance() -> dict:
    """
    Retorna o saldo total (INCOME - EXPENSES) em todo o histórico (ignora TRANSFER).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                -- soma só as entradas
                COALESCE(SUM(CASE WHEN tt.type = 'INCOME'   THEN t.amount ELSE 0 END), 0) AS total_income,
                -- soma só as saídas
                COALESCE(SUM(CASE WHEN tt.type = 'EXPENSES' THEN t.amount ELSE 0 END), 0) AS total_expenses,
                -- saldo = entradas - saídas (TRANSFER é ignorado pelo WHERE abaixo)
                COALESCE(SUM(CASE WHEN tt.type = 'INCOME'   THEN  t.amount
                                  WHEN tt.type = 'EXPENSES' THEN -t.amount END), 0) AS balance
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            WHERE tt.type IN ('INCOME', 'EXPENSES');
            """
        )
        row = cur.fetchone()
        return {
            "status": "ok",
            "total_income": float(row[0]),
            "total_expenses": float(row[1]),
            "balance": float(row[2]),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

@tool("daily_balance")
def daily_balance(date_local: str) -> dict:
    """
    Retorna o saldo (INCOME - EXPENSES) do dia local informado (YYYY-MM-DD) em America/Sao_Paulo.
    Ignora TRANSFER (type=3).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN tt.type = 'INCOME'   THEN t.amount ELSE 0 END), 0) AS total_income,
                COALESCE(SUM(CASE WHEN tt.type = 'EXPENSES' THEN t.amount ELSE 0 END), 0) AS total_expenses,
                COALESCE(SUM(CASE WHEN tt.type = 'INCOME'   THEN  t.amount
                                  WHEN tt.type = 'EXPENSES' THEN -t.amount END), 0) AS balance
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            WHERE tt.type IN ('INCOME', 'EXPENSES')
              -- converte occurred_at para horário de SP antes de comparar com a data informada
              AND (t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date = %s::date;
            """,
            (date_local,)
        )
        row = cur.fetchone()
        return {
            "status": "ok",
            "date": date_local,
            "total_income": float(row[0]),
            "total_expenses": float(row[1]),
            "balance": float(row[2]),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

class UpdateTransactionArgs(BaseModel):
    id: Optional[int] = Field(
        default=None,
        description="ID da transação a atualizar. Se ausente, será feita uma busca por (match_text + date_local)."
    )
    match_text: Optional[str] = Field(
        default=None,
        description="Texto para localizar transação quando id não for informado (busca em source_text/description)."
    )
    date_local: Optional[str] = Field(
        default=None,
        description="Data local (YYYY-MM-DD) em America/Sao_Paulo; usado em conjunto com match_text quando id ausente."
    )
    amount: Optional[float] = Field(default=None, description="Novo valor.")
    type_id: Optional[int] = Field(default=None, description="Novo type_id (1/2/3).")
    type_name: Optional[str] = Field(default=None, description="Novo type_name: INCOME | EXPENSES | TRANSFER.")
    category_id: Optional[int] = Field(default=None, description="Nova categoria (id).")
    category_name: Optional[str] = Field(default=None, description="Nova categoria (nome).")
    description: Optional[str] = Field(default=None, description="Nova descrição.")
    payment_method: Optional[str] = Field(default=None, description="Novo meio de pagamento.")
    occurred_at: Optional[str] = Field(default=None, description="Novo timestamp ISO 8601.")

@tool("update_transaction", args_schema=UpdateTransactionArgs)
def update_transaction(
    id: Optional[int] = None,
    match_text: Optional[str] = None,
    date_local: Optional[str] = None,
    amount: Optional[float] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    description: Optional[str] = None,
    payment_method: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """
    Atualiza uma transação existente.
    Estratégias:
      - Se 'id' for informado: atualiza diretamente por ID.
      - Caso contrário: localiza a transação mais recente que combine (match_text em source_text/description)
        E (date_local em America/Sao_Paulo), então atualiza.
    Retorna: status, rows_affected, id, e o registro atualizado.
    """
    if not any([amount, type_id, type_name, category_id, category_name, description, payment_method, occurred_at]):
        return {"status": "error", "message": "Nada para atualizar: forneça pelo menos um campo (amount, type, category, description, payment_method, occurred_at)."}

    conn = get_conn()
    cur = conn.cursor()
    try:
        # Resolve target_id
        target_id = id
        if target_id is None:
            if not match_text or not date_local:
                return {"status": "error", "message": "Sem 'id': informe match_text E date_local para localizar o registro."}

            # Buscar o mais recente no dia local informado que combine o texto
            cur.execute(
                f"""
                SELECT t.id
                FROM transactions t
                WHERE (t.source_text ILIKE %s OR t.description ILIKE %s)
                  AND {_local_date_filter_sql("t.occurred_at")}
                ORDER BY t.occurred_at DESC
                LIMIT 1;
                """,
                (f"%{match_text}%", f"%{match_text}%", date_local)
            )
            row = cur.fetchone()
            if not row:
                return {"status": "error", "message": "Nenhuma transação encontrada para os filtros fornecidos."}
            target_id = row[0]

        # Resolver type_id / category_id a partir de nomes, se fornecidos
        resolved_type_id = _resolve_type_id(cur, type_id, type_name) if (type_id or type_name) else None
        resolved_category_id = category_id
        if category_name and not category_id:
            resolved_category_id = _get_category_id(cur, category_name)

        # Montar SET dinâmico
        sets = []
        params: List[object] = []
        if amount is not None:
            sets.append("amount = %s")
            params.append(amount)
        if resolved_type_id is not None:
            sets.append("type = %s")
            params.append(resolved_type_id)
        if resolved_category_id is not None:
            sets.append("category_id = %s")
            params.append(resolved_category_id)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if payment_method is not None:
            sets.append("payment_method = %s")
            params.append(payment_method)
        if occurred_at is not None:
            sets.append("occurred_at = %s::timestamptz")
            params.append(occurred_at)

        if not sets:
            return {"status": "error", "message": "Nenhum campo válido para atualizar."}

        params.append(target_id)

        cur.execute(
            f"UPDATE transactions SET {', '.join(sets)} WHERE id = %s;",
            params
        )
        rows_affected = cur.rowcount
        conn.commit()

        # Retornar o registro atualizado
        cur.execute(
            """
            SELECT
              t.id, t.occurred_at, t.amount, tt.type AS type_name,
              c.name AS category_name, t.description, t.payment_method, t.source_text
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.id = %s;
            """,
            (target_id,)
        )
        r = cur.fetchone()
        updated = None
        if r:
            updated = {
                "id": r[0],
                "occurred_at": str(r[1]),
                "amount": float(r[2]),
                "type": r[3],
                "category": r[4],
                "description": r[5],
                "payment_method": r[6],
                "source_text": r[7],
            }

        return {
            "status": "ok",
            "rows_affected": rows_affected,
            "id": target_id,
            "updated": updated
        }

    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass


# Exporta a lista de tools
TOOLS = [add_transaction, query_transactions, total_balance, daily_balance, update_transaction]