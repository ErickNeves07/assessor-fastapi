from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from app.perfil import consultar_perfil as consultar_perfil_no_banco


@tool
def consultar_perfil( config: RunnableConfig, busca: str = "",) -> str:
    """Consulta o PERFIL cadastrado do usuário na tela Perfil.

    Use quando precisar ancorar um conselho financeiro: renda mensal, objetivo,
    tolerância a risco e preferências informadas pelo próprio usuário na tela
    Perfil. Exemplos: "quanto faz sentido eu guardar por mês?", "o que posso
    fazer com meu dinheiro?", sugestões de investimento ou de meta.

    O user_id NÃO é fornecido pelo modelo: ele vem do contexto da requisição.

    Args:
        busca: assunto a procurar SEMANTICAMENTE nas preferências (opcional).
               Ex.: perguntar sobre "cripto" encontra a preferência
               "não quero investimento agressivo" mesmo sem a palavra aparecer.
    """
    configuravel = (config or {}).get("configurable", {})
    user_id      = configuravel.get("user_id") or configuravel.get("thread_id")

    if not user_id:
        return "Não foi possível identificar o usuário para consultar o perfil."

    perfil = consultar_perfil_no_banco(user_id, busca=busca)

    if not perfil:
        return ("Nenhum perfil cadastrado. Oriente o usuário a preencher a tela "
                "Perfil (perfil.html) antes de aconselhar sobre renda, objetivo "
                "ou tolerância a risco. NÃO invente esses dados.")

    linhas = [
        f"Renda mensal: R$ {perfil['renda_mensal']:.2f}",
        f"Objetivo: {perfil['objetivo']}",
        f"Tolerância a risco: {perfil['tolerancia_risco']}",
        f"Preferências: {perfil['preferencias']}",
    ]

    return "\n".join(linhas)


TOOLS_PERFIL = [consultar_perfil]