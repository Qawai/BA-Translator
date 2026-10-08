# -*- coding: utf-8 -*-
"""OCR backends behind one interface.

Bench on real BA frames (2026-10-08, _bench_ocr.py):
  winocr     101-162 ms, tight per-line boxes, no artwork hallucination
  rapidocr  1082-1707 ms, best quality + confidence + CJK, heavy
  tesseract 1100-1700 ms, mega-lines merging the whole screen

"auto" prefers winocr (Windows.Media.Ocr — the fastest and cleanest for
game dialogue), falls back to tesseract (e.g. source_lang ja without the
Japanese OCR language, or a machine without the OCR capability).
rapidocr stays available via config ocr_backend="rapidocr" (dev only —
the exe does not bundle its models).

All backends return lines as dicts {"text", "x", "y", "w", "h"[, "conf"]}
in the coordinate space of the passed frame (no scaling applied by
winocr/rapidocr; the tesseract path keeps its own x2 upscale internally
and reports back in original coordinates via the caller's scale arg).
"""

import logging

logger = logging.getLogger(__name__)


class BackendUnavailable(Exception):
    """Raised when a backend cannot run (missing module/language)."""


# --- winrt vs onnxruntime: mutually exclusive in ONE process ---------------
# HARD crash (silent process death, no traceback) on this machine when
# BOTH `winrt.*` and onnxruntime sessions are loaded — verified: plain
# `import winrt.windows.media.ocr` + RapidOCR() init kills the process.
# First runtime to load binds the process; the other one must degrade
# to a BackendUnavailable -> tesseract fallback instead of crashing.
_native_runtime = None  # None | "winrt" | "onnxruntime"


def _claim_runtime(name):
    global _native_runtime
    if _native_runtime is None:
        _native_runtime = name
        return True
    if _native_runtime == name:
        return True
    logger.warning(
        f"OCR runtime {name} blocked: process already uses "
        f"{_native_runtime} (winrt+onnxruntime crash together); "
        f"backend disabled, tesseract fallback applies")
    return False


# --------------------------------------------------------------------------
# winocr — Windows.Media.Ocr via the tiny winocr wrapper
# --------------------------------------------------------------------------
def winocr_lang_supported(lang="en"):
    if not _claim_runtime("winrt"):
        return False
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        return bool(OcrEngine.is_language_supported(Language(lang)))
    except Exception as e:
        logger.debug(f"winocr availability check failed: {e}")
        return False


def _winocr_lines(frame, lang="en"):
    if not _claim_runtime("winrt"):
        raise BackendUnavailable(
            "winrt blocked: onnxruntime (rapidocr) already active in "
            "this process; restart with ocr_backend=winocr/auto")
    try:
        import winocr
    except Exception as e:
        raise BackendUnavailable(f"winocr import failed: {e}")
    try:
        # BGR ndarray in; picklable dict {lines: [{words: [{text,
        # bounding_rect: {x, y, width, height}}]}]} out (sync wrapper
        # around recognize_async). Verified against real BA frames.
        r = winocr.recognize_cv2_sync(frame, lang)
    except Exception as e:
        raise BackendUnavailable(f"winocr recognize failed: {e}")
    out = []
    for ln in (r.get("lines") or []):
        words = ln.get("words") or []
        rects = [(w.get("text") or "").strip() for w in words]
        text = " ".join(t for t in rects if t).strip()
        if not text:
            continue
        boxes = [w.get("bounding_rect") or {} for w in words]
        try:
            x0 = min(b.get("x", 0) for b in boxes)
            y0 = min(b.get("y", 0) for b in boxes)
            x1 = max(b.get("x", 0) + b.get("width", 0) for b in boxes)
            y1 = max(b.get("y", 0) + b.get("height", 0) for b in boxes)
        except ValueError:
            continue
        out.append({"text": text, "x": int(x0), "y": int(y0),
                    "w": max(1, int(x1 - x0)), "h": max(1, int(y1 - y0))})
    return out


# --------------------------------------------------------------------------
# rapidocr — PP-OCRv6 det+rec over ONNX Runtime (installed in the venv,
# models inside the wheel; NOT bundled into the exe to keep it lean)
# --------------------------------------------------------------------------
_rapid_engine = None


def _rapid_lines(frame, min_conf=0.5):
    global _rapid_engine
    if not _claim_runtime("onnxruntime"):
        raise BackendUnavailable(
            "onnxruntime blocked: winrt (winocr) already active in this "
            "process; restart with ocr_backend=rapidocr")
    try:
        if _rapid_engine is None:
            from rapidocr import RapidOCR
            _rapid_engine = RapidOCR()
    except Exception as e:
        raise BackendUnavailable(f"rapidocr init failed: {e}")
    try:
        res = _rapid_engine(frame)
    except Exception as e:
        raise BackendUnavailable(f"rapidocr infer failed: {e}")
    out = []
    txts = getattr(res, "txts", None)
    scores = getattr(res, "scores", None)
    boxes = getattr(res, "boxes", None)
    txts = [] if txts is None else list(txts)
    scores = [] if scores is None else list(scores)
    boxes = [] if boxes is None else list(boxes)
    for txt, sc, box in zip(txts, scores, boxes):
        txt = str(txt).strip()
        conf = float(sc) if sc is not None else 1.0
        if not txt or conf < min_conf:
            continue
        try:
            pts = [[float(p[0]), float(p[1])] for p in box]
        except Exception:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        out.append({"text": txt, "x": int(x0), "y": int(y0),
                    "w": max(1, int(x1 - x0)), "h": max(1, int(y1 - y0)),
                    "conf": conf})
    return out


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def map_winocr_lang(src_lang):
    """source_lang -> Windows OCR language, None = not supported natively."""
    if src_lang == "en":
        return "en"
    if src_lang == "ja":
        return "ja"
    return None  # "auto"/unknown: mixed jpn+eng needs tesseract's combo


def resolve_backend(pref, src_lang="en"):
    """Config value + source language -> concrete backend name."""
    pref = (pref or "auto").strip().lower()
    if pref == "tesseract":
        return "tesseract"
    if pref == "rapidocr":
        return "rapidocr"
    if pref in ("winocr", "auto"):
        lang = map_winocr_lang(src_lang)
        if lang and winocr_lang_supported(lang):
            return "winocr"
        if pref == "winocr":
            logger.warning(f"winocr unavailable for source_lang={src_lang!r}")
            return "tesseract"
        return "tesseract"
    logger.warning(f"Unknown ocr_backend {pref!r}, using tesseract")
    return "tesseract"


def backend_lines(frame, backend, src_lang="en"):
    """OCR one frame with the given backend; returns frame-coord lines.

    Raises BackendUnavailable when the backend cannot run — the caller
    falls back to the tesseract path.
    """
    if backend == "winocr":
        lang = map_winocr_lang(src_lang) or "en"
        return _winocr_lines(frame, lang)
    if backend == "rapidocr":
        return _rapid_lines(frame)
    raise BackendUnavailable(f"backend {backend!r} has no direct frame path")


def warmup(pref, src_lang="en"):
    """Resolve the backend once at startup: logs packaging problems early
    and pays the one-time import cost before the first real capture."""
    global _rapid_engine
    try:
        name = resolve_backend(pref, src_lang)
        logger.info(f"OCR backend resolved: {name} (pref={pref!r}, "
                    f"source_lang={src_lang!r})")
        if name == "rapidocr":
            if not _claim_runtime("onnxruntime"):
                logger.warning("rapidocr warmup skipped (winrt already "
                               "active); falling back to tesseract")
                return "tesseract"
            from rapidocr import RapidOCR
            if _rapid_engine is None:
                _rapid_engine = RapidOCR()
            logger.info("rapidocr engine initialized")
        return name
    except Exception as e:
        logger.warning(f"OCR backend warmup failed, using tesseract: {e}")
        return "tesseract"
