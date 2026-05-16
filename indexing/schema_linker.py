"""
indexing/schema_linker.py
Charge l'index FAISS et retourne les top-k tables pertinentes
pour une question utilisateur en français.
"""
import os
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings

load_dotenv()

_vectorstore = None


def _load_index():
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    index_path = os.getenv("FAISS_INDEX_PATH", "./indexing/faiss_index")
    if not os.path.exists(index_path):
        raise FileNotFoundError(
            f"Index FAISS non trouvé : {index_path}\n"
            "Exécuter d'abord : python indexing/schema_indexer.py"
        )

    embedding_model = HuggingFaceEmbeddings(
        model_name=os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-base"),
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    _vectorstore = FAISS.load_local(
        index_path,
        embedding_model,
        allow_dangerous_deserialization=True,
    )
    print("[schema_linker] Index FAISS chargé.")
    return _vectorstore


def get_relevant_schema(question: str, k: int = None) -> str:
    """
    Retourne un schéma filtré par score et réduit pour les tables secondaires.
    - Tables Primaires (Score < 0.5) : Schéma complet
    - Tables Secondaires (0.5 < Score < 0.65) : Uniquement colonnes
    """
    if k is None:
        k = int(os.getenv("TOP_K_TABLES", 5))

    vs = _load_index()
    # FAISS avec E5 retourne souvent des distances L2. Plus petit = plus proche.
    docs_and_scores = vs.similarity_search_with_score(question, k=k)

    primary_blocks = []
    secondary_blocks = []
    seen_tables = set()
    
    # Seuils de distance (à ajuster selon les tests)
    PRIMARY_THRESHOLD = 0.60 
    SECONDARY_THRESHOLD = 0.75

    for doc, score in docs_and_scores:
        table = doc.metadata.get("table")
        if table:
            if table in seen_tables: continue
            seen_tables.add(table)
        
        # Filtrage par score
        if score > SECONDARY_THRESHOLD:
            continue
            
        if score <= PRIMARY_THRESHOLD:
            primary_blocks.append(doc.page_content)
        else:
            # Schéma réduit pour les tables secondaires (on extrait juste les colonnes)
            columns = ", ".join(doc.metadata.get("columns", []))
            reduced = f"TABLE: {table} (Colonnes: {columns})"
            secondary_blocks.append(reduced)

    context = ""
    if primary_blocks:
        context += "### TABLES PRINCIPALES (Détails complets):\n" + "\n\n".join(primary_blocks)
    if secondary_blocks:
        context += "\n\n### TABLES CONNEXES (Colonnes uniquement):\n" + "\n".join(secondary_blocks)
        
    return context.strip()


def get_relevant_tables(question: str, k: int = None) -> list[str]:
    """Retourne uniquement les noms de tables pertinentes (filtrées par score)."""
    if k is None:
        k = int(os.getenv("TOP_K_TABLES", 5))
    vs = _load_index()
    docs_and_scores = vs.similarity_search_with_score(question, k=k)
    
    tables = set()
    for doc, score in docs_and_scores:
        if score > 0.75: continue # Même seuil que SECONDARY_THRESHOLD
        
        if "table" in doc.metadata:
            tables.add(doc.metadata["table"])
        if doc.metadata.get("type") == "join_path":
            if "from" in doc.metadata: tables.add(doc.metadata["from"].lower())
            if "to" in doc.metadata: tables.add(doc.metadata["to"].lower())
            
    return list(tables)
