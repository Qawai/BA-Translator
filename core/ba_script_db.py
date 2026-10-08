import os
import re
import sqlite3
import logging

logger = logging.getLogger("BA_Translator")

_DB_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ba_data", "ba_scripts.db"),
    r"D:\BA_trans\ba_data\ba_scripts.db",
]

_db_path = None
_conn = None


def _find_db():
    global _db_path
    if _db_path:
        return _db_path
    for p in _DB_CANDIDATES:
        if os.path.exists(p):
            _db_path = p
            return p
    return None


def _conn_get():
    global _conn
    if _conn is None:
        p = _find_db()
        if not p:
            return None
        _conn = sqlite3.connect(p, check_same_thread=False)
    return _conn


def normalize(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def lookup(en_text):
    """Exact lookup of a dialogue line. Returns dict or None."""
    c = _conn_get()
    if not c:
        return None
    q = normalize(en_text)
    if not q:
        return None
    try:
        row = c.execute(
            "SELECT group_id, en, jp FROM lines WHERE norm=? LIMIT 1", (q,)
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug(f"ba_scripts lookup error: {e}")
        return None
    if not row:
        return None
    return {"group_id": row[0], "en": row[1], "jp": row[2]}


def lookup_prefix(en_text, min_words=5):
    """Fallback: match by first N normalized words (tolerates OCR typos)."""
    c = _conn_get()
    if not c:
        return None
    words = normalize(en_text).split()
    if len(words) < min_words:
        return None
    prefix = " ".join(words[:min_words])
    try:
        rows = c.execute(
            "SELECT group_id, en, jp, norm FROM lines WHERE norm LIKE ? LIMIT 8",
            (prefix + " %",),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    if len(rows) == 1:
        return {"group_id": rows[0][0], "en": rows[0][1], "jp": rows[0][2]}
    # several candidates: pick closest by word count
    target = len(words)
    best = min(rows, key=lambda r: abs(len(r[3].split()) - target))
    return {"group_id": best[0], "en": best[1], "jp": best[2]}


def episode_for_group(group_id):
    """Return 'Vol.X Ch.Y Ep.Z' string or None."""
    c = _conn_get()
    if not c or not group_id:
        return None
    try:
        row = c.execute(
            "SELECT volume, chapter, episode FROM episodes WHERE group_id=? LIMIT 1",
            (group_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    v, ch, ep = row
    return f"Vol.{v} Ch.{ch} Ep.{ep}"


def close():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
