"""
Juiz de fidelidade. Na ordem desta aula ele fala por último.
Quem decide é o JEV: choice e noul, sem redação.
A frase fixa, quando entra, é deste arquivo.
"""
import json
import math
import urllib.error
import urllib.request

from app.config import TYPESAFE_API_KEY

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODELO = "jev-latest"
LIMIAR = 0.55
FRASE_SEGURA = "Não tenho evidência para afirmar isso."

REGRAS = (
    "A pergunta do usuário também é evidência do que ele disse que fez. "
    "Somas, contagens, datas convertidas para o horário de Brasília e "
    "reformatação de valores tirados da evidência contam como apoiada. "
    "Esclarecimento, saudação, oferta de ajuda, sugestão de próximo passo "
    "e ausência de registro contam como apoiada. "
    "Opinião do usuário pode ser citada como fala dele. "
    "Valores estruturados com currency usam a moeda declarada nesse campo; "
    "para valores originais ainda não convertidos, respeite a moeda da fala ou do source_text. "
    "Moeda trocada, valores de moedas diferentes somados num total só, "
    "opinião dita como fato do sistema, número, data, telefone ou regra "
    "que não saiam da evidência contam como sem_apoio."
    " Horários com offsets diferentes podem representar o mesmo instante: "
    "12:30:00+00:00 equivale a 09:30:00-03:00, e 13:30:00+00:00 equivale a 10:30:00-03:00. "
    "Ao avaliar agenda, compare o instante, não apenas os dígitos da hora. "
    "Se query_events retornou evento no intervalo pedido, informar conflito é apoiado. "
    "Pedir outro horário e oferecer consultar alternativas são próximos passos, "
    "não afirmações de disponibilidade. "
    "Quando houver plano de operações, a resposta é um RASCUNHO condicionado à execução posterior: "
    "avalie se as ações e a resposta correspondem ao pedido, às moedas, aos valores e à ORDEM solicitada. "
    "Uma operação preparada ainda não está gravada: não exija ID real nem confunda preparação com execução. "
    "Texto de sucesso só será publicado pelo código após confirmar as ações aprovadas. "
    "Portanto, verbos como registrei, atualizei ou criei no rascunho não são, por si, falta de apoio: "
    "avalie-os como resultado hipotético da execução integral do plano, sem exigir persistência anterior. "
    "Em UPDATE, o novo registro do plano é o resultado proposto e antes é o estado original. "
    "source_text pode preservar o pedido de criação e seu valor antigo; isso não contradiz o novo valor pedido na atualização. "
    "Consultas são fotografias do passo em que ocorreram: incluem apenas operações preparadas ANTES delas. "
    "Se o usuário pede consultar e depois inserir, a lista anterior não deve incluir a inserção posterior. "
    "Conversão de moeda é apoiada quando o plano fornece valor original, taxa, fonte, data da cotação e BRL calculado. "
    "Dados consultados com currency=BRL são valores já registrados em reais; source_text preserva a fala original "
    "e não altera a moeda do campo amount retornado. Nunca aceite uma troca de moeda sem conversão comprovada no preparo. "
    "Se o plano inclui várias ações, verifique todas, inclusive os destinos Google exigidos; "
    "não aprove um rascunho vazio ou que omita partes necessárias para responder ao pedido."
)


def _texto(mensagem) -> str:
    conteudo = getattr(mensagem, "content", "")
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        pedacos = []
        for parte in conteudo:
            if isinstance(parte, str):
                pedacos.append(parte)
            elif isinstance(parte, dict):
                pedacos.append(str(parte.get("text") or ""))
        return "\n".join(p for p in pedacos if p)
    return str(conteudo or "")


def _coleta(estado):
    pergunta = ""
    resposta = ""
    evidencias = []
    for mensagem in estado["messages"]:
        tipo = getattr(mensagem, "type", "")
        texto = _texto(mensagem).strip()
        if tipo == "human" and texto:
            pergunta = texto
            evidencias = []
        elif tipo == "tool" and texto:
            nome = getattr(mensagem, "name", "") or "tool"
            evidencias.append(f"[{nome}]\n{texto}")
        elif tipo == "ai":
            for chamada in getattr(mensagem, "tool_calls", None) or []:
                argumentos = json.dumps(chamada.get("args", {}), ensure_ascii=False)
                evidencias.append(f"[chamada {chamada.get('name')}]\n{argumentos}")
            if texto:
                resposta = texto
    evidencia = "\n\n".join(evidencias).strip()
    if not evidencia:
        evidencia = "(nenhuma tool devolveu conteúdo neste turno)"
    return pergunta, resposta, evidencia


def _publicar(texto, rotulo, resposta_original, aprovado=False, tecnico=False):
    print(f"[juiz] {rotulo}")
    resultado = {"agentes_chamados": [f"juiz:{rotulo}"],
                 "veredito_juiz": {"aprovado": aprovado, "motivo": rotulo, "tecnico": tecnico}}
    if texto.strip() == (resposta_original or "").strip():
        return resultado
    return {**resultado, "messages": [{"role": "assistant", "content": texto.strip()}]}


def _julgar_jev(pergunta, resposta, evidencia):
    if not TYPESAFE_API_KEY:
        return _publicar(FRASE_SEGURA, "sem_chave", resposta, tecnico=True)
    corpo = json.dumps({
        "model": JEV_MODELO,
        "state": {
            "pergunta": pergunta,
            "resposta": resposta,
            "evidencia": evidencia,
            "regras": REGRAS,
        },
        "questions": {
            "veredito": {
                "type": "choice",
                "instructions": "Seguindo as regras, a resposta só afirma o que a evidência sustenta?",
                "criteria": {
                    "apoiada": "toda afirmação factual aparece na evidência",
                    "parcial": "parte aparece e parte não",
                    "sem_apoio": "há fato, número, regra ou opinião sem evidência",
                },
            },
            "apoiada": {
                "type": "noul",
                "instructions": (
                    "Seguindo as regras, a resposta é fiel à evidência: todo valor, moeda, "
                    "data e fato dela sai da evidência, da fala do usuário ou de contas sobre elas?"
                ),
            },
        },
    }).encode()
    pedido = urllib.request.Request(
        JEV_URL,
        data=corpo,
        headers={
            "Authorization": f"Bearer {TYPESAFE_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resp:
            dados = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return _publicar(FRASE_SEGURA, f"erro http {exc.code}", resposta, tecnico=True)
    respostas = dados["answers"]
    choice = respostas["veredito"]["choice"]
    noul = float(respostas["apoiada"]["noul"])
    if choice not in ("apoiada", "parcial", "sem_apoio") or not math.isfinite(noul) or not 0 <= noul <= 1:
        raise ValueError("Veredito do juiz inválido.")
    if choice == "apoiada" and noul >= LIMIAR:
        return _publicar(resposta, f"apoiada noul={noul:.2f}", resposta, aprovado=True)
    rotulo = "apoiada:abaixo_do_limiar" if choice == "apoiada" else choice
    return _publicar(FRASE_SEGURA, f"{rotulo} noul={noul:.2f}", resposta)


def no_juiz(estado: dict) -> dict:
    pergunta, resposta, evidencia = _coleta(estado)
    plano = estado.get("plano_turno") or {}
    if plano:
        evidencia += "\n\n[plano completo, somente preparado]\n" + json.dumps(plano, ensure_ascii=False)
    try:
        if not resposta.strip() or resposta.strip() in ("(sem resposta)", FRASE_SEGURA):
            resultado = _publicar(FRASE_SEGURA, "sem_resposta_valida", resposta)
        elif len(evidencia) > 100000:
            resultado = _publicar("O pedido ficou extenso demais para avaliar com segurança. Divida em pedidos menores.", "evidencia_extensa", resposta)
        else:
            resultado = _julgar_jev(pergunta, resposta, evidencia)
    except Exception as exc:
        print(f"[juiz] erro {exc}")
        resultado = _publicar(FRASE_SEGURA, "erro", resposta, tecnico=True)
    resultado["veredito_juiz"].update(turno_id=plano.get("turno_id"), hash_plano=plano.get("hash"))
    return resultado
