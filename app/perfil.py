"""
=================
Modelagem — Perfil
------------------
Um documento por usuário. O user_id é a chave natural (index único): salvar de
novo com o mesmo user_id faz replace (upsert) — nunca cria um segundo perfil
nem deixa preferencias antigas sobrevivendo ao lado das novas.

Mongo (collection "perfis")               : dados estruturados + texto bruto.
Qdrant (collection "perfil_preferencias") : embedding 768d do texto livre
   (preferencias). A busca é SEMÂNTICA: "cripto" encontra "não quero nada
   agressivo" mesmo sem a palavra aparecer no texto.

Escrita
----------------
Quem escreve é SÓ a rota POST /perfil (tela Perfil). O chat apenas LÊ via a
tool consultar_perfil. Não existe tool de escrita para o agente.
"""
from datetime import datetime, timezone

from pymongo import MongoClient
from qdrant_client import models

from app.config import MONGODB_URI
from app.schemas import Perfil
from app.vectorstore import (
    qdrant,
    gerar_embedding,
    COLLECTION_PERFIL,
    EMBEDDING_DIM,
)

import uuid
# ==============================================================================
# CONEXÃO
# ==============================================================================

_mongo      = MongoClient(MONGODB_URI)
db          = _mongo["assessor"]
col_perfis  = db["perfis"]

# user_id é a chave natural: um perfil por usuário, garantido pelo index único.
col_perfis.create_index("user_id", unique=True)


def _garantir_collection() -> None:
    """Cria a collection no Qdrant se ainda não existir (idempotente)."""
    if not qdrant.collection_exists(COLLECTION_PERFIL):
        qdrant.create_collection(
            collection_name=COLLECTION_PERFIL,
            vectors_config=models.VectorParams(
                size=EMBEDDING_DIM,
                distance=models.Distance.COSINE,
            ),
        )


def _agora() -> datetime:
    return datetime.now(timezone.utc)


# ==============================================================================
# FUNÇÕES
# ==============================================================================

def salvar_perfil(perfil: Perfil) -> dict:
    """
    Grava o perfil nos DOIS bancos a partir do MESMO ponto (a rota).

    - Mongo  : replace_one com upsert por user_id (substitui o cadastro inteiro).
    - Qdrant : upsert do ponto com id=user_id (substitui embedding/payload).
    """
    _garantir_collection()

    # 1) Mongo — estruturado, consultável direto por user_id
    doc = {
        "user_id":          perfil.user_id,
        "renda_mensal":     perfil.renda_mensal,
        "objetivo":         perfil.objetivo,
        "tolerancia_risco": perfil.tolerancia_risco,
        "preferencias":     perfil.preferencias,
        "atualizada_em":    _agora(),
    }
    col_perfis.replace_one(
        {"user_id": perfil.user_id},
        doc,
        upsert=True,
    )

    # 2) Qdrant — embedding do texto livre (busca semântica das preferencias)
    vetor = gerar_embedding(perfil.preferencias)
    qdrant.upsert(
        collection_name=COLLECTION_PERFIL,
        points=[
            models.PointStruct(
                id = str(uuid.uuid5(uuid.NAMESPACE_DNS, perfil.user_id)),
                vector=vetor,
                payload={
                    "user_id":          perfil.user_id,
                    "preferencias":     perfil.preferencias,
                    "objetivo":         perfil.objetivo,
                    "tolerancia_risco": perfil.tolerancia_risco,
                },
            )
        ],
    )

    return doc


def consultar_perfil(user_id: str, busca: str = "") -> dict | None:
    """
    Lê o perfil de um usuário.

    - Sem `busca`: devolve direto do Mongo (dados estruturados).
    - Com `busca`: consulta o Qdrant filtrado por user_id e devolve a preferencia
      semanticamente mais próxima (restrição encontrada por significado).
    """
    doc = col_perfis.find_one({"user_id": user_id})
    if not doc:
        return None

    if busca:
        _garantir_collection()
        vetor = gerar_embedding(busca)
        resultados = qdrant.query_points(
            collection_name=COLLECTION_PERFIL,
            query=vetor,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id),
                    )
                ]
            ),
            limit=1,
        )

        if resultados.points:
            ponto = resultados.points[0]
            doc["preferencias"] = ponto.payload.get("preferencias", "")

    doc.pop("_id", None)
    return doc