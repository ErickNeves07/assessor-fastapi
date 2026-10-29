from dataclasses import dataclass, field

from app.planejamento import dados_json
from app.tools.calendario_google import criar_google_aprovado
from app.tools.db import get_conn


COLUNAS_FINANCEIRO = "id, amount, type, category_id, description, payment_method, occurred_at, source_text"


@dataclass
class ResultadoExecucao:
    status: str
    mensagem: str = ""
    itens: list[dict] = field(default_factory=list)
    tecnico: bool = False


def _fechar(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _cancelar(conn):
    if conn is not None:
        try:
            conn.rollback()
        except Exception:
            pass


def _commit_incerto(exc, iniciado):
    codigo = getattr(exc, "pgcode", None)
    return iniciado and (not codigo or codigo.startswith("08"))


def _financeiro(operacoes):
    conn, commit_iniciado = None, False
    referencias, itens = {}, []
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            for op in operacoes:
                registro = dict(op.dados["registro"])
                campos = COLUNAS_FINANCEIRO.split(", ")[1:]
                valores = [registro[c] for c in campos]
                if op.tipo == "add_transaction":
                    cur.execute("""INSERT INTO transactions
                        (amount,type,category_id,description,payment_method,occurred_at,source_text)
                        VALUES (%s,%s,%s,%s,%s,%s::timestamptz,%s) RETURNING id""", valores)
                    identificador = cur.fetchone()[0]
                    referencias[op.referencia] = identificador
                else:
                    alvo = op.dados.get("referencia_alvo")
                    identificador = referencias.get(alvo) if alvo else op.dados["id"]
                    if identificador is None:
                        raise ValueError("A referência da alteração não foi confirmada neste plano.")
                    cur.execute(f"SELECT {COLUNAS_FINANCEIRO} FROM transactions WHERE id=%s FOR UPDATE", (identificador,))
                    atual = cur.fetchone()
                    if atual is None:
                        raise ValueError("O lançamento deixou de existir antes da confirmação. Consulte novamente.")
                    atual = dados_json(dict(zip(COLUNAS_FINANCEIRO.split(", "), atual)))
                    anterior = op.antes
                    if any(atual[c] != anterior[c] for c in campos):
                        raise ValueError("O lançamento mudou desde a preparação. Consulte novamente antes de alterar.")
                    cur.execute("""UPDATE transactions SET amount=%s,type=%s,category_id=%s,
                        description=%s,payment_method=%s,occurred_at=%s::timestamptz,source_text=%s
                        WHERE id=%s""", [*valores, identificador])
                    if cur.rowcount != 1:
                        raise ValueError("A alteração não confirmou exatamente um lançamento.")
                itens.append({"referencia": op.referencia, "operacao": op.tipo, "id": identificador, "banco": "confirmado"})
        commit_iniciado = True
        conn.commit()
        return ResultadoExecucao("ok", itens=itens)
    except Exception as exc:
        _cancelar(conn)
        incerto = _commit_incerto(exc, commit_iniciado)
        detalhe = str(exc) if isinstance(exc, ValueError) else "Houve uma falha no serviço de dados."
        mensagem = "Não consegui confirmar se os lançamentos foram salvos. Consulte os registros antes de repetir." if incerto else f"Não consegui concluir os lançamentos; nenhuma alteração desta transação foi confirmada. {detalhe}"
        return ResultadoExecucao("incerto" if incerto else "erro", mensagem, tecnico=not isinstance(exc, ValueError))
    finally:
        _fechar(conn)


def _evento(op, google):
    conn, commit_iniciado, google_confirmado = None, False, False
    google_id = None
    nome = op.dados["title"]
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (1729085561,))
            cur.execute("""SELECT id FROM events WHERE start_time < %s::timestamptz
                AND COALESCE(end_time,start_time + INTERVAL '1 hour') > %s::timestamptz LIMIT 1""",
                (op.dados["end_time"], op.dados["start_time"]))
            if cur.fetchone():
                raise ValueError("Surgiu um conflito na agenda antes da confirmação. Consulte outro horário.")
            cur.execute("""INSERT INTO events (title,start_time,end_time,location,notes,source_text)
                VALUES (%s,%s::timestamptz,%s::timestamptz,%s,%s,%s) RETURNING id""",
                tuple(op.dados[c] for c in ("title", "start_time", "end_time", "location", "notes", "source_text")))
            identificador = cur.fetchone()[0]
            if google is not None:
                payload = {c: google.dados.get(c) for c in ("title", "start_time", "end_time", "location", "description")}
                try:
                    resultado = criar_google_aprovado(**payload)
                except Exception:
                    resultado = {"status": "incerto", "message": "A chamada Google terminou sem confirmação."}
                if resultado.get("status") != "ok" or not resultado.get("id"):
                    _cancelar(conn)
                    incerto = resultado.get("status") != "error"
                    texto = f"Não consegui confirmar a criação de '{nome}' no Google. O registro local foi cancelado. Confira o calendário antes de repetir." if incerto else f"Não consegui criar '{nome}' no Google; o registro local foi cancelado. {resultado.get('message', '')}"
                    item = {"referencia": op.referencia, "title": nome, "banco": "cancelado", "google": "incerto" if incerto else "falhou"}
                    return ResultadoExecucao("incerto" if incerto else "erro", texto, [item], True)
                google_confirmado = True
                google_id = resultado["id"]
        commit_iniciado = True
        conn.commit()
        return ResultadoExecucao("ok", itens=[{"referencia": op.referencia, "id": identificador,
            "title": nome, "banco": "confirmado", "google": "confirmado" if google_confirmado else "não solicitado",
            "google_id": google_id}])
    except Exception as exc:
        _cancelar(conn)
        incerto = _commit_incerto(exc, commit_iniciado)
        if google_confirmado:
            texto = f"O Google confirmou '{nome}', mas {'não consegui confirmar o registro local' if incerto else 'a gravação local falhou'}. Consulte a agenda local antes de repetir. Se desejar, solicite somente a inserção local, sem criar novamente no Google."
        elif incerto:
            texto = f"Não consegui confirmar o registro local de '{nome}'. Consulte a agenda antes de repetir."
        else:
            detalhe = str(exc) if isinstance(exc, ValueError) else "Houve uma falha no serviço de dados."
            texto = f"Não consegui salvar '{nome}'. Nenhum novo evento deste item foi confirmado. {detalhe}"
        item = {"referencia": op.referencia, "title": nome, "banco": "incerto" if incerto else "cancelado",
                "google": "confirmado" if google_confirmado else "não chamado", "google_id": google_id}
        return ResultadoExecucao("incerto" if incerto else "erro", texto, [item], not isinstance(exc, ValueError))
    finally:
        _fechar(conn)


def executar_plano(plano, veredito):
    if veredito.get("aprovado") is not True:
        return ResultadoExecucao("recusado", tecnico=veredito.get("tecnico", False))
    if veredito.get("turno_id") != plano.turno_id or veredito.get("hash_plano") != plano.hash_atual() or not plano.congelado:
        return ResultadoExecucao("erro", "O plano não corresponde à aprovação do juiz. Nenhuma operação foi executada.", tecnico=True)
    if plano.consumido:
        return ResultadoExecucao("erro", "Este plano já foi processado; nenhuma operação foi repetida.", tecnico=True)
    plano.consumido = True
    if plano.erros:
        return ResultadoExecucao("erro", "Não foi possível preparar todas as operações. " + plano.erros[0]["mensagem"], tecnico=any(e["tecnico"] for e in plano.erros))
    google_por_evento = {}
    eventos = {op.referencia: op for op in plano.operacoes if op.tipo == "add_event"}
    for op in plano.operacoes:
        if op.tipo == "add_google_event":
            ref = op.dados.get("evento")
            if ref not in eventos or ref in google_por_evento:
                return ResultadoExecucao("erro", "O plano contém uma criação Google sem correspondência única com um evento local.", tecnico=True)
            evento = eventos[ref]
            campos = (("title", "title"), ("start_time", "start_time"), ("end_time", "end_time"), ("location", "location"), ("description", "notes"))
            if any(op.dados.get(a) != evento.dados.get(b) for a, b in campos):
                return ResultadoExecucao("erro", "Os dados locais e Google do plano não correspondem.", tecnico=True)
            google_por_evento[ref] = op
    if any(op.dados.get("google_necessario") != (ref in google_por_evento) for ref, op in eventos.items()):
        return ResultadoExecucao("erro", "O plano não contém todos os destinos exigidos para os eventos.", tecnico=True)
    itens = []
    financeiros = [op for op in plano.operacoes if op.tipo in ("add_transaction", "update_transaction")]
    if financeiros:
        resultado = _financeiro(financeiros)
        if resultado.status != "ok":
            return resultado
        itens.extend(resultado.itens)
    for ref, op in eventos.items():
        resultado = _evento(op, google_por_evento.get(ref))
        if resultado.status != "ok":
            anteriores = [i["title"] for i in itens if i.get("title") and i["banco"] == "confirmado"]
            prefixo = "Eventos anteriores confirmados e preservados: " + ", ".join(anteriores) + ". " if anteriores else ""
            posteriores = list(eventos)[list(eventos).index(ref) + 1:]
            sufixo = " Os eventos seguintes não foram tentados." if posteriores else ""
            limite = " Não tenho ferramentas para alterar ou excluir eventos já cadastrados no Google; somente inserir." if anteriores else ""
            return ResultadoExecucao("parcial" if itens else resultado.status, prefixo + resultado.mensagem + sufixo + limite, itens + resultado.itens, resultado.tecnico)
        itens.extend(resultado.itens)
    return ResultadoExecucao("ok", itens=itens)
