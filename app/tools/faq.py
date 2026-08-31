from langchain.tools import tool
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from app.config import GEMINI_API_KEY, FAQ_PDF_PATH as PDF_PATH

@tool
def faq_retriever(question: str) -> str:
    """Busca no FAQ oficial os trechos mais relevantes para responder a pergunta."""

    # Carregar o PDF e dividir em chunks
    loader = PyPDFLoader(PDF_PATH)
    docs = loader.load()
    
    splitter = RecursiveCharacterTextSplitter(chunk_size=700, chunk_overlap=150)
    texts = splitter.split_documents(docs)

    # Criar embeddings e indexar com FAISS
    embeddings = GoogleGenerativeAIEmbeddings(
        model="gemini-embedding-2-preview",
        google_api_key=GEMINI_API_KEY)
    db = FAISS.from_documents(texts, embeddings)

    # Recuperar os chunks mais relevantes para a pergunta
    results = db.similarity_search(question, k=6)

    return "\n\n".join([result.page_content for result in results])

FAQ_TOOLS = [faq_retriever]