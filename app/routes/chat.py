from fastapi import APIRouter, HTTPException
from groq import APIError as GroqAPIError
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
from app.schemas import ChatRequest, ChatResponse
from app.graph import executar_fluxo_assessor


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def conversar(requisicao: ChatRequest) -> ChatResponse:
    """Uma mensagem do usuário, uma resposta do assessor."""
    try:
        resposta = executar_fluxo_assessor(
            requisicao.pergunta, requisicao.session_id, requisicao.user_id
        )
    except (ChatGoogleGenerativeAIError, GroqAPIError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Os modelos de IA não conseguiram concluir a resposta. "
                   "Confira se a operação já foi registrada antes de repetir o pedido.",
        ) from exc
    return ChatResponse(resposta=resposta)
