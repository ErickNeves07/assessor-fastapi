from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from langchain.tools import tool
from pydantic import BaseModel, Field, model_validator

from app.config import (
    BASE_DIR,
    GOOGLE_CALENDAR_ACCESS_TOKEN,
    GOOGLE_CALENDAR_MCP_URL,
    GOOGLE_OAUTH_CREDENTIALS,
)

ESCOPOS = ["https://www.googleapis.com/auth/calendar.events"]
ARQUIVO_TOKEN = BASE_DIR / "google-calendar.token.json"
CABECALHOS_MCP = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
_COMANDO_LOGIN = (
    'python -c "from app.tools.calendario_google import autenticar; print(autenticar())"'
)


def _credenciais_oauth() -> str | None:
    if not GOOGLE_OAUTH_CREDENTIALS:
        return None
    caminho = Path(GOOGLE_OAUTH_CREDENTIALS)
    if not caminho.is_absolute():
        caminho = BASE_DIR / caminho
    return str(caminho) if caminho.is_file() else None


def _salvar_token(creds) -> None:
    ARQUIVO_TOKEN.write_text(creds.to_json(), encoding="utf-8")


def _carregar_token():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not ARQUIVO_TOKEN.is_file():
        return None
    creds = Credentials.from_authorized_user_file(str(ARQUIVO_TOKEN), ESCOPOS)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _salvar_token(creds)
    return creds if creds.valid else None


def autenticar() -> dict:
    keys = _credenciais_oauth()
    if not keys:
        return {"status": "error", "message": "Falta o JSON OAuth indicado em GOOGLE_OAUTH_CREDENTIALS."}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return {"status": "error", "message": "Instale as dependências: python -m pip install -r requirements.txt"}
    flow = InstalledAppFlow.from_client_secrets_file(keys, ESCOPOS)
    creds = flow.run_local_server(port=0, prompt="consent", timeout_seconds=180)
    _salvar_token(creds)
    return {"status": "ok", "arquivo": str(ARQUIVO_TOKEN)}


def _access_token() -> str | None:
    if GOOGLE_CALENDAR_ACCESS_TOKEN:
        return GOOGLE_CALENDAR_ACCESS_TOKEN
    creds = _carregar_token()
    return creds.token if creds else None


def _resultado_mcp(result: Any) -> dict:
    data = getattr(result, "structured_content", None)
    if data is None:
        data = getattr(result, "structuredContent", None)
    if data is None:
        textos = [bloco.text for bloco in result.content if getattr(bloco, "text", None)]
        if not textos:
            return {"status": "error", "message": "O MCP não devolveu conteúdo."}
        try:
            data = json.loads(textos[0])
        except json.JSONDecodeError:
            data = {"result": "\n".join(textos)}
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        return {"status": "error", "message": str(data)}
    return data if isinstance(data, dict) else {"result": data}


def _motivo(erro: Exception) -> str:
    if isinstance(erro, ExceptionGroup):
        return " | ".join(_motivo(sub) for sub in erro.exceptions)
    return str(erro) or type(erro).__name__


def _status_http(erro: Exception) -> int | None:
    if isinstance(erro, ExceptionGroup):
        return next((codigo for sub in erro.exceptions if (codigo := _status_http(sub))), None)
    resposta = getattr(erro, "response", None)
    return getattr(resposta, "status_code", None)


async def _chamar_via_http(nome: str, argumentos: dict, token: str) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    status_erro = None

    async def registrar_status(resposta):
        nonlocal status_erro
        if resposta.request.method == "POST" and resposta.status_code >= 400:
            status_erro = resposta.status_code

    try:
        async with asyncio.timeout(35):
            async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as client:
                client.event_hooks.setdefault("response", []).append(registrar_status)
                async with streamable_http_client(GOOGLE_CALENDAR_MCP_URL, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool(nome, argumentos)
                        resultado = _resultado_mcp(result)
                        if resultado.get("status") == "error" and status_erro:
                            resultado["http_status"] = status_erro
                        return resultado
    except Exception as exc:
        return {"status": "error", "message": _motivo(exc),
                "http_status": status_erro or _status_http(exc)}


def _executar(coro_factory) -> dict:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def _normalizar_evento(data: dict) -> dict:
    if data.get("status") == "error":
        return data
    evento = data
    for chave in ("event", "result"):
        if isinstance(evento.get(chave), dict):
            evento = evento[chave]
    if not evento.get("id"):
        return {"status": "error", "message": "O Google não confirmou a criação com um ID de evento."}
    inicio = evento.get("start") or {}
    return {
        "status": "ok",
        "id": evento["id"],
        "titulo": evento.get("summary") or evento.get("titulo"),
        "inicio": (inicio.get("dateTime") if isinstance(inicio, dict) else inicio)
        or evento.get("startTime") or evento.get("inicio"),
        "link": evento.get("htmlLink") or evento.get("link"),
    }


def _criar_via_rest(payload: dict, token: str) -> dict:
    corpo = {
        "summary": payload["summary"],
        "start": {"dateTime": payload["startTime"], "timeZone": payload["timeZone"]},
        "end": {"dateTime": payload["endTime"], "timeZone": payload["timeZone"]},
    }
    for chave in ("location", "description"):
        if payload.get(chave):
            corpo[chave] = payload[chave]
    try:
        resposta = httpx.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"}, json=corpo, timeout=30,
        )
        data = resposta.json()
        if resposta.is_error:
            erro = data.get("error") or {}
            return {"status": "error", "message": erro.get("message", "Google recusou a criação."),
                    "http_status": resposta.status_code}
        return _normalizar_evento(data)
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "error", "message": f"Falha na Calendar API: {type(exc).__name__}. Confira o calendário antes de repetir."}


def chamar_google_calendar(nome_tool: str, argumentos: dict) -> dict:
    try:
        token = _access_token()
    except Exception:
        return {"status": "error", "message": f"Não consegui renovar o login Google. Rode novamente: {_COMANDO_LOGIN}"}
    if not token:
        return {"status": "error", "message": f"Sem login do Google Calendar. Rode: {_COMANDO_LOGIN}"}
    try:
        resultado = _executar(lambda: _chamar_via_http(nome_tool, argumentos, token))
    except Exception as exc:
        resultado = {"status": "error", "message": _motivo(exc), "http_status": _status_http(exc)}
    if resultado.get("status") != "error":
        return _normalizar_evento(resultado)
    mensagem = str(resultado.get("message", "")).lower()
    desabilitado = resultado.get("http_status") == 403 or any(
        trecho in mensagem for trecho in ("service_disabled", "has not been used", "api is disabled")
    )
    if nome_tool == "create_event" and desabilitado:
        return _criar_via_rest(argumentos, token)
    return resultado


def listar_tools_google() -> dict:
    try:
        resposta = httpx.post(
            GOOGLE_CALENDAR_MCP_URL, headers=CABECALHOS_MCP,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=30,
        )
        resposta.raise_for_status()
        return resposta.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "error", "message": f"Não consegui ler o catálogo Google: {type(exc).__name__}."}


class AddGoogleEventArgs(BaseModel):
    title: str = Field(..., min_length=1, description="Título do evento.")
    start_time: str = Field(..., description="Início ISO 8601 COM fuso, por exemplo 2026-10-30T12:00:00-03:00.")
    end_time: str | None = Field(default=None, description="Fim ISO 8601 COM fuso. Se omitido, duração de uma hora.")
    location: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validar_horarios(self):
        inicio = datetime.fromisoformat(self.start_time)
        fim = datetime.fromisoformat(self.end_time) if self.end_time else inicio + timedelta(hours=1)
        if inicio.utcoffset() is None or fim.utcoffset() is None:
            raise ValueError("Informe início e fim com fuso horário.")
        if fim <= inicio:
            raise ValueError("O fim precisa ser posterior ao início.")
        return self


@tool(
    "add_google_event",
    args_schema=AddGoogleEventArgs,
    description="Cria no Google Calendar após add_event confirmar a gravação no Postgres. "
                "Não grava no banco local. Se falhar, preserve o evento local e informe a falha.",
)
def add_google_event(title: str, start_time: str, end_time: str | None = None,
                     location: str | None = None, description: str | None = None) -> dict:
    fim = end_time or (datetime.fromisoformat(start_time) + timedelta(hours=1)).isoformat()
    payload = {"summary": title, "startTime": start_time, "endTime": fim, "timeZone": "America/Sao_Paulo"}
    if location:
        payload["location"] = location
    if description:
        payload["description"] = description
    return chamar_google_calendar("create_event", payload)


TOOLS_GOOGLE = [add_google_event]
