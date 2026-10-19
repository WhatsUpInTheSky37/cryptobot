"""THE shared signal path. Backtests, TEST mode and BUY mode all call this and nothing else."""
from __future__ import annotations

import warnings

import numpy as np

from cryptobot.strategy.base import BarArrays, warmup_index
from cryptobot.strategy.config import StrategyConfig
from cryptobot.strategy.library import STRATEGIES
from cryptobot.strategy.regime import regime_gate, vol_target


def compute_target_exposure(config: StrategyConfig, bars: BarArrays) -> tuple[np.ndarray, int]:
    """Return (exposure in [0,1] with NaN during warm-up, warm-up index)."""
    if not config.strategies:
        raise ValueError("no strategy configured")
    parts: list[np.ndarray] = []
    for spec in config.strategies:
        cls = STRATEGIES[spec.id]
        strat = cls()
        e = strat.compute(bars, spec.full_params()).astype(float)
        if config.regime_gate:
            e = regime_gate(e, bars, cls.kind, config.adx_n, config.adx_threshold)
        parts.append(e)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rows during warm-up are expected
        exposure = np.nanmean(np.vstack(parts), axis=0) if len(parts) > 1 else parts[0]
    # nanmean would hide a NaN from one strategy; require all to be warmed up.
    all_nan = np.any(np.isnan(np.vstack(parts)), axis=0)
    exposure = np.where(all_nan, np.nan, exposure)
    pure_schedule = all(STRATEGIES[s.id].kind == "schedule" for s in config.strategies)
    if config.vol_target and not pure_schedule:
        exposure = vol_target(exposure, bars, config.target_annual_vol, config.vol_window)
    exposure = np.clip(exposure, 0.0, 1.0)
    return exposure, warmup_index(exposure)

