import sys
import os
import ctypes
import ctypes.wintypes
import threading
import time
import logging

# One instance only: a second copy would double the mouse/keyboard hooks,
# run two OCR loops over the same screen (UI freezes, "подвис") and truncate
# run.log. Named kernel mutex — released automatically if we crash.
_k32 = ctypes.windll.kernel32
_k32.CreateMutexW.restype = ctypes.c_void_p
_single_mutex = _k32.CreateMutexW(None, False, "BA_Translator_SingleInstance")
if _k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    sys.exit(0)

if getattr(sys, 'frozen', False) and sys.argv and sys.argv[0]:
    # Frozen: __file__ points into the temp extraction dir — run.log must
    # stay NEXT TO THE EXE so users can attach it to an error report.
    _log_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
else:
    _log_dir = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(_log_dir, "run.log"),
            encoding="utf-8", mode="w"),
    ],
)

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import QObject, pyqtSignal

from core.config_manager import ConfigManager
from core.integrity_checker import check_integrity
from ui.settings_window import SettingsWindow, TranslationSignalEmitter
from ui.tray_icon import TrayIcon

user32 = ctypes.windll.user32
WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", ctypes.wintypes.POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.POINTER(ctypes.c_ulong))


class GlobalMouseHook:
    def __init__(self, callback):
        self.callback = callback
        self.hook_id = None
        self._hookproc = HOOKPROC(self._handler)

    def _handler(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam == WM_LBUTTONDOWN:
            struct = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            self.callback(struct.pt.x, struct.pt.y)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def start(self):
        self.hook_id = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hookproc, None, 0)
        if not self.hook_id:
            logging.error("Failed to set mouse hook!")
            return False
        self._thread = threading.Thread(target=self._message_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self.hook_id:
            user32.UnhookWindowsHookEx(self.hook_id)
            self.hook_id = None

    def _message_loop(self):
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
VK_RSHIFT = 0xA1


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


KHOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.POINTER(ctypes.c_ulong))


class GlobalKeyboardHook:
    def __init__(self, callback):
        self.callback = callback
        self.hook_id = None
        self._hookproc = KHOOKPROC(self._handler)

    def _handler(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam == WM_KEYDOWN:
            vk = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
            if vk == VK_RSHIFT:
                self.callback()
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def start(self):
        self.hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hookproc, None, 0)
        if not self.hook_id:
            logging.error("Failed to set keyboard hook!")
            return False
        self._thread = threading.Thread(target=self._message_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self.hook_id:
            user32.UnhookWindowsHookEx(self.hook_id)
            self.hook_id = None

    def _message_loop(self):
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


class App:
    def __init__(self):
        if sys.platform == "win32":
            try:
                # Real app identity in the taskbar/Alt-Tab instead of
                # "pythonw.exe" (generic Python icon).
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "BA-Translator.1.0")
            except Exception:
                pass
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.ico")
        if os.path.isfile(ico):
            from PyQt5.QtGui import QIcon
            self.app.setWindowIcon(QIcon(ico))
        # Whatever quits the event loop (window X, tray exit) must release
        # hooks/tray too — no half-alive processes that answer RShift.
        self.app.aboutToQuit.connect(self._shutdown)

        self.config_manager = ConfigManager()
        self.config = self.config_manager.config

        self.signal_emitter = TranslationSignalEmitter()
        self.settings_window = SettingsWindow(self.signal_emitter)
        self.tray = TrayIcon(self.settings_window)

        self.tray.toggle_translation.connect(self.settings_window.toggle_translation_from_tray)
        self.tray.quit_app.connect(self._quit)
        self.tray.show_settings.connect(self.settings_window.toggle_visibility)

        self._mouse_hook = None
        self._keyboard_hook = None
        self._hook_active = False
        self._rshift_last = 0
        self._shutdown_done = False

    def run(self):
        if not check_integrity():
            QMessageBox.critical(None, "Error", "Missing required data files!")
            return 1

        self.settings_window.show()
        self.tray.show()
        # Startup disclaimer (errors / where to report / log location);
        # user can disable it permanently via the checkbox.
        try:
            from ui.disclaimer import show_disclaimer
            show_disclaimer(self.config, self.config_manager)
        except Exception:
            logging.getLogger("BA_Translator").exception("disclaimer failed")
        # First launch ever: play the subtitle hints over the screen.
        self.settings_window.maybe_play_hints()

        self.signal_emitter.hook_start.connect(self._start_mouse_hook)
        self.signal_emitter.hook_stop.connect(self._stop_mouse_hook)

        self._start_keyboard_hook()

        return self.app.exec_()

    def _start_keyboard_hook(self):
        if self._keyboard_hook:
            return

        def on_rshift():
            # Debounce key-repeat so one physical press = one toggle.
            now = time.time()
            if now - self._rshift_last < 0.3:
                return
            self._rshift_last = now
            self.signal_emitter.settings_hotkey.emit()

        self._keyboard_hook = GlobalKeyboardHook(on_rshift)
        self._keyboard_hook.start()

    def _start_mouse_hook(self):
        if self._hook_active:
            return
        def on_click(x, y):
            # Only forward the click; all heavy work (window detection,
            # OCR, translation) happens in the Qt thread via the signal,
            # so the hook thread is never blocked and the mouse never lags.
            self.signal_emitter.lmb_clicked.emit(x, y)

        self._mouse_hook = GlobalMouseHook(on_click)
        if self._mouse_hook.start():
            self._hook_active = True

    def _stop_mouse_hook(self):
        if self._mouse_hook and self._hook_active:
            self._mouse_hook.stop()
            self._hook_active = False

    def _shutdown(self):
        """Full teardown: unhook mouse+keyboard, drop overlays, hide tray.
        Idempotent; runs on window X (closeEvent -> quit), tray exit and
        any other quit path, so the process always exits with nothing left."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self._stop_mouse_hook()
        except Exception:
            pass
        if self._keyboard_hook:
            try:
                self._keyboard_hook.stop()
            except Exception:
                pass
            self._keyboard_hook = None
        try:
            self.settings_window.overlay_manager.cleanup()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass

    def _quit(self):
        self._shutdown()
        self.app.quit()


if __name__ == "__main__":
    sys.exit(App().run())
