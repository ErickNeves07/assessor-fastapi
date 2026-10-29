import operator
from typing import Annotated
from langgraph.graph import StateGraph, END, MessagesState
from langgraph.types import RunnableConfig
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver
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
)

agenda_app = create_agent(
    model=llm_especialista,
    tools=TOOLS_MEMORIA + TOOLS_AGENDA + TOOLS_GOOGLE,
    system_prompt=AGENDA_PROMPT_COMPLETO,
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

def no_orquestrador(estado: Estado, config: RunnableConfig) -> dict:
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = mensagem.content
            break

    saida = orquestrador_app.invoke({"messages": [{"role": "human", "content": ultima_especialista}]}, config=config)

    return {
        "agentes_chamados": [estado["rota"], "orquestrador"],
        "messages":         [{"role": "assistant", "content": saida["messages"][-1].content}],
    }

def no_guardrail_entrada(estado: Estado, config: RunnableConfig) -> dict:
    msg_original = estado["messages"][-1]
    anonimizado, mapa_pii = anonimizar_entrada(msg_original.content)
    resultado_guardrail = guardrail_entrada(anonimizado)
    session_id = config["configurable"]["thread_id"]
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
grafo = StateGraph(Estado)

grafo.add_node("guardrail_entrada",     no_guardrail_entrada)  
grafo.add_node("roteador",     no_roteador)
grafo.add_node("financeiro",   financeiro_app)
grafo.add_node("agenda",       agenda_app)
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
def executar_fluxo_assessor(pergunta_usuario: str, session_id: str, user_id: str) -> str:
    estado_inicial = {
        "messages":         [{"role": "human", "content": pergunta_usuario}],
        "agentes_chamados": [],
        "rota":             "",
        "mapa_pii":         {},
        "bloqueio_entrada": False,
        "user_id":          user_id
    }

    turno_id = abrir_turno()
    status = "erro"
    rota = ""

    try:
        estado_final = fluxo_agentes.invoke(
            estado_inicial,
            config={"configurable": {"thread_id": session_id, "user_id": user_id}}
        )
        resposta = estado_final["messages"][-1].text

        salvar_mensagem(session_id, "assistant", resposta, user_id=user_id)

        status = status_do_estado(estado_final)
        rota = estado_final.get("rota") or ""

        return resposta
    finally:
        fechar_turno(turno_id, session_id=session_id, rota=rota, status=status)
