# -*- mode: python ; coding: utf-8 -*-
import glob as _glob
import os as _os
from PyInstaller.utils.hooks import collect_submodules as _collect_submodules

# Bundled tesseract: exe + DLLs only (tessdata comes from data/tessdata).
_tess_dir = r"C:\Program Files\Tesseract-OCR"
_tesseract_binaries = [(_os.path.join(_tess_dir, "tesseract.exe"), "tesseract")]
_tesseract_binaries += [(p, "tesseract")
                        for p in _glob.glob(_os.path.join(_tess_dir, "*.dll"))]
if len(_tesseract_binaries) < 2:
    raise SystemExit("tesseract not found at %s" % _tess_dir)

# Primary OCR: winocr (Windows.Media.Ocr) — winocr.py top-level imports
# winrt.* statically, but the winrt namespace packages are easy for the
# analyzer to miss; collect them explicitly. rapidocr/onnxruntime are
# deliberately NOT bundled (dev-only optional backend).
_winocr_hidden = ['winocr'] + _collect_submodules('winrt')

# data/ as (src, dst_dir) pairs; jpn.traineddata at data/ root is a 34MB
# duplicate of data/tessdata/jpn.traineddata — not bundled.
_data_files = []
for _root, _dirs, _files in _os.walk('data'):
    for _f in _files:
        if _f == 'jpn.traineddata':
            continue
        _src = _os.path.join(_root, _f)
        _dst = _os.path.relpath(_root, '.').replace('\\', '/')
        _data_files.append((_src, _dst))

a = Analysis(
    ['Translator_for_BA.py'],
    pathex=[],
    binaries=_tesseract_binaries,
    datas=_data_files + [('ba_data/ba_scripts.db', 'ba_data')],
    hiddenimports=['dxcam', 'win32gui', 'win32process', 'pytesseract',
                   'numpy._core._exceptions', 'numpy.core._exceptions']
                  + _winocr_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # rapidocr/onnxruntime stay OUT of the exe: huge, and loading them
    # next to winrt hard-crashes the process (ocr_engine guards it too).
    excludes=['rapidocr', 'onnxruntime'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BA_Translator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='data\\app.ico',
)
