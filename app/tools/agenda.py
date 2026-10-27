from typing import Optional, List
from datetime import datetime
from zoneinfo import ZoneInfo
from langchain.tools import tool
from pydantic import BaseModel, Field

# A conexão com o Postgres agora vem de um lugar só (antes era uma get_conn()
# local, idêntica à de financeiro.py).
from app.tools.db import get_conn


FUSO_AGENDA = ZoneInfo("America/Sao_Paulo")


def _formatar_horarios(inicio: datetime, fim: Optional[datetime]) -> dict:
    inicio_local = inicio.astimezone(FUSO_AGENDA)
    fim_local = fim.astimezone(FUSO_AGENDA) if fim else None
    return {
        "start_time": inicio_local.isoformat(),
        "end_time": fim_local.isoformat() if fim_local else None,
        "data_local": inicio_local.strftime("%d/%m/%Y"),
        "hora_inicio_local": inicio_local.strftime("%H:%M"),
        "data_fim_local": fim_local.strftime("%d/%m/%Y") if fim_local else None,
        "hora_fim_local": fim_local.strftime("%H:%M") if fim_local else None,
        "fuso_horario": "America/Sao_Paulo",
    }


class AddEventArgs(BaseModel):
    title: str = Field(..., description="Título do evento.")
    source_text: str = Field(..., description="Texto original do usuário.")
    start_time: str = Field(..., description="Início do evento em ISO 8601 (ex.: '2025-06-10T14:00:00-03:00'). Obrigatório.")
    end_time: Optional[str] = Field(
        default=None,
        description="Fim do evento em ISO 8601. Opcional."
    )
    location: Optional[str] = Field(default=None, description="Local do evento (ex.: 'Sala 3', 'Zoom', 'Rua X, 100').")
    notes: Optional[str] = Field(default=None, description="Observações adicionais sobre o evento.")


@tool("add_event", args_schema=AddEventArgs)
def add_event(
    title: str,
    source_text: str,
    start_time: str,
    end_time: Optional[str] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Insere um evento/compromisso na agenda (tabela events) do banco de dados Postgres."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO events
                (title, start_time, end_time, location, notes, source_text)
            VALUES
                (%s, %s::timestamptz, %s::timestamptz, %s, %s, %s)
            RETURNING id, start_time, end_time;
            """,
            (title, start_time, end_time, location, notes, source_text),
        )
        new_id, start, end = cur.fetchone()
        conn.commit()
        return {
            "status": "ok",
            "id": new_id,
            "title": title,
            **_formatar_horarios(start, end),
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


#############
# NOVA TOOL #
#############

class QueryEventsArgs(BaseModel):
    text: Optional[str] = Field(
        default=None,
        description="Texto para busca em title, location, notes OU source_text (ex.: 'dentista', 'Marcos')."
    )
    date_local: Optional[str] = Field(
        default=None,
        description="Data local (YYYY-MM-DD) em America/Sao_Paulo para filtrar o dia."
    )
    date_from_local: Optional[str] = Field(
        default=None,
        description="Data local inicial (YYYY-MM-DD) inclusive."
    )
    date_to_local: Optional[str] = Field(
        default=None,
        description="Data local final (YYYY-MM-DD) inclusive."
    )
    limit: int = Field(default=20, ge=1, le=200, description="Máximo de registros retornados (1..200).")


@tool("query_events", args_schema=QueryEventsArgs)
def query_events(
    text: Optional[str] = None,
    date_local: Optional[str] = None,
    date_from_local: Optional[str] = None,
    date_to_local: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """
    Consulta eventos da agenda (tabela events) com filtros por texto e datas locais (America/Sao_Paulo).
    Use esta tool ANTES de confirmar disponibilidade ou conflito.
    Ordenação:
      - Intervalo (date_from_local/date_to_local): ASC (cronológico).
      - Caso contrário: ASC também (próximos compromissos primeiro a partir do filtro).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        if date_local and (date_from_local or date_to_local):
            return {"status": "error", "message": "Use date_local OU (date_from_local/date_to_local), não ambos."}

        where_sql = "WHERE 1=1"
        params: dict = {"limit": limit}

        if text:
            where_sql += (
                " AND (e.title ILIKE %(text_like)s"
                " OR e.location ILIKE %(text_like)s"
                " OR e.notes ILIKE %(text_like)s"
                " OR e.source_text ILIKE %(text_like)s)"
            )
            params["text_like"] = f"%{text}%"

        if date_local:
            where_sql += (
                " AND e.start_time >= (%(date_local)s::date AT TIME ZONE 'America/Sao_Paulo')"
                " AND e.start_time <  ((%(date_local)s::date + INTERVAL '1 day') AT TIME ZONE 'America/Sao_Paulo')"
            )
            params["date_local"] = date_local
        else:
            if date_from_local:
                where_sql += " AND e.start_time >= (%(date_from)s::date AT TIME ZONE 'America/Sao_Paulo')"
                params["date_from"] = date_from_local
            if date_to_local:
                where_sql += " AND e.start_time < ((%(date_to)s::date + INTERVAL '1 day') AT TIME ZONE 'America/Sao_Paulo')"
                params["date_to"] = date_to_local

        sql = f"""
            SELECT
              e.id,
              e.title,
              e.start_time,
              e.end_time,
              e.location,
              e.notes,
              e.source_text,
              e.recorded_at
            FROM events e
            {where_sql}
            ORDER BY e.start_time ASC
            LIMIT %(limit)s;
        """
        cur.execute(sql, params)
        rows = cur.fetchall()

        results: List[dict] = []
        for r in rows:
            results.append({
                "id": r[0],
                "title": r[1],
                **_formatar_horarios(r[2], r[3]),
                "location": r[4],
                "notes": r[5],
                "source_text": r[6],
                "recorded_at": str(r[7]),
            })

        return {"status": "ok", "count": len(results), "results": results}

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass


# Exporta a lista de tools
TOOLS_AGENDA = [add_event, query_events]
