import ctypes
import time
from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import Qt, QTimer, QRect
from PyQt5.QtGui import QPainter, QColor, QFont, QPen, QFontMetrics

user32 = ctypes.windll.user32


class LineOverlay(QWidget):
    PAD = 4

    def __init__(self, text, x, y, w=None, h=None, parent=None):
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
        # Fully transparent to input (clicks reach the game) and opaque
        # text; only the background box is translucent — see paintEvent.

        self._font = QFont("Segoe UI", 15, QFont.Bold)
        self._text = text
        self._x = x
        self._y = y

        # Size tightly to the actual rendered text (not the wide OCR bbox),
        # so the background only sits behind the translation and never
        # covers the whole target window.
        metrics = QFontMetrics(self._font)
        text_w = int(metrics.horizontalAdvance(text))
        text_h = int(metrics.height())
        # Cover at least the ORIGINAL text box (w/h from OCR) so the source
        # line is hidden and the translation sits exactly in its place.
        self._w = max(text_w + self.PAD * 2, w or 0, 8)
        self._h = max(text_h + self.PAD * 2, h or 0, 8)

        self._make_click_through()
        self._update_geometry()
        self._shown_at = time.time()

    def _make_click_through(self):
        try:
            hwnd = int(self.winId())
            ex_style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, ex_style | 0x20 | 0x80000)
        except:
            pass

    def _update_geometry(self):
        screen = self.screen()
        if screen:
            sg = screen.geometry()
            # If the line is wider than the screen, anchor to the left edge
            # instead of pushing it off-screen to the left.
            if self._w <= sg.width():
                if self._x + self._w > sg.right():
                    self._x = sg.right() - self._w
            else:
                self._x = sg.left()
            if self._y + self._h > sg.bottom():
                self._y = sg.bottom() - self._h
            if self._x < sg.left():
                self._x = sg.left()
            if self._y < sg.top():
                self._y = sg.top()

        self.setGeometry(self._x, self._y, self._w, self._h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Semi-transparent dark box: the bright source text underneath is
        # blended down to ~40/255 (imperceptible), while the box itself
        # still reads as translucent over bright game backgrounds. The
        # translation text is painted fully opaque for readability.
        painter.setBrush(QColor(15, 15, 22, 234))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 4, 4)
        rect = QRect(self.PAD, self.PAD, self._w - self.PAD * 2, self._h - self.PAD * 2)
        # Dark outline first (improves contrast on bright text/backgrounds),
        # then the white fill on top.
        painter.setFont(self._font)
        painter.setPen(QPen(QColor(0, 0, 0), 3))
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, self._text)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, self._text)
        painter.end()


class OverlayManager:
    def __init__(self):
        self.overlays = []
        # Seconds before an overlay is automatically cleared on screen
        # (0 = keep forever). The settings window forces 0: the user's
        # rule is that the translation stays until stopped.
        self.ttl = 0
        # Optional callback fired when TTL actually removed something, so
        # the owner knows not to auto-restore an intentionally cleared set.
        self.on_expire = None
        self._cleanup_timer = QTimer()
        self._cleanup_timer.timeout.connect(self._remove_stale)
        self._cleanup_timer.start(1000)

    def show_lines(self, lines_with_translations):
        self.remove_all()
        if not lines_with_translations:
            return

        sorted_lines = sorted(lines_with_translations, key=lambda l: (l['y'], l['x']))

        for l in sorted_lines:
            text = l.get('translation', l['text'])
            if not text or len(text) < 2:
                continue

            # Place the translation centered vertically on the ORIGINAL text
            # line, so it sits ON the source text instead of below it.
            overlay = LineOverlay(
                text=text,
                x=l['x'],
                y=l['y'],
                w=l['w'],
                h=l['h'],
            )
            overlay.show()
            cx = l['x']
            cy = l['y'] + max(0, (l['h'] - overlay._h) // 2)
            overlay.move(cx, cy)
            self.overlays.append(overlay)

    def remove_all(self):
        for o in self.overlays:
            o.close()
            o.deleteLater()
        self.overlays.clear()

    def toggle(self):
        """Hide/show the current translation overlays (Right Shift)."""
        if not self.overlays:
            return
        if any(o.isVisible() for o in self.overlays):
            for o in self.overlays:
                o.hide()
        else:
            for o in self.overlays:
                o.show()

    def cleanup(self):
        self._cleanup_timer.stop()
        self.remove_all()

    def _remove_stale(self):
        now = time.time()
        # Only time-based expiry: hidden overlays stay alive so RShift
        # toggle can show them again until their ttl runs out.
        keep = []
        expired_any = False
        for o in self.overlays:
            expired = self.ttl > 0 and (now - getattr(o, "_shown_at", now)) >= self.ttl
            if expired:
                o.close()
                o.deleteLater()
                expired_any = True
            else:
                keep.append(o)
        self.overlays = keep
        if expired_any and self.on_expire is not None:
            try:
                self.on_expire()
            except Exception:
                pass
