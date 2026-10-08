import json
import os

DEFAULT_DB = []

def load_dialogs_db():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dialogs_db.json")
    if not os.path.exists(db_path):
        return DEFAULT_DB
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_DB
