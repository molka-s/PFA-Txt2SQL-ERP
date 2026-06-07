import os
import json
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pipeline.txt2sql_pipeline import run
from pipeline.model_loader import generate_sql
import psycopg2
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Txt2SQL API")

# Configuration CORS pour autoriser le frontend React
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_FILE = "corrections_cache.json"

def load_corrections_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Erreur lors de la lecture du cache : {e}")
            return {}
    return {}

def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
        
    return previous_row[-1]

def save_to_corrections_cache(new_corrections: dict):
    cache = load_corrections_cache()
    updated = False
    for k, v in new_corrections.items():
        k_clean = k.strip().strip("?,.!:;()\"'").lower()
        v_clean = v.strip().strip("?,.!:;()\"'").lower()
        if k_clean and v_clean and k_clean != v_clean:
            # Sécurité : ne jamais enregistrer d'éléments techniques ou SQL dans le cache orthographique
            if any(char in k_clean or char in v_clean for char in "._()[]=<>*"):
                continue
            # Sécurité : ne pas corriger les nombres/dates (ex: "2024" -> "annee")
            if k_clean.isdigit() or v_clean.isdigit():
                print(f"[Self-Learning] Rejet de la correction '{k_clean}' -> '{v_clean}' car l'un des termes est un nombre.")
                continue
            # Sécurité : vérifier que la distance de Levenshtein n'est pas trop grande (c'est une correction orthographique, pas une traduction sémantique)
            dist = levenshtein_distance(k_clean, v_clean)
            max_allowed_dist = max(2, len(k_clean) // 3)
            if dist > max_allowed_dist:
                print(f"[Self-Learning] Rejet de la correction '{k_clean}' -> '{v_clean}' car la distance de Levenshtein ({dist}) dépasse le maximum autorisé ({max_allowed_dist})")
                continue
                
            if cache.get(k_clean) != v_clean:
                cache[k_clean] = v_clean
                updated = True
    if updated:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=4, ensure_ascii=False)
            print(f"[Self-Learning] Cache mis à jour avec de nouvelles corrections : {new_corrections}")
        except Exception as e:
            print(f"Erreur lors de l'écriture du cache : {e}")

def preprocess_query(question: str) -> str:
    cache = load_corrections_cache()
    if not cache:
        return question
        
    words = question.split()
    new_words = []
    for w in words:
        # Extraire la ponctuation
        clean_w = w.strip("?,.!:;()\"'").lower()
        if clean_w in cache:
            corrected = cache[clean_w]
            # Conserver la majuscule si le mot d'origine en avait une
            if w and w[0].isupper():
                corrected = corrected.capitalize()
            
            # Reconstruire le mot avec sa ponctuation d'origine
            prefix = ""
            for c in w:
                if c in "?,.!:;()\"'":
                    prefix += c
                else:
                    break
            suffix = ""
            for c in reversed(w):
                if c in "?,.!:;()\"'":
                    suffix = c + suffix
                else:
                    break
            new_words.append(prefix + corrected + suffix)
        else:
            new_words.append(w)
            
    result_q = " ".join(new_words)
    if result_q != question:
        print(f"[Self-Learning] Requête corrigée automatiquement via le cache : '{question}' -> '{result_q}'")
    return result_q

def learn_from_manual_correction(original: str, corrected: str):
    if not original or not corrected or original == corrected:
        return
        
    orig_words = [w.strip("?,.!:;()\"'").lower() for w in original.split() if w.strip("?,.!:;()\"'")]
    corr_words = [w.strip("?,.!:;()\"'").lower() for w in corrected.split() if w.strip("?,.!:;()\"'")]
    
    new_corrections = {}
    
    # Trouver les mots d'origine qui ne sont pas dans la correction
    typos = [w for w in orig_words if w not in corr_words]
    # Trouver les mots de correction qui ne sont pas dans l'origine
    replacements = [w for w in corr_words if w not in orig_words]
    
    for typo in typos:
        best_match = None
        min_dist = 999
        for rep in replacements:
            dist = levenshtein_distance(typo, rep)
            if dist < min_dist:
                min_dist = dist
                best_match = rep
        
        # Seuil d'acceptabilité dynamique
        max_allowed_dist = max(2, len(typo) // 3)
        if best_match and min_dist <= max_allowed_dist:
            new_corrections[typo] = best_match
            if best_match in replacements:
                replacements.remove(best_match)
            
    if new_corrections:
        save_to_corrections_cache(new_corrections)

class QuestionRequest(BaseModel):
    question: str
    original_question: Optional[str] = None

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "entreprise_erp"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "root")
    )

def get_disambiguation(question: str, schema_context: str, error_msg: str) -> dict:
    """
    Module de désambiguïsation: appelle le LLM pour générer des suggestions en cas d'erreur ou de résultat vide.
    """
    prompt = f"""Tu es un assistant IA expert en bases de données ERP.
L'utilisateur a posé la question suivante : "{question}"

Le système n'a pas pu répondre correctement pour la raison suivante (ou car la requête a retourné 0 résultat) :
"{error_msg}"

Voici le schéma simplifié de la base de données :
{schema_context}

TA MISSION :
1. Corrige impérativement toutes les fautes d'orthographe ou de frappe de la question de l'utilisateur (ex: "bitiques" -> "boutiques", "Tuniss" -> "Tunis", "totil" -> "total", "clints" -> "clients").
2. Reste STRICTEMENT dans le contexte exact de la question de l'utilisateur. Les suggestions doivent être uniquement des versions améliorées, clarifiées et corrigées de sa propre question. N'ajoute JAMAIS de nouveaux filtres, de nouveaux critères (ex: n'ajoute pas de dates, de catégories ou de limites si elles n'étaient pas dans la question de départ) ni de concepts de ton cru.
3. Propose 3 suggestions de questions alternatives qui sont extrêmement simples, courtes (maximum 8-10 mots), claires et prêtes à être exécutées par le Text2SQL (ex: pour "CA totil par bitique", la suggestion parfaite est "Quel est le chiffre d'affaires total par boutique ?").
4. L'explication DOIT être limpide, amicale et s'adresser au user en lui expliquant précisément le problème ou les mots corrigés (ex: "Il semble y avoir des fautes de frappe dans 'totil' et 'bitique'. Vouliez-vous dire..."). N'affiche aucun jargon technique (pas de noms de colonnes SQL compliquées) et ne mentionne jamais de tables hors-sujet.

5. Identifie les paires de corrections exactes (mot erroné -> mot corrigé) dans le dictionnaire "corrections" (ex: {{"bitique": "boutique", "totil": "total"}}). Si aucune correction n'est nécessaire, laisse-le vide.

Réponds UNIQUEMENT avec un objet JSON valide ayant la structure suivante, sans aucun texte autour, sans bloc markdown :
{{
    "explication": "Une phrase courte (max 15 mots) expliquant gentiment le problème ou la correction orthographique.",
    "suggestions": [
        "Suggestion de question simple 1 ?",
        "Suggestion de question simple 2 ?",
        "Suggestion de question simple 3 ?"
    ],
    "corrections": {{
        "mot_errone": "mot_corrige"
    }}
}}
"""
    try:
        response = generate_sql([{"role": "user", "content": prompt}], temperature=0.5)
        
        # Nettoyage de la réponse si le modèle ajoute du markdown
        response = response.strip()
        if response.startswith("```json"):
            response = response[7:]
        if response.startswith("```"):
            response = response[3:]
        if response.endswith("```"):
            response = response[:-3]
            
        data = json.loads(response.strip())
        # Auto-apprentissage : Enregistrer les corrections dans notre cache
        corrections = data.get("corrections", {})
        if corrections:
            save_to_corrections_cache(corrections)
        return data
    except Exception as e:
        print(f"Erreur lors de la désambiguïsation : {e}")
        return {
            "explication": "Je n'ai pas pu analyser la requête avec précision.",
            "suggestions": [
                "Quel est le chiffre d'affaires total ?",
                "Combien de commandes ont été passées ?",
                "Quels sont les 5 produits les plus vendus ?"
            ]
        }

@app.post("/ask")
async def ask_question(req: QuestionRequest):
    try:
        # Apprentissage passif si l'utilisateur soumet une correction manuelle ou valide une suggestion
        if req.original_question:
            learn_from_manual_correction(req.original_question, req.question)
            
        # Correction automatique via le dictionnaire d'apprentissage (corrections_cache.json)
        corrected_q = preprocess_query(req.question)
        result = run(corrected_q)
        
        # Condition de déclenchement de la désambiguïsation
        needs_disambiguation = False
        error_msg = ""
        
        if not result.get("success"):
            needs_disambiguation = True
            error_msg = result.get("error", "Erreur SQL inattendue.")
        elif result.get("success"):
            sql_gen = result.get("sql", "")
            if sql_gen and "ERR_UNKNOWN_ENTITY" in sql_gen:
                needs_disambiguation = True
                error_msg = "Je ne trouve pas cette information dans la base. Une entité semble mal orthographiée ou inexistante."
            else:
                # Si le résultat est vide (0 ligne), on ne déclenche pas la désambiguïsation
                # car une requête valide peut légitimement retourner 0 résultat.
                pass
        
        if needs_disambiguation:
            schema_context = result.get("schema_tables", "Schéma non disponible")
            disambiguation_data = get_disambiguation(req.question, schema_context, error_msg)
            result["disambiguation"] = disambiguation_data
            
        return result
    except Exception as e:
        error_str = str(e)
        return {
            "question": req.question,
            "sql": None,
            "success": False,
            "results": None,
            "error": error_str,
            "attempts": 0,
            "disambiguation": get_disambiguation(req.question, "Schéma non disponible", error_str)
        }

@app.get("/schema")
async def get_schema():
    """Récupère la structure complète de la base pour l'Explorateur de Schéma."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Requête pour lister les tables et leurs colonnes avec info PK
        query = """
        SELECT 
            cols.table_name, 
            cols.column_name, 
            cols.data_type,
            CASE WHEN pk.column_name IS NOT NULL THEN 'YES' ELSE 'NO' END AS is_primary
        FROM information_schema.columns cols
        LEFT JOIN (
            SELECT kcu.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY'
        ) pk ON cols.table_name = pk.table_name AND cols.column_name = pk.column_name
        WHERE cols.table_schema = 'public'
        ORDER BY cols.table_name, cols.ordinal_position;
        """
        cur.execute(query)
        rows = cur.fetchall()
        
        schema = {}
        for table, column, dtype, is_pk in rows:
            if table not in schema:
                schema[table] = []
            schema[table].append({
                "name": column, 
                "type": dtype,
                "is_pk": is_pk == 'YES'
            })
            
        cur.close()
        conn.close()
        return schema
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/relations")
async def get_relations():
    """Récupère les relations Foreign Key (FK) pour le diagramme."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        query = """
        SELECT
            tc.table_name AS from_table,
            kcu.column_name AS from_column,
            ccu.table_name AS to_table,
            ccu.column_name AS to_column
        FROM
            information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
              AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY';
        """
        cur.execute(query)
        rows = cur.fetchall()
        
        relations = []
        for row in rows:
            relations.append({
                "from": row[0],
                "from_col": row[1],
                "to": row[2],
                "to_col": row[3]
            })
            
        cur.close()
        conn.close()
        return relations
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard-stats")
async def get_dashboard_stats():
    """Version finale et complète du dashboard analytique."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        stats = {}
        
        # 1. KPIs de base
        cur.execute('SELECT SUM(total_ttc) FROM commandes_ventes')
        stats['total_revenue'] = float(cur.fetchone()[0] or 0)
        
        cur.execute('SELECT COUNT(*) FROM commandes_ventes')
        stats['total_orders'] = cur.fetchone()[0]
        
        cur.execute('SELECT COUNT(*) FROM produits')
        stats['total_products'] = cur.fetchone()[0]
        
        cur.execute('SELECT COUNT(*) FROM clients')
        stats['total_customers'] = cur.fetchone()[0]

        # 1b. Calcul des dépenses et bénéfice net
        cur.execute('SELECT SUM(montant) FROM depenses_fixes')
        fixed_expenses = float(cur.fetchone()[0] or 0)
        
        cur.execute('SELECT SUM(salaire) FROM employes')
        salaries = float(cur.fetchone()[0] or 0)
        
        cur.execute('SELECT SUM(montant_total) FROM achats_fournisseurs')
        purchases = float(cur.fetchone()[0] or 0)
        
        stats['total_expenses'] = fixed_expenses + salaries + purchases
        stats['net_profit'] = stats['total_revenue'] - stats['total_expenses']

        # 2. Évolution Mensuelle
        cur.execute("""
            SELECT TO_CHAR(date_vente, 'Mon') as month, SUM(total_ttc) as total 
            FROM commandes_ventes 
            GROUP BY month, EXTRACT(MONTH FROM date_vente)
            ORDER BY EXTRACT(MONTH FROM date_vente)
        """)
        stats['monthly_sales'] = [{"name": r[0], "value": float(r[1])} for r in cur.fetchall()]

        # 3. Distribution par Catégorie
        cur.execute("""
            SELECT c.nom_categorie, COUNT(p.id) as count 
            FROM categories c
            JOIN sous_categories sc ON c.id = sc.categorie_id
            JOIN produits p ON sc.id = p.sous_cat_id
            GROUP BY c.nom_categorie
            ORDER BY count DESC
        """)
        stats['category_distribution'] = [{"name": r[0], "value": r[1]} for r in cur.fetchall()]

        # 4. Top 5 Produits les plus rentables
        cur.execute("""
            SELECT p.nom_produit, c.nom_categorie, SUM(lv.quantite) as qty, SUM(lv.quantite * lv.prix_unitaire_applique) as rev
            FROM lignes_ventes lv
            JOIN produits p ON lv.produit_id = p.id
            JOIN sous_categories sc ON p.sous_cat_id = sc.id
            JOIN categories c ON sc.categorie_id = c.id
            GROUP BY p.nom_produit, c.nom_categorie
            ORDER BY rev DESC LIMIT 5
        """)
        stats['top_products'] = [{"name": r[0], "category": r[1], "count": r[2], "revenue": float(r[3])} for r in cur.fetchall()]

        # 5. Ventes Récentes (Top 10)
        cur.execute("""
            SELECT cv.id, cl.nom || ' ' || cl.prenom, cv.date_vente, cv.total_ttc, cv.statut_commande
            FROM commandes_ventes cv
            JOIN clients cl ON cv.client_id = cl.id
            ORDER BY cv.date_vente DESC, cv.id DESC
            LIMIT 10
        """)
        stats['recent_sales'] = [{"id": r[0], "client": r[1], "date": str(r[2]), "amount": float(r[3]), "status": r[4]} for r in cur.fetchall()]

        # 6. Alertes Stock Bas (Moins de 20 unités)
        cur.execute("""
            SELECT p.nom_produit, s.quantite_disponible, d.nom_depot
            FROM stocks s
            JOIN produits p ON s.produit_id = p.id
            JOIN depots d ON s.depot_id = d.id
            WHERE s.quantite_disponible < 20
            ORDER BY s.quantite_disponible ASC
            LIMIT 8
        """)
        stats['stock_alerts'] = [{"name": r[0], "qty": r[1], "warehouse": r[2]} for r in cur.fetchall()]

        # 7. Ventes par Région (Gouvernorats)
        cur.execute("""
            SELECT vt.gouvernorat, SUM(cv.total_ttc) as total
            FROM commandes_ventes cv
            JOIN clients c ON cv.client_id = c.id
            JOIN villes_tunisie vt ON c.ville_id = vt.id
            GROUP BY vt.gouvernorat
            ORDER BY total DESC
            LIMIT 8
        """)
        stats['sales_by_region'] = [{"name": r[0], "value": float(r[1])} for r in cur.fetchall()]

        # 8. Clients les plus Fidèles (Top 5)
        cur.execute("""
            SELECT c.nom || ' ' || c.prenom, fp.points_accumules, COALESCE(SUM(cv.total_ttc), 0)
            FROM clients c
            JOIN fidelite_points fp ON c.id = fp.client_id
            LEFT JOIN commandes_ventes cv ON c.id = cv.client_id
            GROUP BY c.id, c.nom, c.prenom, fp.points_accumules
            ORDER BY fp.points_accumules DESC LIMIT 5
        """)
        stats['loyal_customers'] = [{"name": r[0], "points": r[1], "spent": float(r[2])} for r in cur.fetchall()]

        # 9. Modes de paiement (Répartition)
        cur.execute("""
            SELECT mp.type_paiement, SUM(p.montant) as total
            FROM paiements p
            JOIN modes_paiement mp ON p.mode_id = mp.id
            GROUP BY mp.type_paiement
            ORDER BY total DESC
        """)
        stats['payment_methods'] = [{"name": r[0], "value": float(r[1])} for r in cur.fetchall()]

        # 10. Produits les mieux notés (Top 5)
        cur.execute("""
            SELECT p.nom_produit, ROUND(AVG(ap.note), 1) as rating, COUNT(ap.id) as count
            FROM avis_produits ap
            JOIN produits p ON ap.produit_id = p.id
            GROUP BY p.nom_produit
            ORDER BY rating DESC, count DESC
            LIMIT 5
        """)
        stats['top_rated_products'] = [{"name": r[0], "rating": float(r[1]), "count": r[2]} for r in cur.fetchall()]

        cur.close()
        conn.close()
        return stats
    except Exception as e:
        print(f"Error in dashboard-stats: {e}")
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
