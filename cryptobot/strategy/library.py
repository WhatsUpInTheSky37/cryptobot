"""The eight strategies from the plan. See PLAN.md section 2 for the evidence."""
from __future__ import annotations

import numpy as np

from cryptobot.strategy import indicators as ind
from cryptobot.strategy.base import BarArrays, Param, Strategy


class EmaCross(Strategy):
    id = "ema_cross"
    name = "EMA crossover (trend)"
    kind = "trend"
    description = (
        "Invested while the fast EMA is above the slow EMA, in cash otherwise. The best-evidenced "
        "family (time-series momentum). Expect to lag buy-and-hold in strong bull runs; the payoff is "
        "smaller drawdowns. Whipsaws in sideways markets."
    )
    params = (
        Param("fast", "Fast EMA", 20, 3, 100, 1, "Bars in the fast average. Lower = reacts sooner but more false signals."),
        Param("slow", "Slow EMA", 50, 10, 400, 1, "Bars in the slow average. Must be larger than the fast one."),
    )
    grid = tuple({"fast": f, "slow": s} for f, s in [(10, 30), (10, 50), (20, 50), (20, 100), (50, 200)])

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        fast, slow = int(p["fast"]), int(p["slow"])
        if fast >= slow:
            slow = fast + 1
        f, s = ind.ema(bars.close, fast), ind.ema(bars.close, slow)
        out = np.where(f > s, 1.0, 0.0)
        out[np.isnan(s)] = np.nan
        return out


class MacdStrategy(Strategy):
    id = "macd"
    name = "MACD (trend)"
    kind = "trend"
    description = (
        "Invested while the MACD line is above its signal line. A variant of EMA crossover with more "
        "lag; there is little evidence it adds anything beyond it."
    )
    params = (
        Param("fast", "Fast EMA", 12, 3, 50, 1, "Fast EMA length."),
        Param("slow", "Slow EMA", 26, 10, 200, 1, "Slow EMA length."),
        Param("signal", "Signal", 9, 2, 50, 1, "Smoothing of the MACD line."),
    )
    grid = tuple({"fast": f, "slow": s, "signal": g} for f, s, g in [(12, 26, 9), (8, 21, 5), (19, 39, 9), (5, 35, 5)])

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        line, sig, _ = ind.macd(bars.close, int(p["fast"]), int(p["slow"]), int(p["signal"]))
        out = np.where(line > sig, 1.0, 0.0)
        out[np.isnan(sig)] = np.nan
        return out


class RsiReversion(Strategy):
    id = "rsi_reversion"
    name = "RSI mean reversion"
    kind = "reversion"
    description = (
        "Buys when RSI drops below the oversold level, sells when it rises above the overbought level. "
        "Works in ranges; catastrophic in crashes (keeps buying a falling market). Use with the regime "
        "gate and a hard stop."
    )
    params = (
        Param("n", "RSI length", 14, 2, 50, 1, "Bars in the RSI calculation."),
        Param("buy_below", "Buy below", 30, 5, 50, 1, "Enter when RSI is below this."),
        Param("sell_above", "Sell above", 70, 50, 95, 1, "Exit when RSI is above this."),
    )
    grid = tuple({"n": n, "buy_below": b, "sell_above": s} for n, b, s in [(14, 30, 70), (2, 10, 90), (7, 25, 75), (14, 35, 65)])

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        r = ind.rsi(bars.close, int(p["n"]))
        out = np.full(len(r), np.nan)
        state = 0.0
        for i in range(len(r)):
            if np.isnan(r[i]):
                continue
            if r[i] < p["buy_below"]:
                state = 1.0
            elif r[i] > p["sell_above"]:
                state = 0.0
            out[i] = state
        return out


class BollingerReversion(Strategy):
    id = "bollinger_reversion"
    name = "Bollinger Bands mean reversion"
    kind = "reversion"
    description = (
        "Buys when price closes below the lower band, sells when it returns to the middle band. Same "
        "strengths and dangers as RSI mean reversion: 'band walking' in a downtrend keeps you long."
    )
    params = (
        Param("n", "Length", 20, 5, 100, 1, "Bars in the moving average and standard deviation."),
        Param("k", "Width (std devs)", 2.0, 0.5, 4.0, 0.1, "How many standard deviations the bands sit from the middle.", integer=False),
    )
    grid = tuple({"n": n, "k": k} for n, k in [(20, 2.0), (20, 2.5), (10, 1.5), (50, 2.0)])

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        lower, mid, _ = ind.bollinger(bars.close, int(p["n"]), float(p["k"]))
        c = bars.close
        out = np.full(len(c), np.nan)
        state = 0.0
        for i in range(len(c)):
            if np.isnan(mid[i]):
                continue
            if c[i] < lower[i]:
                state = 1.0
            elif c[i] >= mid[i]:
                state = 0.0
            out[i] = state
        return out


class DonchianBreakout(Strategy):
    id = "donchian_breakout"
    name = "Donchian breakout + volume (trend)"
    kind = "trend"
    description = (
        "Buys when price closes above the previous N-bar high on above-average volume; sells when it "
        "closes below the previous M-bar low (the Turtle rule). Most breakouts fail; the few big trends "
        "must pay for them. Bleeds in long ranges."
    )
    params = (
        Param("entry", "Entry lookback", 20, 5, 200, 1, "Buy on a close above the high of this many previous bars."),
        Param("exit", "Exit lookback", 10, 3, 100, 1, "Sell on a close below the low of this many previous bars."),
        Param("vol_mult", "Volume filter", 1.5, 0.0, 4.0, 0.1, "Require volume above this multiple of its 20-bar average (0 = no filter).", integer=False, suffix="x"),
    )
    grid = tuple({"entry": e, "exit": x, "vol_mult": v} for e, x, v in [(20, 10, 1.5), (55, 20, 1.0), (10, 5, 1.5), (20, 10, 0.0)])

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        hi, _ = ind.donchian(bars.high, bars.low, int(p["entry"]))
        _, lo = ind.donchian(bars.high, bars.low, int(p["exit"]))
        vavg = ind.shift(ind.sma(bars.volume, 20), 1)
        c = bars.close
        out = np.full(len(c), np.nan)
        state = 0.0
        for i in range(len(c)):
            if np.isnan(hi[i]) or np.isnan(lo[i]) or np.isnan(vavg[i]):
                continue
            vol_ok = p["vol_mult"] <= 0 or bars.volume[i] >= p["vol_mult"] * vavg[i]
            if state == 0.0 and c[i] > hi[i] and vol_ok:
                state = 1.0
            elif state == 1.0 and c[i] < lo[i]:
                state = 0.0
            out[i] = state
        return out


class GridLadder(Strategy):
    id = "grid"
    name = "Grid (ladder) trading"
    kind = "reversion"
    description = (
        "Holds more as price falls through a ladder of levels below a moving anchor and less as it "
        "rises, profiting from oscillation. This is a short-volatility bet: steady small gains in a "
        "range, large loss in a crash. A floor below the ladder sells everything (the mandatory stop). "
        "HIGHEST TAIL RISK of the strategies here."
    )
    params = (
        Param("anchor", "Anchor length", 50, 10, 300, 1, "Bars in the moving average the ladder is centred on."),
        Param("range_pct", "Ladder range", 20, 4, 60, 1, "Total height of the ladder as % of the anchor price.", suffix="%"),
        Param("levels", "Levels", 10, 2, 40, 1, "Number of rungs between fully in cash (top) and fully invested (bottom)."),
        Param("floor_pct", "Floor stop", 10, 2, 40, 1, "Sell everything if price falls this % below the ladder's bottom.", suffix="%"),
    )
    grid = tuple({"anchor": a, "range_pct": r, "levels": 10, "floor_pct": f} for a, r, f in [(50, 20, 10), (100, 30, 10), (30, 15, 5)])

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        anchor = ind.shift(ind.sma(bars.close, int(p["anchor"])), 1)
        half = float(p["range_pct"]) / 200.0
        upper, lower = anchor * (1 + half), anchor * (1 - half)
        floor = lower * (1 - float(p["floor_pct"]) / 100.0)
        levels = max(1, int(p["levels"]))
        with np.errstate(invalid="ignore", divide="ignore"):
            frac = np.clip((upper - bars.close) / (upper - lower), 0.0, 1.0)
        out = np.round(frac * levels) / levels
        out = np.where(bars.close < floor, 0.0, out)
        out[np.isnan(anchor)] = np.nan
        return out


class Dca(Strategy):
    id = "dca"
    name = "DCA (dollar-cost averaging) baseline"
    kind = "schedule"
    description = (
        "Not a timing strategy: invests the budget in equal portions on a fixed schedule from the start "
        "of the series, then holds. The honest baseline every other strategy is compared with."
    )
    params = (
        Param("interval", "Buy every", 7, 1, 90, 1, "Bars between purchases (7 = weekly on daily bars)."),
        Param("purchases", "Number of purchases", 12, 1, 104, 1, "Budget is split into this many equal purchases."),
    )
    grid = ({"interval": 7, "purchases": 12},)

    def compute(self, bars: BarArrays, p: dict[str, float]) -> np.ndarray:
        n = len(bars)
        i = np.arange(n)
        bought = np.minimum(i // max(1, int(p["interval"])) + 1, int(p["purchases"]))
        return bought / float(max(1, int(p["purchases"])))


STRATEGIES: dict[str, type[Strategy]] = {
    s.id: s for s in (EmaCross, MacdStrategy, RsiReversion, BollingerReversion, DonchianBreakout, GridLadder, Dca)
}
