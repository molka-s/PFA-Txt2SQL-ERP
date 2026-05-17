# Txt2SQL ERP Pipeline — Cosmétix Tunisie

Ce projet est un moteur de recherche en langage naturel (Natural Language to SQL) conçu pour un ERP du secteur cosmétique. Il permet de poser des questions complexes en français et de recevoir les résultats directement depuis une base PostgreSQL.

---

## 🚀 Fonctionnalités Clés

- **Interface Conversationnelle** : Posez vos questions métier en français (ex: "Top 5 produits vendus à Ariana en 2024").
- **RAG Schema Linker** : Sélection dynamique des tables et colonnes pertinentes via **FAISS** et des embeddings **Multilingual-E5**.
- **Autocorrection Intelligente** : Si une requête SQL échoue, le pipeline analyse l'erreur PostgreSQL et tente de la corriger automatiquement.
- **Explorateur de Schéma** : Visualisation en temps réel des tables, colonnes et relations de la base de données.
- **Inférence Locale** : Utilisation d'**Ollama** pour garantir la confidentialité des données et l'indépendance cloud.

---

## 🏗️ Architecture du Projet

```text
txt2sql_erp(qwen)/
├── api.py                  # Serveur FastAPI (Backend)
├── indexing/               # Moteur de recherche vectoriel (RAG)
│   ├── schema_indexer.py   # Script de génération de l'index FAISS
│   └── schema_linker.py    # Recherche sémantique des tables au runtime
├── pipeline/               # Logique coeur Txt2SQL
│   ├── model_loader.py     # Connecteur Ollama
│   └── txt2sql_pipeline.py # Orchestrateur (RAG -> LLM -> Correction)
├── webapp/                 # Interface Utilisateur (React + Vite)
└── requirements.txt        # Dépendances Python
```

---

## 🛠️ Installation et Configuration

### 1. Prérequis
- Python 3.10+
- Node.js & npm (pour le frontend)
- PostgreSQL (avec vos données ERP)
- **Ollama** installé localement

### 2. Backend (Python)
```bash
# Créer l'environnement
python -m venv venv
source venv/bin/activate  # venv\Scripts\activate sur Windows

# Installer les dépendances
pip install -r requirements.txt

# Configurer les variables d'environnement
# Modifiez le fichier .env avec vos accès PostgreSQL et l'URL Ollama
```

### 3. Frontend (React)
```bash
cd webapp
npm install
```

---

## 🚦 Démarrage

### Étape 1 : Initialiser l'index sémantique
Cette étape vectorise la structure de votre base pour que l'IA sache quelles tables utiliser.
```bash
python indexing/schema_indexer.py
```

### Étape 2 : Lancer l'API Backend
```bash
python api.py
```
Le serveur sera disponible sur `http://localhost:8000`.

### Étape 3 : Lancer le Frontend
```bash
cd webapp
npm run dev
```
Ouvrez `http://localhost:5173` dans votre navigateur.

---

## ⚙️ Configuration (.env)

| Variable | Description |
|---|---|
| `DB_NAME` | Nom de votre base PostgreSQL |
| `OLLAMA_MODEL` | Modèle utilisé (ex: `qwen2.5-coder` ou `mon-modele-sql`) |
| `TOP_K_TABLES` | Nombre de tables injectées dans le prompt (défaut: 4) |
| `MAX_RETRIES` | Nombre de tentatives d'autocorrection (défaut: 3) |


