"""Simulated (TEST) account: cash plus per-coin positions, with cost-basis accounting."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from cryptobot.engine.executors import Fill


@dataclass
class Position:
    symbol: str
    units: float = 0.0
    basis: float = 0.0  # money at risk: cost of the units held, fees included
    entry_price: float | None = None
    entry_ts_ms: int | None = None
    stop: float | None = None
    take_profit: float | None = None
    high_water: float = 0.0
    held_e: float = 0.0  # exposure fraction currently deployed
    blocked: bool = False  # after a stop/take-profit, wait for the signal to go flat
    position_id: int | None = None
    last_price: float | None = None
    atr: float | None = None
    last_decided_ts_ms: int | None = None

    @property
    def open(self) -> bool:
        return self.units > 0

    def value(self) -> float:
        return self.units * (self.last_price or self.entry_price or 0.0)


@dataclass
class Ledger:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    peak: float = 0.0
    day_key: str | None = None
    day_start_equity: float = 0.0
    realized_pnl: float = 0.0

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol))

    def in_use(self) -> float:
        return sum(p.basis for p in self.positions.values())

    def market_value(self) -> float:
        return sum(p.value() for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + self.market_value()

    def apply_fill(self, fill: Fill) -> float | None:
        """Update cash/units/basis. Returns realised P&L for sells, None for buys."""
        p = self.position(fill.symbol)
        if fill.side == "buy":
            cost = fill.notional + fill.fee
            self.cash -= cost
            if not p.open:
                p.entry_price = fill.price
                p.entry_ts_ms = fill.ts_ms
                p.high_water = fill.price
            p.units += fill.amount
            p.basis += cost
            p.last_price = fill.price
            return None
        amt = min(fill.amount, p.units)
        if amt <= 0:
            return 0.0
        frac = amt / p.units
        cost_portion = p.basis * frac
        proceeds = fill.notional - fill.fee
        pnl = proceeds - cost_portion
        self.cash += proceeds
        p.units -= amt
        p.basis -= cost_portion
        p.last_price = fill.price
        self.realized_pnl += pnl
        if p.units <= 1e-12:
            p.units = 0.0
            p.basis = 0.0
            p.entry_price = None
            p.entry_ts_ms = None
            p.stop = None
            p.take_profit = None
            p.high_water = 0.0
            p.held_e = 0.0
            p.position_id = None
        return pnl

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "peak": self.peak,
            "day_key": self.day_key,
            "day_start_equity": self.day_start_equity,
            "realized_pnl": self.realized_pnl,
            "positions": {s: asdict(p) for s, p in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ledger":
        led = cls(float(d["cash"]), {}, float(d.get("peak", 0.0)), d.get("day_key"), float(d.get("day_start_equity", 0.0)), float(d.get("realized_pnl", 0.0)))
        for s, pd in (d.get("positions") or {}).items():
            led.positions[s] = Position(**pd)
        return led
