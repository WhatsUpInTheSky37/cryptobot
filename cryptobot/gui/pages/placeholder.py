from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    def __init__(self, title: str, milestone: str, blurb: str) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        h = QLabel(title)
        h.setObjectName("h1")
        lay.addWidget(h)
        m = QLabel(f"Coming in {milestone}.")
        m.setObjectName("muted")
        lay.addWidget(m)
        b = QLabel(blurb)
        b.setWordWrap(True)
        b.setObjectName("muted")
        lay.addWidget(b)
        lay.addStretch(1)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
