"""Keep player and BA character names verbatim in translations.

The EN->RU backend transliterates names ("Shiroko" -> "Широко") and the
player's own name into a random guess. A dialogue line is therefore split
into name / non-name segments: only the non-name segments go to the
translator and the names are stitched back exactly as written."""

import re

# Distinctive Blue Archive character names only — common English words
# (Marina, Shun, Angel, Grace...) are deliberately excluded so normal
# dialogue words are never frozen by mistake.
BA_NAMES = (
    "Airi", "Akane", "Akari", "Ako", "Aris", "Arona", "Aru", "Atsuko",
    "Azusa", "Cherino", "Chinatsu", "Eimi", "Fubuki", "Hanae", "Haruka",
    "Hasumi", "Hifumi", "Hina", "Hinata", "Hiori", "Hiyori", "Hoshino",
    "Ichika", "Iori", "Izuna", "Juri", "Karin", "Kayoko", "Kikyou",
    "Koharu", "Midori", "Mika", "Miyako", "Misaki", "Moe", "Momiji",
    "Momoi", "Mutsuki", "Nagisa", "Nodoka", "Nonomi", "Plana", "Rin",
    "Rumi", "Saori", "Serika", "Shiroko", "Suzumi", "Tomoe", "Tsukuyo",
    "Tsurugi", "Wakamo", "Yuuka", "Yuuna",
)

_player_name = ""
_pat = None


def set_player_name(name):
    """Register the player's in-game name ("" = none). Rebuilds the
    name pattern so it takes effect immediately."""
    global _player_name, _pat
    _player_name = (name or "").strip()[:32]
    _rebuild()


def get_player_name():
    return _player_name


def _rebuild():
    global _pat
    names = [n for n in BA_NAMES if n]
    if _player_name:
        names.append(_player_name)
    if not names:
        _pat = None
        return
    names = sorted(set(names), key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    # CAPTURING group: re.split then keeps the matched names at odd
    # indices (even = non-name parts) — the structure prepare()/stitch()
    # rely on. \b keeps matches word-bounded; longest-first alternation
    # makes multi-word names safe too.
    _pat = re.compile(r"\b(" + alternation + r")\b", re.IGNORECASE)


def contains_name(text):
    return bool(text) and _pat is not None and _pat.search(text) is not None


def prepare(text):
    """Split `text` for name-preserving translation.

    Returns None when the line contains no names (normal fast path), else
    a tuple (segs, idxs, src):
      segs  - re.split result, even indices are non-name parts, odd are names
      idxs  - indices of the NON-EMPTY non-name parts
      src   - those parts joined by "\\n" (one translator input per line)
    """
    if not text or _pat is None:
        return None
    segs = _pat.split(text)
    if len(segs) == 1:
        return None
    idxs = [i for i in range(0, len(segs), 2) if segs[i].strip()]
    src = "\n".join(segs[i] for i in idxs)
    return (segs, idxs, src)


def stitch(prepared, translated_src):
    """Merge translated non-name parts back between the untouched names.
    Returns None when the counts do not match (caller keeps the source)."""
    if not prepared:
        return None
    segs, idxs, _src = prepared
    if not idxs:
        # The line was nothing but names.
        return "".join(segs)
    if not translated_src:
        return None
    pieces = translated_src.split("\n")
    if len(pieces) != len(idxs):
        return None
    out = list(segs)
    for i, piece in zip(idxs, pieces):
        orig = segs[i]
        lead = orig[: len(orig) - len(orig.lstrip())]
        trail = orig[len(orig.rstrip()):]
        out[i] = lead + piece.strip() + trail
    return "".join(out)


_rebuild()
