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
    - Tables Primaires (Score < 0.99) : Schéma complet
    - Tables Secondaires (0.99 < Score < 0.99) : Uniquement colonnes
    """
    if k is None:
        k = int(os.getenv("TOP_K_TABLES", 5))

    # --- Heuristiques Métier (Keyword Matching) ---
    q_lower = question.lower()
    
    force_villes = any(kw in q_lower for kw in ["gouvernorat", "ville", "tunis", "region", "région", "sfax", "sousse", "nabeul"])
    force_sales = any(kw in q_lower for kw in ["vente", "ca", "chiffre", "affaires", "revenu", "commande", "quantite", "quantité", "prix", "panier", "facture"])
    force_boutiques = any(kw in q_lower for kw in ["boutique", "magasin", "point de vente"])
    force_clients = any(kw in q_lower for kw in ["client", "fidelite", "fidélité", "points"])
    force_stocks = any(kw in q_lower for kw in ["stock", "depot", "dépôt", "entrepot", "entrepôt", "inventaire"])
    force_produits = any(kw in q_lower for kw in ["produit", "article", "reference", "référence", "marque", "categorie", "catégorie"])
    force_brands = any(kw in q_lower for kw in ["marque", "brand"])
    force_categories = any(kw in q_lower for kw in ["categorie", "catégorie", "rayon", "famille", "sous-categorie", "sous-catégorie"])

    if force_villes or force_sales or force_boutiques or force_clients or force_stocks or force_produits or force_brands or force_categories:
        k = max(k, 12) # Augmente K pour ratisser plus large avec FAISS

    vs = _load_index()
    # Utiliser le préfixe standard E5 pour les requêtes
    prefixed_question = f"query: {question}"
    docs_and_scores = vs.similarity_search_with_score(prefixed_question, k=k)

    primary_blocks = []
    secondary_blocks = []
    seen_tables = set()
    
    PRIMARY_THRESHOLD = 0.99 
    SECONDARY_THRESHOLD = 0.99

    promo_keywords = ["promo", "solde", "remise", "reduction", "réduction", "offre", "black friday", "ramadan", "fête des mères", "fete des meres", "aïd", "aid", "destockage", "déstockage"]
    has_promo_in_question = any(k in q_lower for k in promo_keywords)

    for doc, score in docs_and_scores:
        table = doc.metadata.get("table")
        if table:
            table_lower = table.lower()
            if table_lower in seen_tables: continue
            if table_lower in ["promotions", "produits_promos"] and not has_promo_in_question:
                continue
            seen_tables.add(table_lower)
        
        # Filtrage par score (très tolérant pour ne rien perdre d'utile)
        if score > SECONDARY_THRESHOLD:
            continue
            
        if score <= PRIMARY_THRESHOLD:
            primary_blocks.append(doc.page_content)
        else:
            columns = ", ".join(doc.metadata.get("columns", []))
            reduced = f"TABLE: {table} (Colonnes: {columns})"
            secondary_blocks.append(reduced)

    # --- Injection garantie des tables critiques via heuristics ---
    injections = []
    if force_villes: injections.append("villes_tunisie")
    if force_sales:
        injections.append("commandes_ventes")
        injections.append("lignes_ventes")
    if force_boutiques: injections.append("boutiques")
    if force_clients: injections.append("clients")
    if force_stocks:
        injections.append("stocks")
        injections.append("depots")
    if force_produits: injections.append("produits")
    if force_brands: injections.append("marques")
    if force_categories:
        injections.append("categories")
        injections.append("sous_categories")

    for t_name in injections:
        if t_name not in seen_tables:
            docs = vs.similarity_search(f"query: {t_name}", k=5)
            for d in docs:
                if d.metadata.get("table", "").lower() == t_name:
                    primary_blocks.append(d.page_content)
                    seen_tables.add(t_name)
                    break

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
        if score > 0.75: continue
        
        if "table" in doc.metadata:
            tables.add(doc.metadata["table"].lower())
        if doc.metadata.get("type") == "join_path":
            if "from" in doc.metadata: tables.add(doc.metadata["from"].lower())
            if "to" in doc.metadata: tables.add(doc.metadata["to"].lower())
            
    q_lower = question.lower()
    
    if any(kw in q_lower for kw in ["gouvernorat", "ville", "tunis", "region", "région", "sfax", "sousse", "nabeul"]):
        tables.add("villes_tunisie")
    if any(kw in q_lower for kw in ["vente", "ca", "chiffre", "affaires", "revenu", "commande", "quantite", "quantité", "prix", "panier", "facture"]):
        tables.add("commandes_ventes")
        tables.add("lignes_ventes")
    if any(kw in q_lower for kw in ["boutique", "magasin", "point de vente"]):
        tables.add("boutiques")
    if any(kw in q_lower for kw in ["client", "fidelite", "fidélité", "points"]):
        tables.add("clients")
    if any(kw in q_lower for kw in ["stock", "depot", "dépôt", "entrepot", "entrepôt", "inventaire"]):
        tables.add("stocks")
        tables.add("depots")
    if any(kw in q_lower for kw in ["produit", "article", "reference", "référence", "marque", "categorie", "catégorie"]):
        tables.add("produits")
        
    promo_keywords = ["promo", "solde", "remise", "reduction", "réduction", "offre", "black friday", "ramadan", "fête des mères", "fete des meres", "aïd", "aid", "destockage", "déstockage"]
    has_promo_in_question = any(k in q_lower for k in promo_keywords)
    if not has_promo_in_question:
        tables = {t for t in tables if t not in ["promotions", "produits_promos"]}
            
    return list(tables)
