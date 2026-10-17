"""TEST → BUY confirmation: shows TEST results, lists anything blocking, requires typing BUY."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QMessageBox, QVBoxLayout

from cryptobot.engine.modes import TestStats
from cryptobot.gui.theme import AMBER, GREEN, RED


class ModeSwitchDialog(QDialog):
    def __init__(self, exchange_name: str, stats: TestStats, reasons: list[str], budget: float, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Switch to BUY mode")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        head = QLabel(f"<span style='color:{RED}; font-size:18px; font-weight:700'>BUY mode sends REAL orders to {exchange_name}.</span>")
        lay.addWidget(head)
        pnl_color = GREEN if stats.total_pnl >= 0 else RED
        lay.addWidget(
            QLabel(
                "<b>Your TEST results so far</b><br>"
                f"Days in TEST: {stats.days_in_test:.1f} · Round-trip trades: {stats.trades} · Win rate: {stats.win_rate:.0%}<br>"
                f"Total P&L: <span style='color:{pnl_color}'>${stats.total_pnl:+,.2f}</span> · Fees: ${stats.fees:,.2f} · "
                f"Worst drawdown: {stats.max_drawdown:.1%} · Equity: ${stats.equity:,.2f}<br>"
                f"Budget that will be used for real: <b>${budget:,.2f}</b>"
            )
        )
        if stats.trades < 20 or stats.total_pnl <= 0:
            warn = QLabel(
                f"<span style='color:{AMBER}'>Honest note: "
                + ("fewer than 20 trades is not enough evidence of an edge. " if stats.trades < 20 else "")
                + ("TEST mode has not made money. " if stats.total_pnl <= 0 else "")
                + "Switching is your decision, but nothing here suggests BUY mode will do better than TEST did.</span>"
            )
            warn.setWordWrap(True)
            lay.addWidget(warn)
        self.ok_allowed = not reasons
        if reasons:
            r = QLabel("<b>BUY mode is locked:</b><br>• " + "<br>• ".join(reasons))
            r.setWordWrap(True)
            r.setStyleSheet(f"color: {RED};")
            lay.addWidget(r)
        else:
            lay.addWidget(QLabel("Type <b>BUY</b> to confirm:"))
            self.confirm = QLineEdit()
            self.confirm.setPlaceholderText("BUY")
            self.confirm.textChanged.connect(self._check)
            lay.addWidget(self.confirm)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Switch to BUY")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def _check(self, text: str) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.ok_allowed and text.strip() == "BUY")


def ask_back_to_test(parent, open_positions: int) -> str | None:
    """Returns 'close', 'keep', or None (cancelled)."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Switch to TEST mode")
    box.setText(f"Switch back to TEST mode now? You have {open_positions} open REAL position(s).")
    box.setInformativeText("Keep: they stay on the exchange and the app stops managing them (no stop-loss!). Close: sell them now at market.")
    close = box.addButton("Close them now", QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton("Keep them (unmanaged)", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() is close:
        return "close"
    if box.clickedButton() is keep:
        return "keep"
    return None
