"""System tray icon: shows the mode colour, pops up notifications, keeps the bot alive when the window is hidden."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from cryptobot.core.models import Mode
from cryptobot.gui.theme import MODE_COLORS


def mode_icon(mode: Mode, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(MODE_COLORS[mode.value]))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(6, 6, size - 12, size - 12)
    p.setBrush(QColor("#0f1115"))
    p.drawEllipse(size // 2 - 8, size // 2 - 8, 16, 16)
    p.end()
    return QIcon(pm)


class Tray(QSystemTrayIcon):
    def __init__(self, show_window, quit_app, parent=None) -> None:
        super().__init__(mode_icon(Mode.STOPPED), parent)
        self.enabled_popups = True
        menu = QMenu()
        self.status_action = QAction("Stopped", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        show = QAction("Show CryptoBot", menu)
        show.triggered.connect(show_window)
        menu.addAction(show)
        quit_ = QAction("Quit CryptoBot (stops the bot)", menu)
        quit_.triggered.connect(quit_app)
        menu.addAction(quit_)
        self.setContextMenu(menu)
        self.setToolTip("CryptoBot — stopped")
        self.activated.connect(lambda reason: show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    def set_mode(self, mode: Mode, text: str = "") -> None:
        self.setIcon(mode_icon(mode))
        label = f"{mode.value}" + (f" · {text}" if text else "")
        self.status_action.setText(label)
        self.setToolTip(f"CryptoBot — {label}")

    def notify(self, title: str, message: str, bad: bool = False) -> None:
        if not self.enabled_popups or not self.isVisible():
            return
        icon = QSystemTrayIcon.MessageIcon.Warning if bad else QSystemTrayIcon.MessageIcon.Information
        self.showMessage(title, message, icon, 6000)
