import logging

from PyQt5.QtWidgets import QWidget, QGraphicsOpacityEffect
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QPropertyAnimation, QRect, QEasingCurve
from PyQt5.QtGui import QPainter, QColor, QFont, QPen, QFontMetrics

logger = logging.getLogger("BA_Translator")

user32 = None
try:
    import ctypes
    user32 = ctypes.windll.user32
except Exception:
    pass


class SubtitleBox(QWidget):
    """Film-style hint subtitle: dark rounded card pinned bottom-centre,
    fully click-through and never takes focus (the game keeps receiving
    input while the tutorial plays)."""

    PAD_X = 22
    PAD_Y = 14

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.WindowDoesNotAcceptFocus |
            Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._text_font = QFont("Segoe UI", 14, QFont.Bold)
        self._counter_font = QFont("Segoe UI", 9)
        self._text = ""
        self._counter = ""
        self._w = 400
        self._h = 80

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(220)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

        self._make_click_through()

    def _make_click_through(self):
        try:
            hwnd = int(self.winId())
            ex = user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
            # WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            user32.SetWindowLongW(hwnd, -20, ex | 0x20 | 0x80000)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _wrap_lines(self, text, max_w):
        fm = QFontMetrics(self._text_font)
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if fm.horizontalAdvance(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def set_step(self, text, counter):
        self._text = text
        self._counter = counter
        self._relayout()
        # A fade-out started by stop()/fade_out_then_hide() may still be
        # wired to _on_faded_out. If that connection survives into the
        # fade-IN below, its finished signal fires when the card fully
        # appears — and _on_faded_out hides it instantly (hints flicker
        # and vanish on every restart). Always unwire before fading in.
        try:
            self._fade.finished.disconnect(self._on_faded_out)
        except TypeError:
            pass
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def fade_out_then_hide(self):
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        try:
            self._fade.finished.disconnect(self._on_faded_out)
        except TypeError:
            pass
        self._fade.finished.connect(self._on_faded_out)
        self._fade.start()

    def _on_faded_out(self):
        self.hide()

    def _relayout(self):
        screen = self.screen()
        sg = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
        max_w = min(920, max(320, sg.width() - 80))
        lines = self._wrap_lines(self._text, max_w - self.PAD_X * 2)
        fm = QFontMetrics(self._text_font)
        cf = QFontMetrics(self._counter_font)
        text_h = max(1, len(lines)) * (fm.height() + 4)
        self._h = self.PAD_Y * 2 + cf.height() + 6 + text_h + 8
        self._w = max_w
        x = sg.left() + (sg.width() - self._w) // 2
        y = sg.bottom() - self._h - 64   # above the taskbar / dialogue box
        self.setGeometry(x, y, self._w, self._h)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 10, 18, 228))
        p.drawRoundedRect(self.rect(), 10, 10)
        # accent strip on the left
        p.setBrush(QColor(124, 58, 237, 220))
        p.drawRoundedRect(QRect(0, 0, 5, self._h), 3, 3)

        inner = QRect(self.PAD_X, self.PAD_Y,
                      self._w - self.PAD_X * 2, self._h - self.PAD_Y * 2)
        p.setFont(self._counter_font)
        p.setPen(QColor(196, 181, 253))
        counter_rect = QRect(inner.x(), inner.y(), inner.width(),
                             QFontMetrics(self._counter_font).height())
        p.drawText(counter_rect, Qt.AlignLeft | Qt.AlignVCenter, self._counter)

        text_rect = QRect(inner.x(), counter_rect.bottom() + 4,
                          inner.width(), inner.bottom() - counter_rect.bottom() - 4)
        p.setFont(self._text_font)
        p.setPen(QPen(QColor(0, 0, 0), 2))          # outline for contrast
        p.drawText(text_rect, Qt.AlignCenter | Qt.TextWordWrap, self._text)
        p.setPen(QColor(255, 255, 255))
        p.drawText(text_rect, Qt.AlignCenter | Qt.TextWordWrap, self._text)
        p.end()


class SubtitleTimeline(QObject):
    """Sequenced hint subtitles: [(text, seconds), ...]. Plays once through,
    then emits finished. Click-through, does not touch translation state."""
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._steps = []
        self._idx = -1
        self._box = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)

    @property
    def active(self):
        return self._idx >= 0

    def start(self, steps):
        if not steps:
            return
        self.stop()
        self._steps = list(steps)
        self._idx = 0
        if self._box is None:
            self._box = SubtitleBox()
        logger.info("Subtitle timeline started (%d steps)", len(self._steps))
        self._show_current()

    def stop(self):
        was_active = self._idx >= 0
        self._timer.stop()
        self._idx = -1
        if self._box is not None:
            if self._box.isVisible():
                self._box.fade_out_then_hide()
        if was_active:
            logger.info("Subtitle timeline stopped")

    def _show_current(self):
        text, secs = self._steps[self._idx]
        counter = f"Подсказка {self._idx + 1} / {len(self._steps)}"
        self._box.set_step(text, counter)
        self._timer.start(max(200, int(secs * 1000)))

    def _advance(self):
        self._idx += 1
        if self._idx >= len(self._steps):
            self._idx = -1
            if self._box is not None:
                self._box.fade_out_then_hide()
            logger.info("Subtitle timeline finished")
            self.finished.emit()
            return
        self._show_current()
