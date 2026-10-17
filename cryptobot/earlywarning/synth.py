"""Synthetic market data for tests and demos: random-walk 1-minute candles with injected
accumulation-then-surge episodes. Never a substitute for exchange data in the research report."""
from __future__ import annotations

import numpy as np

from cryptobot.earlywarning.store import klines_array, trades_array


def synthetic_klines(days: int = 30, *, seed: int = 1, price: float = 1.0, surges: int = 6, base_vol: float = 50.0, start_ms: int = 1_750_000_000_000, accumulation_min: int = 90, surge_min: int = 20, surge_pct: float = 16.0):
    """(klines structured array, list of injected surge start indices)."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    ret = rng.normal(0, 0.0008, n)
    vol = base_vol * rng.lognormal(0, 0.5, n)
    buy_share = np.clip(rng.normal(0.5, 0.08, n), 0.05, 0.95)
    trades = np.maximum(1, rng.poisson(8, n))
    starts = sorted(rng.choice(np.arange(2000, n - 2000), size=surges, replace=False))
    for s in starts:
        a0 = s - accumulation_min
        # accumulation: volume up, buying dominant, price flat (returns damped)
        vol[a0:s] *= rng.uniform(1.8, 3.0)
        buy_share[a0:s] = np.clip(rng.normal(0.66, 0.05, s - a0), 0.5, 0.9)
        trades[a0:s] = (trades[a0:s] * 2.5).astype(int)
        ret[a0:s] *= 0.4
        # surge: strong up-move over surge_min minutes, then partial give-back
        ramp = np.full(surge_min, np.log1p(surge_pct / 100.0) / surge_min)
        ret[s:s + surge_min] = ramp + rng.normal(0, 0.001, surge_min)
        vol[s:s + surge_min] *= 6
        buy_share[s:s + surge_min] = 0.7
        ret[s + surge_min:s + surge_min + 60] -= np.log1p(surge_pct / 100.0) * 0.5 / 60
    close = price * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[price], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0015, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    ts = start_ms + np.arange(n) * 60_000
    qv = vol
    rows = [(int(ts[i]), float(open_[i]), float(high[i]), float(low[i]), float(close[i]), float(qv[i] / close[i]), float(qv[i]), int(trades[i]), float(qv[i] * buy_share[i] / close[i]), float(qv[i] * buy_share[i])) for i in range(n)]
    return klines_array(rows), [int(s) for s in starts]


def synthetic_aggtrades(k, *, seed: int = 2, per_bar: int = 6):
    """Trades consistent with the candles: taker side split by the bar's taker-buy share."""
    rng = np.random.default_rng(seed)
    rows = []
    agg = 0
    for b in k:
        q = float(b["quote_volume"])
        if q <= 0:
            continue
        share = float(b["taker_buy_quote"]) / q
        sizes = rng.dirichlet(np.ones(per_bar)) * q
        for j in range(per_bar):
            t = int(b["ts"]) + int(j * 60_000 / per_bar)
            px = float(b["low"]) + (float(b["high"]) - float(b["low"])) * rng.uniform()
            buyer_maker = 0 if rng.uniform() < share else 1
            rows.append((agg, t, px, sizes[j] / px, buyer_maker))
            agg += 1
    return trades_array([(ts, p, q, m) for _a, ts, p, q, m in rows]), [(a, ts, p, q, m) for a, ts, p, q, m in rows]
