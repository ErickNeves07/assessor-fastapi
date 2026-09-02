from fastapi import APIRouter
from app.schemas import ChatRequest, ChatResponse
from app.graph import executar_fluxo_assessor


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def conversar(requisicao: ChatRequest) -> ChatResponse:
    """Uma mensagem do usuário, uma resposta do assessor."""
    resposta = executar_fluxo_assessor(
        requisicao.pergunta, requisicao.session_id, requisicao.user_id
    )
    return ChatResponse(resposta=resposta)