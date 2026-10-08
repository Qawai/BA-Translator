import logging
import urllib.request
import urllib.parse
import json
import time
import re

logger = logging.getLogger("BA_Translator")

MYMEMORY_API = "https://api.mymemory.translated.net/get"
LIBRETRANSLATE_API = "https://libretranslate.com/translate"
GOOGLE_API = "https://translate.googleapis.com/translate_a/single"

MAX_RETRIES = 1
REQUEST_TIMEOUT = 5

SYSTEM_PATTERNS = [
    "MYMEMORY WARNING",
    "QUERY LENGTH LIMIT EXCEEDED",
    "LENGTH LIMIT",
    "QUOTA",
    "INVALID LANGUAGE",
]

_JP_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
_CY_RE = re.compile(r'[\u0400-\u04ff]')


def _looks_garbled(text, target_lang="ru"):
    """Reject translations that are mojibake, mostly question marks,
    replacement characters, or contain non-target-script characters.
    Google's free endpoint occasionally returns garbage for short/partial
    inputs or rate-limited requests."""
    if not text or len(text.strip()) < 2:
        return True
    # Count replacement / question-mark characters
    bad = sum(1 for c in text if c in '?\ufffd\ufffe\uffff')
    total = len(text)
    if bad > 0 and bad / total >= 0.2:
        return True
    if target_lang == "ru":
        # For Russian translations: every letter-like character must be
        # Cyrillic, Latin (common in names/abbreviations), or CJK (name
        # plate text). Anything else = mojibake from the API.
        foreign = 0
        letters = 0
        for c in text:
            if c.isalpha():
                letters += 1
                cp = ord(c)
                if (0x0400 <= cp <= 0x04FF    # Cyrillic
                        or 0x0041 <= cp <= 0x007A  # Latin
                        or 0x4E00 <= cp <= 0x9FFF  # CJK
                        or 0x3040 <= cp <= 0x30FF  # Japanese
                        or cp in (0x0456, 0x0457, 0x0454, 0x0491)):  # їієґ
                    continue
                foreign += 1
        if letters >= 3 and foreign / letters > 0.2:
            return True
        # A Russian translation should be mostly Cyrillic letters, not
        # Latin. If >60% of letters are Latin, it's likely untranslated
        # or garbled (names/abbreviations are a small fraction).
        lat = sum(1 for c in text if 'A' <= c <= 'Z' or 'a' <= c <= 'z')
        if letters >= 4 and lat / letters > 0.6:
            return True
    return False


def detect_source_lang(text):
    """Best-effort script detection so we don't OCR/translate garbage.

    Japanese -> 'ja' (translate JP->RU)
    Cyrillic  -> 'ru' (already the target; show as-is)
    otherwise -> 'en' (translate EN->RU)
    """
    if _JP_RE.search(text or ""):
        return "ja"
    if _CY_RE.search(text or ""):
        return "ru"
    return "en"


def _is_system_message(text):
    if not text:
        return True
    upper = text.upper()
    return any(pattern in upper for pattern in SYSTEM_PATTERNS)


def _translate_google(text, target_lang="ru", source_lang="ja"):
    try:
        params = {
            "client": "gtx",
            "sl": source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text,
        }
        url = GOOGLE_API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and data[0]:
                translated = "".join(part[0] for part in data[0] if part[0])
                if translated and translated.strip():
                    if _looks_garbled(translated, target_lang):
                        logger.warning(f"Google result looks garbled, skipping: '{translated[:60]}'")
                        return None
                    return translated.strip()
    except Exception as e:
        logger.warning(f"Google translate failed: {e}")
    return None


def _translate_mymemory(text, target_lang="ru", source_lang="ja"):
    try:
        params = {
            "q": text,
            "langpair": f"{source_lang}|{target_lang}"
        }
        url = MYMEMORY_API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            translated = data.get("responseData", {}).get("translatedText", "")
            if translated and translated.strip() and not _is_system_message(translated):
                if _looks_garbled(translated, target_lang):
                    logger.warning(f"MyMemory result looks garbled, skipping: '{translated[:60]}'")
                    return None
                return translated.strip()
    except Exception as e:
        logger.warning(f"MyMemory translate failed: {e}")
    return None


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
# Free-tier models to try, newest first, when the configured one 404s
# (Google retires old models; gemini-1.5-flash no longer answers).
GEMINI_MODEL_FALLBACKS = ["gemini-2.5-flash", "gemini-2.0-flash"]
# Free-tier latency easily exceeds the global REQUEST_TIMEOUT (5s).
GEMINI_TIMEOUT = 12


def _extract_json_array(raw):
    """Pull a JSON array out of a model response, tolerating markdown fences
    or stray surrounding text."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r'^```[a-zA-Z]*\n?', '', s)
        s = re.sub(r'\n?```$', '', s).strip()
    start = s.find('[')
    end = s.rfind(']')
    if start != -1 and end != -1 and end > start:
        try:
            arr = json.loads(s[start:end + 1])
            if isinstance(arr, list):
                return arr
        except Exception:
            pass
    return None


def gemini_translate_list(texts, target_lang="ru", model=DEFAULT_GEMINI_MODEL, api_key=None):
    """Translate a list of lines via Gemini, returning a 1:1 ordered list.

    Asks for a JSON array response so the number of lines is preserved exactly
    (Google's free endpoint merges lines and we lose them). High quality for
    visual-novel / game text. Needs a free API key from Google AI Studio.
    Tries the configured model first, then free-tier fallbacks (retired
    models 404); gives up early on quota errors (same key, useless to retry).
    """
    if not api_key or not texts:
        return None
    lang_names = {"ru": "Russian", "en": "English", "ja": "Japanese"}
    tgt = lang_names.get(target_lang, "Russian")
    prompt = (
        "You are a professional translator of visual novels and games (source: English or Japanese) "
        f"into {tgt}. Translate ONLY the dialogue/text; do not add notes, "
        "explanations, or extra lines. Preserve the exact number of lines and "
        "their order. Respond with a JSON array of strings only (one "
        "translation per input line), no markdown, no numbering.\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )

    models = [model or DEFAULT_GEMINI_MODEL]
    for m in [DEFAULT_GEMINI_MODEL] + GEMINI_MODEL_FALLBACKS:
        if m not in models:
            models.append(m)

    def _call(m):
        url = GEMINI_API.format(model=m) + "?key=" + api_key
        gen_cfg = {"temperature": 0.2, "responseMimeType": "application/json"}
        if "2.5" in (m or ""):
            # 2.5-flash thinks before answering (several seconds of "hanging"
            # on free keys): turn thinking off for instant replies.
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return _extract_json_array(raw)

    for m in models:
        try:
            result = _call(m)
            if result is not None:
                if m != (model or DEFAULT_GEMINI_MODEL):
                    logger.info(f"Gemini: using fallback model {m}")
                return [str(r) for r in result]
            logger.warning(f"Gemini model {m}: no JSON array in response")
        except Exception as e:
            msg = str(e).lower()
            logger.warning(f"Gemini model {m} failed: {e}")
            if "429" in msg or "resource_exhausted" in msg or "quota" in msg or "rate" in msg:
                break  # key-level quota: every model will fail the same way
        # otherwise (404/retired model etc.): try the next model
    return None


def translate_text(text, target_lang="ru", source_lang="ja"):
    if not text or not text.strip():
        logger.warning("translate_text called with empty text")
        return None

    if len(text) > 4000:
        text = text[:4000]

    logger.debug(f"Translation request: {source_lang}|{target_lang}, text='{text[:50]}'")

    # Google's free endpoint hiccups (429/network) — retry before giving up;
    # a single failed line used to silently fall back to the untranslated
    # source ("some words just stay in English").
    for attempt in range(MAX_RETRIES + 1):
        result = _translate_google(text, target_lang, source_lang)
        if result:
            logger.debug(f"Google result: '{result[:50]}'")
            return result
        if attempt < MAX_RETRIES:
            time.sleep(0.3)

    result = _translate_mymemory(text, target_lang, source_lang)
    if result:
        logger.debug(f"MyMemory result: '{result[:50]}'")
        return result

    logger.error("All translation APIs failed")
    return None

