"""
indexing/schema_indexer.py
Extrait le schéma PostgreSQL ERP, l'enrichit avec les synonymes métier,
et construit un index FAISS pour le schema linker.
Exécuter une seule fois : python indexing/schema_indexer.py
"""
import os
import json
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain.schema import Document

load_dotenv()

# ─── Synonymes métier ERP Cosmétiques TN ──────────────────────────────────────
SCHEMA_SEMANTICS = {
    "Clients": {
        "description": "Clients, acheteurs, membres, personnes inscrites, utilisateurs",
        "keywords": ["client", "acheteur", "membre", "inscrit", "personne", "consommateur",
                     "fidele", "programme fidelite", "profil client"]
    },
    "Boutiques": {
        "description": "Boutiques, magasins, points de vente, succursales, enseignes",
        "keywords": ["boutique", "magasin", "point de vente", "succursale", "enseigne",
                     "commerce", "shop", "agence", "etablissement"]
    },
    "Produits": {
        "description": "Produits, articles, references, cosmetiques, items du catalogue",
        "keywords": ["produit", "article", "reference", "cosmetique", "item", "bien",
                     "marchandise", "stock", "catalogue", "soin", "maquillage", "parfum"]
    },
    "Commandes_Ventes": {
        "description": "Commandes, ventes, achats, transactions, factures, tickets de caisse",
        "keywords": ["commande", "vente", "achat", "transaction", "facture", "ticket",
                     "CA", "chiffre affaires", "revenu", "recette", "encaissement"]
    },
    "Lignes_Ventes": {
        "description": "Lignes de vente, details commande, articles commandes, quantites vendues",
        "keywords": ["ligne vente", "detail commande", "quantite vendue", "article commande",
                     "panier", "contenu commande", "produit vendu"]
    },
    "Stocks": {
        "description": "Stocks, inventaire, quantites disponibles, niveaux de stock",
        "keywords": ["stock", "inventaire", "quantite", "disponible", "rupture", "alerte stock",
                     "approvisionnement", "reserve", "entrepot"]
    },
    "Depots": {
        "description": "Depots, entrepots, centres de stockage, magasins centraux",
        "keywords": ["depot", "entrepot", "stockage", "centre logistique", "magasin central",
                     "reserve", "chambre froide"]
    },
    "Fournisseurs": {
        "description": "Fournisseurs, prestataires, distributeurs, grossistes",
        "keywords": ["fournisseur", "prestataire", "distributeur", "grossiste", "vendeur",
                     "importateur", "livreur", "partenaire commercial"]
    },
    "Achats_Fournisseurs": {
        "description": "Achats fournisseurs, bons de commande, approvisionnements",
        "keywords": ["achat", "fournisseur", "approvisionnement", "commande fournisseur",
                    "reception", "montant achat", "quantite achat"],
    },
    "Marques": {
        "description": "Marques, fabricants, labels, enseignes de produits cosmetiques",
        "keywords": ["marque", "fabricant", "label", "enseigne", "brand", "L Oreal",
                     "Garnier", "Nivea", "Maybelline", "Dove"]
    },
    "Categories": {
        "description": "Categories, rayons, familles de produits cosmetiques",
        "keywords": ["categorie", "rayon", "famille", "type produit", "gamme",
                     "soins visage", "soins corps", "maquillage", "parfum", "shampooing"]
    },
    "Sous_Categories": {
        "description": "Sous-categories, sous-rayons, types precis de produits",
        "keywords": ["sous-categorie", "sous-rayon", "type precis", "segment",
                     "creme", "serum", "fond teint", "mascara", "rouge levres"]
    },
    "Promotions": {
        "description": "Promotions, offres, remises, reductions, soldes, bons plans",
        "keywords": ["promotion", "promo", "offre", "remise", "reduction", "solde",
                     "bon plan", "discount", "Ramadan", "Black Friday", "Fete meres"]
    },
    "Produits_Promos": {
        "description": "Association produits et promotions, articles en promo",
        "keywords": ["produit promo", "article en promotion", "article remise",
                     "produit solde", "article offre"]
    },
    "Livraisons": {
        "description": "Livraisons, expeditions, colis, suivi commandes, transporteurs",
        "keywords": ["livraison", "expedition", "colis", "suivi", "transporteur",
                     "societe livraison", "Aramex", "La Poste", "TunExpress", "statut livraison"]
    },
    "Retours_Clients": {
        "description": "Retours clients, remboursements, SAV, produits retournes, insatisfactions",
        "keywords": ["retour", "remboursement", "SAV", "service apres vente", "reclamation",
                     "insatisfaction", "produit retourne", "echange", "motif retour"]
    },
    "Employes": {
        "description": "Employes, personnel, salaries, vendeurs, conseillers beaute, managers",
        "keywords": ["employe", "personnel", "salarie", "vendeur", "conseillere beaute",
                     "maquilleuse", "responsable boutique", "staff", "equipe", "salaire"]
    },
    "Avis_Produits": {
        "description": "Avis clients, notes, commentaires, evaluations, satisfaction produits",
        "keywords": ["avis", "note", "commentaire", "evaluation", "satisfaction", "etoile",
                     "feedback", "opinion", "notation", "review"]
    },
    "Fidelite_Points": {
        "description": "Programme de fidelite, points fidelite, carte membre, avantages clients",
        "keywords": ["fidelite", "points", "carte membre", "avantage", "programme",
                     "cumul points", "recompense", "niveau fidelite"]
    },
    "Villes_Tunisie": {
        "description": "Villes et gouvernorats de Tunisie, localisation geographique clients",
        "keywords": ["ville", "gouvernorat", "region", "localite", "zone", "Tunis",
                     "Sfax", "Sousse", "Nabeul", "geographie", "emplacement"]
    },
    "Depenses_Fixes": {
        "description": "Depenses, charges, couts operationnels des boutiques, factures",
        "keywords": ["depense", "charge", "cout", "loyer", "electricite", "salaire",
                     "marketing", "frais", "budget", "operationnel", "montant depense"]
    },
    "Paiements": {
        "description": "Paiements, règlements, transactions financières",
        "keywords": ["paiement", "reglement", "montant paye", "transaction", "encaissement"]
    },
    "Modes_Paiement": {
        "description": "Modes de paiement, types de reglement (espèces, carte, chèque)",
        "keywords": ["mode paiement", "type paiement", "especes", "carte", "cheque", "virement"]
    },
    "Details_Achats": {
        "description": "Details des achats fournisseurs, lignes de commande fournisseur",
        "keywords": ["detail achat", "ligne achat", "quantite achetee", "prix achat"]
    },
    "Logs_Systeme": {
        "description": "Logs systeme, historique actions, journal evenements, activite utilisateurs",
        "keywords": ["log", "journal", "historique", "action", "evenement", "trace",
                     "activite", "connexion", "audit", "systeme"]
    },
}

# ─── Auto‑découverte des valeurs de colonnes à faible cardinalité ────────────
AUTO_DISCOVER = {
    "Villes_Tunisie": ["nom_ville", "gouvernorat"],
    "Marques": ["nom_marque"],
    "Categories": ["nom_categorie"],
    "Sous_Categories": ["nom_sous_cat"],
    "Modes_Paiement": ["type_paiement"],
    "Promotions": ["nom_promo"],
    "Boutiques": ["nom_boutique"],
    "Fournisseurs": ["nom_fournisseur"],
    "Depots": ["nom_depot"],
    "Produits": ["statut"],
    "Commandes_Ventes": ["statut_commande"],
    "Livraisons": ["statut", "societe_livraison"],
    "Retours_Clients": ["motif"],
    "Employes": ["poste"],
    "Clients": ["genre"],
    "Depenses_Fixes": ["type_depense"],
}


def get_distinct_values(engine, table, column, max_vals=50):
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(f"SELECT DISTINCT {column} FROM {table} "
                     f"WHERE {column} IS NOT NULL LIMIT {max_vals}")
            )
            return [str(row[0]) for row in result]
    except Exception as e:
        print(f"  → Attention : impossible de lire {table}.{column} - {e}")
        return []


# ─── Récupération automatique des relations depuis PostgreSQL ────────────────
def extract_all_relations(inspector, table_names):
    """
    Récupère toutes les FK de la base et construit :
    - relations_by_table : { table -> [{ local_col, foreign_table, foreign_col }] }
    - join_paths         : { (tableA, tableB) -> "SQL JOIN clause" }
    - graph              : { table -> [tables directement liées] }
    """
    relations_by_table = {t: [] for t in table_names}
    graph = {t: [] for t in table_names}

    for table in table_names:
        fks = inspector.get_foreign_keys(table)
        for fk in fks:
            local_cols = fk["constrained_columns"]
            foreign_table = fk["referred_table"]
            foreign_cols = fk["referred_columns"]

            if not local_cols or not foreign_cols:
                continue

            rel = {
                "local_col": local_cols[0],
                "foreign_table": foreign_table,
                "foreign_col": foreign_cols[0],
            }
            relations_by_table[table].append(rel)

            # Graphe bidirectionnel
            if foreign_table not in graph[table]:
                graph[table].append(foreign_table)
            if table in graph and table not in graph.get(foreign_table, []):
                graph.setdefault(foreign_table, []).append(table)

    # Construire les clauses JOIN directes
    join_paths = {}
    for table, rels in relations_by_table.items():
        for rel in rels:
            ft = rel["foreign_table"]
            join_clause = (
                f"JOIN {ft} ON {table}.{rel['local_col']} = {ft}.{rel['foreign_col']}"
            )
            join_paths[(table, ft)] = join_clause
            # Jointure inverse aussi
            join_paths[(ft, table)] = (
                f"JOIN {table} ON {ft}.{rel['foreign_col']} = {table}.{rel['local_col']}"
            )

    return relations_by_table, join_paths, graph


def find_join_path(start, end, graph, join_paths, max_depth=3):
    """
    BFS pour trouver le chemin de jointure entre deux tables non directement liées.
    Retourne une liste de clauses JOIN ordonnées, ou None si pas de chemin trouvé.
    """
    if start == end:
        return []

    # Chemin direct
    if (start, end) in join_paths:
        return [join_paths[(start, end)]]

    # BFS
    from collections import deque
    queue = deque([(start, [start], [])])
    visited = {start}

    while queue:
        current, path, joins = queue.popleft()

        if len(path) > max_depth:
            continue

        for neighbor in graph.get(current, []):
            if neighbor in visited:
                continue

            new_joins = joins + [join_paths.get((current, neighbor), "")]
            new_path = path + [neighbor]

            if neighbor == end:
                return new_joins

            visited.add(neighbor)
            queue.append((neighbor, new_path, new_joins))

    return None


# ─── Construction de l'index FAISS ──────────────────────────────────────────
def build_faiss_index():
    db_url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    engine = create_engine(db_url)
    inspector = inspect(engine)

    embedding_model = HuggingFaceEmbeddings(
        model_name=os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-base"),
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    table_names = inspector.get_table_names()
    print(f"[indexer] {len(table_names)} tables trouvées dans la base ERP")

    # ── Extraction automatique des relations ──
    print("[indexer] Extraction des relations FK depuis PostgreSQL...")
    relations_by_table, join_paths, graph = extract_all_relations(inspector, table_names)
    print(f"[indexer] {len(join_paths)//2} relations FK trouvées")
    print(f"[indexer] {len(join_paths)//2} relations FK trouvees")

    # ── Affichage du graphe de relations (debug) ──
    print("\n[indexer] Graphe de relations :")
    for table, neighbors in graph.items():
        if neighbors:
            print(f"  {table} <-> {', '.join(neighbors)}")

    documents = []

    # ── Document par table ──
    for table in table_names:
        cols = inspector.get_columns(table)
        col_descriptions = [f"{c['name']} ({str(c['type'])})" for c in cols]

        # Recherche de sémantique insensible à la casse
        semantics = {}
        for key, value in SCHEMA_SEMANTICS.items():
            if key.lower() == table.lower():
                semantics = value
                break
        
        keywords  = ", ".join(semantics.get("keywords", []))
        description = semantics.get("description", table)

        content = (
            f"TABLE NAME: {table}\n"
            f"TABLE DESCRIPTION: {description}\n"
            f"SYNONYMS AND KEYWORDS: {keywords}\n"
            f"COLUMNS FOR TABLE {table}: {', '.join(col_descriptions)}\n"
        )

        # Relations directes (FK sortantes)
        rels = relations_by_table.get(table, [])
        if rels:
            content += "Relations directes:\n"
            for rel in rels:
                ft = rel["foreign_table"]
                content += (
                    f"  {table}.{rel['local_col']} -> {ft}.{rel['foreign_col']}\n"
                    f"  SQL: JOIN {ft} ON {table}.{rel['local_col']} = {ft}.{rel['foreign_col']}\n"
                )

        # Tables accessibles via jointures (voisins dans le graphe)
        neighbors = graph.get(table, [])
        if neighbors:
            content += f"Tables liees: {', '.join(neighbors)}\n"

        # Valeurs distinctes
        lookup_table = table if table in AUTO_DISCOVER else table.capitalize()
        if lookup_table in AUTO_DISCOVER:
            for column in AUTO_DISCOVER[lookup_table]:
                values = get_distinct_values(engine, table, column)
                if values:
                    content += f"Valeurs possibles pour {column}: {', '.join(values)}\n"

        documents.append(Document(
            page_content=content,
            metadata={
                "table": table,
                "columns": [c["name"] for c in cols],
                "related_tables": neighbors,
            }
        ))
        print(f"  -> {table} ({len(cols)} cols, {len(rels)} FK)")

    # ── Documents de chemins de jointure multi-tables ──
    print("\n[indexer] Generation des chemins de jointure multi-tables...")
    important_pairs = [
        ("Clients",           "Produits"),
        ("Clients",           "Boutiques"),
        ("Clients",           "Promotions"),
        ("Clients",           "Fidelite_Points"),
        ("Produits",          "Fournisseurs"),
        ("Produits",          "Promotions"),
        ("Produits",          "Categories"),
        ("Produits",          "Marques"),
        ("Produits",          "Stocks"),
        ("Commandes_Ventes",  "Produits"),
        ("Commandes_Ventes",  "Clients"),
        ("Commandes_Ventes",  "Boutiques"),
        ("Boutiques",         "Employes"),
        ("Boutiques",         "Stocks"),
        ("Livraisons",        "Clients"),
        ("Retours_Clients",   "Produits"),
        ("Avis_Produits",     "Clients"),
    ]

    for start, end in important_pairs:
        # Recherche insensible à la casse
        start_low = start.lower()
        end_low = end.lower()
        path = find_join_path(start_low, end_low, graph, join_paths)
        if path:
            joins_sql = "\n".join(path)
            content = (
                f"Pour relier {start} et {end}:\n"
                f"Chemin: {start} -> {end}\n"
                f"Jointures SQL:\n{joins_sql}\n"
            )
            documents.append(Document(
                page_content=content,
                metadata={"type": "join_path", "from": start, "to": end}
            ))
            print(f"  [OK] {start} -> {end} ({len(path)} JOIN(s))")
        else:
            print(f"  [ERR] Pas de chemin trouve : {start} -> {end}")

    # ── Sauvegarde FAISS ──
    index_path = os.getenv("FAISS_INDEX_PATH", "./indexing/faiss_index")
    os.makedirs(index_path, exist_ok=True)

    vectorstore = FAISS.from_documents(documents, embedding_model)
    vectorstore.save_local(index_path)
    print(f"\n[indexer] Index FAISS sauvegardé dans : {index_path}")

    # ── Sauvegarde JSON (debug) ──
    schema_json = {
        doc.metadata["table"]: {
            "columns": doc.metadata["columns"],
            "related_tables": doc.metadata.get("related_tables", []),
        }
        for doc in documents
        if "table" in doc.metadata
    }
    with open(os.path.join(index_path, "schema_raw.json"), "w", encoding="utf-8") as f:
        json.dump(schema_json, f, ensure_ascii=False, indent=2)

    # Sauvegarder aussi les join paths pour usage externe
    join_paths_serializable = {f"{k[0]}→{k[1]}": v for k, v in join_paths.items()}
    with open(os.path.join(index_path, "join_paths.json"), "w", encoding="utf-8") as f:
        json.dump(join_paths_serializable, f, ensure_ascii=False, indent=2)

    print(f"[indexer] schema_raw.json + join_paths.json sauvegardés")


if __name__ == "__main__":
    build_faiss_index()