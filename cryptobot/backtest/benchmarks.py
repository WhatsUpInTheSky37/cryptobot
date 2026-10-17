"""Buy-and-hold and DCA benchmark equity curves under the same costs."""
from __future__ import annotations

import numpy as np

from cryptobot.backtest.engine import Costs
from cryptobot.strategy.base import BarArrays


def buy_and_hold(bars: BarArrays, budget: float, costs: Costs, start_index: int) -> np.ndarray:
    n = len(bars)
    eq = np.full(n, np.nan)
    i0 = min(start_index + 1, n - 1)
    px = bars.open[i0] * (1 + costs.slip)
    value = budget / (1 + costs.fee)
    units = value / px
    cash = budget - value * (1 + costs.fee)
    eq[start_index] = budget
    eq[i0:] = cash + units * bars.close[i0:]
    return eq


def dca(bars: BarArrays, budget: float, costs: Costs, start_index: int, every_bars: int) -> np.ndarray:
    """Invest ``budget`` in equal portions every ``every_bars`` across the whole window."""
    n = len(bars)
    eq = np.full(n, np.nan)
    i0 = min(start_index + 1, n - 1)
    buys = list(range(i0, n, max(1, every_bars)))
    portion = budget / len(buys)
    cash, units = budget, 0.0
    buyset = set(buys)
    for i in range(start_index, n):
        if i in buyset:
            value = portion / (1 + costs.fee)
            px = bars.open[i] * (1 + costs.slip)
            units += value / px
            cash -= value * (1 + costs.fee)
        eq[i] = cash + units * bars.close[i]
    return eq
