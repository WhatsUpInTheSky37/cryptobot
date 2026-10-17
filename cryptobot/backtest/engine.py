"""Event-by-bar backtest simulator (long-only, one asset, hard budget cap).

Signals are decided on a bar's close and filled at the NEXT bar's open with
slippage and fees. Stops are checked against each bar's low/high. The budget
is a hard cap: profits above it sit in cash unless ``compound`` is set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptobot.strategy import indicators as ind
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import RiskRules


@dataclass(frozen=True)
class Costs:
    fee_pct: float = 0.40  # per side, percent of notional
    slippage_pct: float = 0.05  # percent, adverse on every fill

    @property
    def fee(self) -> float:
        return self.fee_pct / 100.0

    @property
    def slip(self) -> float:
        return self.slippage_pct / 100.0


@dataclass
class Fill:
    ts_ms: int
    side: str  # buy / sell
    price: float
    amount: float
    fee: float
    reason: str


@dataclass
class RoundTrip:
    entry_ts_ms: int
    exit_ts_ms: int
    entry_price: float
    exit_price: float
    max_amount: float
    cost: float  # total spent on buys incl. fees
    proceeds: float  # total received from sells net of fees
    fees: float
    bars_held: int
    exit_reason: str

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def pnl_pct(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0


@dataclass
class SimResult:
    ts_ms: np.ndarray
    equity: np.ndarray
    cash: np.ndarray
    units: np.ndarray
    in_use: np.ndarray | None = None  # cost basis (incl. fees) of the open position: the money at risk
    fills: list[Fill] = field(default_factory=list)
    trades: list[RoundTrip] = field(default_factory=list)
    fees_paid: float = 0.0
    max_in_use: float = 0.0
    start_index: int = 0
    stop_outs: int = 0
    take_profits: int = 0


def simulate(
    bars: BarArrays,
    exposure: np.ndarray,
    budget: float,
    costs: Costs,
    risk: RiskRules,
    start_index: int = 0,
    compound: bool = False,
) -> SimResult:
    n = len(bars)
    o, h, l, c, ts = bars.open, bars.high, bars.low, bars.close, bars.ts_ms
    atr = ind.atr(h, l, c, risk.atr_n) if risk.stop_atr_mult > 0 else np.full(n, np.nan)

    cash = float(budget)
    units = 0.0
    equity = np.full(n, np.nan)
    cash_s = np.full(n, np.nan)
    units_s = np.zeros(n)
    in_use_s = np.zeros(n)
    res = SimResult(ts, equity, cash_s, units_s, in_use_s)
    basis = 0.0

    stop: float | None = None
    tp: float | None = None
    high_water: float = 0.0
    blocked = False  # after a stop/take-profit, wait for the signal to go flat before re-entering
    held_e = 0.0  # exposure fraction currently deployed (reset to 0 when flat)
    rt_open: dict | None = None
    thresh = budget * risk.rebalance_threshold_pct / 100.0

    def buy(i: int, px: float, spend: float, reason: str) -> None:
        """``spend`` is the money to put at risk INCLUDING the fee, so it fits inside the budget."""
        nonlocal cash, units, rt_open, stop, tp, high_water, basis
        fill_px = px * (1 + costs.slip)
        value = min(spend, cash) / (1 + costs.fee)
        if value <= 1e-9:
            return
        amt = value / fill_px
        fee = value * costs.fee
        cash -= value + fee
        units += amt
        basis += value + fee
        res.max_in_use = max(res.max_in_use, basis)
        res.fees_paid += fee
        res.fills.append(Fill(int(ts[i]), "buy", fill_px, amt, fee, reason))
        if rt_open is None:
            rt_open = {"entry_ts": int(ts[i]), "entry_px": fill_px, "cost": 0.0, "proceeds": 0.0, "fees": 0.0, "max_amt": 0.0, "bar": i}
            high_water = fill_px
            a = atr[i - 1] if i > 0 else np.nan
            stop = fill_px - risk.stop_atr_mult * a if risk.stop_atr_mult > 0 and not np.isnan(a) else None
            tp = fill_px * (1 + risk.take_profit_pct / 100.0) if risk.take_profit_pct > 0 else None
        rt_open["cost"] += value + fee
        rt_open["fees"] += fee
        rt_open["max_amt"] = max(rt_open["max_amt"], units)

    def sell(i: int, px: float, amt: float, reason: str) -> None:
        nonlocal cash, units, rt_open, stop, tp, held_e, basis
        fill_px = px * (1 - costs.slip)
        amt = min(amt, units)
        if amt <= 0:
            return
        value = amt * fill_px
        fee = value * costs.fee
        cash += value - fee
        basis *= (units - amt) / units
        units -= amt
        res.fees_paid += fee
        res.fills.append(Fill(int(ts[i]), "sell", fill_px, amt, fee, reason))
        if rt_open is not None:
            rt_open["proceeds"] += value - fee
            rt_open["fees"] += fee
            if units <= 1e-12:
                units = 0.0
                res.trades.append(
                    RoundTrip(
                        rt_open["entry_ts"], int(ts[i]), rt_open["entry_px"], fill_px, rt_open["max_amt"],
                        rt_open["cost"], rt_open["proceeds"], rt_open["fees"], i - rt_open["bar"], reason,
                    )
                )
                rt_open = None
                stop = tp = None
                held_e = 0.0
                basis = 0.0

    for i in range(n):
        if i < start_index:
            continue
        # 1) Execute yesterday's decision at today's open. Trades happen only when the
        #    signal CHANGES; a position that grows through profit is left alone, so the
        #    budget caps what is bought, never forces winners to be trimmed.
        if i > start_index:
            e = exposure[i - 1]
            e = 0.0 if np.isnan(e) else float(e)
            if e <= 0:
                blocked = False
            if not blocked:
                base = budget if not compound else cash + units * o[i]
                d = e - held_e
                if e <= 0 and units > 0:
                    sell(i, o[i], units, "signal exit")
                elif d * base > thresh:
                    buy(i, o[i], d * base, "signal entry" if units == 0 else "signal add")
                    held_e = e
                elif -d * base > thresh and units > 0 and held_e > 0:
                    sell(i, o[i], units * min(1.0, -d / held_e), "signal reduce")
                    held_e = e
        # 2) Protective exits inside the bar.
        if units > 0:
            if stop is not None and l[i] <= stop:
                px = min(o[i], stop)  # gap through the stop fills at the open
                sell(i, px, units, "stop-loss")
                res.stop_outs += 1
                blocked = True
            elif tp is not None and h[i] >= tp:
                px = max(o[i], tp)
                sell(i, px, units, "take-profit")
                res.take_profits += 1
                blocked = True
        # 3) Trail the stop on the bar's high.
        if units > 0 and risk.trailing_pct > 0:
            high_water = max(high_water, h[i])
            trail = high_water * (1 - risk.trailing_pct / 100.0)
            stop = trail if stop is None else max(stop, trail)
        equity[i] = cash + units * c[i]
        cash_s[i] = cash
        units_s[i] = units
        in_use_s[i] = basis
    res.start_index = start_index
    return res
