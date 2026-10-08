import os
import sys
import json
import logging
import re
import threading
import time
import cv2
import numpy as np
import pytesseract
import mss

from core.config_manager import ConfigManager
from core.translator_api import translate_text
from core import ocr_engine

logger = logging.getLogger("BA_Translator")

# OCR languages keyed by the language of the on-screen text (source_lang).
# "en" games must NOT use jpn: misreads of HUD glyphs came back as CJK
# and sailed through the looks_like_text CJK rule as fake dialogue.
SOURCE_OCR_LANG = {
    "en": "eng",
    "ja": "jpn+eng",
    "auto": "jpn+eng",
}

if sys.platform == "win32":
    tesseract_paths = [
        r"C:\Program Files\Tesseract-OCR",
        r"C:\Program Files (x86)\Tesseract-OCR",
        os.path.join(os.path.dirname(sys.executable), "tesseract"),
    ]
    if getattr(sys, 'frozen', False):
        # Bundled tesseract (ships inside the exe, extracted to _MEIPASS).
        tesseract_paths.append(os.path.join(getattr(sys, '_MEIPASS', ''), "tesseract"))
    tesseract_paths += [
        r"Z:\Tesseract-OCR",
        r"Z:\tesseract",
    ]
    for _p in tesseract_paths:
        tess_exe = os.path.join(_p, "tesseract.exe")
        if os.path.isfile(tess_exe):
            pytesseract.pytesseract.tesseract_cmd = tess_exe
            os.environ["PATH"] = _p + ";" + os.environ.get("PATH", "")
            logger.debug(f"Tesseract found at: {_p}")
            break
    else:
        logger.warning("Tesseract not found in standard locations")


def looks_like_text(s, allow_cjk=True):
    """Heuristic: does this OCR fragment look like real dialogue text
    (letters/words) rather than HUD glyph garbage?

    Junk that used to pass ('Vv eS', 'mm | Vv ~', '2??? ose om') is a pair
    of 1-2 letter "words" — real dialogue has at least two 3-letter words;
    a lone 3-letter word among small ones ('i -- et a, Ty | Ss',
    ': | ne were a ... - a J oa a') is transition/frame noise. A two-word
    fragment ending in sentence punctuation ("Hi there.") is kept, and so
    is an asterisk-wrapped sound caption ('*whirring*') — BA renders SFX
    that way and it is the only text on screen during many cuts."""
    if not s:
        return False
    if allow_cjk:
        cjk = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]', s))
        if cjk >= 3:
            return True
    # Asterisk-wrapped single word = game SFX caption, not HUD noise.
    cap = re.fullmatch(r"\s*\*([^*]+)\*\s*", s)
    if cap and sum(1 for c in cap.group(1) if c.isalpha()) >= 3:
        return True
    words = re.findall(r"[A-Za-z']+", s)
    strong = sum(1 for w in words if len(w) >= 3)
    weak = sum(1 for w in words if len(w) >= 2)
    looks_wordy = strong >= 2
    if not looks_wordy and weak >= 2 and re.search(r'[.!?]["\')\]]*\s*$', s.strip()):
        looks_wordy = True
    if not looks_wordy:
        return False
    letters = sum(1 for c in s if c.isalpha())
    visible = sum(1 for c in s if not c.isspace())
    return visible > 0 and letters / visible >= 0.5


class ScreenProcessor:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config
        # mss instances are thread-affine (thread-local DC state): keep one
        # per thread instead of sharing across GUI/worker threads.
        self._tls = threading.local()
        self._capture_lock = threading.Lock()
        self._dxcam = None
        try:
            import dxcam
            self._dxcam = dxcam.create()
            logger.info("DXGI capture (dxcam) initialized")
        except Exception as e:
            logger.info(f"dxcam unavailable, using mss: {e}")
            self._dxcam = None
        self._tessdata_dir = self._find_tessdata()
        self._available_langs = self._get_available_langs()
        logger.debug(f"ScreenProcessor init: tessdata={self._tessdata_dir}, langs={self._available_langs}")
        # Resolve/imports the configured OCR backend ONCE here: packaging
        # or language problems show up in the log at startup, not on the
        # first click.
        self._ocr_backend = ocr_engine.warmup(
            self.config.get("ocr_backend", "auto"),
            self.config.get("source_lang", "en"))

    def _get_available_langs(self):
        langs = ['eng']
        if self._tessdata_dir:
            try:
                files = os.listdir(self._tessdata_dir)
                for f in files:
                    if f.endswith('.traineddata'):
                        lang = f.replace('.traineddata', '').lower()
                        if lang not in langs:
                            langs.append(lang)
                logger.info(f"Available Tesseract languages from files: {langs}")
            except Exception as e:
                logger.warning(f"Failed to list tessdata files: {e}")
        else:
            try:
                langs = [l.lower() for l in pytesseract.get_languages(config='')]
            except Exception as e:
                logger.warning(f"Failed to get Tesseract languages: {e}")
        return langs

    def _normalize_region(self, region):
        if not region:
            return None
        if "left" in region and "width" in region:
            return region
        if "x" in region and "w" in region:
            return {"left": region["x"], "top": region["y"], "width": region["w"], "height": region["h"]}
        return None

    def _get_sct(self):
        s = getattr(self._tls, "sct", None)
        if s is None:
            s = mss.mss()
            self._tls.sct = s
        return s

    def _capture_frame(self, region):
        left = int(region.get("left", 0))
        top = int(region.get("top", 0))
        width = int(region.get("width", 0))
        height = int(region.get("height", 0))
        mon = {"left": left, "top": top, "width": width, "height": height}
        with self._capture_lock:
            # Prefer DXGI (Desktop Duplication) so GPU-rendered windows like
            # Roblox / DirectX games are captured (GDI/BitBlt yields black frames).
            if self._dxcam is not None:
                for attempt in range(3):
                    try:
                        shot = self._dxcam.grab()
                    except Exception as e:
                        logger.warning(f"dxcam grab failed, falling back to mss: {e}")
                        break
                    if shot is not None and shot.size:
                        fh, fw = shot.shape[:2]
                        bottom = min(top + height, fh)
                        right = min(left + width, fw)
                        if top < fh and left < fw and bottom > top and right > left:
                            crop = shot[top:bottom, left:right]
                            if crop.size:
                                return crop
                    # None => no fresh frame yet (or cross-thread race): brief wait.
                    time.sleep(0.03)
            # Fallback: mss (GDI), thread-local instance
            try:
                img = self._get_sct().grab(mon)
                frame = np.array(img)[:, :, :3]
                return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            except Exception as e:
                logger.warning(f"mss grab failed ({e}), reinitializing")
                self._tls.sct = mss.mss()
                img = self._get_sct().grab(mon)
                frame = np.array(img)[:, :, :3]
                return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    def _find_tessdata(self):
        if getattr(sys, 'frozen', False):
            base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates_tessdata = [
            os.path.join(base_dir, "data", "tessdata"),
            os.path.join(base_dir, "data"),
            r"C:\Program Files\Tesseract-OCR\tessdata",
            os.path.join(os.path.dirname(sys.executable), "tessdata"),
        ]
        for td in candidates_tessdata:
            if os.path.isdir(td):
                files = os.listdir(td)
                has_traineddata = any(f.endswith('.traineddata') for f in files)
                logger.debug(f"_find_tessdata: checking {td}, files={files[:5]}, has_traineddata={has_traineddata}")
                if has_traineddata:
                    return td
        tessdata_env = os.environ.get("TESSDATA_PREFIX", "")
        if tessdata_env and os.path.isdir(tessdata_env):
            return tessdata_env
        return ""

    def _preprocess(self, frame):
        try:
            h, w = frame.shape[:2]
            resized = cv2.resize(frame, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
            return gray
        except Exception:
            return frame

    def _lines_from_data(self, data, region_left, region_top, scale=1.0):
        lines = {}
        n = len(data['text'])
        for i in range(n):
            text = data['text'][i].strip()
            if not text:
                continue
            block_num = data['block_num'][i]
            line_num = data['line_num'][i]
            key = (block_num, line_num)
            if key not in lines:
                lines[key] = {
                    'text': [],
                    'left': data['left'][i],
                    'top': data['top'][i],
                    'right': data['left'][i] + data['width'][i],
                    'bottom': data['top'][i] + data['height'][i],
                }
            lines[key]['text'].append(text)
            lines[key]['left'] = min(lines[key]['left'], data['left'][i])
            lines[key]['top'] = min(lines[key]['top'], data['top'][i])
            lines[key]['right'] = max(lines[key]['right'], data['left'][i] + data['width'][i])
            lines[key]['bottom'] = max(lines[key]['bottom'], data['top'][i] + data['height'][i])

        out = []
        for key in sorted(lines.keys()):
            line = lines[key]
            text = ' '.join(line['text'])
            if not text:
                continue
            if len(text) < 2:
                continue
            line_w = line['right'] - line['left']
            line_h = line['bottom'] - line['top']
            if line_h < 5 or line_w < 10:
                continue
            out.append({
                'text': text,
                'x': int(region_left + line['left'] / scale),
                'y': int(region_top + line['top'] / scale),
                'w': int(line_w / scale),
                'h': int(line_h / scale),
            })
        return out

    def _dedupe_lines(self, lines):
        # psm6 (multi-block) and the psm4 retry can return OVERLAPPING boxes
        # for the same text (e.g. 'Welcome to the Shittim Chest...' twice) —
        # both were drawn as overlays. Keep the better reading (more words,
        # then longer) and drop anything that mostly overlaps a kept box.
        def score(l):
            return (len(re.findall(r"[A-Za-z']{2,}", l["text"])), len(l["text"]))

        out = []
        for l in sorted(lines, key=score, reverse=True):
            dup = False
            for o in out:
                if (o["text"] == l["text"]
                        and abs(o["x"] - l["x"]) < 20
                        and abs(o["y"] - l["y"]) < 20):
                    dup = True
                    break
                ix = max(0, min(o["x"] + o["w"], l["x"] + l["w"])
                         - max(o["x"], l["x"]))
                iy = max(0, min(o["y"] + o["h"], l["y"] + l["h"])
                         - max(o["y"], l["y"]))
                if ix * iy >= 0.4 * min(o["w"] * o["h"], l["w"] * l["h"]):
                    dup = True
                    break
            if not dup:
                out.append(l)
        return out

    def get_text_lines(self, region):
        """Capture one frame of the region and OCR it."""
        monitor = self._normalize_region(region)
        if not monitor:
            return []

        try:
            frame = self._capture_frame(monitor)
        except Exception as e:
            logger.error(f"Screen capture failed: {e}")
            return []

        if frame is None or frame.size == 0:
            return []

        if np.mean(frame) < 15:
            return []

        return self.lines_from_frame(frame, monitor)

    def lines_from_frame(self, frame, monitor):
        """OCR an already-captured BGR frame.

        Routes through the configured backend (core/ocr_engine): winocr /
        rapidocr first — both return tight per-line boxes in frame
        coordinates — tesseract as fallback (and for source_lang ja when
        winocr has no Japanese language). The tesseract path keeps its own
        x2 preprocess, so results are translated back to frame/original
        coordinates by _tesseract_lines.
        """
        src_lang = self.config.get("source_lang", "en")
        allow_cjk = src_lang != "en"
        pref = self.config.get("ocr_backend", "auto")
        left, top = monitor.get('left', 0), monitor.get('top', 0)

        lines = None
        try:
            backend = ocr_engine.resolve_backend(pref, src_lang)
        except Exception as e:
            logger.warning(f"OCR backend resolve failed ({e}), using tesseract")
            backend = "tesseract"
        used = backend

        if backend != "tesseract":
            try:
                raw = ocr_engine.backend_lines(frame, backend, src_lang)
                lines = []
                for r in raw:
                    lines.append({"text": r["text"],
                                  "x": r["x"] + left, "y": r["y"] + top,
                                  "w": r["w"], "h": r["h"],
                                  **({"conf": r["conf"]} if "conf" in r else {})})
            except ocr_engine.BackendUnavailable as e:
                logger.warning(f"OCR backend {backend} failed ({e}), "
                               f"falling back to tesseract")
                lines = None
                used = "tesseract"

        if lines is None:
            lines = self._tesseract_lines(frame, monitor, src_lang, allow_cjk)

        result = self._dedupe_lines(lines)

        logger.debug(f"Found {len(result)} text lines (backend={used})")
        for r in result:
            logger.info(f"  LINE: x={r['x']}, y={r['y']}, w={r['w']}, h={r['h']}, text='{r['text']}'")
        return result

    def _tesseract_lines(self, frame, monitor, src_lang, allow_cjk):
        """Legacy tesseract path: x2 upscale preprocess + psm 6/4."""
        orig_w = frame.shape[1]
        frame = self._preprocess(frame)
        # _preprocess may upscale (x2); OCR coords come back in that space.
        scale = (frame.shape[1] / orig_w) if orig_w else 1.0

        ocr_lang = SOURCE_OCR_LANG.get(src_lang, "eng")
        # Keep only languages whose traineddata is actually present.
        parts = [p for p in ocr_lang.split("+") if p in self._available_langs]
        if not parts:
            parts = ['eng'] if 'eng' in self._available_langs else (self._available_langs[:1] or ['eng'])
        ocr_lang = "+".join(parts)

        os.environ["TESSDATA_PREFIX"] = self._tessdata_dir or ""
        # psm 6 (uniform block) is the most reliable for game/VN dialogue
        # boxes. We deliberately avoid psm 11 here: it scatters detections
        # (UI fragments, wrong coordinates) which made overlays overlap.
        result = []
        for psm in (6, 4):
            config = f'--psm {psm}'
            if self._tessdata_dir:
                config += f' --tessdata-dir {self._tessdata_dir}'
            try:
                data = pytesseract.image_to_data(frame, lang=ocr_lang, config=config, output_type=pytesseract.Output.DICT)
            except Exception as e:
                logger.error(f"OCR data extraction failed (psm {psm}): {e}")
                continue
            psm_lines = self._lines_from_data(data, monitor.get('left', 0), monitor.get('top', 0), scale)
            result.extend(psm_lines)
            # Only stop at psm 6 when it produced REAL text; otherwise its
            # glyph garbage blocked the psm 4 retry (missed dialogue).
            if any(looks_like_text(l['text'], allow_cjk) for l in psm_lines):
                break
        return result

    def process_region(self, region, target_lang=None):
        lang = target_lang or self.config.get("lang", "ru")
        logger.info(f"Processing region: {region}, lang={lang}")

        lines = self.get_text_lines(region)
        if not lines:
            logger.warning("No text lines found")
            return None

        all_texts = []
        for line in lines:
            all_texts.append(line['text'])

        full_text = ' '.join(all_texts)

        if lang == "ja":
            return full_text

        translated = self._load_dialog_from_db(full_text)
        if translated:
            logger.info(f"DB translation found: '{translated[:80]}'")
            return translated

        logger.debug("Calling translation API...")
        translated = translate_text(full_text, target_lang=lang)
        if translated:
            logger.info(f"API translation: '{translated[:80]}'")
        else:
            logger.warning("Translation API returned None")
        return translated

    def _run_ocr(self, frame, lang):
        tessdata_dir = self._tessdata_dir or ""

        if lang not in self._available_langs and 'eng' in self._available_langs:
            if lang != 'eng':
                logger.warning(f"Tesseract language '{lang}' not installed, using 'eng'")
            lang = 'eng'

        custom_config = f"--psm 6"
        if tessdata_dir:
            custom_config += f" --tessdata-dir {tessdata_dir}"

        try:
            text = pytesseract.image_to_string(frame, lang=lang, config=custom_config).strip()
            if text:
                logger.debug(f"OCR result ({lang}): '{text[:60]}'")
                return text
        except pytesseract.TesseractError as e:
            logger.warning(f"Tesseract error: {e}")
            if lang != 'eng' and 'eng' in self._available_langs:
                try:
                    text = pytesseract.image_to_string(frame, lang="eng", config=custom_config).strip()
                    if text:
                        return text
                except Exception as e2:
                    logger.error(f"English OCR also failed: {e2}")
            return None
        except Exception as e:
            logger.error(f"OCR unexpected error: {e}", exc_info=True)
            return None

    def _clean_text(self, text):
        import re
        text = re.sub(r'[^\w\s.,!?;:\'"\-()а-яА-ЯёЁ]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _filter_noise(self, text):
        import re

        has_japanese = bool(re.search(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]', text))
        if has_japanese:
            japanese_parts = re.findall(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]+', text)
            return ' '.join(japanese_parts)

        noise_words = {
            'windows', 'crlf', 'utf', 'lf', 'ascii', 'cr',
            'acctxt', 'morphb', 'acctt', 'ds', 'bet',
            'the', 'a', 'an', 'is', 'it', 'to', 'of',
            'x', 'b', 'c', 'd', 'e', 'f', 'g', 'h',
        }
        vowels = set('aeiou')
        words = text.split()
        filtered = []
        for w in words:
            stripped = w.strip('.,!?;:\'"-()[]{}')
            lower = stripped.lower()
            if lower in noise_words:
                continue
            if re.match(r'^\d+\.?\d*$', stripped):
                continue
            if '.' in stripped and not stripped.endswith('.'):
                continue
            if re.match(r'^\w+\.(trt|tet|txt|json|xml|dll|exe|py|js|png|jpg|bmp)$', w, re.IGNORECASE):
                continue
            alpha_chars = [c for c in lower if c.isalpha()]
            if len(alpha_chars) < 3:
                continue
            if len(stripped) <= 2:
                continue
            if alpha_chars and not any(c in vowels for c in alpha_chars):
                continue
            filtered.append(w)
        result = ' '.join(filtered)
        result = re.sub(r'\b[A-Z]\b', '', result)
        result = re.sub(r'\b\d+\.?\d*\b', '', result)
        result = re.sub(r'\s+', ' ', result).strip()
        return result if result else ''

    def _load_dialog_from_db(self, text):
        try:
            db_path = os.path.join(os.path.dirname(self.config_manager.config_path), "dialogs_db.json")
            if not os.path.exists(db_path):
                return None
            with open(db_path, "r", encoding="utf-8-sig") as f:
                db = json.load(f)
            cleaned = text.strip()
            for row in db:
                if row.get("jpn", "").strip() == cleaned:
                    return row.get("ru", None)
        except Exception as e:
            logger.debug(f"DB lookup error: {e}")
        return None

    def save_dialog_to_db(self, jpn_text, ru_text):
        try:
            db_path = os.path.join(os.path.dirname(self.config_manager.config_path), "dialogs_db.json")
            try:
                with open(db_path, "r", encoding="utf-8-sig") as f:
                    db = json.load(f)
            except Exception:
                db = []
            if not isinstance(db, list):
                db = []
            jpn = jpn_text.strip()
            if not jpn:
                return
            for row in db:
                if row.get("jpn", "").strip() == jpn:
                    row["ru"] = ru_text
                    break
            else:
                db.append({"jpn": jpn, "ru": ru_text})
            with open(db_path, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
            logger.debug(f"Saved to DB ({len(db)} entries): '{jpn[:40]}'")
        except Exception as e:
            logger.debug(f"DB save error: {e}")
