from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import chat, sessions
from fastapi.middleware.cors import CORSMiddleware
from app.config import validar_config, FRONTEND_DIR


app = FastAPI(
    title="Assessor IA",
    description="Assessor financeiro e de agenda com LangChain e LangGraph.",
    version="0.1.0",
)

@app.get("/health")
def health() -> dict:
    problemas = validar_config()
    return {
        "status": "ok" if not problemas else "atencao",
        "problemas_de_configuracao": problemas,
    }

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(sessions.router)

# ==============================================================================
# FRONTEND
# Qualquer rota não casada acima cai aqui: serve index.html, app.js, style.css.
# A ordem importa — o mount "/" captura tudo que não casou antes, então ele
# precisa vir DEPOIS dos routers, senão engoliria as rotas /chat e /sessions.
# ==============================================================================
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")