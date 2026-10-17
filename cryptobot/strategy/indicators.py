"""Technical indicators as causal numpy functions.

Every function returns an array the same length as its input, with NaN while
the indicator is warming up. Value ``i`` depends only on inputs ``<= i``; the
no-look-ahead test in ``tests/test_strategies.py`` enforces this for every
strategy built on them.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def _nan(n: int) -> np.ndarray:
    return np.full(n, np.nan)


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.concatenate(([0.0], x)))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def _first_valid(x: np.ndarray) -> int:
    idx = np.flatnonzero(~np.isnan(x))
    return int(idx[0]) if len(idx) else len(x)


def _smooth(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """Exponential smoothing seeded with the SMA of the first ``n`` valid values."""
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    s = _first_valid(x)
    if n <= 0 or len(x) - s < n:
        return out
    out[s + n - 1] = x[s : s + n].mean()
    for i in range(s + n, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _smooth(x, n, 2.0 / (n + 1))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's smoothing (used by RSI, ATR, ADX)."""
    return _smooth(x, n, 1.0 / n)


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if n <= 1 or len(x) < n:
        return out
    out[n - 1 :] = sliding_window_view(x, n).std(axis=1)
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if n <= 0 or len(x) < n:
        return out
    out[n - 1 :] = sliding_window_view(x, n).max(axis=1)
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if n <= 0 or len(x) < n:
        return out
    out[n - 1 :] = sliding_window_view(x, n).min(axis=1)
    return out


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    """Value from ``k`` bars earlier (NaN at the start)."""
    x = np.asarray(x, dtype=float)
    out = _nan(len(x))
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    out = _nan(len(close))
    if len(close) < 2:
        return out
    delta = np.diff(close)
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    au = rma(up, n)
    ad = rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(ad == 0, 100.0, 100.0 - 100.0 / (1.0 + au / ad))
    r[np.isnan(au)] = np.nan
    out[1:] = r
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(close, n)
    sd = rolling_std(close, n)
    return mid - k * sd, mid, mid + k * sd


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    pc = shift(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    tr[0] = high[0] - low[0] if len(tr) else np.nan
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), n)


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """Average Directional Index, 0..100. Above ~25 is usually read as 'trending'."""
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    m = len(close)
    out = _nan(m)
    if m < 2:
        return out
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(high, low, close)[1:]
    atr_ = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, n) / atr_
        minus_di = 100.0 * rma(minus_dm, n) / atr_
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    dx = np.where(np.isnan(dx) & ~np.isnan(atr_), 0.0, dx)
    a = rma(dx, n)
    out[1:] = a
    return out


def donchian(high: np.ndarray, low: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Prior-``n``-bar high and low, excluding the current bar (so a breakout is detectable)."""
    return shift(rolling_max(high, n), 1), shift(rolling_min(low, n), 1)


def realized_vol(close: np.ndarray, n: int, periods_per_year: float) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    out = _nan(len(close))
    if len(close) < 2:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(close))
    out[1:] = rolling_std(lr, n) * np.sqrt(periods_per_year)
    return out
