"""
Rota de escrita do Perfil — consumida pela tela frontend/perfil.html.

Só existe POST (escrita). Não há leitura, listagem nem exclusão: a API responde
com o perfil gravado para confirmar o salvamento.

A validação acontece pelo schema Perfil (Pydantic): renda_mensal > 0,
objetivo obrigatório, tolerancia_risco em {baixa, media, alta}. Dado inválido
devolve 422 automaticamente — nunca vira erro interno.
"""

from fastapi import APIRouter

from app.perfil import salvar_perfil
from app.schemas import Perfil, PerfilResponse

router = APIRouter(prefix="/perfil", tags=["perfil"])


@router.post("", response_model=PerfilResponse)
def gravar_perfil(perfil: Perfil) -> PerfilResponse:
    """
    Cria ou atualiza o perfil do usuário.

    O mesmo user_id sobrescreve o cadastro anterior (upsert nos dois bancos).
    Responde com o perfil gravado.
    """
    gravado = salvar_perfil(perfil)
    return PerfilResponse(
        user_id=gravado["user_id"],
        renda_mensal=gravado["renda_mensal"],
        objetivo=gravado["objetivo"],
        tolerancia_risco=gravado["tolerancia_risco"],
        preferencias=gravado["preferencias"],
    )