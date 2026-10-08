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
- Python 3.11+ (только для запуска из исходников)
- BlueStacks (or any Android emulator) or Steam version of Blue Archive

**Tesseract НЕ нужен** — основной OCR-движок встроенный (Windows.Media.Ocr / winocr).

## Как запустить

### Способ 1: Готовый exe (рекомендуется, ничего ставить не надо)

1. Скачай `BA_Translator.exe` со страницы [Releases](https://github.com/Qawai/BA-Translator/releases).
2. Запусти его — никакой установки и зависимостей не нужно.
3. В окне настроек нажми **«Старт»** (окно настроек скроется).
4. Открой Blue Archive в BlueStacks — перевод появится поверх игры.
5. Чтобы вернуть окно настроек — нажми **«Стоп»** или иконку в трее.

### Способ 2: Из исходников (Python)

```bat
git clone https://github.com/Qawai/BA-Translator.git
cd BA-Translator

python -m venv venv_win
venv_win\Scripts\activate
pip install -r requirements.txt

python Translator_for_BA.py
```

Далее — «Старт» в окне настроек и открыть игру.

### Способ 3: Собрать exe самостоятельно

```bat
venv_win\Scripts\pip install pyinstaller
venv_win\Scripts\pyinstaller --noconfirm --clean Translator_for_BA_win.spec
```

Готовый файл: `dist\BA_Translator.exe`.

### Порядок работы

1. Запусти переводчик (exe или Python).
2. Выбери язык игры (EN/JA) и целевой язык (RU) в настройках.
3. Нажми **Старт** — окно настроек скроется, начнётся захват экрана.
4. Открой Blue Archive: переводчик сам найдёт окно эмулятора и начнёт переводить диалоги поверх игры.
5. **Стоп** (или иконка в трее) — остановить перевод и вернуть окно настроек.

> **Важно:** окно эмулятора не должно быть закрыто другими окнами в зоне диалогов (низ экрана) — иначе OCR не увидит текст.

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