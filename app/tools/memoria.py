from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from app.memory import recuperar_historico

@tool
def buscar_historico(busca: str, config: RunnableConfig) -> str:
    """Consulta conversas ANTERIORES do usuário (sessões já encerradas)."""
    configuravel = (config or {}).get("configurable", {})
    user_id = configuravel.get("user_id") or configuravel.get("thread_id")

    if not user_id:
        return "Não foi possível identificar o usuário para buscar o histórico."

    historico = recuperar_historico(user_id, busca=busca, limite=3)

    if not historico:
        return "Nenhuma conversa anterior relevante encontrada."

    return "\n\n".join(
        f"[{h['iniciada_em']:%d/%m/%Y}] {h['resumo']}" for h in historico
    )

TOOLS_MEMORIA = [buscar_historico]