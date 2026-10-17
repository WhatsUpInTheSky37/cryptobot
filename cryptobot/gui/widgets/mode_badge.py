from __future__ import annotations

from PySide6.QtWidgets import QLabel

from cryptobot.core.models import Mode
from cryptobot.gui.theme import MODE_COLORS


class ModeBadge(QLabel):
    """The always-visible STOPPED / TEST / BUY indicator."""

    def __init__(self, big: bool = False) -> None:
        super().__init__()
        self._big = big
        self.set_mode(Mode.STOPPED)

    def set_mode(self, mode: Mode) -> None:
        color = MODE_COLORS[mode.value]
        size = 28 if self._big else 16
        pad = "6px 16px" if self._big else "3px 10px"
        self.setText(f"●  {mode.value}")
        self.setToolTip(
            {
                Mode.STOPPED: "The bot is not trading.",
                Mode.TEST: "TEST mode: live prices, simulated orders. No real money is used.",
                Mode.BUY: "BUY mode: REAL orders are being sent to the exchange.",
            }[mode]
        )
        self.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: {size}px; padding: {pad};"
            f"border: 2px solid {color}; border-radius: 8px; background: transparent;"
        )
