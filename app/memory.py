"""
=================
Modelagem
---------
Um documento por acesso (sessão = uma conversa completa).
O _id é um UUID gerado internamente — a main.py só conhece o session_id.
O session_id identifica o usuário.

Documento
---------
{
  "_id":           "uuid-gerado-internamente",
  "session_id":    "id_usuario",
  "iniciada_em":   datetime,
  "atualizada_em": datetime,
  "resumo":        "Usuário registrou Pix de R$50...",
  "mensagens":     [
    {"role": "usuario",     "content": "oi"},
    {"role": "assistente", "content": "Olá!"}
  ]
}

Funções
----------------
  iniciar_sessao(session_id)                 → cria documento no MongoDB
  salvar_mensagem(session_id, role, content) → adiciona mensagem na sessão ativa
  encerrar_sessao(session_id)                → gera resumo e salva no documento
"""

import uuid
from datetime import datetime, timezone

from app.llms import llm_rapido
from pymongo import MongoClient
from app.config import MONGODB_URI

from qdrant_client import models
from app.vectorstore import qdrant, gerar_embedding, COLLECTION_MEMORIA

# ==============================================================================
# CONEXÃO
# ==============================================================================

_mongo      = MongoClient(MONGODB_URI)
db          = _mongo["assessor"]
col_sessoes = db["sessoes"]

col_sessoes.create_index("session_id")
col_sessoes.create_index("iniciada_em")

# ==============================================================================
# LLM PARA RESUMO
# ==============================================================================

_PROMPT_RESUMO = """\
Você é um assistente que resume conversas de assessoria financeira e agenda.
Gere um resumo conciso em 2-4 frases capturando:
- O que o usuário fez NESTA conversa (transações registradas, eventos agendados)
- O que o usuário perguntou
- Informações relevantes mencionadas (valores, datas, categorias)

REGRA IMPORTANTE: se o usuário apenas consultou CONVERSAS ANTERIORES
(ex.: "o que falamos na última sessão?", "o que eu disse sobre X?"),
registre SOMENTE a pergunta/consulta feita. NÃO re-registre o fato
recuperado da memória como se fosse uma ação nova acontecida nesta conversa.

Responda APENAS com o resumo, sem introdução ou explicação.

Conversa:
{conversa}
"""
_sessoes_ativas: dict = {}

def _agora() -> datetime:
    return datetime.now(timezone.utc)

def _formatar_conversa(mensagens: list[dict]) -> str:
    """Formata o array de mensagens em texto para o prompt de resumo."""
    linhas = []
    for msg in mensagens:
        linhas.append(f"{msg['role']}: {msg['content']}")
    return "\n".join(linhas)


def _gerar_resumo(mensagens: list[dict]) -> str:
    """Chama o LLM para gerar o resumo da sessão."""
    conversa = _formatar_conversa(mensagens)
    return llm_rapido.invoke(
        _PROMPT_RESUMO.format(conversa=conversa)
    ).content.strip()


# ==============================================================================
# FUNÇÕES
# ==============================================================================
def iniciar_sessao(session_id: str, user_id: str = "usuario_teste") -> None: # se o session_id for int tem que mudar aqui
    """
    Cria um novo documento de sessão no MongoDB.
    O doc_id (UUID) é gerado aqui e guardado em _sessoes_ativas.
    """
    if _doc_id_da_sessao(session_id):    # ← se já tem sessão aberta, não faz nada
        return
    doc_id = str(uuid.uuid4())
    agora  = _agora()

    col_sessoes.insert_one({
        "_id":           doc_id,
        "session_id":    session_id,
        "user_id":       user_id,
        "iniciada_em":   agora
    })

    _sessoes_ativas[session_id] = doc_id


def salvar_mensagem(session_id: str, role: str, content: str, user_id: str = "usuario_teste") -> None:
    """ Adiciona uma mensagem ao array de mensagens da sessão ativa. """
    iniciar_sessao(session_id, user_id=user_id)               
    doc_id = _doc_id_da_sessao(session_id)   

    col_sessoes.update_one(
        {"_id": doc_id},
        {
            "$push": {"mensagens": {"role": role, "content": content}},
            "$set":  {"atualizada_em": _agora()},
        },
    )


def encerrar_sessao(session_id) -> str:
    """
    Encerra a sessão ativa:
      1. Carrega mensagens do MongoDB
      2. Gera resumo via LLM
      3. Atualiza documento com resumo e atualizada_em
      4. Remove sessão do estado interno
    Retorna o resumo gerado ou string vazia se não houver mensagens.
    """
    doc_id = _doc_id_da_sessao(session_id)    

    if not doc_id:
        return ""

    doc = col_sessoes.find_one({"_id": doc_id})

    if not doc or not doc.get("mensagens"):
        _sessoes_ativas.pop(session_id, None)
        return ""

    resumo = _gerar_resumo(doc["mensagens"])

    col_sessoes.update_one(
        {"_id": doc_id},
        {"$set": {"resumo": resumo, "atualizada_em": _agora()}},
    )

    # Salva o embedding do resumo no Qdrant para busca semântica futura.
    # O filtro de multitenancy usa user_id (estável entre sessões), não session_id.
    user_id = doc.get("user_id", "usuario_teste")
    vetor = gerar_embedding(resumo)
    qdrant.upsert(
        collection_name=COLLECTION_MEMORIA,
        points=[
            models.PointStruct(
                id=doc_id,
                vector=vetor,
                payload={
                    "user_id":     user_id,
                    "session_id":  session_id,
                    "resumo":      resumo,
                    "iniciada_em": doc["iniciada_em"].isoformat(),
                },
            )
        ],
    )

    _sessoes_ativas.pop(session_id)

    return resumo

def _doc_id_da_sessao(session_id):
    doc_id = _sessoes_ativas.get(session_id)
    if doc_id:
        return doc_id

    doc = col_sessoes.find_one(
        {"session_id": session_id, "resumo": {"$in": ["", None]}},
        {"_id": 1},
        sort=[("iniciada_em", -1)],
    )
    if not doc:
        return None

    _sessoes_ativas[session_id] = doc["_id"]
    return doc["_id"]

def recuperar_historico(user_id: str, busca: str = "", limite: int = 3) -> list[dict]:
    if busca:
        vetor = gerar_embedding(busca)
        resultados = qdrant.query_points(
            collection_name=COLLECTION_MEMORIA,
            query=vetor,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id),
                    )
                ]
            ),
            limit=limite,
        )

        if resultados.points:
            return [
                {
                    "doc_id":      ponto.id,
                    "iniciada_em": ponto.payload.get("iniciada_em", ""),
                    "resumo":      ponto.payload["resumo"],
                }
                for ponto in resultados.points
            ]

    filtro = {"user_id": user_id, "resumo": {"$nin": ["", None]}}
    docs = (
        col_sessoes
        .find(filtro, {"resumo": 1, "iniciada_em": 1})
        .sort("iniciada_em", -1)
        .limit(limite)
    )

    return [
        {"doc_id": d["_id"], "iniciada_em": d["iniciada_em"], "resumo": d["resumo"]}
        for d in docs
    ]

    

    # só sessões DESTE usuário que já têm resumo (= já encerradas).
    # O $nin descarta a sessão em andamento, cujo resumo ainda está vazio —
    # sem ele a tool devolveria a própria conversa atual como se fosse passado.
    filtro = {"session_id": session_id, "resumo": {"$nin": ["", None]}}

    # se houver termo de busca, filtra resumos que o contenham (case-insensitive).
    # Acrescenta ao filtro existente em vez de substituí-lo, senão o $nin acima
    # se perderia e a sessão atual voltaria para o resultado.
    if busca:
        filtro["resumo"]["$regex"]   = busca
        filtro["resumo"]["$options"] = "i"

    docs = (
        col_sessoes
        .find(filtro, {"resumo": 1, "iniciada_em": 1})  # projeção: sem mensagens
        .sort("iniciada_em", -1)                          # mais recentes primeiro
        .limit(limite)
    )

    return [
        {"doc_id": d["_id"], "iniciada_em": d["iniciada_em"], "resumo": d["resumo"]}
        for d in docs
    ]


def recuperar_mensagens(doc_id: str) -> list[dict]:
    """
    Busca o array completo de mensagens de um documento específico, pelo _id.
    Usada no passo 2 — só quando o resumo deu match e você precisa do detalhe
    literal da conversa. No futuro, o doc_id virá do Qdrant.
    """
    doc = col_sessoes.find_one({"_id": doc_id}, {"mensagens": 1})
    return doc["mensagens"] if doc else []
