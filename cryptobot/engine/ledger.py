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
    last_tick_ms: int = 0
    realized_pnl: float = 0.0
    exit_pending: str = ""
    schedule_started_ms: int | None = None
    schedule_purchases: int = 0
    protection_client_id: str | None = None
    protected_stop: float | None = None

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
    external_fees: float = 0.0

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol))

    def in_use(self) -> float:
        return sum(p.basis for p in self.positions.values())

    def market_value(self) -> float:
        return sum(p.value() for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + self.market_value() - self.external_fees

    def apply_fill(self, fill: Fill) -> float | None:
        """Update cash/units/basis. Returns realised P&L for sells, None for buys."""
        p = self.position(fill.symbol)
        if fill.side == "buy":
            net_units = fill.amount - fill.base_fee
            if net_units <= 0:
                raise ValueError("commission consumed the purchased quantity")
            cost = fill.notional + fill.cash_fee + fill.external_fee_value
            self.cash -= fill.notional + fill.cash_fee
            self.external_fees += fill.external_fee_value
            if not p.open:
                p.entry_price = fill.price
                p.entry_ts_ms = fill.ts_ms
                p.high_water = fill.price
                p.realized_pnl = 0.0
            else:
                p.entry_price = ((p.entry_price or fill.price) * p.units + fill.price * net_units) / (p.units + net_units)
            p.units += net_units
            p.basis += cost
            p.last_price = fill.price
            return None
        amt = fill.amount + fill.base_fee
        if amt > p.units + max(1e-12, p.units * 1e-9):
            raise ValueError("sell execution exceeds recorded inventory; reconciliation required")
        amt = min(amt, p.units)
        if amt <= 0:
            return 0.0
        frac = amt / p.units
        cost_portion = p.basis * frac
        proceeds = fill.notional - fill.cash_fee
        pnl = proceeds - fill.external_fee_value - cost_portion
        self.cash += proceeds
        self.external_fees += fill.external_fee_value
        p.units -= amt
        p.basis -= cost_portion
        p.last_price = fill.price
        self.realized_pnl += pnl
        p.realized_pnl += pnl
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
            "external_fees": self.external_fees,
            "positions": {s: asdict(p) for s, p in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ledger":
        led = cls(float(d["cash"]), {}, float(d.get("peak", 0.0)), d.get("day_key"), float(d.get("day_start_equity", 0.0)), float(d.get("realized_pnl", 0.0)))
        led.external_fees = float(d.get("external_fees", 0.0))
        for s, pd in (d.get("positions") or {}).items():
            led.positions[s] = Position(**pd)
        return led

