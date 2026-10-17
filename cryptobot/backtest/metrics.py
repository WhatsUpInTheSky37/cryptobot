from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from cryptobot.backtest.engine import RoundTrip


@dataclass
class Metrics:
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    volatility: float
    win_rate: float
    profit_factor: float
    trades: int
    avg_trade_pct: float
    exposure_time: float
    fees_paid: float
    final_equity: float
    days: float

    def as_dict(self) -> dict:
        return asdict(self)


def max_drawdown(eq: np.ndarray) -> float:
    eq = eq[~np.isnan(eq)]
    if len(eq) == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, 1.0 - eq / peak, 0.0)
    return float(np.nanmax(dd)) if len(dd) else 0.0


def compute_metrics(
    equity: np.ndarray,
    budget: float,
    periods_per_year: float,
    trades: list[RoundTrip] | None = None,
    units: np.ndarray | None = None,
    fees_paid: float = 0.0,
) -> Metrics:
    valid = ~np.isnan(equity)
    eq = equity[valid]
    n = len(eq)
    if n < 2:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, fees_paid, budget, 0)
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    years = (n - 1) / periods_per_year
    total = eq[-1] / budget - 1.0
    cagr = (eq[-1] / budget) ** (1 / years) - 1.0 if years > 0 and eq[-1] > 0 else 0.0
    vol = float(np.std(rets, ddof=1) * np.sqrt(periods_per_year)) if len(rets) > 1 else 0.0
    sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(periods_per_year)) if len(rets) > 1 and np.std(rets, ddof=1) > 0 else 0.0
    downside = rets[rets < 0]
    sortino = float(np.mean(rets) / np.std(downside, ddof=1) * np.sqrt(periods_per_year)) if len(downside) > 1 and np.std(downside, ddof=1) > 0 else 0.0
    trades = trades or []
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gp = sum(t.pnl for t in wins)
    gl = -sum(t.pnl for t in losses)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    wr = len(wins) / len(trades) if trades else 0.0
    avg = float(np.mean([t.pnl_pct for t in trades])) if trades else 0.0
    expo = float(np.mean(units[valid] > 0)) if units is not None else 0.0
    return Metrics(
        float(total), float(cagr), max_drawdown(eq), sharpe, sortino, vol, wr, pf, len(trades), avg, expo,
        fees_paid, float(eq[-1]), years * 365.0,
    )
