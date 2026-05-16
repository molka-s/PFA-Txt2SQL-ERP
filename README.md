# Txt2SQL — ERP Cosmétiques Tunisie

Pipeline local Text-to-SQL basé sur Phi-2 fine-tuné (LoRA) + RAG vectoriel sur schéma PostgreSQL.

---

## Architecture des fichiers

```
txt2sql_erp/
│
├── adapter/                        ← Coller ici les fichiers du zip phi2_peft
│   ├── adapter_config.json
│   ├── adapter_model.safetensors
│   ├── tokenizer.json
│   └── tokenizer_config.json
│
├── data/
│   └── dataset_erp_10k.json        ← Dataset d'entraînement (référence)
│
├── indexing/
│   ├── schema_indexer.py           ← Étape 1 : construire l'index FAISS (une seule fois)
│   ├── schema_linker.py            ← Schema linker RAG (utilisé au runtime)
│   └── faiss_index/                ← Généré automatiquement par schema_indexer.py
│       ├── index.faiss
│       ├── index.pkl
│       └── schema_raw.json
│
├── pipeline/
│   ├── model_loader.py             ← Charge Phi-2 + adapter LoRA
│   ├── txt2sql_pipeline.py         ← Pipeline principal (RAG → LLM → SQL → exec)
│   └── feedback.py                 ← Feedback loop MongoDB
│
├── ui/
│   └── app.py                      ← Interface Gradio
│
├── mongodb_exports/                ← Exports pour re-training
├── logs/
├── .env                            ← Variables d'environnement (à configurer)
└── requirements.txt
```

---

## Installation

```bash
# 1. Créer un environnement virtuel
python -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Copier l'adapter LoRA dans adapter/
# (décompresser phi2_peft.zip → copier les 4 fichiers dans adapter/)

# 4. Configurer .env avec vos paramètres PostgreSQL et MongoDB
```

---

## Démarrage — ordre obligatoire

### Étape 1 — Construire l'index FAISS (une seule fois)
```bash
python indexing/schema_indexer.py
```
Connecte à PostgreSQL, vectorise le schéma ERP + synonymes métier, sauvegarde l'index FAISS.

### Étape 2 — Lancer l'interface
```bash
python ui/app.py
```
Ouvrir http://localhost:7860

---

## Variables .env importantes

| Variable | Valeur par défaut | Description |
|---|---|---|
| `BASE_MODEL` | `microsoft/phi-2` | Modèle de base HuggingFace |
| `ADAPTER_PATH` | `./adapter` | Chemin vers l'adapter LoRA |
| `LOAD_IN_4BIT` | `true` | Quantization 4-bit (GPU requis) |
| `DB_NAME` | `entreprise_erp` | Nom de la base PostgreSQL |
| `TOP_K_TABLES` | `5` | Nombre de tables retournées par le RAG |
| `MAX_RETRIES` | `3` | Tentatives d'autocorrection SQL |

---

## Re-training (Phase 3)

Quand 500 interactions validées sont atteintes dans MongoDB :
```bash
# Exporter les données validées
python -c "from pipeline.feedback import export_for_retraining; export_for_retraining()"
# → mongodb_exports/retrain_dataset.json

# Uploader ce fichier dans Colab et relancer le fine-tuning QLoRA
```

---

## Notes techniques

- **CPU only** : mettre `LOAD_IN_4BIT=false` dans `.env` (lent, ~30s/requête)
- **GPU NVIDIA** : `LOAD_IN_4BIT=true` (rapide, ~2s/requête, 4 Go VRAM min)
- **MongoDB** : optionnel pour démarrer — commenter les imports `feedback` dans `app.py` si non installé
