import operator
from typing import Annotated
from langgraph.graph import StateGraph, END, MessagesState
from langgraph.types import RunnableConfig
from langchain_core.messages import RemoveMessage
from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver

from app.tools.memoria import TOOLS_MEMORIA
from app.tools.financeiro import TOOLS
from app.tools.faq import FAQ_TOOLS
from app.prompts import (
    ROUTER_PROMPT_COMPLETO,
    FINANCEIRO_PROMPT_COMPLETO,
    AGENDA_PROMPT_COMPLETO,
    ORQUESTRADOR_PROMPT_COMPLETO,
    FAQ_PROMPT_COMPLETO,
)
from app.guardrail import anonimizar_entrada, guardrail_entrada, guardrail_saida
from app.llms import llm_rapido, llm_especialista
from app.memory import salvar_mensagem

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
    tools=TOOLS + TOOLS_MEMORIA,
    system_prompt=FINANCEIRO_PROMPT_COMPLETO,
)

agenda_app = create_agent(
    model=llm_especialista,
    tools=TOOLS_MEMORIA,
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


# ==============================================================================
# NÓS
# ==============================================================================
def no_roteador(estado: Estado, config: RunnableConfig) -> dict:
    saida = router_app.invoke(
        {"messages": list(estado["messages"])},
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

def no_orquestrador(estado: Estado) -> dict:
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = mensagem.content
            break

    saida = orquestrador_app.invoke({"messages": [{"role": "human", "content": ultima_especialista}]})

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
            "agentes_chamados": ["guardrail_entrada"],
            "rota":             "fim",
            "messages":         [{"role": "assistant", "content": resultado_guardrail["mensagem"]}],
            "mapa_pii":         mapa_pii
        }
    else:
        return {
            "agentes_chamados": ["guardrail_entrada"],
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
grafo.add_edge("guardrail_saida", END)
grafo.add_edge("faq",          END)   # FAQ bypassa o orquestrador

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
        "user_id":          user_id
    }

    estado_final = fluxo_agentes.invoke(
        estado_inicial,
        config={"configurable": {"thread_id": session_id, "user_id": user_id}}
    )
    return estado_final["messages"][-1].text