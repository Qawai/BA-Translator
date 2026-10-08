import sys
import os
import re
import logging
import time
import threading
import psutil
import cv2
import numpy as np
from difflib import SequenceMatcher
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QApplication,
    QLabel, QSpinBox, QDoubleSpinBox, QPushButton, QComboBox, QCheckBox, QLineEdit,
    QFrame, QMessageBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QPoint, QObject
from PyQt5.QtGui import QFont, QColor

from core.config_manager import ConfigManager
from core.screen_processor import ScreenProcessor, looks_like_text
from core.window_handler import (
    get_window_at_cursor, get_visible_windows,
    find_target_window_region,
    get_own_window_rects, top_window_at, is_target_window,
    TARGET_PROCESSES, TARGET_TITLES, _SKIP_TITLES
)
from core.translator_api import translate_text, detect_source_lang, gemini_translate_list
from core import ba_script_db
from core import glossary
from ui.overlay_window import OverlayManager
from ui.subtitle_timeline import SubtitleTimeline

import mss

logger = logging.getLogger("BA_Translator")

# Lines taller than this (screen px) are OCR merging the whole window into
# one "line" — they produced full-screen junk overlays.
MAX_LINE_HEIGHT = 150


def _normalize_text(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


# Script markup tags in ba_scripts.db: [s] [ns] [s1].. [ns14] [/log]
# [log] [i] [/i] [wa:10] [-] and color tags [FF6666] [56cd7b] [7cd0ff].
_DB_TAG_RE = re.compile(r"\[[^\[\]\n]{1,30}\]")
_USERNAME_TAG = "[USERNAME]"


def _norm_word(w):
    return re.sub(r"[^0-9a-z]", "", (w or "").lower())


def _word_match(a, b):
    a1, b1 = _norm_word(a), _norm_word(b)
    if not a1 or not b1:
        return False
    if a1 == b1:
        return True
    if abs(len(a1) - len(b1)) > 3:
        return False
    return SequenceMatcher(None, a1, b1).ratio() >= 0.75


def _clean_script_tags(s):
    return re.sub(r"\s+", " ", _DB_TAG_RE.sub(" ", s or "")).strip()


def apply_db_line(db_en, ocr_text):
    """Merge the canonical DB line with system data actually on screen.

    [USERNAME] is replaced by the real player name read by OCR (fuzzy
    alignment of the text around the placeholder), other script tags are
    stripped. Returns None when the DB line can't be aligned — the caller
    then keeps the raw OCR text."""
    if not db_en:
        return None
    idx = db_en.find(_USERNAME_TAG)
    if idx < 0:
        return _clean_script_tags(db_en)
    pre = db_en[:idx].split()
    suf = db_en[idx + len(_USERNAME_TAG):].split()
    ow = (ocr_text or "").split()
    if not ow:
        return None
    # forward: match the text before the placeholder
    i = 0
    for w in pre:
        while i < len(ow) and not _word_match(w, ow[i]):
            i += 1
        if i >= len(ow):
            return None
        i += 1
    # backward: match the text after the placeholder
    j = len(ow)
    for w in reversed(suf):
        while j > 0 and not _word_match(w, ow[j - 1]):
            j -= 1
        if j <= i:
            return None
        j -= 1
    value = ""
    # The configured player name is authoritative — OCR can misread it.
    if glossary.get_player_name():
        value = glossary.get_player_name()
    else:
        value = " ".join(ow[i:j]).strip(" ,.!?;:")
    if not value:
        return None
    parts = []
    if pre:
        parts.append(" ".join(pre))
    parts.append(value)
    if suf:
        parts.append(" ".join(suf))
    return _clean_script_tags(" ".join(parts))


def filter_dialogue_lines(lines, region, title="", allow_cjk=True, strip_mode=False):
    """Shared filter for real translation and periodic re-check.

    strip_mode=True: `lines` came from the bottom-40% dialogue strip, where
    window-title mega-lines cannot exist — merged multi-row dialogue (h up
    to ~8 rows) must survive the oversized check instead of being dropped."""
    if title and len(title) > 3:
        t = title.lower()
        before = len(lines)
        lines = [l for l in lines if t not in l["text"].lower()]
        if before != len(lines):
            logger.info(f"=== dropped {before - len(lines)} title-bar line(s)")

    # Which window actually paints the line's center point?
    # 1) Our own windows (settings UI, hint card, translation overlays)
    #    own those pixels no matter what — OCR reading them is reading OUR
    #    UI, never the game. The overlay/hint windows are WS_EX_TRANSPARENT
    #    (invisible to WindowFromPoint) so they are checked by rect.
    # 2) Otherwise WindowFromPoint gives ground-truth z-order; only target
    #    (game) windows count as visible content.
    # 3) Fallback: the enumerated stack (handles stale/moved regions).
    stack = get_visible_windows(region)
    own_rects = get_own_window_rects()

    def _visible(line):
        # Sample 5 points across the line: OCR mega-lines often MERGE our
        # own status/settings text with game text, and a narrow overlay
        # rect (x 57..260) inside a strip-wide line (w~1766) dodged the
        # old 3-point sampling (0.15/0.5/0.85 -> x 323/941/1559) and kept
        # re-translating itself. The line survives only when no sample
        # hits a foreign surface.
        cy = line["y"] + line["h"] / 2
        for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
            cx = line["x"] + line["w"] * frac
            hit_own = False
            for ol, ot, orr, ob in own_rects:
                if ol <= cx <= orr and ot <= cy <= ob:
                    hit_own = True
                    break
            if hit_own:
                return False
            hw = top_window_at(cx, cy)
            if hw:
                if not is_target_window(hw):
                    return False
                continue
            for wl, wt, wr, wb, is_target in stack:
                if wl <= cx <= wr and wt <= cy <= wb:
                    if not is_target:
                        return False
                    break
            # point not covered by any window: keep this sample
        return True

    before = len(lines)
    lines = [l for l in lines if _visible(l)]
    if before != len(lines):
        logger.info(f"=== dropped {before - len(lines)} occluded line(s)")

    before = len(lines)
    limit = 320 if strip_mode else MAX_LINE_HEIGHT
    lines = [l for l in lines if l["h"] <= limit]
    if before != len(lines):
        logger.info(f"=== dropped {before - len(lines)} oversized line(s)")

    # Hairlines (the name/text separator, thin CG gradient edges) OCR as
    # words: '; J 2) . enna'-style junk. Real dialogue rows are >= ~25px.
    before = len(lines)
    lines = [l for l in lines if l["h"] >= 20]
    if before != len(lines):
        logger.info(f"=== dropped {before - len(lines)} hairline row(s)")

    # Full-window fallback captures OCR the ARTWORK area as well, where
    # gradients read as words -> floating junk overlays over the CG
    # ('фи; SZ q ia `al' at y~0.27). Dialogue lives in the bottom 60%:
    # the dialogue box AND the centered choice cards ('Follow her.' had
    # cy~481 = 0.47 — the old 0.60 cut band-killed every card). Artwork
    # junk sits above y~0.40; the top-HUD cut below it takes the rest.
    # Strip captures are that band by construction -> full captures only.
    if not strip_mode and region and region.get("height") and region.get("top") is not None:
        band_top = region["top"] + region["height"] * 0.40
        before = len(lines)
        lines = [l for l in lines if l["y"] + l["h"] / 2 >= band_top]
        if before != len(lines):
            logger.info(f"=== dropped {before - len(lines)} non-dialogue band line(s)")

    # Top 12% of the window is HUD (window title, BA Auto/Menu buttons,
    # BlueStacks toolbar icons) — never dialogue, always junk overlays.
    if region and region.get("height") and region.get("top") is not None:
        top_cut = region["top"] + region["height"] * 0.12
        before = len(lines)
        lines = [l for l in lines if l["y"] + l["h"] / 2 >= top_cut]
        if before != len(lines):
            logger.info(f"=== dropped {before - len(lines)} top-HUD line(s)")

    before = len(lines)
    lines = [l for l in lines if looks_like_text(l["text"], allow_cjk)]
    if before != len(lines):
        logger.info(f"=== dropped {before - len(lines)} non-text line(s)")
    return lines


class TranslationSignalEmitter(QObject):
    lmb_clicked = pyqtSignal(int, int)
    settings_hotkey = pyqtSignal()
    hook_start = pyqtSignal()
    hook_stop = pyqtSignal()


class TranslationWorker(QThread):
    finished = pyqtSignal(list)
    episode = pyqtSignal(str)
    _processor = None

    @classmethod
    def _get_processor(cls):
        if cls._processor is None:
            cls._processor = ScreenProcessor()
        return cls._processor

    def __init__(self, region, lang, backend="google", api_key="", gemini_model="gemini-2.5-flash"):
        super().__init__()
        self.region = region
        self.lang = lang
        self.backend = backend
        self.api_key = api_key
        self.gemini_model = gemini_model
        self._aborted = False
        self._watchdog = None

    def _on_watchdog(self):
        # Called from the watchdog thread if the task runs too long.
        self._aborted = True
        logger.warning("TranslationWorker watchdog: task timed out, aborting")

    def _start_watchdog(self, seconds):
        self._watchdog = threading.Timer(seconds, self._on_watchdog)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _stop_watchdog(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def run(self):
        try:
            logger.info(f"=== TranslationWorker START: region={self.region}, lang={self.lang}")
            processor = self._get_processor()
            processor.config["lang"] = self.lang
            self._start_watchdog(30)

            wtitle = (self.region.get("title") or "").strip()
            allow_cjk = processor.config.get("source_lang", "en") != "en"

            def _capture(reg, strip_mode):
                got = processor.get_text_lines(reg)
                if not got:
                    # First capture can race with overlay cleanup; retry once.
                    time.sleep(0.25)
                    got = processor.get_text_lines(reg)
                if not got:
                    return []
                return filter_dialogue_lines(
                    got, self.region, wtitle, allow_cjk, strip_mode=strip_mode)

            # Dialogue lives in the bottom 40% of the window: capture that
            # FIRST. A full-window capture regularly merges the window title
            # with the dialogue into one giant line, the filter drops it and
            # the click/auto-retry did nothing at all. The full window stays
            # as a fallback for text outside the strip (menus, titles).
            strip = None
            h = int(self.region.get("height") or 0)
            if h >= 120:
                cut = int(h * 0.60)
                if h - cut >= 60:
                    strip = {"left": self.region["left"],
                             "top": self.region["top"] + cut,
                             "width": self.region["width"],
                             "height": h - cut}
            lines = []
            if strip:
                lines = _capture(strip, strip_mode=True)
                if lines:
                    logger.info(f"=== dialogue-strip capture: {len(lines)} lines")
            if not lines:
                logger.info("=== strip empty, falling back to full-window capture")
                lines = _capture(self.region, strip_mode=False)
            if self._aborted:
                self.finished.emit([])
                return
            if not lines:
                logger.warning("=== TranslationWorker: no text lines after filters")
                self.finished.emit([])
                return

            # Noise filter: reject OCR lines that are mostly symbols/
            # artifacts (buttons, icons, UI elements) before they reach
            # the translator. A real dialogue line has >= 40% letters.
            def _is_noise(text):
                if not text:
                    return True
                letters = sum(1 for c in text if c.isalpha())
                visible = sum(1 for c in text if not c.isspace())
                # Too short for real dialogue
                if visible < 6:
                    return True
                # Too many symbols (not enough letters)
                if visible > 0 and letters / visible < 0.4:
                    return True
                # Ends with multiple question marks = OCR artifact
                if text.rstrip().endswith('??'):
                    return True
                # Too many consecutive non-alphanumeric chars = artifact
                import re
                if re.search(r'[^A-Za-z\u0400-\u04ff\d]{5,}', text):
                    return True
                return False

            before = len(lines)
            lines = [l for l in lines if not _is_noise(l["text"])]
            if before != len(lines):
                logger.info(f"=== dropped {before - len(lines)} noise line(s)")

            # Typewriter detection: BA types dialogue character by character.
            # If the last line doesn't end with sentence-ending punctuation,
            # the game is still typing — wait and re-capture until stable.
            for _tw in range(4):
                combined = " ".join(l["text"] for l in lines).rstrip()
                if (not combined
                        or combined[-1] in '.!?'
                        or combined[-1] in '"»\'…'
                        or combined.endswith('...')):
                    break
                logger.info(f"=== typewriter: text incomplete ({combined[-30:]!r}), waiting {_tw+1}/4")
                time.sleep(0.45)
                if self._aborted:
                    self.finished.emit([])
                    return
                new_lines = _capture(strip or self.region,
                                     strip_mode=bool(strip))
                if new_lines:
                    new_lines = [l for l in new_lines if not _is_noise(l["text"])]
                    if new_lines:
                        lines = new_lines
                else:
                    break

            logger.info(f"=== TranslationWorker: using {len(lines)} lines")

            # Keep the pre-lookup (raw OCR) text: the re-check compares raw
            # OCR against raw OCR, DB replacement would skew the comparison.
            for l in lines:
                l["raw"] = l["text"]

            # BA script lookup: replace OCR noise with the exact line from
            # the game script and detect the episode being played. System
            # data ([USERNAME] -> real player name) comes from the OCR.
            episode_text = None
            for l in lines:
                hit = ba_script_db.lookup(l["text"]) or ba_script_db.lookup_prefix(l["text"])
                if hit:
                    merged = apply_db_line(hit["en"], l.get("raw") or l["text"])
                    l["text"] = merged if merged else (l.get("raw") or l["text"])
                    l["jp"] = _clean_script_tags(hit.get("jp", ""))
                    l["group_id"] = hit.get("group_id", 0)
                    if hit.get("group_id") and not episode_text:
                        episode_text = ba_script_db.episode_for_group(hit["group_id"])
            if episode_text:
                logger.info(f"=== BA episode: {episode_text}")
                self.episode.emit(episode_text)

            if self.lang == "ja":
                for line in lines:
                    line['translation'] = line.get('jp') or line['text']
                self.finished.emit(lines)
                return

            full_text = "\n".join(line['text'] for line in lines)
            line_srcs = [detect_source_lang(l['text']) for l in lines]
            if line_srcs and all(s == self.lang for s in line_srcs):
                # Every line already speaks the target language (e.g. lang=en
                # over English text): show the DB-cleaned lines as-is instead
                # of feeding them to a translator. Checked PER LINE — one
                # stray Cyrillic fragment in the batch used to flip the whole
                # detection and silently return everything untranslated.
                logger.info(f"Source already in target language ({self.lang}), showing as-is")
                for l in lines:
                    l['translation'] = l['text']
                self.finished.emit(lines)
                return
            if self._aborted:
                self.finished.emit([])
                return

            # High-quality backend (Gemini) when configured: returns a 1:1
            # list, so every line is translated and order is preserved.
            backend = self.backend
            api_key = self.api_key
            gemini_model = self.gemini_model

            def _tr_batch(srcs):
                """Translate a list of sources with the active backend.
                Returns a list aligned with srcs ("" = failed for that src)."""
                if not srcs:
                    return []
                if backend == "gemini" and api_key:
                    try:
                        r = gemini_translate_list(
                            srcs, self.lang, gemini_model, api_key)
                        if r and len(r) == len(srcs):
                            return [t.strip() if t and t.strip() else ""
                                    for t in r]
                        logger.warning("Gemini result count mismatch, falling back to Google")
                    except Exception as e:
                        logger.warning(f"Gemini failed, fallback to Google: {e}")
                out = []
                for s in srcs:
                    sl = detect_source_lang(s)
                    if sl == self.lang:
                        out.append(s)
                        continue
                    t = translate_text(s, target_lang=self.lang, source_lang=sl)
                    out.append(t.strip() if t else "")
                return out

            # Player and BA character names must stay verbatim — the EN->RU
            # backend transliterates them. Such lines are split into
            # name/other segments; only the other segments are translated
            # and the names are stitched back unchanged.
            name_plan = {}
            if self.lang == "ru":
                for i, l in enumerate(lines):
                    prep = glossary.prepare(l["text"])
                    if prep:
                        name_plan[i] = prep
            named_trans = {}
            if name_plan:
                order = sorted(name_plan)
                # prep = (segs, idxs, src): [2] is the "\n"-joined TEXT to
                # translate — [1] is idxs (a list) and crashed detect_source_lang.
                results = _tr_batch([name_plan[i][2] for i in order])
                for i, r in zip(order, results):
                    stitched = glossary.stitch(name_plan[i], r)
                    if stitched:
                        named_trans[i] = stitched
                    else:
                        logger.warning(
                            f"name stitch failed, keeping source: {lines[i]['text'][:60]!r}")
                logger.info(f"=== name protection: {len(name_plan)} line(s)")
            plain_indices = [i for i in range(len(lines)) if i not in name_plan]

            if backend == "gemini" and api_key and plain_indices:
                try:
                    translated_list = gemini_translate_list(
                        [lines[i]['text'] for i in plain_indices], self.lang,
                        gemini_model, api_key)
                    if translated_list and len(translated_list) == len(plain_indices):
                        for i, t in zip(plain_indices, translated_list):
                            lines[i]['translation'] = t.strip() if t and t.strip() else lines[i]['text']
                        for i, t in named_trans.items():
                            lines[i]['translation'] = t
                        if self.lang == "ru":
                            processor.save_dialog_to_db(full_text, "\n".join(l['translation'] for l in lines))
                        self.finished.emit(lines)
                        return
                    logger.warning("Gemini result count mismatch, falling back to Google")
                except Exception as e:
                    logger.warning(f"Gemini failed, fallback to Google: {e}")

            # Fallback / default: translate each line individually so every
            # line is covered (Google's free endpoint merges lines otherwise).
            # dialogs_db.json stores RU translations keyed by source text —
            # only the ru target may read/write it, otherwise lang=en/ja
            # would get (and poison the cache with) Russian output.
            # Lines containing names bypass the READ: older cache entries
            # were saved with transliterated names (they are overwritten
            # below with the corrected translation).
            cached = (processor._load_dialog_from_db(full_text)
                      if self.lang == "ru" and not name_plan else None)
            if cached:
                parts = cached.split("\n")
                logger.info("DB hit (whole window)")
            else:
                # parts align with plain_indices, NOT with lines.
                parts = []
                plain_text = "\n".join(lines[i]['text'] for i in plain_indices)
                # One request for the whole block when possible: much faster
                # (one round-trip instead of N sequential ones — the old loop
                # felt like the app hung) and reads more coherently. Google
                # occasionally re-wraps lines, so only an exact line-count
                # match is accepted; otherwise translate line by line.
                if 1 < len(plain_indices) <= 8 and len(plain_text) <= 1500:
                    bsl = detect_source_lang(plain_text)
                    if bsl != self.lang:
                        tr = translate_text(plain_text, target_lang=self.lang,
                                            source_lang=bsl)
                        pieces = tr.split("\n") if tr else []
                        if len(pieces) == len(plain_indices):
                            parts = [p.strip() for p in pieces]
                if not parts:
                    for i in plain_indices:
                        if self._aborted:
                            self.finished.emit([])
                            return
                        l = lines[i]
                        sl = detect_source_lang(l['text'])
                        if sl == self.lang:
                            parts.append(l['text'])
                            continue
                        tr = translate_text(l['text'], target_lang=self.lang,
                                            source_lang=sl)
                        if not tr:
                            logger.warning(
                                f"translation failed, keeping source: {l['text'][:60]!r}")
                        parts.append(tr if tr else l['text'])

            for idx, i in enumerate(plain_indices):
                piece = parts[idx].strip() if idx < len(parts) else ""
                lines[i]['translation'] = piece if piece else lines[i]['text']
            for i, t in named_trans.items():
                lines[i]['translation'] = t
            if self.lang == "ru" and not cached:
                processor.save_dialog_to_db(
                    full_text, "\n".join(l['translation'] for l in lines))

            self.finished.emit(lines)

        except Exception as e:
            logger.error(f"=== TranslationWorker ERROR: {e}", exc_info=True)
            self.finished.emit([])
        finally:
            self._stop_watchdog()


def is_strip_mostly_own(strip, threshold=0.6):
    """Quick check: is the dialogue strip mostly covered by non-target windows?

    Samples ~12 grid points across the strip and checks what window is on
    top at each point. Returns True when >= `threshold` fraction is covered
    by windows that are NOT the target (game) — meaning OCR would just read
    UI text (our settings, terminal, file manager, etc.) and the result
    would be garbage anyway.

    Costs ~12 * WindowFromPoint calls (~30-50ms) — worth it to skip the
    ~1.5s OCR pass that would produce nothing useful.
    """
    if not strip or not strip.get("width") or not strip.get("height"):
        return False
    from core.window_handler import top_window_at, is_target_window
    cols, rows = 4, 3
    blocked = 0
    total = cols * rows
    for ry in range(rows):
        cy = strip["top"] + int(strip["height"] * (ry + 0.5) / rows)
        for rx in range(cols):
            cx = strip["left"] + int(strip["width"] * (rx + 0.5) / cols)
            hw = top_window_at(cx, cy)
            if hw and not is_target_window(hw):
                blocked += 1
    return blocked / total >= threshold


class RecheckWorker(QThread):
    """Re-OCRs the dialogue strip only (cheap) and reports the normalized
    text. Emits None on error so the caller keeps its baseline."""
    result = pyqtSignal(object)

    def __init__(self, ocr_region, full_region, title, lang, frame=None):
        super().__init__()
        self.ocr_region = ocr_region
        self.full_region = full_region
        self.title = title
        self.lang = lang
        # Pre-captured BGR strip frame (overlays already hidden while it
        # was taken): OCR it directly instead of capturing the screen
        # again — no race with overlays the caller keeps visible.
        self.frame = frame

    def run(self):
        try:
            # FAST PATH: if most of the strip is covered by our own
            # windows (settings UI, hint card), skip the expensive OCR
            # entirely — it would just read our UI text and filter it out.
            if is_strip_mostly_own(self.ocr_region):
                self.result.emit("")
                return
            processor = TranslationWorker._get_processor()
            processor.config["lang"] = self.lang
            if self.frame is not None and getattr(self.frame, "size", 0):
                lines = processor.lines_from_frame(self.frame, self.ocr_region)
            else:
                lines = processor.get_text_lines(self.ocr_region)
            if not lines:
                self.result.emit("")
                return
            allow_cjk = processor.config.get("source_lang", "en") != "en"
            lines = filter_dialogue_lines(lines, self.full_region, self.title,
                                          allow_cjk, strip_mode=True)
            self.result.emit(_normalize_text(" ".join(l["text"] for l in lines)))
        except Exception as e:
            logger.error(f"RecheckWorker error: {e}", exc_info=True)
            self.result.emit(None)


class SettingsWindow(QMainWindow):
    def __init__(self, signal_emitter=None):
        super().__init__()
        logger.info("SettingsWindow initializing...")
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config
        # The player's in-game name freezes in translations (via glossary);
        # must be registered before any script lookup / translation runs.
        glossary.set_player_name(self.config.get("player_name", ""))
        self.overlay_manager = OverlayManager()
        self._translation_active = False
        self._worker = None
        self._click_x = 0
        self._click_y = 0
        self._translate_cache = {}
        self._last_region_key = None
        self._pending_region = None
        # True from the click-path overlay hide until its result arrives:
        # blocks the re-check "restore overlays" branches from re-showing
        # our translation into the capture that is already in flight
        # (OCR then read our own Cyrillic -> junk re-translation loop).
        self._capture_pending = False
        # True while the settings window is hidden by _start_translation;
        # _stop_translation shows it back only when we were the ones hiding.
        self._hidden_for_game = False
        # Periodic re-check state
        self._last_region = None
        self._last_source = ""
        # Last successfully translated lines: kept while the source text is
        # on screen so overlays can be restored after an accidental hide.
        self._last_lines = []
        # True once TTL intentionally cleared the overlays: same text must
        # NOT resurrect them (only genuinely new text starts a new 10s).
        self._expired_by_ttl = False
        # Consecutive "no lines" worker results: used to decide when an
        # empty screen is real (baseline adopt) vs a transient OCR miss.
        self._empty_streak = 0
        # Consecutive rechecks where our own windows covered the strip:
        # raises the changed-threshold so idle UI repaints don't trigger
        # an expensive OCR pass every second.
        self._own_strip_streak = 0
        self._baseline = None
        self._recheck_worker = None
        self._recheck_timer = QTimer(self)
        self._recheck_timer.timeout.connect(self._recheck_tick)
        self._apply_recheck_timer()
        self._signal_emitter = signal_emitter or TranslationSignalEmitter()
        self.overlay_manager.on_expire = self._on_overlays_expired
        # Onboarding subtitle timeline (hints about controls), plays on the
        # first launch and on demand via the «Подсказки» button.
        self._timeline = SubtitleTimeline(self)
        app_inst = QApplication.instance()
        if app_inst is not None:
            app_inst.aboutToQuit.connect(self._timeline.stop)

        # Old screenshots from previous runs never belong on disk.
        self._purge_screenshots()

        self.setWindowTitle("BA Translator")
        self.setFixedWidth(380)
        self._refit_pending = False
        self._refit_tries = 0
        ico = os.path.join(self.config_manager.base_dir, "data", "app.ico")
        if os.path.isfile(ico):
            from PyQt5.QtGui import QIcon
            self.setWindowIcon(QIcon(ico))

        self._setup_ui()
        self._connect_signals()
        self._apply_theme()
        self._apply_titlebar()
        self._refit_window()
        logger.info("SettingsWindow initialized successfully")

    def _apply_titlebar(self):
        """Dark caption (not Windows blue) + white text + our own icon."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            on = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
            txt = ctypes.c_uint(0x00FFFFFF)
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), ctypes.sizeof(txt))
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_titlebar()

    @property
    def signal_emitter(self):
        return self._signal_emitter

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(24, 24, 24, 24)

        header_layout = QHBoxLayout()
        title = QLabel("BA Translator")
        title.setFont(QFont("Segoe UI", 18, QFont.Bold))
        title.setObjectName("titleLabel")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self.status_indicator = QLabel("●")
        self.status_indicator.setFont(QFont("Segoe UI", 14))
        self.status_indicator.setObjectName("statusIndicator")
        header_layout.addWidget(self.status_indicator)
        main_layout.addLayout(header_layout)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("separator")
        main_layout.addWidget(sep)

        lang_container = QWidget()
        lang_layout = QHBoxLayout(lang_container)
        lang_layout.setContentsMargins(0, 0, 0, 0)
        lang_label = QLabel("Язык перевода:")
        lang_label.setFont(QFont("Segoe UI", 10))
        lang_layout.addWidget(lang_label)
        lang_layout.addStretch()
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["Русский (ru)", "English (en)", "日本語 (ja)"])
        self.lang_combo.setCurrentText(self._get_lang_display(self.config.get("lang", "ru")))
        self.lang_combo.currentTextChanged.connect(self._on_lang_changed)
        self.lang_combo.setObjectName("langCombo")
        lang_layout.addWidget(self.lang_combo)
        main_layout.addWidget(lang_container)

        coming_soon = QLabel("More languages: coming soon...")
        coming_soon.setFont(QFont("Segoe UI", 9))
        coming_soon.setStyleSheet("color: #4b5563; padding-left: 4px;")
        main_layout.addWidget(coming_soon)

        delay_container = QWidget()
        delay_layout = QHBoxLayout(delay_container)
        delay_layout.setContentsMargins(0, 0, 0, 0)
        delay_label = QLabel("Задержка (мс):")
        delay_label.setFont(QFont("Segoe UI", 10))
        delay_layout.addWidget(delay_label)
        delay_layout.addStretch()
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(100, 2000)
        self.delay_spin.setSingleStep(50)
        self.delay_spin.setValue(self.config.get("delay_ms", 300))
        self.delay_spin.valueChanged.connect(self._on_delay_changed)
        self.delay_spin.setObjectName("delaySpin")
        delay_layout.addWidget(self.delay_spin)
        main_layout.addWidget(delay_container)

        # Player's in-game name: kept verbatim in translations and used
        # for [USERNAME] lines of the BA script database.
        pname_container = QWidget()
        pname_layout = QHBoxLayout(pname_container)
        pname_layout.setContentsMargins(0, 0, 0, 0)
        pname_label = QLabel("Имя игрока:")
        pname_label.setFont(QFont("Segoe UI", 10))
        pname_layout.addWidget(pname_label)
        pname_layout.addStretch()
        self.pname_edit = QLineEdit(str(self.config.get("player_name", "")))
        self.pname_edit.setPlaceholderText("как в игре (латиницей)")
        self.pname_edit.setMaxLength(32)
        self.pname_edit.setFixedWidth(170)
        self.pname_edit.textChanged.connect(self._on_player_name_changed)
        self.pname_edit.setObjectName("playerNameEdit")
        self.pname_edit.setStyleSheet(
            "QLineEdit { background:#1e1e30; color:#e0e0e0; border:1px solid #3d3d5c;"
            " border-radius:6px; padding:6px 10px; }")
        pname_layout.addWidget(self.pname_edit)
        main_layout.addWidget(pname_container)

        # Delay before on-disk screenshots are deleted (0 = never).
        # NOTE: does NOT touch overlays — the translation stays on screen
        # until replaced by a new one or the user stops the translator.
        ttl_container = QWidget()
        ttl_layout = QHBoxLayout(ttl_container)
        ttl_layout.setContentsMargins(0, 0, 0, 0)
        ttl_label = QLabel("Удаление скриншотов (с):")
        ttl_label.setFont(QFont("Segoe UI", 10))
        ttl_layout.addWidget(ttl_label)
        ttl_layout.addStretch()
        self.ttl_spin = QSpinBox()
        self.ttl_spin.setRange(0, 300)
        self.ttl_spin.setSingleStep(5)
        self.ttl_spin.setValue(int(self.config.get("overlay_ttl", 10)))
        self.ttl_spin.valueChanged.connect(self._on_ttl_changed)
        self.ttl_spin.setObjectName("delaySpin")
        ttl_layout.addWidget(self.ttl_spin)
        main_layout.addWidget(ttl_container)
        # Overlays are never auto-cleared: they live until a retranslation
        # replaces them or the user stops the translator (user's rule).
        self.overlay_manager.ttl = 0

        # Auto-capture: master switch + interval between screenshots.
        auto_container = QWidget()
        auto_layout = QHBoxLayout(auto_container)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        auto_label = QLabel("Авто-скрин:")
        auto_label.setFont(QFont("Segoe UI", 10))
        auto_layout.addWidget(auto_label)
        self.auto_check = QCheckBox()
        self.auto_check.setChecked(bool(self.config.get("auto_screenshot", True)))
        self.auto_check.toggled.connect(self._on_auto_toggled)
        auto_layout.addWidget(self.auto_check)
        auto_layout.addStretch()
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.5, 30.0)
        self.interval_spin.setSingleStep(0.5)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" с")
        self.interval_spin.setValue(int(self.config.get("recheck_ms", 1000)) / 1000.0)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        self.interval_spin.setObjectName("delaySpin")
        auto_layout.addWidget(self.interval_spin)
        main_layout.addWidget(auto_container)

        # ---- Translation backend (quality) ----
        backend_container = QWidget()
        backend_layout = QHBoxLayout(backend_container)
        backend_layout.setContentsMargins(0, 0, 0, 0)
        backend_label = QLabel("Движок перевода:")
        backend_label.setFont(QFont("Segoe UI", 10))
        backend_layout.addWidget(backend_label)
        backend_layout.addStretch()
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["Google (бесплатно)", "Gemini (бесплатный ключ)"])
        self.backend_combo.setCurrentIndex(0 if self.config.get("backend", "google") == "google" else 1)
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        self.backend_combo.setObjectName("backendCombo")
        self.backend_combo.setStyleSheet(
            "QComboBox { background:#1e1e30; color:#e0e0e0; border:1px solid #3d3d5c;"
            " border-radius:6px; padding:6px 10px; }"
            " QComboBox QAbstractItemView { background:#1e1e30; color:#e0e0e0; selection-background-color:#7c3aed; }")
        backend_layout.addWidget(self.backend_combo)
        main_layout.addWidget(backend_container)

        self.apikey_container = QWidget()
        apikey_layout = QVBoxLayout(self.apikey_container)
        apikey_layout.setContentsMargins(0, 0, 0, 0)
        apikey_hint = QLabel("Gemini: бесплатный ключ с ai.google.dev (Google AI Studio).")
        apikey_hint.setFont(QFont("Segoe UI", 9))
        apikey_hint.setStyleSheet("color: #4b5563;")
        apikey_layout.addWidget(apikey_hint)
        self.apikey_edit = QLineEdit()
        self.apikey_edit.setPlaceholderText("Вставьте API-ключ Gemini")
        self.apikey_edit.setText(self.config.get("api_key", ""))
        self.apikey_edit.setEchoMode(QLineEdit.Password)
        self.apikey_edit.textChanged.connect(self._on_apikey_changed)
        self.apikey_edit.setObjectName("apiKeyEdit")
        self.apikey_edit.setStyleSheet(
            "QLineEdit { background:#1e1e30; color:#e0e0e0; border:1px solid #3d3d5c;"
            " border-radius:6px; padding:6px 10px; }")
        apikey_layout.addWidget(self.apikey_edit)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("Модель (по умолч. gemini-2.5-flash)")
        self.model_edit.setText(self.config.get("gemini_model", "gemini-2.5-flash"))
        self.model_edit.textChanged.connect(self._on_model_changed)
        self.model_edit.setObjectName("modelEdit")
        self.model_edit.setStyleSheet(
            "QLineEdit { background:#1e1e30; color:#e0e0e0; border:1px solid #3d3d5c;"
            " border-radius:6px; padding:6px 10px; }")
        apikey_layout.addWidget(self.model_edit)
        main_layout.addWidget(self.apikey_container)
        self._update_backend_ui()

        self.toggle_btn = QPushButton("▶  Запустить перевод")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setMinimumHeight(36)
        self.toggle_btn.setObjectName("toggleBtn")
        self.toggle_btn.toggled.connect(self._on_toggle)
        main_layout.addWidget(self.toggle_btn)

        self.autostart_check = QCheckBox("  Автозапуск при старте системы")
        self.autostart_check.setChecked(self.config.get("autostart", False))
        self.autostart_check.toggled.connect(self._on_autostart)
        self.autostart_check.setObjectName("autostartCheck")
        main_layout.addWidget(self.autostart_check)

        # Reset-to-defaults with a two-step confirmation: first press arms
        # ("Уверены?"), second press actually resets (auto-disarms in 4s).
        self.reset_btn = QPushButton("Сброс настроек")
        self.reset_btn.setMinimumHeight(30)
        self.reset_btn.setObjectName("resetBtn")
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        self._reset_arm_timer = QTimer(self)
        self._reset_arm_timer.setSingleShot(True)
        self._reset_arm_timer.timeout.connect(self._disarm_reset)

        self.hints_btn = QPushButton("Подсказки")
        self.hints_btn.setMinimumHeight(30)
        self.hints_btn.setObjectName("hintsBtn")
        self.hints_btn.setToolTip("Показать субтитры-инструкцию ещё раз")
        self.hints_btn.clicked.connect(self.play_hints)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        tools_row.addWidget(self.reset_btn, 1)
        tools_row.addWidget(self.hints_btn)
        main_layout.addLayout(tools_row)

        main_layout.addStretch()

        self.status_label = QLabel("Переводчик остановлен")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setObjectName("statusLabel")
        main_layout.addWidget(self.status_label)

        self.info_label = QLabel("ЛКМ по тексту — вручную  •  Right Shift — скрытие/показа")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setObjectName("infoLabel")
        self.info_label.setWordWrap(True)
        main_layout.addWidget(self.info_label)

        self._debug_label = QLabel("")
        self._debug_label.setAlignment(Qt.AlignCenter)
        self._debug_label.setObjectName("debugLabel")
        self._debug_label.setWordWrap(True)
        main_layout.addWidget(self._debug_label)

    def _connect_signals(self):
        self._signal_emitter.lmb_clicked.connect(self._handle_lmb_click)
        self._signal_emitter.settings_hotkey.connect(self.toggle_app_visibility)

    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a1a2e;
            }
            QWidget {
                color: #e0e0e0;
            }
            QLabel#titleLabel {
                color: #a78bfa;
            }
            QLabel#statusIndicator {
                color: #ef4444;
            }
            QFrame#separator {
                background-color: #2d2d44;
                max-height: 1px;
            }
            QComboBox#langCombo {
                background-color: #2d2d44;
                color: #e0e0e0;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 6px 12px;
                min-width: 140px;
                font-family: 'Segoe UI';
                font-size: 11px;
            }
            QComboBox#langCombo:hover {
                border-color: #6366f1;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d44;
                color: #e0e0e0;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                selection-background-color: #6366f1;
            }
            QSpinBox#delaySpin, QDoubleSpinBox#delaySpin {
                background-color: #2d2d44;
                color: #e0e0e0;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 6px 8px;
                min-width: 80px;
                font-family: 'Segoe UI';
                font-size: 11px;
            }
            QSpinBox#delaySpin:hover, QDoubleSpinBox#delaySpin:hover {
                border-color: #6366f1;
            }
            QPushButton#toggleBtn {
                background-color: #6366f1;
                color: white;
                border: none;
                border-radius: 8px;
                font-family: 'Segoe UI';
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#toggleBtn:hover {
                background-color: #818cf8;
            }
            QPushButton#toggleBtn:checked {
                background-color: #ef4444;
            }
            QPushButton#toggleBtn:checked:hover {
                background-color: #f87171;
            }
            QCheckBox {
                color: #a0a0b0;
                font-family: 'Segoe UI';
                font-size: 10px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #3d3d5c;
                background-color: #2d2d44;
            }
            QCheckBox::indicator:checked {
                background-color: #6366f1;
                border-color: #6366f1;
            }
            QPushButton#resetBtn {
                background-color: #2d2d44;
                color: #a0a0b0;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                font-family: 'Segoe UI';
                font-size: 10px;
                padding: 4px;
            }
            QPushButton#resetBtn:hover {
                background-color: #3d3d5c;
                color: #e0e0e0;
            }
            QPushButton#hintsBtn {
                background-color: #2d2d44;
                color: #c4b5fd;
                border: 1px solid #4c3a8f;
                border-radius: 6px;
                font-family: 'Segoe UI';
                font-size: 10px;
                padding: 4px;
            }
            QPushButton#hintsBtn:hover {
                background-color: #4c3a8f;
                color: #f5f3ff;
            }
            QLabel#statusLabel {
                color: #6b7280;
                font-family: 'Segoe UI';
                font-size: 11px;
            }
            QLabel#infoLabel {
                color: #4b5563;
                font-family: 'Segoe UI';
                font-size: 9px;
            }
            QLabel#debugLabel {
                color: #ff9800;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 10px;
                padding: 4px;
                background: #1e1e32;
                border-radius: 4px;
            }
        """)

    def _get_lang_display(self, lang_code):
        mapping = {"ru": "Русский (ru)", "en": "English (en)", "ja": "日本語 (ja)"}
        return mapping.get(lang_code, "Русский (ru)")

    def _get_lang_code(self, display_text):
        mapping = {"Русский (ru)": "ru", "English (en)": "en", "日本語 (ja)": "ja"}
        return mapping.get(display_text, "ru")

    def _on_lang_changed(self, display_text):
        code = self._get_lang_code(display_text)
        self.config["lang"] = code
        self.config_manager.save_config(self.config)
        logger.info(f"Language changed to {code}")

    def _on_delay_changed(self, value):
        self.config["delay_ms"] = value
        self.config_manager.save_config(self.config)

    def _on_player_name_changed(self, value):
        self.config["player_name"] = (value or "").strip()
        self.config_manager.save_config(self.config)
        glossary.set_player_name(self.config["player_name"])

    def _on_ttl_changed(self, value):
        self.config["overlay_ttl"] = value
        self.config_manager.save_config(self.config)
        logger.info(f"Screenshot cleanup delay set to {value}s")

    def _on_auto_toggled(self, checked):
        self.config["auto_screenshot"] = bool(checked)
        self.config_manager.save_config(self.config)
        logger.info(f"Auto-capture {'enabled' if checked else 'disabled'}")
        self._apply_recheck_timer()
        if self._translation_active:
            self.status_label.setText(self._active_status_text())

    def _on_interval_changed(self, value):
        self.config["recheck_ms"] = int(round(value * 1000))
        self.config_manager.save_config(self.config)
        logger.info(f"Auto-capture interval set to {value:g}s")
        self._apply_recheck_timer()
        if self._translation_active:
            self.status_label.setText(self._active_status_text())

    def _apply_recheck_timer(self):
        """(Re)start the auto-capture timer honoring the on/off switch and
        the configured interval. Timer runs only while auto-screen is ON."""
        self._recheck_timer.stop()
        if not self.config.get("auto_screenshot", True):
            return
        recheck_ms = int(self.config.get("recheck_ms", 1000))
        if recheck_ms > 0:
            self._recheck_timer.start(recheck_ms)

    def _active_status_text(self):
        if not self.config.get("auto_screenshot", True):
            return "● Перевод активен — только клики ЛКМ"
        sec = int(self.config.get("recheck_ms", 1000)) / 1000.0
        return f"● Перевод активен — авто-скрин раз в {sec:g} с"

    def _refit_window(self):
        """Schedule a size refit on the next event-loop pass. Measuring
        synchronously right after show/hide is wrong: the layouts are still
        stale and rows come out squashed (API key field was 3px tall)."""
        if self._refit_pending:
            return
        self._refit_pending = True
        QTimer.singleShot(0, self._refit_apply)

    def _refit_apply(self):
        self._refit_pending = False
        self._refit_tries = 0
        self._do_refit()
        # Settle pass: children get re-laid at the new size, which may
        # change the required height once more.
        self._refit_pending = True
        QTimer.singleShot(0, self._refit_settle)

    def _refit_settle(self):
        self._refit_pending = False
        self._refit_tries += 1
        h_before = self.height()
        self._do_refit()
        # Converge until the content really fits inside the window (stale
        # child geometries used to stay at the previous size forever).
        fits = self._debug_label.geometry().bottom() + 24 <= self.height()
        if (not fits or self.height() != h_before) and self._refit_tries < 6:
            self._refit_pending = True
            QTimer.singleShot(0, self._refit_settle)

    def _do_refit(self):
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setFixedWidth(380)
        # Resize FIRST: sizeHint comes from the layout engine (row hints),
        # so it is correct even while child geometries are stale.
        self.adjustSize()
        self.setFixedSize(380, self.size().height())
        # Assign children AT THE FINAL size — activate() before the resize
        # silently leaves them at the old size (rows clipped / 3px fields).
        central = self.centralWidget()
        lay = central.layout() if central else None
        if lay is not None:
            lay.invalidate()
            lay.activate()
            for child in central.findChildren(QWidget):
                cl = child.layout()
                if cl is not None:
                    cl.activate()

    def _on_toggle(self, checked):
        if checked:
            self._start_translation()
        else:
            self._stop_translation()

    def _on_autostart(self, checked):
        self.config_manager.set_autostart(checked)

    def _on_backend_changed(self, index):
        self.config["backend"] = "google" if index == 0 else "gemini"
        self.config_manager.save_config(self.config)
        self._update_backend_ui()

    def _on_apikey_changed(self, text):
        self.config["api_key"] = text.strip()
        self.config_manager.save_config(self.config)

    def _on_model_changed(self, text):
        self.config["gemini_model"] = text.strip() or "gemini-2.5-flash"
        self.config_manager.save_config(self.config)

    def _update_backend_ui(self):
        is_gemini = self.config.get("backend", "google") == "gemini"
        if hasattr(self, "apikey_container"):
            self.apikey_container.setVisible(is_gemini)
        # Key/model rows change the window height — refit so the fields are
        # never squeezed (they used to be squashed into a fixed height).
        self._refit_window()

    # ------------------------------------------------------------------
    # Screenshot file hygiene: the app itself keeps captures in memory,
    # but debug/legacy *.png/*.jpg piles appear in the app folder — they
    # are removed on launch, 10s after every translation and on close.
    # Only the app's OWN folder and only screenshot-looking names.
    # ------------------------------------------------------------------
    _SHOT_PATTERNS = ("ba_now*", "ba_state*", "check_now*", "topstrip*",
                      "screen*", "_screen*", "screenshot*", "снимок*",
                      "monitor-*", "sct-*")

    def _purge_screenshots(self):
        try:
            import glob
            exts = (".png", ".jpg", ".jpeg", ".bmp")
            roots = {self.config_manager.base_dir}
            if getattr(sys, "frozen", False):
                roots.add(os.path.dirname(sys.executable))
            removed = 0
            for root in roots:
                for pat in self._SHOT_PATTERNS:
                    for path in glob.glob(os.path.join(root, pat)):
                        if os.path.isfile(path) and path.lower().endswith(exts):
                            try:
                                os.remove(path)
                                removed += 1
                            except OSError:
                                pass
            if removed:
                logger.info(f"Screenshot cleanup: removed {removed} old file(s)")
        except Exception:
            logger.debug("Screenshot cleanup failed", exc_info=True)

    # ------------------------------------------------------------------
    # Reset to defaults: arm/disarm two-step ("Уверены?") confirmation.
    # ------------------------------------------------------------------
    def _on_reset_clicked(self):
        if self.reset_btn.property("armed"):
            self._disarm_reset()
            self._do_reset()
        else:
            self.reset_btn.setProperty("armed", True)
            self.reset_btn.setText("Уверены?")
            self.reset_btn.setStyleSheet(
                "QPushButton#resetBtn { background:#7f1d1d; color:#fecaca;"
                " border:1px solid #ef4444; border-radius:6px; padding:6px;"
                " font-weight:bold; }")
            self._reset_arm_timer.start(4000)

    def _disarm_reset(self):
        self._reset_arm_timer.stop()
        self.reset_btn.setProperty("armed", False)
        self.reset_btn.setText("Сброс настроек")
        self.reset_btn.setStyleSheet("")

    def _do_reset(self):
        prev_autostart = bool(self.config.get("autostart", False))
        self.config_manager.reset_to_defaults()
        self.config = self.config_manager.config

        # Re-sync every control (their change handlers re-save the same
        # fresh config, which is harmless).
        self.lang_combo.setCurrentText(self._get_lang_display(self.config.get("lang", "ru")))
        self.delay_spin.setValue(int(self.config.get("delay_ms", 300)))
        self.pname_edit.setText(str(self.config.get("player_name", "")))
        glossary.set_player_name(self.config.get("player_name", ""))
        self.ttl_spin.setValue(int(self.config.get("overlay_ttl", 10)))
        self.backend_combo.setCurrentIndex(0 if self.config.get("backend", "google") == "google" else 1)
        self.apikey_edit.setText(self.config.get("api_key", ""))
        self.model_edit.setText(self.config.get("gemini_model", "gemini-2.5-flash"))
        if prev_autostart and not self.config.get("autostart", False):
            self.autostart_check.setChecked(False)  # also removes the Run key
        self.overlay_manager.ttl = 0

        # Restart the auto-capture loop with the default interval.
        self.auto_check.setChecked(bool(self.config.get("auto_screenshot", True)))
        self.interval_spin.setValue(int(self.config.get("recheck_ms", 1000)) / 1000.0)
        self._apply_recheck_timer()
        if self._translation_active:
            self.status_label.setText(self._active_status_text())

        self._debug_label.setText("Настройки сброшены")
        logger.info("Settings reset to defaults")

    # ------------------------------------------------------------------
    # Onboarding: film-style subtitle timeline explaining the controls.
    # ------------------------------------------------------------------
    def play_hints(self):
        iv = max(0.5, int(self.config.get("recheck_ms", 1000)) / 1000.0)
        ttl = int(self.config.get("overlay_ttl", 10))
        steps = [
            ("Кликните ЛКМ по тексту в игре — перевод появится поверх него", 4.5),
            (f"Или оставьте авто-скрин включённым: диалоги переводятся сами, "
             f"раз в {iv:g} с", 5.0),
            ("Правый Shift — скрыть/показать это окно настроек; при старте перевода окно прячется само", 5.5),
            (f"Перевод остаётся на экране, пока вы его не выключите; скриншоты удаляются через {ttl} с", 4.5),
            ("Язык, Google/Gemini и API-ключ — здесь же; «Сброс настроек» "
             "вернёт всё по умолчанию", 5.5),
            ("Готово! Повторить подсказки можно кнопкой «Подсказки»", 4.0),
        ]
        self._timeline.start(steps)

    def maybe_play_hints(self):
        """First launch ever -> play the hint subtitles once."""
        if not self.config.get("first_run", True):
            return
        self.config["first_run"] = False
        try:
            self.config_manager.save_config(self.config)
        except Exception:
            logger.exception("Failed to persist first_run")
        QTimer.singleShot(1500, self.play_hints)

    def _start_translation(self):
        self._translation_active = True
        self._expired_by_ttl = False
        self._signal_emitter.hook_start.emit()
        self.status_label.setText(self._active_status_text())
        self.status_label.setStyleSheet("color: #4ade80; font-family: 'Segoe UI'; font-size: 11px;")
        self.status_indicator.setStyleSheet("color: #4ade80;")
        self.toggle_btn.setText("■  Остановить перевод")
        self._debug_label.setText("Ожидание текста...")
        # The window covers the dialogue/choice-card zone: hide it so OCR
        # and clicks see the game, not our own controls. Shown again on stop.
        if self.isVisible():
            self._hidden_for_game = True
            self.hide()
            logger.info("Settings window hidden for game capture (shown again on stop)")
        logger.info("Translation started")

    def _stop_translation(self):
        self._translation_active = False
        self._pending_region = None
        self._capture_pending = False
        self._signal_emitter.hook_stop.emit()
        self.overlay_manager.remove_all()
        self._last_lines = []
        self._last_source = ""
        self._last_region = None
        self._baseline = None
        self._expired_by_ttl = False
        self.status_label.setText("Переводчик остановлен")
        self.status_label.setStyleSheet("color: #6b7280; font-family: 'Segoe UI'; font-size: 11px;")
        self.status_indicator.setStyleSheet("color: #ef4444;")
        self.toggle_btn.setText("▶  Запустить перевод")
        self._debug_label.setText("")
        if self._hidden_for_game:
            self._hidden_for_game = False
            self.show()
            self.raise_()
            self.activateWindow()
            logger.info("Settings window shown again after stop")
        logger.info("Translation stopped")

    def toggle_translation_from_tray(self):
        self.toggle_btn.setChecked(not self.toggle_btn.isChecked())

    def toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.activateWindow()
            self.raise_()

    def toggle_app_visibility(self):
        """Right Shift: hide/show the entire app (window + overlays)."""
        if self.isVisible():
            self.overlay_manager.remove_all()
            # User hid everything on purpose: don't auto-restore overlays.
            self._last_lines = []
            self.hide()
        else:
            self.show()
            self.activateWindow()
            self.raise_()

    def _on_overlays_expired(self):
        # TTL removed the overlays intentionally: no auto-restore, and the
        # unchanged-text re-check must not resurrect them either.
        self._last_lines = []
        self._expired_by_ttl = True

    def toggle_overlays(self):
        """Fallback: hide/show only the on-screen translation overlays."""
        self.overlay_manager.toggle()

    def _handle_lmb_click(self, x, y):
        try:
            logger.info(f"LMB click received at ({x}, {y}), active={self._translation_active}")

            if not self._translation_active:
                logger.debug("Translation not active, ignoring click")
                return

            # Old _is_target_window() checked the FOREGROUND window, which
            # often wasn't the game (clicks were silently dropped). The
            # window UNDER THE CURSOR is what matters — region lookup below
            # validates it (title/process of the clicked window) and logs why.
            region = self._get_window_region_at_cursor(x, y)
            if not region:
                logger.warning(f"No window found at ({x}, {y}), skipping")
                return

            self._click_x = x
            self._click_y = y
            logger.info(f"Capturing region: {region}")
            delay = self.config.get("delay_ms", 300)
            QTimer.singleShot(delay, lambda r=region: self._do_translate(r))
        except Exception as e:
            logger.error(f"Click handling error (ignored): {e}", exc_info=True)

    def _is_target_window(self):
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False

            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return False
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()

            if any(t in title for t in TARGET_TITLES):
                return True

            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                handle = kernel32.OpenProcess(0x0400, False, pid.value)
                if handle:
                    name_buf = ctypes.create_unicode_buffer(512)
                    kernel32.GetModuleBaseNameW(handle, None, name_buf, 512)
                    kernel32.CloseHandle(handle)
                    proc_name = name_buf.value.lower()
                    if any(p in proc_name for p in TARGET_PROCESSES):
                        return True
            except AttributeError:
                pass

            try:
                proc_name = psutil.Process(pid.value).name().lower()
                if any(p in proc_name for p in TARGET_PROCESSES):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        except Exception as e:
            logger.debug(f"Window check error: {e}")
        return False

    def _get_window_region_at_cursor(self, x, y):
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32

            pt = ctypes.wintypes.POINT(x, y)
            hwnd = user32.WindowFromPoint(pt)
            if not hwnd:
                return None

            root = user32.GetAncestor(hwnd, 2)
            if root:
                hwnd = root

            # Ignore windows that are not translation targets (e.g. taskbar
            # under the cursor) — their capture produced junk lines.
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            title_l = buf.value.lower()
            proc_l = ""
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # GetModuleBaseNameW lives in psapi.dll (NOT kernel32) — calling
            # it via kernel32 raised AttributeError and killed every click.
            try:
                proc_l = psutil.Process(pid.value).name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            is_target = (
                any(t in title_l for t in TARGET_TITLES if t.strip())
                or any(p in proc_l for p in TARGET_PROCESSES if p.strip())
            )
            if not is_target:
                logger.info(f"Window under cursor is not a target "
                            f"(title='{buf.value}', proc='{proc_l}'), skipping")
                return None

            if any(s in title_l for s in _SKIP_TITLES):
                # Clicks land on BlueStacks' invisible fullscreen keymap
                # overlay: translate against the REAL game window instead
                # (correct rect and the game title for the title-line drop).
                reg = find_target_window_region()
                if reg:
                    logger.info("Click over keymap overlay -> game region")
                    return reg
                return None

            rect = ctypes.wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    return {"left": rect.left, "top": rect.top, "width": w,
                            "height": h, "title": buf.value, "hwnd": hwnd}
        except Exception as e:
            logger.error(f"Failed to get window rect: {e}", exc_info=True)
        return None

    def _do_translate(self, region):
        try:
            logger.debug(f"_do_translate called: region={region}")
            if not self._translation_active:
                logger.debug("_do_translate: not active, skipping")
                return

            # Guard against a deleted/stale worker object (would raise on
            # isRunning()); if anything is off, just clear it.
            if self._worker is not None:
                try:
                    running = self._worker.isRunning()
                except RuntimeError:
                    self._worker = None
                    running = False
                if running:
                    # Don't drop the click: queue it and run it right after
                    # the current worker finishes (previously "missed" clicks).
                    logger.info("Worker busy, queueing click for after it finishes")
                    self._pending_region = (self._click_x, self._click_y)
                    return

            key = (region.get("left"), region.get("top"), region.get("width"), region.get("height"))
            cached = self._translate_cache.get(key)
            # Short TTL: a longer one returned the PREVIOUS dialogue line when
            # the player clicked through text quickly ("missed" translations).
            if cached is not None and (time.time() - cached[0]) < 0.5:
                logger.info("Reusing cached translation for region (skips OCR/translate)")
                self._on_translation_done(cached[1])
                return

            self._last_region_key = key
            self._last_region = dict(region)
            logger.info(f"Starting translation for region: {region}")
            # Clear any existing overlays so OCR captures the GAME text, not our
            # own translation from the previous click (caused "no text" repeats).
            self._capture_pending = True
            self.overlay_manager.remove_all()
            self._debug_label.setText("Перевод...")
            # Defer the actual worker start a few ms so the overlay hide fully
            # takes effect before the capture grabs the screen.
            QTimer.singleShot(60, lambda r=region: self._start_worker(r))
            logger.debug("TranslationWorker scheduled")
        except Exception as e:
            logger.error(f"Failed to start translation (ignored): {e}", exc_info=True)
            self._debug_label.setText(f"Ошибка: {e}")

    def _start_worker(self, region):
        try:
            if self._worker is not None and self._worker.isRunning():
                return
            self._worker = TranslationWorker(
                region, self.config.get("lang", "ru"),
                self.config.get("backend", "google"),
                self.config.get("api_key", ""),
                self.config.get("gemini_model", "gemini-2.5-flash"))
            self._worker.finished.connect(self._on_translation_done)
            self._worker.episode.connect(self._on_episode)
            self._worker.finished.connect(self._worker.deleteLater)
            self._worker.start()
            logger.debug("TranslationWorker started")
        except Exception as e:
            logger.error(f"Failed to start worker: {e}", exc_info=True)
            self._capture_pending = False

    def _on_episode(self, ep):
        logger.info(f"Current episode: {ep}")
        try:
            self._debug_label.setText(ep)
        except Exception:
            pass

    def _flush_pending(self):
        pending = self._pending_region
        self._pending_region = None
        if not pending or not self._translation_active:
            return
        x, y = pending
        region = self._get_window_region_at_cursor(x, y)
        if not region:
            return
        logger.info(f"Processing queued click region: {region}")
        QTimer.singleShot(150, lambda r=region: self._do_translate(r))

    def _on_translation_done(self, lines):
        # The capture this result belongs to has finished: the re-check
        # may restore overlays again.
        self._capture_pending = False
        # On-disk screenshots (debug/legacy tooling) live only briefly.
        ttl_s = int(self.config.get("overlay_ttl", 10))
        if ttl_s > 0:
            QTimer.singleShot(ttl_s * 1000, self._purge_screenshots)
        if not lines:
            logger.warning("Translation done: no lines")
            self._debug_label.setText("Нет текста")
            self._worker = None
            # Remember the (text-free) screen so the re-check can notice
            # when dialogue text appears later — without re-OCR'ing blindly.
            self._last_source = ""
            self._last_lines = []
            self._expired_by_ttl = False
            # Deliberately do NOT adopt a new baseline on the first miss:
            # text is often still on screen (OCR/filter hiccup) and a fresh
            # baseline would mask the change so the auto-recheck stopped
            # retrying — the translator just sat there doing nothing.
            # After two misses in a row accept the screen (real cutscene).
            self._empty_streak += 1
            if self._empty_streak >= 2:
                logger.info("Repeated empty result -> adopting baseline")
                self._update_baseline()
            self._flush_pending()
            return

        logger.info(f"Translation done: {len(lines)} lines")
        for line in lines:
            logger.info(f"  [{line['x']},{line['y']}] '{line['text'][:40]}' -> '{line['translation'][:40]}'")

        if self._last_region_key is not None and lines:
            self._translate_cache[self._last_region_key] = (time.time(), lines)

        self._last_lines = list(lines)
        self._expired_by_ttl = False
        self._empty_streak = 0
        self._own_strip_streak = 0
        # The re-check only OCRs the dialogue strip: _last_source must be
        # built from the SAME view (strip lines only), otherwise the
        # comparison never matches and every tick re-translates.
        strip = self._dialogue_strip(self._last_region)

        def _in_strip(l):
            if not strip:
                return True
            cy = l["y"] + l["h"] / 2
            return strip["top"] <= cy <= strip["top"] + strip["height"]

        self._last_source = _normalize_text(
            " ".join((l.get("raw") or l["text"]) for l in lines if _in_strip(l)))
        # Baseline must be captured BEFORE overlays cover the source text.
        self._update_baseline()

        self._debug_label.setText(f"Переведено: {len(lines)} строк")
        self.overlay_manager.show_lines(lines)
        # Worker is finished and scheduled for deleteLater; drop our reference
        # so the next click's isRunning() guard can't touch a deleted object.
        self._worker = None
        self._flush_pending()

    # ------------------------------------------------------------------
    # Periodic screen re-check: same letters -> do nothing, new letters ->
    # retranslate. Cutscenes without dialogue text show nothing at all.
    # ------------------------------------------------------------------
    def _dialogue_strip(self, region):
        """Bottom 60% of the window: the dialogue box (bottom 40%) PLUS
        the centered choice cards (~0.47) that auto-recheck must not miss
        — at the old 0.40-cut the strip started below every card. Still
        cheap to OCR and it dodges whatever overlaps the top of the screen."""
        if not region:
            return None
        cut = int(region["height"] * 0.40)
        if cut <= 0 or region["height"] - cut < 40:
            return None
        return {"left": region["left"], "top": region["top"] + cut,
                "width": region["width"], "height": region["height"] - cut}

    def _capture_strip_bgr(self):
        """BGR frame of the dialogue strip (for OCR). None on failure."""
        strip = self._dialogue_strip(self._last_region)
        if not strip:
            return None
        try:
            frame = TranslationWorker._get_processor()._capture_frame(strip)
            if frame is None or frame.size == 0:
                return None
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            return frame
        except Exception as e:
            logger.debug(f"Strip BGR capture failed: {e}")
            return None

    def _capture_strip(self):
        frame = self._capture_strip_bgr()
        if frame is None:
            return None
        if frame.ndim == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return frame

    def _update_baseline(self):
        if any(o.isVisible() for o in self.overlay_manager.overlays):
            return  # overlays cover the source text; don't poison baseline
        self._baseline = self._capture_strip()

    def _refresh_region(self):
        """Keep the re-check region glued to the window (it may move or
        resize while we watch). Returns False if the window is gone."""
        region = self._last_region
        hwnd = region.get("hwnd") if region else None
        if not hwnd:
            return region is not None
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            rect = ctypes.wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    region["left"] = rect.left
                    region["top"] = rect.top
                    region["width"] = w
                    region["height"] = h
            return True
        except Exception as e:
            logger.debug(f"region refresh failed: {e}")
            return True  # keep going with the old rect

    def _overlay_mask(self, strip):
        """Bool mask (strip coords) of pixels covered by visible overlays."""
        mask = np.zeros((strip["height"], strip["width"]), dtype=bool)
        for o in self.overlay_manager.overlays:
            if not o.isVisible():
                continue
            x0 = max(0, o.x() - strip["left"])
            y0 = max(0, o.y() - strip["top"])
            x1 = min(strip["width"], o.x() + o.width() - strip["left"])
            y1 = min(strip["height"], o.y() + o.height() - strip["top"])
            if x1 > x0 and y1 > y0:
                mask[y0:y1, x0:x1] = True
        return mask

    def _grab_clean_frame(self, frame, visible_ov):
        """Strip frame with our overlays out of the way.

        Hides (not destroys) the currently visible overlays, waits a DWM
        frame, captures, and shows them straight back — ~80ms blackout
        instead of the old remove_all()+150ms+whole-OCR vanish that
        flickered visibly every time an idle animation nudged `changed`
        over the threshold. Returns None if the capture failed (the
        overlays are shown back first)."""
        if not visible_ov:
            return frame
        for o in visible_ov:
            o.hide()
        time.sleep(0.08)  # let DWM recompose without the overlays
        clean = self._capture_strip_bgr()
        for o in visible_ov:
            o.show()
        return clean

    def _recheck_tick(self):
        try:
            if not self._translation_active:
                return
            if self._capture_pending:
                # A click is mid-capture (_do_translate removed its own
                # overlays and a capture is scheduled): don't race it.
                return
            if self._last_region is None:
                # Auto mode: no click yet — find the game window ourselves.
                reg = find_target_window_region()
                if not reg:
                    return  # game not open (or minimized) this second
                logger.info(f"Auto-capture: target window found: {reg.get('title')!r}")
                self._last_region = reg
                self._baseline = None
            if not self._refresh_region():
                logger.info("Re-check: target window is gone, pausing")
                self.overlay_manager.remove_all()
                self._last_region = None
                self._baseline = None
                self._last_source = ""
                self._last_lines = []
                return
            if self._worker is not None:
                try:
                    if self._worker.isRunning():
                        return
                except RuntimeError:
                    self._worker = None
            if self._recheck_worker is not None:
                try:
                    if self._recheck_worker.isRunning():
                        return
                except RuntimeError:
                    self._recheck_worker = None

            strip = self._dialogue_strip(self._last_region)
            if not strip:
                return
            frame = self._capture_strip_bgr()
            if frame is None:
                return
            cur = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            visible_ov = [o for o in self.overlay_manager.overlays
                          if o.isVisible()]
            has_ov = bool(visible_ov)
            b = self._baseline
            if b is None:
                # First screenshot of the session: remember the clean
                # screen AND OCR it right away (text may already be on it).
                clean = self._grab_clean_frame(frame, visible_ov)
                if clean is None:
                    return
                self._baseline = cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY)
                self._start_recheck(frame=clean)
                return
            if cur.shape != b.shape:
                b = cv2.resize(b, (cur.shape[1], cur.shape[0]))
                self._baseline = b
            diff = np.abs(cur.astype(np.int16) - b.astype(np.int16)) > 25

            if has_ov:
                # Our overlays cover the source text — those pixels are the
                # translation, not the game. Only pixels OUTSIDE them can
                # reveal a screen change; unchanged => overlays stay until
                # their TTL clears them.
                mask = self._overlay_mask(strip)
                valid = ~mask
                n = int(valid.sum())
                if n == 0:
                    return
                changed = float(diff[valid].sum()) / n
            else:
                changed = float(diff.mean())

            # Adaptive threshold: when our own windows cover the strip
            # (settings UI, hint card), tiny pixel changes from cursor
            # blinks and UI repaints constantly cross the 0.01 threshold
            # and trigger an expensive OCR pass that just reads our own
            # UI and produces nothing. Raise the threshold progressively
            # after consecutive own-covered rechecks.
            own_strip = is_strip_mostly_own(strip)
            if own_strip:
                self._own_strip_streak += 1
                effective = min(0.01 * (1 + self._own_strip_streak), 0.10)
            else:
                self._own_strip_streak = 0
                effective = 0.01

            if changed < effective:
                if (not has_ov and self._last_lines and not self._expired_by_ttl
                        and not self._capture_pending):
                    logger.info("Re-check: source text still on screen -> restoring overlays")
                    self.overlay_manager.show_lines(self._last_lines)
                return  # letters didn't change -> do nothing (no OCR at all)

            logger.info(f"Re-check: screen changed ({changed:.3f}), re-OCRing strip")
            # If the strip is covered by our own windows (settings UI,
            # hint card, etc.), skip the expensive hide-capture-show-OCR
            # cycle entirely — it would just read our own UI text and the
            # filter would drop every line anyway.
            if own_strip:
                logger.info("Re-check: strip covered by own windows, skipping OCR")
                return
            clean = self._grab_clean_frame(frame, visible_ov)
            if clean is None:
                return
            self._baseline = cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY)
            self._start_recheck(frame=clean)
        except Exception as e:
            logger.error(f"Recheck tick error: {e}", exc_info=True)

    def _start_recheck(self, frame=None):
        try:
            if not self._translation_active or self._last_region is None:
                return
            if self._recheck_worker is not None:
                try:
                    if self._recheck_worker.isRunning():
                        return
                except RuntimeError:
                    self._recheck_worker = None
            strip = self._dialogue_strip(self._last_region)
            self._recheck_worker = RecheckWorker(
                strip, self._last_region,
                (self._last_region.get("title") or "").strip(),
                self.config.get("lang", "ru"), frame=frame)
            self._recheck_worker.result.connect(self._on_recheck_done)
            self._recheck_worker.finished.connect(self._recheck_worker.deleteLater)
            self._recheck_worker.start()
        except Exception as e:
            logger.error(f"Recheck start error: {e}", exc_info=True)

    def _on_recheck_done(self, norm):
        try:
            self._recheck_worker = None
            if norm is None or not self._translation_active:
                return
            if self._worker is not None:
                try:
                    if self._worker.isRunning():
                        return  # a click won the race; its result is newer
                except RuntimeError:
                    self._worker = None
            if not norm:
                # Cutscene / nothing readable. Keep the overlays and
                # _last_lines: the hide-capture-show cycle already put the
                # overlays back, the user's rule is "translation stays
                # until replaced", and the old clear here wiped a just-
                # shown translation until a full retranslate (flicker).
                # The tick already baselined the clean frame — refresh it
                # once more in case something moved during the OCR.
                logger.info("Re-check: no text on screen -> keeping overlays")
                self._update_baseline()
                return
            prev = self._last_source
            if prev and SequenceMatcher(None, norm, prev).ratio() >= 0.85:
                # Same letters: refresh the baseline (no-op while overlays
                # are visible — the tick baselined its clean frame).
                self._update_baseline()
                if self._capture_pending:
                    # A click removed the overlays for its own capture: let
                    # ITS result decide what to show, don't race it (a
                    # restore here would put our Cyrillic text back into
                    # the in-flight OCR frame -> junk re-translation loop).
                    logger.info("Re-check: text unchanged, click capture in flight -> staying hidden")
                    return
                if any(o.isVisible() for o in self.overlay_manager.overlays):
                    # The tick's hide-capture-show already restored them;
                    # recreating the windows here would flicker anyway.
                    logger.info("Re-check: text unchanged -> overlays kept")
                    return
                if self._last_lines:
                    logger.info("Re-check: text unchanged -> restoring overlays")
                    self.overlay_manager.show_lines(self._last_lines)
                elif self._expired_by_ttl:
                    # TTL already showed this text for its full 10s: leave
                    # it hidden — only NEW text earns a fresh overlay.
                    logger.info("Re-check: text unchanged, overlays expired by TTL -> staying hidden")
                else:
                    logger.info("Re-check: text unchanged but overlays lost -> retranslating")
                    self._do_translate(self._last_region)
                return
            if not prev:
                logger.info(f"Re-check: text found -> translating: {norm[:80]}")
            else:
                logger.info(f"Re-check: text changed -> retranslating\n"
                            f"  old: {prev[:80]}\n  new: {norm[:80]}")
            self._do_translate(self._last_region)
        except Exception as e:
            logger.error(f"Recheck done error: {e}", exc_info=True)

    def closeEvent(self, event):
        logger.info("Close event triggered")
        self._translation_active = False
        self._timeline.stop()
        self._pending_region = None
        self._recheck_timer.stop()
        self._signal_emitter.hook_stop.emit()
        # Don't block the close waiting on a slow network translation.
        if self._worker is not None:
            try:
                self._worker.quit()
            except Exception:
                pass
            self._worker = None
        if self._recheck_worker is not None:
            try:
                self._recheck_worker.quit()
            except Exception:
                pass
            self._recheck_worker = None
        self.overlay_manager.cleanup()
        # Take our screenshots with us and EXIT THE PROCESS completely —
        # no lingering tray state, no RShift resurrection, no RAM kept.
        self._purge_screenshots()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)
        super().closeEvent(event)
