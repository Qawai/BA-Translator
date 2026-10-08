import os

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QWidget
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon


class DisclaimerDialog(QDialog):
    """Startup screen about the app's imperfections: what to do on an
    error (support branch, what to include in a report) and where the
    logs live. Shown on every launch until the user ticks
    «Больше не показывать»."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dont_show_again = False

        self.setWindowTitle("BA Translator — перед началом")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(
            "QDialog { background:#12121f; }"
            " QLabel { color:#e5e7eb; }"
            " QCheckBox { color:#c4b5fd; }"
        )
        ico = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "app.ico")
        if os.path.isfile(ico):
            self.setWindowIcon(QIcon(ico))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        title = QLabel("Приложение несовершенно и может выдавать ошибки")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        title.setStyleSheet("color:#c4b5fd;")
        title.setWordWrap(True)
        layout.addWidget(title)

        body = QLabel(
            "Если произошло подобное:\n"
            "1. Посмотрите решения в ветке поддержки — возможно, ошибка уже описана.\n"
            "2. Если решения нет — напишите в ветку об ошибке, указав:\n"
            "   • устройство;\n"
            "   • ОС;\n"
            "   • комплектующие;\n"
            "   • логи ошибок — файл run.log рядом с программой.\n\n"
            "Логи пишутся автоматически: run.log (в папке программы)."
        )
        body.setFont(QFont("Segoe UI", 10))
        body.setWordWrap(True)
        body.setTextFormat(Qt.PlainText)
        layout.addWidget(body)

        hint = QLabel("Реквизиты автора будут добавлены позже.")
        hint.setFont(QFont("Segoe UI", 9))
        hint.setStyleSheet("color:#6b7280;")
        layout.addWidget(hint)

        self.chk = QCheckBox("Больше не показывать")
        self.chk.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.chk)

        btn = QPushButton("Понятно")
        btn.setMinimumHeight(34)
        btn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        btn.setStyleSheet(
            "QPushButton { background:#7c3aed; color:#ffffff; border:none;"
            " border-radius:8px; padding:8px 18px; }"
            " QPushButton:hover { background:#8b5cf6; }")
        btn.clicked.connect(self._accept)
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch()
        hl.addWidget(btn)
        layout.addWidget(row)

    def _accept(self):
        self.dont_show_again = self.chk.isChecked()
        self.accept()


def show_disclaimer(config, config_manager, parent=None):
    """Show the dialog when the user still wants it; persists the
    «не показывать» choice. Safe to call on every launch."""
    if not config.get("show_disclaimer", True):
        return
    dlg = DisclaimerDialog(parent)
    dlg.exec_()
    if dlg.dont_show_again:
        config["show_disclaimer"] = False
        config_manager.save_config(config)
