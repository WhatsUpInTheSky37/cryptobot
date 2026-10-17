"""Built-in user guide: section list on the left, Markdown on the right."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QTextBrowser, QVBoxLayout, QWidget

from cryptobot.gui.theme import BORDER, PANEL

GUIDE = Path(__file__).resolve().parent.parent.parent / "help" / "guide.md"


def load_sections(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"^# (.+)$", text, flags=re.MULTILINE)
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1].strip()))
    return out


class HelpPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        try:
            text = GUIDE.read_text(encoding="utf-8")
        except OSError:
            text = "# Help\n\nThe user guide could not be found."
        self.sections = load_sections(text)
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)
        left = QVBoxLayout()
        h = QLabel("Help")
        h.setObjectName("h1")
        left.addWidget(h)
        self.list = QListWidget()
        self.list.setFixedWidth(220)
        self.list.setStyleSheet(f"QListWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }} QListWidget::item {{ padding: 6px; }}")
        for title, _ in self.sections:
            self.list.addItem(title)
        self.list.currentRowChanged.connect(self._show)
        left.addWidget(self.list, 1)
        root.addLayout(left)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet(f"QTextBrowser {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 14px; font-size: 14px; }}")
        root.addWidget(self.view, 1)
        if self.sections:
            self.list.setCurrentRow(0)

    def _show(self, row: int) -> None:
        if 0 <= row < len(self.sections):
            title, body = self.sections[row]
            self.view.setMarkdown(f"# {title}\n\n{body}")

    def show_section(self, title: str) -> None:
        for i, (t, _) in enumerate(self.sections):
            if t.lower() == title.lower():
                self.list.setCurrentRow(i)
                return
