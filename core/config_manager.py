import os
import sys
import json
import logging

DEFAULT_CONFIG = {
    "lang": "ru",
    "delay_ms": 300,
    "autostart": False,
    "first_run": True,
    "backend": "google",
    "api_key": "",
    "gemini_model": "gemini-2.5-flash",
    # Seconds before ON-DISK screenshots are deleted (0 = never).
    # NOTE: overlays are NOT auto-cleared — the translation stays on
    # screen until replaced or the translator is stopped (user's rule).
    "overlay_ttl": 10,
    # The player's in-game name: used for [USERNAME] script lines and
    # kept verbatim in translations ("" = not set).
    "player_name": "",
    # Startup error-reporting screen; hidden permanently when the user
    # ticks "не показывать".
    "show_disclaimer": True,
    # How often the app grabs the screen by itself (auto-capture loop).
    "recheck_ms": 1000,
    # Auto-capture master switch: False = manual LMB clicks only.
    "auto_screenshot": True,
    # Language of the ON-SCREEN text (OCR): "en" -> eng-only OCR,
    # "ja"/"auto" -> jpn+eng.
    "source_lang": "en",
    # OCR engine: "auto" (Windows.Media.Ocr if available, else tesseract),
    # "winocr", "rapidocr", "tesseract".
    "ocr_backend": "auto"
}

class ConfigManager:
    def __init__(self):
        if getattr(sys, 'frozen', False):
            # Writable state (config.json, dialogs_db.json) must live NEXT
            # TO THE EXE — sys._MEIPASS is a temp extraction dir that is
            # wiped on reboot, so settings saved there silently vanish.
            argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
            if argv0:
                self.base_dir = os.path.dirname(os.path.abspath(argv0))
            else:
                self.base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        self.config_path = os.path.join(self.base_dir, "data", "config.json")
        self.config = self.load_config()

    def load_config(self):
        if not os.path.exists(self.config_path):
            return DEFAULT_CONFIG.copy()
        try:
            # utf-8-sig: tolerate a BOM — Notepad/PowerShell re-saves add
            # one and plain json.load would reject the whole file.
            with open(self.config_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            for key, val in DEFAULT_CONFIG.items():
                if key not in cfg:
                    cfg[key] = val
            # gemini-1.5 was retired by Google: migrate the old default
            # (stored configs keep it forever otherwise -> always 404).
            if cfg.get("gemini_model") == "gemini-1.5-flash":
                cfg["gemini_model"] = DEFAULT_CONFIG["gemini_model"]
            return cfg
        except Exception:
            logging.exception("Failed to load config from %s, using defaults", self.config_path)
            return DEFAULT_CONFIG.copy()

    def save_config(self, config_data):
        self.config = config_data
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)

    def reset_to_defaults(self):
        self.save_config(DEFAULT_CONFIG.copy())
        return self.config

    def set_autostart(self, enable):
        self.config["autostart"] = enable
        self.save_config(self.config)
        if sys.platform == "win32":
            self._set_autostart_win(enable)
        else:
            self._set_autostart_linux(enable)

    def _set_autostart_win(self, enable):
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            )
            if enable:
                if getattr(sys, 'frozen', False):
                    exe = f'"{sys.executable}"'
                else:
                    script = os.path.abspath(
                        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "Translator_for_BA.py")
                    )
                    exe = f'"{sys.executable}" "{script}"'
                winreg.SetValueEx(key, "BA_Translator", 0, winreg.REG_SZ, exe)
            else:
                try:
                    winreg.DeleteValue(key, "BA_Translator")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception:
            logging.exception("Failed to set autostart (win32)")

    def _set_autostart_linux(self, enable):
        try:
            autostart_dir = os.path.expanduser("~/.config/autostart")
            desktop_file = os.path.join(autostart_dir, "ba-translator.desktop")
            if enable:
                os.makedirs(autostart_dir, exist_ok=True)
                if getattr(sys, 'frozen', False):
                    exe_dir = os.path.dirname(sys.executable)
                    exe = os.path.join(exe_dir, "Translator_for_BA")
                else:
                    script = os.path.abspath(
                        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "Translator_for_BA.py")
                    )
                    exe = f'{sys.executable} "{script}"'
                with open(desktop_file, "w", encoding="utf-8") as f:
                    f.write(
                        f"[Desktop Entry]\n"
                        f"Type=Application\n"
                        f"Name=BA Translator\n"
                        f"Exec={exe}\n"
                        f"Hidden=false\n"
                        f"NoDisplay=false\n"
                    )
            else:
                if os.path.exists(desktop_file):
                    os.remove(desktop_file)
        except Exception:
            logging.exception("Failed to set autostart (linux)")
