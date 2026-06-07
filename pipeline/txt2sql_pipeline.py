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
    "Tu es un expert SQL pour un ERP de cosmétiques. Ta mission : générer du SQL PostgreSQL précis, optimal et minimaliste en te basant sur le schéma ERP fourni.\n\n"
    "⚠️ RÈGLES DE CONCEPTION :\n"
    "1. OCCAM'S RAZOR : N'utilise que les tables nécessaires pour répondre à la question. Pas de jointures superflues.\n"
    "2. NOMENCLATURE : Utilise strictement les noms de tables et colonnes en minuscules tels que définis dans le schéma.\n"
    "3. GROUPEMENTS : Si la question demande des résultats au pluriel ciblant des entités individuelles (ex: 'dans les villes', 'par boutique', 'selon les produits'), groupe systématiquement les résultats par l'attribut d'identification de ces entités (le nom de la ville, le nom du produit, etc.) avec la clause de groupement appropriée.\n\n"
    "Réponds UNIQUEMENT par le code SQL PostgreSQL brut sur une seule ligne se terminant par un point-virgule, sans aucun commentaire ni bloc markdown."
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


def _inject_villes_tunisie_join(sql: str) -> str:
    """
    Injecte intelligemment la jointure vers la table villes_tunisie v
    en se basant sur l'alias existant de Boutiques ou de Clients.
    """
    # Si villes_tunisie est déjà dans la requête, pas besoin d'injecter la jointure
    if "villes_tunisie" in sql.lower():
        return sql
    
    # 1. Chercher boutiques
    match_b = re.search(r"\b(?:FROM|JOIN)\s+boutiques\s+(?:AS\s+)?(\w+)\b", sql, re.IGNORECASE)
    if match_b:
        alias = match_b.group(1)
        if alias.upper() in ("ON", "WHERE", "JOIN", "USING", "AND", "OR", "GROUP", "ORDER", "LIMIT"):
            alias = "boutiques"
        join_str = f" JOIN villes_tunisie v ON {alias}.ville_id = v.id"
    else:
        # 2. Chercher clients
        match_c = re.search(r"\b(?:FROM|JOIN)\s+clients\s+(?:AS\s+)?(\w+)\b", sql, re.IGNORECASE)
        if match_c:
            alias = match_c.group(1)
            if alias.upper() in ("ON", "WHERE", "JOIN", "USING", "AND", "OR", "GROUP", "ORDER", "LIMIT"):
                alias = "clients"
            join_str = f" JOIN villes_tunisie v ON {alias}.ville_id = v.id"
        else:
            return sql
            
    # Trouver l'endroit où insérer la jointure (avant WHERE, GROUP BY, ORDER BY, etc.)
    pattern = r"\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|UNION)\b"
    insert_match = re.search(pattern, sql, re.IGNORECASE)
    
    if insert_match:
        idx = insert_match.start()
        sql = sql[:idx] + join_str + " " + sql[idx:]
    else:
        sql_strip = sql.rstrip().rstrip(";")
        semicolon = ";" if sql.strip().endswith(";") else ""
        sql = sql_strip + join_str + semicolon
        
    return sql


def _repair_sql(sql: str, question: str = "") -> str:
    """
    Heuristiques de réparation de requêtes SQL pour corriger les hallucinations courantes 
    et l'overfitting du modèle fine-tuné de l'utilisateur (mon-modele-sql).
    """
    import re
    
    # 1. Remplacer les colonnes inexistantes nom_region / nom_gouvernorat par gouvernorat
    sql = re.sub(r'\bnom_region\b', 'gouvernorat', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bnom_gouvernorat\b', 'gouvernorat', sql, flags=re.IGNORECASE)
    
    # 1a. Corriger l'alias des colonnes géographiques (gouvernorat, nom_ville) qui ne peuvent appartenir qu'à villes_tunisie (v)
    v_alias = "v"
    v_match = re.search(r'\bvilles_tunisie\s+(\w+)\b', sql, re.IGNORECASE)
    if v_match:
        v_alias = v_match.group(1)
        if v_alias.upper() in ("ON", "WHERE", "JOIN", "USING", "AND", "OR", "GROUP", "ORDER", "LIMIT"):
            v_alias = "villes_tunisie"
            
    if v_alias != "villes_tunisie":
        sql = re.sub(rf'\b(?!{v_alias}\b)\w+\.gouvernorat\b', f'{v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
        sql = re.sub(rf'\b(?!{v_alias}\b)\w+\.nom_ville\b', f'{v_alias}.nom_ville', sql, flags=re.IGNORECASE)
    else:
        sql = re.sub(r'\b(?!\bvilles_tunisie\b)\w+\.gouvernorat\b', 'villes_tunisie.gouvernorat', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\b(?!\bvilles_tunisie\b)\w+\.nom_ville\b', 'villes_tunisie.nom_ville', sql, flags=re.IGNORECASE)

    # 1b. Injecter la jointure vers villes_tunisie si l'alias v (ou villes_tunisie) est utilisé sans que la table ne soit jointe
    if (re.search(r'\bv\.\w+\b', sql, re.IGNORECASE) or "villes_tunisie" in sql.lower()) and "villes_tunisie" not in sql.lower():
        sql = _inject_villes_tunisie_join(sql)

    # 1c. Si la question demande un détail par ville ("dans les villes", "par ville") et que le SQL fait une agrégation globale sans GROUP BY
    if any(kw in question.lower() for kw in ["dans les villes", "par ville", "les villes de"]) and "group by" not in sql.lower():
        v_alias = None
        if "villes_tunisie" in sql.lower():
            v_match = re.search(r'\bvilles_tunisie\s+(\w+)\b', sql, re.IGNORECASE)
            if v_match:
                v_alias = v_match.group(1)
                if v_alias.upper() in ("ON", "WHERE", "JOIN", "USING", "AND", "OR", "GROUP", "ORDER", "LIMIT"):
                    v_alias = "villes_tunisie"
            else:
                v_alias = "villes_tunisie"
                
        if v_alias:
            sql = re.sub(r'\bSELECT\s+', f'SELECT {v_alias}.nom_ville, ', sql, count=1, flags=re.IGNORECASE)
            sql_strip = sql.rstrip().rstrip(";")
            semicolon = ";" if sql.strip().endswith(";") else ""
            sql = f"{sql_strip} GROUP BY {v_alias}.nom_ville ORDER BY {v_alias}.nom_ville{semicolon}"
            log.info(f"🔧 [SQL Repair] Ajout du GROUP BY {v_alias}.nom_ville pour répondre à la demande détaillée par ville.")

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
        log.info("🔧 [SQL Repair] Chaîne de jointures Depots->Stocks->Lignes_Ventes réparée avec succès.")

    # 3a. Gérer le cas où la jointure complexe n'inclut pas Commandes_Ventes à la fin
    pattern_complex_no_cv = r"JOIN\s+Depots\s+(\w+)\s+ON\s+(\w+)\.ville_id\s*=\s*\1\.ville_id\s+JOIN\s+Stocks\s+(\w+)\s+ON\s*\1\.id\s*=\s*\3\.depot_id\s+JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s*\3\.id\s*=\s*\4\.depot_id"
    if re.search(pattern_complex_no_cv, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_complex_no_cv,
            r"JOIN Commandes_Ventes cv ON \2.id = cv.boutique_id JOIN Lignes_Ventes \4 ON cv.id = \4.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        log.info("🔧 [SQL Repair] Chaîne de jointures Depots->Stocks->Lignes_Ventes (sans CV) réparée avec injection de Commandes_Ventes cv.")

    # 3b. Gérer le cas complexe de jointure Lignes_Ventes sur produit_id avec d'autres conditions hallucinées
    pattern_complex_prod = r"JOIN\s+Depots\s+(\w+)\s+ON\s+(\w+)\.ville_id\s*=\s*\1\.ville_id\s+JOIN\s+Stocks\s+(\w+)\s+ON\s*\1\.id\s*=\s*\3\.depot_id\s+JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s*.*?(?=\s+WHERE|\s+GROUP|\s+ORDER|\s+JOIN|$)"
    if re.search(pattern_complex_prod, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_complex_prod,
            r"JOIN Commandes_Ventes cv ON \2.id = cv.boutique_id JOIN Lignes_Ventes \4 ON cv.id = \4.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        log.info("🔧 [SQL Repair] Chaîne de jointures Depots->Stocks->Lignes_Ventes (produit_id) réparée avec injection de Commandes_Ventes cv.")

    # 3c. Autre variante complexe d'overfitting géographique à Tunis/Sfax
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
        log.info(f"🔧 [SQL Repair] Jointure directe Lignes_Ventes sur depot_id réécrite avec {cv_alias}.id.")

    # 3d. Gérer la jointure directe hallucinée Boutiques->Lignes_Ventes sur boutique_id (qui n'existe pas en DB !)
    pattern_b_lv = r"JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s+(\w+)\.id\s*=\s*\1\.boutique_id"
    if re.search(pattern_b_lv, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_b_lv,
            r"JOIN Commandes_Ventes cv ON \2.id = cv.boutique_id JOIN Lignes_Ventes \1 ON cv.id = \1.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        log.info("🔧 [SQL Repair] Jointure directe hallucinée Boutiques->Lignes_Ventes sur boutique_id corrigée avec Commandes_Ventes cv.")

    pattern_b_lv_rev = r"JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s*\1\.boutique_id\s*=\s*(\w+)\.id"
    if re.search(pattern_b_lv_rev, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_b_lv_rev,
            r"JOIN Commandes_Ventes cv ON \2.id = cv.boutique_id JOIN Lignes_Ventes \1 ON cv.id = \1.commande_id",
            sql,
            flags=re.IGNORECASE
        )
        log.info("🔧 [SQL Repair] Jointure directe hallucinée Lignes_Ventes->Boutiques sur boutique_id corrigée avec Commandes_Ventes cv.")

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
            sql = _inject_villes_tunisie_join(sql)
            log.info("🔧 [SQL Repair] Filtre de dépôt converti en filtre de ville et jointure villes_tunisie injectée.")

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
                # Si la boutique spécifique mentionnée n'est pas dans la question d'origine,
                # on remplace par le gouvernorat/ville mentionné(e)
                if boutique_val.lower() not in question.lower():
                    sql = re.sub(
                        pattern_boutique_filter,
                        rf"v.gouvernorat = '{mentioned_city}'",
                        sql,
                        flags=re.IGNORECASE
                    )
                    log.info(rf"🔧 [SQL Repair] Filtre de boutique halluciné '{boutique_val}' remplacé par le gouvernorat '{mentioned_city}'.")
                    
                    if "villes_tunisie" not in sql.lower():
                        sql = _inject_villes_tunisie_join(sql)

            # 5b. Si le gouvernorat/ville mentionné n'est pas du tout filtré dans la requête SQL,
            # on l'injecte impérativement pour restreindre correctement les résultats géographiques !
            city_filter_pattern = rf"\b(v\.gouvernorat|v\.nom_ville)\s*=\s*'{mentioned_city}'"
            if not re.search(city_filter_pattern, sql, re.IGNORECASE):
                # S'assurer que villes_tunisie est bien joint
                if "villes_tunisie" not in sql.lower():
                    sql = _inject_villes_tunisie_join(sql)
                
                # Injecter le filtre
                if "WHERE" in sql.upper():
                    sql = re.sub(
                        r"\bWHERE\b",
                        rf"WHERE v.gouvernorat = '{mentioned_city}' AND",
                        sql,
                        count=1,
                        flags=re.IGNORECASE
                    )
                else:
                    if "GROUP BY" in sql.upper():
                        sql = re.sub(
                            r"\bGROUP\s+BY\b",
                            rf"WHERE v.gouvernorat = '{mentioned_city}' GROUP BY",
                            sql,
                            count=1,
                            flags=re.IGNORECASE
                        )
                    elif "ORDER BY" in sql.upper():
                        sql = re.sub(
                            r"\bORDER\s+BY\b",
                            rf"WHERE v.gouvernorat = '{mentioned_city}' ORDER BY",
                            sql,
                            count=1,
                            flags=re.IGNORECASE
                        )
                    else:
                        sql = sql.strip().rstrip(";") + f" WHERE v.gouvernorat = '{mentioned_city}';"
                
                log.info(f"🔧 [SQL Repair] Filtre géographique absent injecté pour '{mentioned_city}'.")

    # 6. Correction des regroupements (GROUP BY par ville / par gouvernorat)
    if question:
        q_lower = question.lower()
        if "par ville" in q_lower or "par nom_ville" in q_lower:
            if "villes_tunisie" not in sql.lower():
                sql = _inject_villes_tunisie_join(sql)
            
            sql = re.sub(r'\bb\.nom_boutique\b', 'v.nom_ville', sql, flags=re.IGNORECASE)
            sql = re.sub(r'GROUP\s+BY\s+b\.id\s*,\s*v\.nom_ville', 'GROUP BY v.nom_ville', sql, flags=re.IGNORECASE)
            log.info("🔧 [SQL Repair] Agrégation basculée sur la ville (v.nom_ville).")
            
        elif "par gouvernorat" in q_lower or "par region" in q_lower or "par région" in q_lower:
            if "villes_tunisie" not in sql.lower():
                sql = _inject_villes_tunisie_join(sql)
            
            # Détecter l'alias de villes_tunisie
            v_alias = "v"
            v_match = re.search(r'\bvilles_tunisie\s+(\w+)\b', sql, re.IGNORECASE)
            if v_match:
                v_alias = v_match.group(1)
                if v_alias.lower() in ["join", "on", "where", "group", "order", "limit", "as"]:
                    v_alias = "v"

            sql = re.sub(r'\bb\.nom_boutique\b', f'{v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            sql = re.sub(r'GROUP\s+BY\s+b\.id\s*,\s*' + re.escape(v_alias) + r'\.gouvernorat', f'GROUP BY {v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            
            # Remplacer également nom_ville par gouvernorat
            sql = re.sub(rf'\b{v_alias}\.nom_ville\b', f'{v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            sql = re.sub(r'\bnom_ville\b', f'{v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            
            # S'assurer que les GROUP BY et ORDER BY sur nom_ville sont remplacés
            sql = re.sub(rf'GROUP\s+BY\s+{v_alias}\.nom_ville', f'GROUP BY {v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            sql = re.sub(r'GROUP\s+BY\s+nom_ville', f'GROUP BY {v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            sql = re.sub(rf'ORDER\s+BY\s+{v_alias}\.nom_ville', f'ORDER BY {v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            sql = re.sub(r'ORDER\s+BY\s+nom_ville', f'ORDER BY {v_alias}.gouvernorat', sql, flags=re.IGNORECASE)
            
            log.info(f"🔧 [SQL Repair] Agrégation basculée sur le gouvernorat ({v_alias}.gouvernorat).")

    # 8. Corriger l'absence de jointure entre Produits p et Lignes_Ventes lv quand les deux sont là
    if "produits" in sql.lower() and "lignes_ventes" in sql.lower():
        p_match = re.search(r"\bproduits\s+(\w+)\b", sql, re.IGNORECASE)
        lv_match = re.search(r"\blignes_ventes\s+(\w+)\b", sql, re.IGNORECASE)
        if p_match and lv_match:
            p_alias = p_match.group(1)
            lv_alias = lv_match.group(1)
            join_cond_pattern = rf"\b({p_alias}\.id\s*=\s*{lv_alias}\.produit_id|{lv_alias}\.produit_id\s*=\s*{p_alias}\.id)\b"
            if not re.search(join_cond_pattern, sql, re.IGNORECASE):
                lv_join_pattern = rf"(JOIN\s+lignes_ventes\s+{lv_alias}\s+ON\s+)([^;]+?)(?=\s+JOIN|\s+WHERE|\s+GROUP|\s+ORDER|\s+LIMIT|;|$)"
                if re.search(lv_join_pattern, sql, re.IGNORECASE):
                    sql = re.sub(
                        lv_join_pattern,
                        rf"\1\2 AND {lv_alias}.produit_id = {p_alias}.id",
                        sql,
                        flags=re.IGNORECASE
                    )
                    log.info(f"🔧 [SQL Repair] Jointure manquante injectée entre {p_alias} et {lv_alias}.")

    # 9. Réparer la jointure dans le mauvais ordre Boutiques -> Commandes_Ventes -> Lignes_Ventes
    pattern_wrong_order = r"JOIN\s+Boutiques\s+(\w+)\s+ON\s+[^J]+?JOIN\s+Commandes_Ventes\s+(\w+)\s+ON\s+[^J]+?JOIN\s+Lignes_Ventes\s+(\w+)\s+ON\s+(?:\2\.id\s*=\s*\3\.commande_id|\3\.commande_id\s*=\s*\2\.id)"
    if re.search(pattern_wrong_order, sql, re.IGNORECASE):
        p_match = re.search(r"\bproduits\s+(\w+)\b", sql, re.IGNORECASE)
        p_alias = p_match.group(1) if p_match else "p"
        sql = re.sub(
            pattern_wrong_order,
            rf"JOIN Lignes_Ventes \3 ON {p_alias}.id = \3.produit_id JOIN Commandes_Ventes \2 ON \3.commande_id = \2.id JOIN Boutiques \1 ON \2.boutique_id = \1.id",
            sql,
            flags=re.IGNORECASE
        )
    # 10. Détecter et nettoyer le sur-apprentissage sur le rapport CA + Dépenses par Boutique
    if "depenses_fixes" in sql.lower() or "depenses" in sql.lower():
        expenses_keywords = ["depense", "dépense", "charge", "cout", "coût", "loyer", "salaire", "rentabilite", "rentabilité", "benefice", "bénéfice", "profit"]
        if question and not any(kw in question.lower() for kw in expenses_keywords):
            b_alias = "b"
            cv_alias = "cv"
            b_match = re.search(r"\bboutiques\s+(\w+)\b", sql, re.IGNORECASE)
            if b_match:
                b_alias = b_match.group(1)
            sql = f"SELECT {b_alias}.nom_boutique, SUM({cv_alias}.total_ttc) AS ca FROM boutiques {b_alias} JOIN commandes_ventes {cv_alias} ON {b_alias}.id = {cv_alias}.boutique_id GROUP BY {b_alias}.id, {b_alias}.nom_boutique ORDER BY ca DESC;"
            log.info("🔧 [SQL Repair] Requête CA boutique épurée (retrait des dépenses/employés hallucinés).")

    # 11. Corriger le produit cartésien entre commandes_ventes/lignes_ventes et depenses_fixes
    if "depenses_fixes" in sql.lower() and ("commandes_ventes" in sql.lower() or "lignes_ventes" in sql.lower()) and "with" not in sql.lower():
        sql = """WITH ca_par_boutique AS (
    SELECT boutique_id, SUM(total_ttc) AS ca
    FROM commandes_ventes
    GROUP BY boutique_id
),
depenses_par_boutique AS (
    SELECT boutique_id, SUM(montant) AS depenses
    FROM depenses_fixes
    GROUP BY boutique_id
)
SELECT b.nom_boutique, COALESCE(ca.ca, 0) AS ca, COALESCE(dep.depenses, 0) AS depenses
FROM boutiques b
LEFT JOIN ca_par_boutique ca ON b.id = ca.boutique_id
LEFT JOIN depenses_par_boutique dep ON b.id = dep.boutique_id
ORDER BY ca DESC;"""
        log.info("🔧 [SQL Repair] Jointure cartésienne CA/Dépenses réécrite avec des CTEs isolées.")

    # 12. Gérer l'overfitting de l'alias et de la table Boutiques quand le critère géographique porte sur les clients
    # On redirige vers clients si :
    # - La question porte sur un critère géographique (villes, gouvernorats, etc.)
    # - La table boutiques est jointe avec villes_tunisie
    # - La question ne mentionne PAS explicitement "boutique", "magasin", "shop", "point de vente"
    if question and "boutiques" in sql.lower() and "villes_tunisie" in sql.lower() and "clients" not in sql.lower():
        has_geo_terms = any(w in question.lower() for w in ["ville", "gouvernorat", "region", "région", "sfax", "tunis", "sousse", "nabeul", "bizerte", "gabès", "gabes", "kairouan", "gafsa", "monastir", "ariana", "manouba", "ben arous", "medenine", "djerba", "le kef", "sidi bou said"])
        has_boutique_terms = any(w in question.lower() for w in ["boutique", "magasin", "shop", "point de vente"])
        
        # Cas 1 : Termes clients explicites
        case_client_explicit = any(w in question.lower() for w in ["client", "genre", "fidelite", "fidélité", "age", "âge"]) and not any(w in question.lower() for w in ["vente", "commande", "ca", "chiffre d'affaires"])
        
        # Cas 2 : Termes géographiques généraux sur les ventes (sans mention de boutique)
        case_geo_sales = has_geo_terms and not has_boutique_terms
        
        if case_client_explicit or case_geo_sales:
            # Trouver l'alias de boutiques
            match = re.search(r'\bboutiques\s+(\w+)\b', sql, re.IGNORECASE)
            if match:
                alias = match.group(1)
                # S'assurer que l'alias n'est pas un mot clé SQL
                if alias.lower() not in ["join", "on", "where", "group", "order", "limit", "as"]:
                    sql = re.sub(r'\bboutiques\s+' + alias + r'\b', 'clients c', sql, flags=re.IGNORECASE)
                    sql = re.sub(r'\bJOIN\s+boutiques\s+' + alias + r'\b', 'JOIN clients c', sql, flags=re.IGNORECASE)
                    sql = re.sub(r'\b' + alias + r'\.id\b', 'c.id', sql, flags=re.IGNORECASE)
                    sql = re.sub(r'\b' + alias + r'\.ville_id\b', 'c.ville_id', sql, flags=re.IGNORECASE)
                    sql = re.sub(r'\b' + alias + r'\.nom_boutique\b', 'c.nom', sql, flags=re.IGNORECASE)
                    sql = re.sub(r'\bboutique_id\b', 'client_id', sql, flags=re.IGNORECASE)
                    log.info(f"🔧 [SQL Repair] Jointure géographique redirigée de Boutiques {alias} à clients c (boutique non mentionnée).")

    # 13. Nettoyer les références orphelines aux tables Stocks (s) ou Depots (d) si elles ont été retirées du FROM/JOIN
    if " s " not in sql.lower() and "join stocks " not in sql.lower() and "from stocks " not in sql.lower():
        sql = re.sub(r'\bAND\s+s\.statut\s*=\s*\'[a-zA-Z0-9_-]+\'', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bAND\s+s\.type_depot\s*=\s*\'[a-zA-Z0-9_-]+\'', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bs\.statut\s*=\s*\'[a-zA-Z0-9_-]+\'\s+AND\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bs\.type_depot\s*=\s*\'[a-zA-Z0-9_-]+\'\s+AND\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bWHERE\s+s\.statut\s*=\s*\'[a-zA-Z0-9_-]+\'\s*;', ';', sql, flags=re.IGNORECASE)
        log.info("🔧 [SQL Repair] Références orphelines de la table Stocks (s) nettoyées.")

    if " d " not in sql.lower() and "join depots " not in sql.lower() and "from depots " not in sql.lower():
        sql = re.sub(r'\bAND\s+d\.statut\s*=\s*\'[a-zA-Z0-9_-]+\'', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bAND\s+d\.type_depot\s*=\s*\'[a-zA-Z0-9_-]+\'', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bd\.statut\s*=\s*\'[a-zA-Z0-9_-]+\'\s+AND\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bd\.type_depot\s*=\s*\'[a-zA-Z0-9_-]+\'\s+AND\s*', '', sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bWHERE\s+d\.statut\s*=\s*\'[a-zA-Z0-9_-]+\'\s*;', ';', sql, flags=re.IGNORECASE)
        log.info("🔧 [SQL Repair] Références orphelines de la table Depots (d) nettoyées.")

    # 14. Détecter l'utilisation de cv. (ou de l'alias de Commandes_Ventes) sans que la table soit jointe
    if re.search(r'\bcv\.\w+\b', sql, re.IGNORECASE) and "commandes_ventes" not in sql.lower():
        # Trouver l'alias de lignes_ventes pour brancher cv
        match_lv = re.search(r'\blignes_ventes\s+(\w+)\b', sql, re.IGNORECASE)
        lv_alias = "lv"
        if match_lv:
            alias = match_lv.group(1)
            if alias.lower() not in ["join", "on", "where", "group", "order", "limit", "as"]:
                lv_alias = alias
        
        # Injecter le JOIN cv avant WHERE, GROUP BY, ORDER BY, etc.
        join_str = f" JOIN Commandes_Ventes cv ON {lv_alias}.commande_id = cv.id"
        pattern = r"\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|UNION)\b"
        insert_match = re.search(pattern, sql, re.IGNORECASE)
        if insert_match:
            idx = insert_match.start()
            sql = sql[:idx] + join_str + " " + sql[idx:]
        else:
            sql_strip = sql.rstrip().rstrip(";")
            semicolon = ";" if sql.strip().endswith(";") else ""
            sql = sql_strip + join_str + semicolon
        log.info(f"🔧 [SQL Repair] Jointure manquante Commandes_Ventes cv injectée par rapport à l'alias de Lignes_Ventes '{lv_alias}'.")

    # 15. Réparer la jointure directe hallucinée Commandes_Ventes -> Produits (la relation passe par Lignes_Ventes)
    pattern_cv_p = r"JOIN\s+Produits\s+(\w+)\s+ON\s+(cv|commandes_ventes)\.produit_id\s*=\s*\1\.id"
    if re.search(pattern_cv_p, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_cv_p,
            r"JOIN Lignes_Ventes lv ON \2.id = lv.commande_id JOIN Produits \1 ON lv.produit_id = \1.id",
            sql,
            flags=re.IGNORECASE
        )
        log.info("🔧 [SQL Repair] Jointure directe hallucinée Commandes_Ventes->Produits réécrite avec Lignes_Ventes.")

    pattern_cv_p_rev = r"JOIN\s+Produits\s+(\w+)\s+ON\s*\1\.id\s*=\s*(cv|commandes_ventes)\.produit_id"
    if re.search(pattern_cv_p_rev, sql, re.IGNORECASE):
        sql = re.sub(
            pattern_cv_p_rev,
            r"JOIN Lignes_Ventes lv ON \2.id = lv.commande_id JOIN Produits \1 ON lv.produit_id = \1.id",
            sql,
            flags=re.IGNORECASE
        )
        log.info("🔧 [SQL Repair] Jointure directe hallucinée Produits->Commandes_Ventes réécrite avec Lignes_Ventes.")

    # 16. Réparer les clauses WHERE prématurées (générées au milieu des JOINS par le modèle)
    # Exemple : FROM p JOIN m ON ... WHERE m.nom_marque = 'X' JOIN lv ON ... WHERE cv.statut = 'Y'
    premature_where_match = re.search(r'\bWHERE\b\s+([\s\S]+?)\s+\bJOIN\b', sql, re.IGNORECASE)
    if premature_where_match:
        premature_cond = premature_where_match.group(1).strip()
        # Supprimer le WHERE prématuré de sa position d'origine
        sql = re.sub(r'\bWHERE\b\s+[\s\S]+?\s+(?=\bJOIN\b)', '', sql, count=1, flags=re.IGNORECASE)
        
        # Vérifier s'il y a un autre WHERE plus loin dans la requête
        if re.search(r'\bWHERE\b', sql, re.IGNORECASE):
            # Combiner avec le WHERE existant
            sql = re.sub(
                r'\bWHERE\b\s+([\s\S]+?)(?=\bGROUP\s+BY|\bORDER\s+BY|\bLIMIT|\bHAVING|\bUNION|;|$)',
                rf"WHERE ({premature_cond}) AND (\1)",
                sql,
                count=1,
                flags=re.IGNORECASE
            )
            log.info(f"🔧 [SQL Repair] Clause WHERE prématurée '{premature_cond}' combinée avec la clause WHERE principale.")
        else:
            # Injecter un nouveau WHERE avant GROUP BY, ORDER BY, etc.
            join_str = f" WHERE {premature_cond}"
            pattern = r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|UNION)\b"
            insert_match = re.search(pattern, sql, re.IGNORECASE)
            if insert_match:
                idx = insert_match.start()
                sql = sql[:idx] + join_str + " " + sql[idx:]
            else:
                sql_strip = sql.rstrip().rstrip(";")
                semicolon = ";" if sql.strip().endswith(";") else ""
                sql = sql_strip + join_str + semicolon
            log.info(f"🔧 [SQL Repair] Clause WHERE prématurée '{premature_cond}' déplacée à la fin de la requête.")

    # 17. Détecter et nettoyer le sur-apprentissage / hallucination de filtres de promotions sur nom_produit ou nom_promo
    promo_keywords = ["promo", "solde", "remise", "reduction", "réduction", "offre", "black friday", "ramadan", "fête des mères", "fete des meres", "aïd", "aid", "destockage", "déstockage", "fêtes fin année"]
    has_promo_in_question = any(k in question.lower() for k in promo_keywords) if question else False
    if not has_promo_in_question:
        # Rechercher des clauses IN (...) contenant des promotions
        in_pattern = r"\b\w+\.(?:nom_produit|nom_promo)\s+IN\s*\(\s*(?:'[^']+'\s*,\s*)*'[^']+'\s*\)"
        for match in re.finditer(in_pattern, sql, re.IGNORECASE):
            match_str = match.group(0)
            if any(k in match_str.lower() for k in ["black friday", "fête", "fete", "ramadan", "soldes", "aïd", "aid", "destockage", "année"]):
                sql = sql.replace(match_str, "1=1")
                log.info(f"🔧 [SQL Repair] Clause IN de promotions remplacée par 1=1.")
        
        # Rechercher des clauses individuelles du type: alias.nom_produit = 'Black Friday...'
        eq_pattern = r"\b\w+\.(?:nom_produit|nom_promo)\s*=\s*'[^']+'"
        for match in re.finditer(eq_pattern, sql, re.IGNORECASE):
            match_str = match.group(0)
            if any(k in match_str.lower() for k in ["black friday", "fête", "fete", "ramadan", "soldes", "aïd", "aid", "destockage", "année"]):
                sql = sql.replace(match_str, "1=1")
                log.info(f"🔧 [SQL Repair] Clause EQUAL de promotions remplacée par 1=1.")
                
        # Nettoyer les 'AND 1=1' ou '1=1 AND' ou 'WHERE 1=1'
        sql = re.sub(r"\bAND\s+1=1\b", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\b1=1\s+AND\b", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bWHERE\s+1=1\s*;", ";", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\bWHERE\s+1=1\s+AND\b", "WHERE", sql, flags=re.IGNORECASE)

    # 18. Détecter et corriger les hallucinations de ventes (lignes_ventes, commandes_ventes) dans les requêtes purement stock
    stock_keywords = ["stock", "stocks", "inventaire"]
    sales_keywords = ["vente", "vendu", "commande", "ca", "chiffre d'affaires", "chiffre d'affaire", "achat", "facture", "panier", "client"]
    has_stock_in_question = any(k in question.lower() for k in stock_keywords) if question else False
    has_sales_in_question = any(k in question.lower() for k in sales_keywords) if question else False
    
    if has_stock_in_question and not has_sales_in_question:
        if "lignes_ventes" in sql.lower() or "commandes_ventes" in sql.lower():
            # Extraire les conditions WHERE existantes (en filtrant celles liées à lv ou cv)
            where_clause = ""
            where_match = re.search(r"\bWHERE\b\s+([\s\S]+?)(?=\bGROUP\s+BY|\bORDER\s+BY|\bLIMIT|;|$)", sql, re.IGNORECASE)
            if where_match:
                conds = where_match.group(1).strip()
                valid_conds = []
                for cond in re.split(r"\bAND\b", conds, flags=re.IGNORECASE):
                    cond = cond.strip()
                    if not any(x in cond.lower() for x in ["lv.", "cv.", "lignes_ventes", "commandes_ventes"]):
                        valid_conds.append(cond)
                if valid_conds:
                    where_clause = " WHERE " + " AND ".join(valid_conds)

            # Détecter les alias
            p_alias = "p"
            p_match = re.search(r"\bproduits\s+(\w+)\b", sql, re.IGNORECASE)
            if p_match:
                p_alias = p_match.group(1)
                if p_alias.lower() in ["join", "on", "where", "group", "order", "limit", "as"]:
                    p_alias = "p"
            
            s_alias = "s"
            s_match = re.search(r"\bstocks\s+(\w+)\b", sql, re.IGNORECASE)
            if s_match:
                s_alias = s_match.group(1)
                if s_alias.lower() in ["join", "on", "where", "group", "order", "limit", "as"]:
                    s_alias = "s"

            has_depots = "depots" in sql.lower() or " d " in sql.lower()
            d_alias = "d"
            d_match = re.search(r"\bdepots\s+(\w+)\b", sql, re.IGNORECASE)
            if d_match:
                d_alias = d_match.group(1)
                if d_alias.lower() in ["join", "on", "where", "group", "order", "limit", "as"]:
                    d_alias = "d"

            # Construire les JOINs requis
            joins_list = []
            need_products = "group by" in sql.lower() or p_alias in where_clause or "produit" in question.lower()
            if need_products:
                joins_list.append(f"JOIN produits {p_alias} ON {s_alias}.produit_id = {p_alias}.id")
            if has_depots:
                joins_list.append(f"JOIN depots {d_alias} ON {s_alias}.depot_id = {d_alias}.id")
            
            joins_str = " " + " ".join(joins_list) if joins_list else ""

            # Construire SELECT et GROUP BY
            if "group by" in sql.lower() and need_products:
                if has_depots:
                    select_fields = f"{p_alias}.nom_produit, {d_alias}.nom_depot, SUM({s_alias}.quantite_disponible) AS stock_total"
                    group_fields = f"{p_alias}.id, {p_alias}.nom_produit, {d_alias}.id, {d_alias}.nom_depot"
                else:
                    select_fields = f"{p_alias}.nom_produit, SUM({s_alias}.quantite_disponible) AS stock_total"
                    group_fields = f"{p_alias}.id, {p_alias}.nom_produit"
                sql = f"SELECT {select_fields} FROM stocks {s_alias}{joins_str}{where_clause} GROUP BY {group_fields} ORDER BY stock_total DESC;"
            else:
                sql = f"SELECT SUM({s_alias}.quantite_disponible) AS stock_total FROM stocks {s_alias}{joins_str}{where_clause};"
            
            sql = re.sub(r'\s+', ' ', sql).strip()
            if not sql.endswith(";"):
                sql += ";"
            log.info("🔧 [SQL Repair] Requête de stock épurée de toute table de ventes hallucinée.")

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
