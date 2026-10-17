"""Raising the budget: show performance, type the new amount twice, cap the step."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout

MAX_STEP = 0.50  # an increase may not exceed +50% at a time


class BudgetDialog(QDialog):
    def __init__(self, current: float, stats: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change trading budget")
        self.current = current
        self.value: float | None = None
        lay = QVBoxLayout(self)
        intro = QLabel(
            f"<b>Current budget: ${current:,.2f}</b><br>"
            f"TEST equity ${stats.get('equity', current):,.2f} · realised P&L ${stats.get('realized', 0):+,.2f} · "
            f"{stats.get('trades', 0)} TEST trades · worst drawdown {stats.get('drawdown', 0):.1%}<br><br>"
            f"Increases are limited to +{MAX_STEP:.0%} at a time (max ${current * (1 + MAX_STEP):,.2f}). "
            "Decreases are unrestricted. Type the new amount twice."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        self.a = QDoubleSpinBox()
        self.b = QDoubleSpinBox()
        for w in (self.a, self.b):
            w.setRange(10, 1_000_000)
            w.setPrefix("$")
            w.setValue(current)
        form.addRow("New budget", self.a)
        form.addRow("Type it again", self.b)
        lay.addLayout(form)
        self.msg = QLabel("")
        self.msg.setStyleSheet("color: #ef4444;")
        lay.addWidget(self.msg)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self) -> None:
        a, b = self.a.value(), self.b.value()
        if abs(a - b) > 1e-9:
            self.msg.setText("The two amounts do not match.")
            return
        if a > self.current * (1 + MAX_STEP) + 1e-9:
            self.msg.setText(f"Too big a step. Maximum this time is ${self.current * (1 + MAX_STEP):,.2f}.")
            return
        self.value = a
        self.accept()
