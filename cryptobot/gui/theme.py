"""Dark theme: colour tokens and the Qt stylesheet."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0f1115"
PANEL = "#171a21"
PANEL2 = "#1f2330"
BORDER = "#2a2f3a"
TEXT = "#e5e7eb"
MUTED = "#9ca3af"
BLUE = "#3b82f6"
RED = "#ef4444"
GREEN = "#22c55e"
GREY = "#6b7280"
AMBER = "#f59e0b"

MODE_COLORS = {"STOPPED": GREY, "TEST": BLUE, "BUY": RED}

QSS = f"""
* {{ font-family: "Helvetica Neue", "Segoe UI", "Helvetica Neue", Arial, sans-serif; font-size: 13px; }}
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}
QLabel {{ background: transparent; }}
QFrame#navRail {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QFrame#topBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 12px; color: {TEXT}; }}
QPushButton:hover {{ border-color: {BLUE}; }}
QPushButton:disabled {{ color: {GREY}; }}
QPushButton#nav {{ background: transparent; border: none; border-radius: 8px; padding: 10px 6px; color: {MUTED}; text-align: center; }}
QPushButton#nav:hover {{ background: {PANEL2}; color: {TEXT}; }}
QPushButton#nav:checked {{ background: {PANEL2}; color: {TEXT}; border-left: 3px solid {BLUE}; }}
QPushButton#tf {{ padding: 4px 10px; }}
QPushButton#tf:checked {{ background: {BLUE}; border-color: {BLUE}; color: white; }}
QComboBox {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; min-width: 120px; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; selection-background-color: {BLUE}; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#h1 {{ font-size: 22px; font-weight: 600; }}
QLabel#price {{ font-size: 28px; font-weight: 600; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QPlainTextEdit {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; font-family: Menlo, Consolas, monospace; font-size: 12px; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL2))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(PANEL2))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(BLUE))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(PANEL2))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
    pg.setConfigOptions(background=BG, foreground=MUTED, antialias=True)
