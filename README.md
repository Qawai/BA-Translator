# BA Translator

Оверлейный переводчик **Blue Archive** (EN → RU) в реальном времени — BlueStacks / Steam.

---

## Возможности

- **Быстрый OCR**: Windows.Media.Ocr (winocr) — 100–160 мс на кадр, в 10 раз быстрее Tesseract
- **Ожидание печати текста**: переводит только полную реплику, а не печатающуюся по буквам
- **Отбраковка «кривых» переводов**: ловит мусор от Google Translate API
- **Фильтр шумов**: отбрасывает артефакты OCR (кнопки, иконки, UI-элементы)
- **Без мерцания**: 86 мс затемнения вместо 0.5–2 с при перепроверке
- **Защита имён**: имена персонажей (Rin, Shiroko и т.д.) не портятся при переводе
- **База сценариев**: берёт перевод из базы скриптов игры, когда возможно

## Требования

- Windows 10/11
- Python 3.11+ (только для запуска из исходников)
- BlueStacks (или любой другой Android-эмулятор) либо Steam-версия Blue Archive

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

## Настройки

Файл `data/config.json`:
- `lang`: целевой язык (по умолчанию "ru")
- `backend`: сервис перевода ("google", "gemini")
- `ocr_backend`: OCR-движок ("auto", "winocr", "tesseract")
- `source_lang`: язык игры ("en", "ja", "auto")
- `overlay_ttl`: время жизни оверлея в секундах (0 — вечно)
- `recheck_ms`: интервал перепроверки в миллисекундах

## Структура проекта

- `core/ocr_engine.py`: OCR-движки (winocr, rapidocr, tesseract)
- `core/screen_processor.py`: захват экрана и обработка OCR
- `core/translator_api.py`: API перевода (Google, MyMemory, Gemini)
- `core/config_manager.py`: управление конфигурацией
- `core/window_handler.py`: поиск окна и проверка перекрытий
- `ui/settings_window.py`: основной UI и логика перевода
- `ui/overlay_window.py`: окна-оверлеи перевода

## Лицензия

MIT License

---

# BA Translator (English)

A real-time English → Russian overlay translator for **Blue Archive** (BlueStacks / Steam).

## Features

- **Fast OCR**: Windows.Media.Ocr (winocr) — 100–160 ms per frame, 10x faster than Tesseract
- **Typewriter detection**: Waits for the full dialogue line before translating
- **Garbled translation rejection**: Rejects mojibake from Google Translate API
- **Noise filter**: Rejects OCR artifacts (buttons, icons, UI elements)
- **Flicker-free**: 86 ms blackout instead of 0.5–2 s during rechecks
- **Name protection**: Preserves character names (Rin, Shiroko, etc.) in translations
- **Script DB lookup**: Uses the game script database for accurate translations

## Requirements

- Windows 10/11
- Python 3.11+ (only for running from source)
- BlueStacks (or any other Android emulator) or the Steam version of Blue Archive

**Tesseract is NOT required** — the primary OCR engine is built in (Windows.Media.Ocr / winocr).

## How to Run

### Method 1: Prebuilt exe (recommended, nothing to install)

1. Download `BA_Translator.exe` from the [Releases](https://github.com/Qawai/BA-Translator/releases) page.
2. Run it — no installation or dependencies needed.
3. Click **Start** in the settings window (it will hide).
4. Open Blue Archive in BlueStacks — translations appear over the game.
5. To bring the settings window back — click **Stop** or the tray icon.

### Method 2: From source (Python)

```bat
git clone https://github.com/Qawai/BA-Translator.git
cd BA-Translator

python -m venv venv_win
venv_win\Scripts\activate
pip install -r requirements.txt

python Translator_for_BA.py
```

Then click **Start** in the settings window and open the game.

### Method 3: Build the exe yourself

```bat
venv_win\Scripts\pip install pyinstaller
venv_win\Scripts\pyinstaller --noconfirm --clean Translator_for_BA_win.spec
```

Output: `dist\BA_Translator.exe`.

### Workflow

1. Launch the translator (exe or Python).
2. Select the game language (EN/JA) and target language (RU) in settings.
3. Click **Start** — the settings window hides and screen capture begins.
4. Open Blue Archive: the translator finds the emulator window automatically and overlays translations on the dialogue.
5. **Stop** (or the tray icon) — stops translation and brings the settings window back.

> **Important:** the emulator window must not be covered by other windows in the dialogue area (bottom of the screen) — otherwise OCR cannot see the text.

## Configuration

File `data/config.json`:
- `lang`: target language (default: "ru")
- `backend`: translation backend ("google", "gemini")
- `ocr_backend`: OCR engine ("auto", "winocr", "tesseract")
- `source_lang`: source language ("en", "ja", "auto")
- `overlay_ttl`: overlay time-to-live in seconds (0 = forever)
- `recheck_ms`: recheck interval in milliseconds

## Architecture

- `core/ocr_engine.py`: OCR backends (winocr, rapidocr, tesseract)
- `core/screen_processor.py`: screen capture and OCR processing
- `core/translator_api.py`: translation APIs (Google, MyMemory, Gemini)
- `core/config_manager.py`: configuration management
- `core/window_handler.py`: window detection and occlusion checks
- `ui/settings_window.py`: main UI and translation logic
- `ui/overlay_window.py`: translation overlay windows

## License

MIT License
