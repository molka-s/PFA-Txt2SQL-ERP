import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pipeline.txt2sql_pipeline import run
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

class QuestionRequest(BaseModel):
    question: str

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "entreprise_erp"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "root")
    )

@app.post("/ask")
async def ask_question(req: QuestionRequest):
    try:
        result = run(req.question)
        return result
    except Exception as e:
        return {
            "question": req.question,
            "sql": None,
            "success": False,
            "results": None,
            "error": str(e),
            "attempts": 0
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
