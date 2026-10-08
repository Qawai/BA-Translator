# BA Translator

A real-time English → Russian overlay translator for **Blue Archive** (BlueStacks / Steam).

## Features

- **Fast OCR**: Windows.Media.Ocr (winocr) — 100-160ms per frame, 10x faster than Tesseract
- **Typewriter detection**: Waits for full dialogue before translating
- **Garbled translation rejection**: Rejects mojibake from Google Translate API
- **Noise filter**: Rejects OCR artifacts (buttons, icons, UI elements)
- **Flicker-free**: 86ms blackout instead of 0.5-2s during rechecks
- **Name protection**: Preserves character names (Rin, Shiroko, etc.) in translations
- **Script DB lookup**: Uses game script database for accurate translations

## Requirements

- Windows 10/11
- Python 3.11+
- BlueStacks (or any Android emulator) or Steam version of Blue Archive

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Qawai/BA-Translator.git
   cd BA-Translator
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv_win
   venv_win\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Install Tesseract OCR:
   - Download from: https://github.com/UB-Mannheim/tesseract/wiki
   - Install to `C:\Program Files\Tesseract-OCR`
   - Copy `eng.traineddata` and `jpn.traineddata` to `data/tessdata/`

## Usage

1. Run the translator:
   ```bash
   python Translator_for_BA.py
   ```

2. Click "Start" in the settings window
3. Open Blue Archive in BlueStacks
4. The translator will automatically detect and translate dialogue

## Building the Executable

```bash
pyinstaller --noconfirm --clean Translator_for_BA_win.spec
```

The executable will be in `dist/BA_Translator.exe`.

## Configuration

Edit `data/config.json`:
- `lang`: Target language (default: "ru")
- `backend`: Translation backend ("google", "gemini")
- `ocr_backend`: OCR engine ("auto", "winocr", "tesseract")
- `source_lang`: Source language ("en", "ja", "auto")
- `overlay_ttl`: Overlay time-to-live in seconds (0 = forever)
- `recheck_ms`: Recheck interval in milliseconds

## Architecture

- `core/ocr_engine.py`: OCR backends (winocr, rapidocr, tesseract)
- `core/screen_processor.py`: Screen capture and OCR processing
- `core/translator_api.py`: Translation APIs (Google, MyMemory, Gemini)
- `core/config_manager.py`: Configuration management
- `core/window_handler.py`: Window detection and occlusion checks
- `ui/settings_window.py`: Main UI and translation logic
- `ui/overlay_window.py`: Translation overlay windows

## License

MIT License