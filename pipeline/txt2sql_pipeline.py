"""
pipeline/txt2sql_pipeline.py
Pipeline complet : question FR → schéma RAG → prompt → SQL → exécution → autocorrection
"""
import os
import re
import logging
import psycopg2
from dotenv import load_dotenv
from indexing.schema_linker import get_relevant_schema
from pipeline.model_loader import generate_sql

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "Tu es un expert SQL pour un ERP de cosmétiques. Ta mission : générer du SQL PostgreSQL précis et minimaliste.\n\n"
    "⚠️ STRUCTURE CRITIQUE (NE PAS SE TROMPER) :\n"
    "- La table `commandes_ventes` n'a PAS de `produit_id`. \n"
    "- Pour lier ventes et produits : `commandes_ventes` (id) -> `lignes_ventes` (commande_id/produit_id) -> `produits` (id).\n"
    "- Pour les prix : utilise `lignes_ventes.prix_unitaire_applique`.\n\n"
    "CONSIGNES :\n"
    "1. OCCAM'S RAZOR : N'utilise que les tables nécessaires. Pas de jointures inutiles.\n"
    "2. RÉFÉRENTIEL DATES : La colonne `date_vente` n'existe QUE dans `commandes_ventes`. Elle n'existe PAS dans `lignes_ventes`.\n"
    "3. NOMENCLATURE : Tables et colonnes en MINUSCULES (ex: `quantite` et non `qte_vendue`).\n\n"
    "EXEMPLES :\n"
    "Q: CA total par année ?\n"
    "SQL: SELECT EXTRACT(YEAR FROM date_vente) AS annee, SUM(total_ttc) FROM commandes_ventes GROUP BY annee;\n\n"
    "Q: Total dépenses Cosmétix Ariana en 2024 ?\n"
    "SQL: SELECT SUM(montant) FROM depenses_fixes df JOIN boutiques b ON df.boutique_id = b.id WHERE b.nom_boutique = 'Cosmétix Ariana' AND EXTRACT(YEAR FROM date_depense) = 2024;\n\n"
    "Q: Top 5 produits vendus ?\n"
    "SQL: SELECT p.nom_produit, SUM(lv.quantite) FROM produits p JOIN lignes_ventes lv ON p.id = lv.produit_id GROUP BY p.id ORDER BY 2 DESC LIMIT 5;\n\n"
    "RÉPONDS UNIQUEMENT PAR LE SQL."
)

# Température progressive par tentative (0.0=précis, 0.9=créatif/alternatif)
TEMPERATURES = [0.0, 0.5, 0.9]


def _simplify_error(error: str) -> str:
    """Extrait le cœur du message d'erreur PostgreSQL pour aider le modèle."""
    clean = str(error)
    # Enlève le préfixe technique
    clean = re.sub(r'^(?:[A-Z]+:\s+|psycopg2\.errors\.[a-zA-Z]+\s*)', '', clean)
    # Garde seulement la première ligne
    clean = clean.split('\n')[0]
    # Enlève les indications de position
    clean = re.sub(r'(?:Ligne|LINE)\s+\d+:.*', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def _build_messages(question: str, schema_context: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_INSTRUCTION},
        {"role": "user", "content": f"Schéma ERP:\n{schema_context}\n\nQuestion: {question}"}
    ]


def _build_correction_messages(question: str, schema_context: str,
                                history: list[dict]) -> list[dict]:
    """Messages de correction pour Qwen."""
    last_failure = history[-1]
    simple_error = _simplify_error(last_failure['error'])
    
    return [
        {"role": "system", "content": SYSTEM_INSTRUCTION},
        {"role": "user", "content": f"Schéma ERP:\n{schema_context}\n\nQuestion: {question}"},
        {"role": "assistant", "content": last_failure['sql']},
        {"role": "user", "content": f"ERREUR SQL : Cette requête a échoué avec le message suivant :\n\"{simple_error}\"\n\nAnalyse à nouveau le schéma ERP ci-dessus et propose une correction valide. Assure-toi de ne plus utiliser la colonne ou la table qui a causé l'erreur."}
    ]


def _clean_sql(raw: str) -> str:
    """Nettoie la sortie brute du modèle pour extraire uniquement le SQL."""
    raw = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*", "", raw)
    # Cherche SELECT ... ;
    match = re.search(r"(SELECT[\s\S]+?;)", raw, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Si pas de ; cherche juste SELECT ...
    match = re.search(r"(SELECT[\s\S]+)", raw, re.IGNORECASE)
    if match:
        sql = match.group(1).strip()
        if not sql.endswith(";"):
            sql += ";"
        return sql
    return raw.strip()


def _get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "entreprise_erp"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "root"),
        options="-c default_transaction_read_only=on",
    )


def _execute_sql(sql: str) -> tuple[bool, any, str]:
    try:
        conn = _get_db_connection()
        cur  = conn.cursor()
        cur.execute(sql)
        results = cur.fetchmany(100)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        cur.close()
        conn.close()
        return True, {"columns": columns, "rows": results}, ""
    except Exception as e:
        return False, None, str(e)


def run(question: str) -> dict:
    """
    Pipeline avec RAG dynamique et correction intelligente.
    """
    max_retries = int(os.getenv("MAX_RETRIES", 1))
    if max_retries <= 1:
        log.info("ℹ️ Mode Sécurisé : Autocorrection désactivée (max_retries=1).")
    
    log.info(f"🚀 Question reçue : {question}")

    failed_attempts = []
    current_k = int(os.getenv("TOP_K_TABLES", 5))

    for attempt in range(1, max_retries + 1):
        # RAG dynamique : on élargit la recherche de tables si on échoue
        schema_context = get_relevant_schema(question, k=current_k)
        from indexing.schema_linker import get_relevant_tables
        relevant_tables = get_relevant_tables(question, k=current_k)
        log.info(f"🔍 [Schema Linker] Tables sélectionnées : {relevant_tables}")
        
        if attempt == 1:
            messages = _build_messages(question, schema_context)
        else:
            messages = _build_correction_messages(question, schema_context, failed_attempts)

        temperature = TEMPERATURES[min(attempt-1, len(TEMPERATURES) - 1)]
        raw_sql = generate_sql(messages, temperature=temperature)
        sql = _clean_sql(raw_sql)
        
        log.info(f"Tentative {attempt} (k={current_k}, temp={temperature}) :\n{sql}")
        
        success, results, error = _execute_sql(sql)

        if success:
            log.info(f"SQL valide à la tentative {attempt}")
            return {
                "question":      question,
                "sql":           sql,
                "success":       True,
                "results":       results,
                "error":         None,
                "attempts":      attempt,
                "schema_tables": schema_context,
                "failed":        failed_attempts,
            }

        simple_err = _simplify_error(error)
        log.warning(f"Échec tentative {attempt} : {simple_err}")
        failed_attempts.append({"sql": sql, "error": error})
        
        # On augmente k pour donner plus de contexte au prochain tour
        current_k += 2 

    log.error(f"Échec final après {max_retries} tentatives.")
    return {
        "question":      question,
        "sql":           sql if 'sql' in locals() else None,
        "success":       False,
        "results":       None,
        "error":         _simplify_error(failed_attempts[-1]["error"]) if failed_attempts else "Erreur inattendue lors de l'exécution.",
        "attempts":      max_retries,
        "schema_tables": schema_context if 'schema_context' in locals() else None,
        "failed":        failed_attempts,
    }
