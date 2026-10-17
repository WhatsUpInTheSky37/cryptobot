"""Plain data types shared by the engine, the database, and the GUI.

Everything here is picklable so it can cross the process boundary between the
trading engine process and the GUI process.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    STOPPED = "STOPPED"
    TEST = "TEST"
    BUY = "BUY"


@dataclass(frozen=True, slots=True)
class Tick:
    """A single public trade on the exchange (anyone's trade, not ours)."""

    symbol: str
    ts_ms: int
    price: float
    amount: float
    trade_id: str | None = None
    side: str | None = None  # taker side: "buy" = aggressive buyer, "sell" = aggressive seller


@dataclass(slots=True)
class Bar:
    """One OHLCV candle. ``ts_ms`` is the bar's open time, aligned to the timeframe."""

    symbol: str
    timeframe_s: int
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    trades: int = 0
    closed: bool = False

    @property
    def end_ms(self) -> int:
        return self.ts_ms + self.timeframe_s * 1000

    def apply(self, price: float, amount: float) -> None:
        """Fold one trade into this bar."""
        if self.trades == 0:
            # A bar that was created as a flat placeholder adopts the first real price.
            self.open = self.high = self.low = self.close = price
        else:
            if price > self.high:
                self.high = price
            if price < self.low:
                self.low = price
            self.close = price
        self.volume += amount
        self.trades += 1
