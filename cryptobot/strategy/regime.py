"""Regime gate and volatility targeting: modifiers applied on top of any strategy."""
from __future__ import annotations

import numpy as np

from cryptobot.strategy import indicators as ind
from cryptobot.strategy.base import BarArrays, Kind


def regime_gate(exposure: np.ndarray, bars: BarArrays, kind: Kind, adx_n: int = 14, threshold: float = 25.0) -> np.ndarray:
    """Trend strategies trade only when ADX >= threshold; mean reversion only when ADX < threshold."""
    if kind == "schedule":
        return exposure
    a = ind.adx(bars.high, bars.low, bars.close, adx_n)
    allowed = a >= threshold if kind == "trend" else a < threshold
    out = np.where(allowed, exposure, 0.0)
    out[np.isnan(a) | np.isnan(exposure)] = np.nan
    return out


def vol_target(exposure: np.ndarray, bars: BarArrays, target_annual_vol: float = 0.40, window: int = 20) -> np.ndarray:
    """Scale exposure down when realised volatility exceeds the target. Never scales up above 1."""
    v = ind.realized_vol(bars.close, window, bars.periods_per_year)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.clip(target_annual_vol / v, 0.0, 1.0)
    out = exposure * scale
    out[np.isnan(v) | np.isnan(exposure)] = np.nan
    return out
