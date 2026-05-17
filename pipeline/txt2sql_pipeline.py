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
    "Tu es un expert SQL pour un ERP de cosmétiques. Ta mission : générer du SQL PostgreSQL précis, optimal et minimaliste.\n\n"
    "⚠️ STRUCTURE CRITIQUE & RÈGLES DE JONCTION (TRÈS IMPORTANT) :\n"
    "- NE JAMAIS JOINDRE LES TABLES `stocks` OU `depots` pour des questions sur les ventes, le chiffre d'affaires (CA), les revenus ou les quantités vendues. Les ventes sont enregistrées uniquement dans `commandes_ventes` et `lignes_ventes`.\n"
    "- Pour relier les ventes à la géographie (gouvernorat ou ville) : fais la jointure suivante :\n"
    "  `commandes_ventes cv JOIN clients c ON cv.client_id = c.id JOIN villes_tunisie v ON c.ville_id = v.id`\n"
    "  (ou via `boutiques` si la question mentionne explicitement des boutiques).\n"
    "- Filtres géographiques (ex: Sfax, Tunis, Sousse) : utilise TOUJOURS la colonne `villes_tunisie.gouvernorat` ou `villes_tunisie.nom_ville` (ex: `v.gouvernorat = 'Sfax'`). Ne filtre JAMAIS sur des noms d'entrepôts ou de boutiques pour des critères géographiques.\n"
    "- Pour lier ventes et produits : `commandes_ventes` (id) -> `lignes_ventes` (commande_id/produit_id) -> `produits` (id).\n"
    "- Pour les prix : utilise `lignes_ventes.prix_unitaire_applique`.\n"
    "- Traite 'région' et 'gouvernorat' de la même manière. La colonne dans `villes_tunisie` est strictement nommée `gouvernorat`.\n"
    "- Pour les quantités : utilise TOUJOURS `quantite` (ex: `lignes_ventes.quantite`). N'utilise JAMAIS `qte` ni `qte_vendue`.\n"
    "- Le chiffre d'affaires (CA) ou ventes = `commandes_ventes.total_ttc` ou la somme de `lignes_ventes.quantite * lignes_ventes.prix_unitaire_applique`.\n"
    "- OPTIMISATION : Si on demande 'par boutique' ou 'par client' sans demander spécifiquement le nom, utilise directement `commandes_ventes.boutique_id` ou `commandes_ventes.client_id` pour éviter des jointures inutiles.\n\n"
    "CONSIGNES :\n"
    "1. OCCAM'S RAZOR : N'utilise que les tables nécessaires. Pas de jointures superflues.\n"
    "2. RÉFÉRENTIEL DATES : La colonne `date_vente` n'existe QUE dans `commandes_ventes`. Elle n'existe PAS dans `lignes_ventes`.\n"
    "3. NOMENCLATURE : Tables et colonnes en MINUSCULES.\n\n"
    "EXEMPLES PRÉCIS :\n"
    "Q: CA total par année ?\n"
    "SQL: SELECT EXTRACT(YEAR FROM date_vente) AS annee, SUM(total_ttc) FROM commandes_ventes GROUP BY annee;\n\n"
    "Q: Total ventes dans Sfax par ville ?\n"
    "SQL: SELECT v.nom_ville, SUM(cv.total_ttc) AS ca FROM commandes_ventes cv JOIN clients c ON cv.client_id = c.id JOIN villes_tunisie v ON c.ville_id = v.id WHERE v.gouvernorat = 'Sfax' AND cv.statut_commande = 'Livrée' GROUP BY v.nom_ville;\n\n"
    "Q: Top 5 produits vendus ?\n"
    "SQL: SELECT p.nom_produit, SUM(lv.quantite) FROM produits p JOIN lignes_ventes lv ON p.id = lv.produit_id GROUP BY p.id ORDER BY 2 DESC LIMIT 5;\n\n"
    "RÉPONDS UNIQUEMENT PAR LE SQL EN UNE LIGNE."
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


def _repair_sql(sql: str, question: str = "") -> str:
    """
    Heuristiques de réparation de requêtes SQL pour corriger les hallucinations courantes 
    et l'overfitting du modèle fine-tuné de l'utilisateur (mon-modele-sql).
    """
    import re
    
    # 1. Remplacer les colonnes inexistantes nom_region / nom_gouvernorat par gouvernorat
    sql = re.sub(r'\bnom_region\b', 'gouvernorat', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bnom_gouvernorat\b', 'gouvernorat', sql, flags=re.IGNORECASE)
    
    # 2. Corriger la jointure invalide de Fidelite_Points sur Boutique
    # Motif : JOIN Fidelite_Points fp ON b.id = fp.client_id
    if "fidelite_points" in sql.lower() and "boutique_id" in sql.lower():
        pattern_fp = r"JOIN\s+Fidelite_Points\s+(\w+)\s+ON\s+(\w+)\.id\s*=\s*\1\.client_id"
        if re.search(pattern_fp, sql, re.IGNORECASE):
            sql = re.sub(
                pattern_fp,
                r"JOIN Clients c ON cv.client_id = c.id JOIN Fidelite_Points \1 ON c.id = \1.client_id",
                sql,
                flags=re.IGNORECASE
            )
            log.info("🔧 [SQL Repair] Jointure Fidelite_Points corrigée pour passer par la table Clients.")

    # 3. Corriger la grosse jointure sur-apprise de Depots et Stocks vers Lignes_Ventes
    # Motif : JOIN Depots d ON b.ville_id = d.ville_id JOIN Stocks s ON d.id = s.depot_id JOIN Lignes_Ventes lv ON s.id = lv.depot_id JOIN Commandes_Ventes cv ON lv.commande_id = cv.id
    pattern_complex = r"JOIN\s+Depots\s+(\w+)\s+ON\s+(\w+)\.ville_id\s*=\s*\1\.ville_id\s+JOIN\s+Stocks\s+(\w+)\s+ON\s*\1\.id\s*=\s*\3\.depot_id\s+JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s*\3\.id\s*=\s*\4\.depot_id\s+JOIN\s+Commandes_Ventes\s+(\w+)\s+ON\s*\4\.commande_id\s*=\s*\5\.id"
    if re.search(pattern_complex, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_complex,
            r"JOIN Commandes_Ventes \5 ON \2.id = \5.boutique_id JOIN Lignes_Ventes \4 ON \5.id = \4.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        log.info(" [SQL Repair] Chaîne de jointures Depots->Stocks->Lignes_Ventes réparée avec succès.")

    # 3b. Autre variante complexe d'overfitting géographique à Tunis/Sfax
    # Motif : FROM Villes_Tunisie v JOIN Depots d ON v.id = d.ville_id JOIN Stocks s ON d.id = s.depot_id JOIN Lignes_Ventes lv ON s.produit_id = lv.produit_id
    pattern_villes_complex = r"FROM\s+Villes_Tunisie\s+(\w+)\s+JOIN\s+Depots\s+(\w+)\s+ON\s+\1\.id\s*=\s*\2\.ville_id\s+JOIN\s+Stocks\s+(\w+)\s+ON\s*\2\.id\s*=\s*\3\.depot_id\s+JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s*\3\.produit_id\s*=\s*\4\.produit_id"
    if re.search(pattern_villes_complex, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_villes_complex,
            r"FROM Villes_Tunisie \1 JOIN Boutiques b ON \1.id = b.ville_id JOIN Commandes_Ventes cv ON b.id = cv.boutique_id JOIN Lignes_Ventes \4 ON cv.id = \4.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        sql = re.sub(
            r"WHERE\s+.*?(s\.statut\s*=\s*'actif'\s+AND\s+s\.type_depot\s*=\s*'Boutique')",
            r"WHERE cv.statut_commande = 'Livrée'",
            sql,
            flags=re.IGNORECASE
        )
        sql = re.sub(
            r"s\.statut\s*=\s*'actif'\s+AND\s+s\.type_depot\s*=\s*'Boutique'",
            r"cv.statut_commande = 'Livrée'",
            sql,
            flags=re.IGNORECASE
        )
        sql = re.sub(r"\bv\.nom_ville\s*=\s*'Tunis'", r"v.gouvernorat = 'Tunis'", sql, flags=re.IGNORECASE)
        log.info("🔧 [SQL Repair] Jointures géographiques complexes de Villes_Tunisie réparées avec succès.")

    # Autre variante de jointure directe fausse : JOIN Lignes_Ventes lv ON s.id = lv.depot_id
    pattern_lv_depot = r"JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s+(\w+)\.id\s*=\s*\1\.depot_id"
    if re.search(pattern_lv_depot, sql, re.IGNORECASE):
        cv_alias = "cv"
        cv_match = re.search(r"commandes_ventes\s+(\w+)", sql, re.IGNORECASE)
        if cv_match:
            cv_alias = cv_match.group(1)
        sql = re.sub(
            pattern_lv_depot,
            rf"JOIN Lignes_Ventes \1 ON {cv_alias}.id = \1.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        log.info(f" [SQL Repair] Jointure directe Lignes_Ventes sur depot_id réécrite avec {cv_alias}.id.")

    # 4. Corriger le filtrage géographique sur Depots (ex: d.nom_depot = 'Entrepôt Sfax' ou 'Sfax')
    # d.nom_depot = 'Entrepôt Sfax' -> v.gouvernorat = 'Sfax'
    pattern_sfax_depot = r"(\w+)\.nom_depot\s*=\s*'\s*(Entrep[ôo]t\s+)?([a-zA-Z\u00C0-\u00FF\s\-]+)\s*'"
    if re.search(pattern_sfax_depot, sql, re.IGNORECASE):
        match = re.search(pattern_sfax_depot, sql, re.IGNORECASE)
        city_name = match.group(3).strip()
        sql = re.sub(
            pattern_sfax_depot,
            rf"v.gouvernorat = '{city_name}'",
            sql,
            flags=re.IGNORECASE
        )
        
        if "villes_tunisie" not in sql.lower():
            b_alias = "b"
            b_match = re.search(r"boutiques\s+(\w+)", sql, re.IGNORECASE)
            if b_match:
                b_alias = b_match.group(1)
            sql = sql.replace(
                f"FROM Boutiques {b_alias}",
                f"FROM Boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
            )
            log.info(" [SQL Repair] Filtre de dépôt converti en filtre de ville et jointure villes_tunisie injectée.")

    # 5. Détection de filtres de boutiques/dépôts hallucinés (ex: Glam Beauty Hammamet ou Entrepôt Sfax)
    # Si le SQL filtre par une boutique spécifique alors que la question n'en parle pas,
    # mais que la question mentionne un gouvernorat/ville, on corrige le filtre !
    if question:
        cities = ["Sfax", "Tunis", "Sousse", "Nabeul", "Bizerte", "Gabès", "Kairouan", "Gafsa", "Monastir", "Ariana", "Manouba", "Ben Arous", "Medenine", "Djerba", "Le Kef", "Sidi Bou Said"]
        mentioned_city = None
        for city in cities:
            if city.lower() in question.lower():
                mentioned_city = city
                break
                
        if mentioned_city:
            pattern_boutique_filter = r"(\w+)\.nom_boutique\s*=\s*'\s*([a-zA-Z\u00C0-\u00FF\s\d\-]+)\s*'"
            if re.search(pattern_boutique_filter, sql, re.IGNORECASE):
                match_bf = re.search(pattern_boutique_filter, sql, re.IGNORECASE)
                boutique_val = match_bf.group(2).strip()
                # Si la ville mentionnée n'est pas dans le filtre de boutique
                # et que le nom exact de cette boutique n'est pas dans la question d'origine
                if mentioned_city.lower() not in boutique_val.lower() and boutique_val.lower() not in question.lower():
                    sql = re.sub(
                        pattern_boutique_filter,
                        rf"v.gouvernorat = '{mentioned_city}'",
                        sql,
                        flags=re.IGNORECASE
                    )
                    log.info(rf"🔧 [SQL Repair] Filtre de boutique halluciné '{boutique_val}' remplacé par le gouvernorat '{mentioned_city}'.")
                    
                    if "villes_tunisie" not in sql.lower():
                        b_alias = "b"
                        b_match = re.search(r"boutiques\s+(\w+)", sql, re.IGNORECASE)
                        if b_match:
                            b_alias = b_match.group(1)
                        if f"FROM Boutiques {b_alias}" in sql:
                            sql = sql.replace(
                                f"FROM Boutiques {b_alias}",
                                f"FROM Boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
                            )
                        elif f"FROM boutiques {b_alias}" in sql:
                            sql = sql.replace(
                                f"FROM boutiques {b_alias}",
                                f"FROM boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
                            )

    # 6. Correction des regroupements (GROUP BY par ville / par gouvernorat)
    if question:
        q_lower = question.lower()
        if "par ville" in q_lower or "par nom_ville" in q_lower:
            if "villes_tunisie" not in sql.lower():
                b_alias = "b"
                b_match = re.search(r"boutiques\s+(\w+)", sql, re.IGNORECASE)
                if b_match:
                    b_alias = b_match.group(1)
                if f"FROM Boutiques {b_alias}" in sql:
                    sql = sql.replace(
                        f"FROM Boutiques {b_alias}",
                        f"FROM Boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
                    )
                elif f"FROM boutiques {b_alias}" in sql:
                    sql = sql.replace(
                        f"FROM boutiques {b_alias}",
                        f"FROM boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
                    )
            
            sql = re.sub(r'\bb\.nom_boutique\b', 'v.nom_ville', sql, flags=re.IGNORECASE)
            sql = re.sub(r'GROUP\s+BY\s+b\.id\s*,\s*v\.nom_ville', 'GROUP BY v.nom_ville', sql, flags=re.IGNORECASE)
            log.info(" [SQL Repair] Agrégation basculée sur la ville (v.nom_ville).")
            
        elif "par gouvernorat" in q_lower or "par region" in q_lower or "par région" in q_lower:
            if "villes_tunisie" not in sql.lower():
                b_alias = "b"
                b_match = re.search(r"boutiques\s+(\w+)", sql, re.IGNORECASE)
                if b_match:
                    b_alias = b_match.group(1)
                if f"FROM Boutiques {b_alias}" in sql:
                    sql = sql.replace(
                        f"FROM Boutiques {b_alias}",
                        f"FROM Boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
                    )
                elif f"FROM boutiques {b_alias}" in sql:
                    sql = sql.replace(
                        f"FROM boutiques {b_alias}",
                        f"FROM boutiques {b_alias} JOIN villes_tunisie v ON {b_alias}.ville_id = v.id"
                    )
            
            sql = re.sub(r'\bb\.nom_boutique\b', 'v.gouvernorat', sql, flags=re.IGNORECASE)
            sql = re.sub(r'GROUP\s+BY\s+b\.id\s*,\s*v\.gouvernorat', 'GROUP BY v.gouvernorat', sql, flags=re.IGNORECASE)
            log.info(" [SQL Repair] Agrégation basculée sur le gouvernorat (v.gouvernorat).")

    return sql


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
        log.info("ℹ Mode Sécurisé : Autocorrection désactivée (max_retries=1).")
    # Normalisation métier : 'région' et 'nom_gouvernorat' sont strictement traduits en 'gouvernorat'
    import re
    original_question = question
    question = re.sub(r'\b(nom_)?r[eé]gions?\b', 'gouvernorat', question, flags=re.IGNORECASE)
    question = re.sub(r'\bnom_gouvernorats?\b', 'gouvernorat', question, flags=re.IGNORECASE)
    
    log.info(f" Question reçue : {original_question} (normalisée en : {question})")

    failed_attempts = []
    current_k = int(os.getenv("TOP_K_TABLES", 5))

    for attempt in range(1, max_retries + 1):
        # RAG dynamique : on élargit la recherche de tables si on échoue
        schema_context = get_relevant_schema(question, k=current_k)
        from indexing.schema_linker import get_relevant_tables
        relevant_tables = get_relevant_tables(question, k=current_k)
        log.info(f" [Schema Linker] Tables sélectionnées : {relevant_tables}")
        
        if attempt == 1:
            messages = _build_messages(question, schema_context)
        else:
            messages = _build_correction_messages(question, schema_context, failed_attempts)

        temperature = TEMPERATURES[min(attempt-1, len(TEMPERATURES) - 1)]
        raw_sql = generate_sql(messages, temperature=temperature)
        sql = _clean_sql(raw_sql)
        sql = _repair_sql(sql, question)
        
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
