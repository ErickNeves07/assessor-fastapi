from pydantic import BaseModel, Field
from typing import Optional

class ChatRequest(BaseModel):
    """O que o navegador envia no POST /chat."""
    session_id: str = Field(..., examples=["id_usuario"])
    pergunta:   str = Field(..., min_length=1, examples=["gastei 50 reais no mercado"])


class ChatResponse(BaseModel):
    """O que a API devolve no POST /chat."""
    resposta:         str
    # agentes_chamados: list[str]


class SessionResponse(BaseModel):
    """O que a API devolve no POST /sessions/{session_id}/encerrar."""
    session_id: str
    resumo:     str | None = None