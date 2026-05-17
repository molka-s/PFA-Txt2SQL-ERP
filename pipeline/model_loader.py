"""
pipeline/model_loader.py
Interface pour la génération SQL via Ollama.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

def generate_sql_ollama(messages: list[dict] | str, temperature: float = 0.0) -> str:
    """
    Génère du SQL via l'API locale d'Ollama.
    """
    base_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    base_url = base_url.replace("/api/generate", "").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "mon-modele-sql")
    
    if isinstance(messages, list):
        url = f"{base_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature
            }
        }
    else:
        url = f"{base_url}/api/generate"
        payload = {
            "model": model,
            "prompt": messages,
            "stream": False,
            "options": {
                "temperature": temperature
            }
        }
    
    print(f"[ollama] Envoi requête à {url} (modèle: {model})")
    
    # Tentative avec retry pour les erreurs 500 intermittentes
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, timeout=60)
            if response.status_code == 500 and attempt == 0:
                print("[ollama] Erreur 500 reçue, nouvelle tentative...")
                continue
                
            response.raise_for_status()
            res_json = response.json()
            if isinstance(messages, list):
                sql = res_json.get("message", {}).get("content", "")
            else:
                sql = res_json.get("response", "")
            
            # Nettoyage des \n littéraux
            sql = sql.replace("\\n", "\n")
            
            return sql.strip()
        except Exception as e:
            if attempt == 1:
                print(f"[ollama] Erreur persistante : {e}")
                return f"-- Erreur Ollama: {str(e)}"
            print(f"[ollama] Erreur (tentative {attempt+1}): {e}")
    
    return "-- Erreur inconnue Ollama"


def generate_sql(messages: list[dict] | str, temperature: float = 0.0) -> str:
    """
    Point d'entrée principal pour la génération SQL.
    Utilise Ollama par défaut.
    """
    return generate_sql_ollama(messages, temperature)
