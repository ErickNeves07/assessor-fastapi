import operator
import hashlib
import json
import logging
from dataclasses import asdict
from typing import Annotated
from langgraph.graph import StateGraph, END, MessagesState
from langgraph.types import RunnableConfig
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.runtime import Runtime
from app.memory import salvar_mensagem
from app.observabilidade import abrir_turno, fechar_turno, status_do_estado

from app.tools.memoria import TOOLS_MEMORIA
from app.tools.financeiro import TOOLS
from app.tools.perfil import TOOLS_PERFIL
from app.tools.faq import FAQ_TOOLS
from app.tools.agenda import TOOLS_AGENDA
from app.tools.calendario_google import TOOLS_GOOGLE
from app.prompts import (
    ROUTER_PROMPT_COMPLETO,
    FINANCEIRO_PROMPT_COMPLETO,
    AGENDA_PROMPT_COMPLETO,
    ORQUESTRADOR_PROMPT_COMPLETO,
    FAQ_PROMPT_COMPLETO,
)
from app.guardrail import anonimizar_entrada, guardrail_entrada, guardrail_saida
from app.llms import llm_rapido, llm_especialista

from app.juiz import no_juiz
from app.planejamento import ContextoTurno, dados_json, sessao_exclusiva
from app.execucao import ResultadoExecucao, executar_plano

# ==============================================================================
# MODELOS E AGENTES  (sem checkpointer — a memória fica no grafo)
# ==============================================================================

router_app = create_agent(
    model=llm_rapido,
    tools=TOOLS_MEMORIA,
    system_prompt=ROUTER_PROMPT_COMPLETO,
)

financeiro_app = create_agent(
    model=llm_especialista,
    tools=TOOLS + TOOLS_MEMORIA + TOOLS_PERFIL,
    system_prompt=FINANCEIRO_PROMPT_COMPLETO,
    context_schema=ContextoTurno,
)

agenda_app = create_agent(
    model=llm_especialista,
    tools=TOOLS_MEMORIA + TOOLS_AGENDA + TOOLS_GOOGLE,
    system_prompt=AGENDA_PROMPT_COMPLETO,
    context_schema=ContextoTurno,
)

orquestrador_app = create_agent(
    model=llm_rapido,
    system_prompt=ORQUESTRADOR_PROMPT_COMPLETO,
)

faq_app = create_agent(
    model=llm_rapido,
    tools=FAQ_TOOLS,
    system_prompt=FAQ_PROMPT_COMPLETO,
)


# ==============================================================================
# ESTADO
# ==============================================================================
class Estado(MessagesState):
    agentes_chamados: Annotated[list[str], operator.add]
    rota:             str
    mapa_pii:         dict   
    user_id:          str
    session_id:       str
    bloqueio_entrada: bool
    plano_turno: dict
    veredito_juiz: dict
    resultado_execucao: dict
    erro_tecnico: bool


# ==============================================================================
# NÓS
# ==============================================================================
def _mensagens_roteador(mensagens: list[BaseMessage]) -> list[BaseMessage]:
    historico = []
    ultima_resposta = None
    for mensagem in mensagens:
        if mensagem.type == "human":
            if ultima_resposta is not None:
                historico.append(ultima_resposta)
            historico.append(HumanMessage(content=mensagem.content))
            ultima_resposta = None
        elif (
            mensagem.type == "ai"
            and not mensagem.tool_calls
            and not mensagem.invalid_tool_calls
            and mensagem.text.strip()
        ):
            ultima_resposta = AIMessage(content=mensagem.text)
    if ultima_resposta is not None:
        historico.append(ultima_resposta)
    return historico


def no_roteador(estado: Estado, config: RunnableConfig) -> dict:
    saida = router_app.invoke(
        {"messages": _mensagens_roteador(estado["messages"])},
        config=config
    )
    texto = saida["messages"][-1].text

    # Resposta direta (saudação, fora de escopo): já escreve no campo final
    if not texto.strip().startswith("ROUTE="):
        return {
            "agentes_chamados": ["roteador"],
            "rota":             "fim",
            "messages":         [{"role": "assistant", "content": texto}],
        }

    rota = "fim"
    for linha in texto.splitlines():
        if linha.startswith("ROUTE="):
            rota = linha.split("=", 1)[1].strip()
            break

    return {
        "agentes_chamados": ["roteador"],
        "rota":             rota,
        # Não adiciona nada ao messages — especialista lê histórico limpo
    }

def no_financeiro(estado: Estado, config: RunnableConfig, runtime: Runtime[ContextoTurno]) -> dict:
    return financeiro_app.invoke({"messages": list(estado["messages"])},
                                 config={**config, "max_concurrency": 1}, context=runtime.context)


def no_agenda(estado: Estado, config: RunnableConfig, runtime: Runtime[ContextoTurno]) -> dict:
    return agenda_app.invoke({"messages": list(estado["messages"])},
                             config={**config, "max_concurrency": 1}, context=runtime.context)


def no_orquestrador(estado: Estado, config: RunnableConfig, runtime: Runtime[ContextoTurno]) -> dict:
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "human":
            break
        if mensagem.type == "ai" and mensagem.text and not mensagem.tool_calls:
            ultima_especialista = mensagem.text
            break
    plano = runtime.context.plano
    if ultima_especialista.strip() or plano.operacoes or plano.passos or plano.erros:
        entrada = json.dumps({"pedido": runtime.context.pergunta, "especialista": ultima_especialista,
                              "plano_temporario": plano.conteudo()}, ensure_ascii=False)
        saida = orquestrador_app.invoke({"messages": [{"role": "human", "content": entrada}]}, config=config)
        resposta = saida["messages"][-1].text.strip()
    else:
        resposta = ""
    if not resposta or resposta == "(sem resposta)":
        plano.falha("Os modelos não produziram uma resposta válida para concluir o pedido. Nenhuma escrita foi executada.", tecnico=True)
        resposta = plano.erros[-1]["mensagem"]

    return {
        "agentes_chamados": [estado["rota"], "orquestrador"],
        "messages":         [{"role": "assistant", "content": resposta}],
        "plano_turno":      plano.fechar(),
    }

def no_guardrail_entrada(estado: Estado, config: RunnableConfig, runtime: Runtime[ContextoTurno]) -> dict:
    msg_original = estado["messages"][-1]
    anonimizado, mapa_pii = anonimizar_entrada(msg_original.content)
    resultado_guardrail = guardrail_entrada(anonimizado)
    session_id = runtime.context.session_id
    runtime.context.pergunta = anonimizado
    salvar_mensagem(session_id, "human", anonimizado, estado["user_id"])
    if resultado_guardrail["bloqueado"]:
        return {
            "agentes_chamados": [f"guardrail_entrada:{resultado_guardrail['motivo']}"],
            "bloqueio_entrada": True,
            "rota":             "fim",
            "messages":         [{"role": "assistant", "content": resultado_guardrail["mensagem"]}],
            "mapa_pii":         mapa_pii
        }
    else:
        return {
            "agentes_chamados": ["guardrail_entrada:aprovado"],
            "bloqueio_entrada": False,
            "rota":             "roteador",
            "messages":         [RemoveMessage(id=msg_original.id), {"role": "human", "content": anonimizado}],
            "mapa_pii":         mapa_pii
        }
    
    
def no_guardrail_saida(estado: Estado) -> dict:
    ultima = estado["messages"][-1].content
    resultado = guardrail_saida(ultima, estado.get("mapa_pii", {}))
    return {
        "messages":         [{"role": "assistant", "content": resultado["conteudo"]}],
        "agentes_chamados": ["guardrail_saida"]
    }




# ==============================================================================
# FUNÇÃO DE DECISÃO
# ==============================================================================
def decidir_especialista(estado: Estado) -> str:
    return  estado["rota"] if  estado["rota"] in ("financeiro", "agenda", "faq") else "fim"

def decidir_pos_guardrail_entrada(estado: Estado) -> str:
    return "fim" if estado["rota"] == "fim" else "roteador"


# ==============================================================================
# CONSTRUÇÃO DO GRAFO
# ==============================================================================
grafo = StateGraph(Estado, context_schema=ContextoTurno)

grafo.add_node("guardrail_entrada",     no_guardrail_entrada)  
grafo.add_node("roteador",     no_roteador)
grafo.add_node("financeiro",   no_financeiro)
grafo.add_node("agenda",       no_agenda)
grafo.add_node("faq",          faq_app)
grafo.add_node("orquestrador", no_orquestrador)
grafo.add_node("guardrail_saida",       no_guardrail_saida)

grafo.add_node("juiz", no_juiz)

grafo.set_entry_point("guardrail_entrada")
grafo.add_conditional_edges(
    "guardrail_entrada",
    decidir_pos_guardrail_entrada,
    {
        "roteador": "roteador",   
        "fim":  END,
    },
)

grafo.add_conditional_edges(
    "roteador",
    decidir_especialista,
    {
        "financeiro": "financeiro",
        "agenda":     "agenda",
        "faq":        "faq",
        "fim":        END,       # resposta direta: sem especialista nem orquestrador
    },
)

grafo.add_edge("financeiro",   "orquestrador")
grafo.add_edge("agenda",       "orquestrador")
grafo.add_edge("orquestrador", "guardrail_saida")
grafo.add_edge("guardrail_saida", "juiz")
grafo.add_edge("faq",             "juiz")
grafo.add_edge("juiz",            END)

# Memória centralizada no grafo — persiste o Estado inteiro entre turns
memory = MemorySaver()
fluxo_agentes = grafo.compile(checkpointer=memory)


# ==============================================================================
# FLUXO PRINCIPAL
# ==============================================================================
def _executar_turno(pergunta_usuario: str, session_id: str, user_id: str, turno_id: str) -> str:
    contexto = ContextoTurno(user_id=user_id, session_id=session_id, pergunta=pergunta_usuario)
    chave = hashlib.sha256(f"{user_id}\0{session_id}".encode()).hexdigest()
    config = {"configurable": {"thread_id": chave, "session_id": session_id, "user_id": user_id}}
    estado_inicial = {
        "messages":         [{"role": "human", "content": pergunta_usuario}],
        "agentes_chamados": [],
        "rota":             "",
        "mapa_pii":         {},
        "bloqueio_entrada": False,
        "user_id":          user_id,
        "plano_turno": {}, "veredito_juiz": {}, "resultado_execucao": {}, "erro_tecnico": False,
    }

    status = "erro"
    rota = ""

    try:
        estado_final = fluxo_agentes.invoke(
            estado_inicial,
            config=config, context=contexto,
        )
        resposta = estado_final["messages"][-1].text
        resultado = ResultadoExecucao("ok")
        if contexto.plano.operacoes or contexto.plano.erros:
            resultado = executar_plano(contexto.plano, estado_final.get("veredito_juiz") or {})
            if resultado.status not in ("ok", "recusado"):
                resposta = resultado.mensagem
        estado_final["erro_tecnico"] = resultado.tecnico or (estado_final.get("veredito_juiz") or {}).get("tecnico", False)
        try:
            ultima = estado_final["messages"][-1]
            fluxo_agentes.update_state(config, {
                "messages": [AIMessage(content=resposta, id=ultima.id)],
                "resultado_execucao": dados_json(asdict(resultado)),
                "plano_turno": {}, "veredito_juiz": {}, "erro_tecnico": estado_final["erro_tecnico"],
            })
        except Exception:
            estado_final["erro_tecnico"] = True
            logging.exception("Falha ao atualizar histórico do grafo; não reexecutar operações confirmadas.")
        try:
            salvar_mensagem(session_id, "assistant", resposta, user_id=user_id)
        except Exception:
            estado_final["erro_tecnico"] = True
            logging.exception("Falha ao salvar resposta no Mongo; resultado de negócio preservado.")

        status = status_do_estado(estado_final)
        rota = estado_final.get("rota") or ""

        return resposta
    finally:
        fechar_turno(turno_id, session_id=session_id, rota=rota, status=status)


def executar_fluxo_assessor(pergunta_usuario: str, session_id: str, user_id: str) -> str:
    turno_id = abrir_turno()
    with sessao_exclusiva(user_id, session_id):
        return _executar_turno(pergunta_usuario, session_id, user_id, turno_id)
