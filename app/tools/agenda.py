import json
import re
from datetime import date, timedelta
from typing import Optional

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.planejamento import ContextoTurno, PlanoTurno, dados_json, horario, obter_plano, preparado
from app.tools.db import get_conn


class AddEventArgs(BaseModel):
    title: str = Field(..., min_length=1)
    source_text: str = Field(..., description="Pedido original.")
    start_time: str = Field(..., description="Início ISO 8601, preferencialmente com fuso de São Paulo.")
    end_time: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None


class QueryEventsArgs(BaseModel):
    text: Optional[str] = None
    date_local: Optional[str] = None
    date_from_local: Optional[str] = None
    date_to_local: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=200)


def _visao(plano):
    sql = """WITH p AS (
        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(
            id bigint,title text,start_time timestamptz,end_time timestamptz,
            location text,notes text,source_text text,recorded_at timestamptz,referencia_temporaria text)
    ), visao AS (
        SELECT e.id,e.title,e.start_time,e.end_time,e.location,e.notes,e.source_text,e.recorded_at,
               NULL::text AS referencia_temporaria,false AS projetado FROM events e
        UNION ALL SELECT p.*,true AS projetado FROM p
    ) """
    return sql, [json.dumps(plano.projecoes("agenda"), ensure_ascii=False)]


def _ler(plano, where="", parametros=None, limit=200):
    prefixo, params = _visao(plano)
    params.extend(parametros or [])
    params.append(limit)
    conn = get_conn()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(prefixo + "SELECT * FROM visao " + where + " ORDER BY start_time,id NULLS LAST,referencia_temporaria LIMIT %s", params)
            nomes = [c[0] for c in cur.description]
            return [dict(zip(nomes, row)) for row in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()


def _formato(registro, plano):
    r = dict(registro)
    inicio = horario(r["start_time"], plano.agora)
    fim = horario(r["end_time"], plano.agora) if r.get("end_time") else None
    r.update(start_time=inicio.isoformat(), end_time=fim.isoformat() if fim else None,
             data_local=inicio.strftime("%d/%m/%Y"), hora_inicio_local=inicio.strftime("%H:%M"),
             data_fim_local=fim.strftime("%d/%m/%Y") if fim else None,
             hora_fim_local=fim.strftime("%H:%M") if fim else None, fuso_horario="America/Sao_Paulo")
    return dados_json(r)


def somente_local(pergunta):
    return bool(re.search(r"(?:somente|apenas).{0,40}(?:banco|local)|sem.{0,20}google", pergunta, re.IGNORECASE))


@tool("add_event", args_schema=AddEventArgs)
def add_event(title: str, source_text: str, start_time: str, end_time: Optional[str] = None,
              location: Optional[str] = None, notes: Optional[str] = None,
              runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Prepara evento local e sua criação Google; não grava. Google é incluído automaticamente, exceto pedido explícito somente local."""
    plano = obter_plano(runtime)
    try:
        inicio = horario(start_time, plano.agora)
        fim = horario(end_time, plano.agora) if end_time else inicio + timedelta(hours=1)
        if fim <= inicio:
            raise ValueError("O fim deve ser posterior ao início.")
        google = not somente_local(runtime.context.pergunta)
        if not google:
            existentes = _ler(plano, "WHERE title=%s AND start_time=%s::timestamptz AND COALESCE(end_time,start_time + INTERVAL '1 hour')=%s::timestamptz", [title, inicio, fim])
            if existentes:
                return plano.falha("O evento já consta na agenda local; não foi preparada outra inserção.")
        conflitos = _ler(plano, "WHERE start_time < %s::timestamptz AND COALESCE(end_time,start_time + INTERVAL '1 hour') > %s::timestamptz", [fim, inicio])
        if conflitos:
            return plano.falha("Existe conflito com: " + ", ".join(r["title"] for r in conflitos) + ". Informe outro horário.")
        op = plano.adicionar("add_event", dict(id=None, title=title, start_time=inicio, end_time=fim,
                             location=location, notes=notes, source_text=runtime.context.pergunta,
                             recorded_at=plano.agora, google_necessario=google))
        if google:
            plano.adicionar("add_google_event", {"evento": op.referencia, "title": title,
                            "start_time": inicio, "end_time": fim, "location": location, "description": notes})
        return preparado(op)
    except Exception as exc:
        return plano.falha(str(exc), tecnico=not isinstance(exc, ValueError))


@tool("query_events", args_schema=QueryEventsArgs)
def query_events(text: Optional[str] = None, date_local: Optional[str] = None,
                 date_from_local: Optional[str] = None, date_to_local: Optional[str] = None,
                 limit: int = 20, runtime: ToolRuntime[ContextoTurno] = None) -> dict:
    """Consulta agenda local e eventos preparados antes desta leitura. Não consulta o Google."""
    contexto = getattr(runtime, "context", None)
    plano = contexto.plano if isinstance(contexto, ContextoTurno) else PlanoTurno()
    try:
        if date_local and (date_from_local or date_to_local):
            raise ValueError("Use date_local ou intervalo, não os dois.")
        conditions, params = [], []
        if text:
            conditions.append("(title ILIKE %s OR location ILIKE %s OR notes ILIKE %s OR source_text ILIKE %s)")
            params.extend([f"%{text}%"] * 4)
        inicio, fim = date_local or date_from_local, date_local or date_to_local
        if inicio:
            date.fromisoformat(inicio)
            conditions.append("start_time >= (%s::date::timestamp AT TIME ZONE 'America/Sao_Paulo')")
            params.append(inicio)
        if fim:
            date.fromisoformat(fim)
            conditions.append("start_time < ((%s::date + INTERVAL '1 day') AT TIME ZONE 'America/Sao_Paulo')")
            params.append(fim)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        resultados = [_formato(r, plano) for r in _ler(plano, where, params, limit)]
        return plano.leitura("query_events", dict(text=text, date_local=date_local,
                            date_from_local=date_from_local, date_to_local=date_to_local, limit=limit),
                            {"status": "ok", "count": len(resultados), "results": resultados,
                             "projecao": bool(plano.projecoes("agenda"))})
    except Exception as exc:
        return plano.falha(str(exc), tecnico=not isinstance(exc, ValueError))


TOOLS_AGENDA = [add_event, query_events]
