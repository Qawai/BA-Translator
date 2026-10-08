import os
import sys
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PyQt5.QtCore import pyqtSignal, QObject, Qt


def create_tray_icon():
    ico = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "app.ico")
    if os.path.isfile(ico):
        return QIcon(ico)
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(109, 99, 255))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, 32, 32, 8, 8)
    painter.setPen(QColor(255, 255, 255))
    painter.setFont(QFont("Segoe UI", 16, QFont.Bold))
    painter.drawText(pixmap.rect(), 0x0084, "T")
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QObject):
    show_settings = pyqtSignal()
    toggle_translation = pyqtSignal()
    quit_app = pyqtSignal()

    def __init__(self, settings_window=None):
        super().__init__()
        self.settings_window = settings_window
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(create_tray_icon())
        self.tray.setToolTip("BA Translator — Переводчик Blue Archive")
        self.tray.activated.connect(self._on_activated)

        menu = QMenu()

        toggle_action = QAction("Включить перевод", self)
        toggle_action.triggered.connect(self.toggle_translation.emit)
        menu.addAction(toggle_action)

        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self._show_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.quit_app.emit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)

    def show(self):
        self.tray.show()

    def hide(self):
        self.tray.hide()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._show_settings()

    def _show_settings(self):
        if self.settings_window:
            self.settings_window.toggle_visibility()
        self.show_settings.emit()

    def showMessage(self, title, msg):
        self.tray.showMessage(title, msg)
