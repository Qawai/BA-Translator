import os
import sys
import logging

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def check_integrity():
    base_dir = get_base_dir()

    required_files = [
        os.path.join("data", "config.json"),
        os.path.join("data", "dialogs_db.json"),
    ]
    missing = []
    for f in required_files:
        full = os.path.join(base_dir, f)
        if not os.path.exists(full):
            missing.append(f)
    if missing:
        logging.error("Missing required files: %s in %s", missing, base_dir)
        return False
    return True
