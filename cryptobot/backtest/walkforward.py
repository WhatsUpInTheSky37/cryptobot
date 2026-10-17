"""Walk-forward validation.

The series is cut into ``folds`` consecutive out-of-sample segments. For each,
the best parameters on all data *before* it (in-sample, from the strategy's
small fixed grid) are evaluated on the segment. If the best in-sample
parameters do not hold up out of sample, the strategy is fragile and any
good-looking full-period backtest should not be trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptobot.backtest.engine import Costs, simulate
from cryptobot.backtest.metrics import compute_metrics
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import StrategyConfig, StrategySpec
from cryptobot.strategy.library import STRATEGIES
from cryptobot.strategy.signals import compute_target_exposure


@dataclass
class Fold:
    index: int
    is_start_ms: int
    is_end_ms: int
    oos_start_ms: int
    oos_end_ms: int
    best_params: dict[str, float]
    is_return: float
    oos_return: float
    is_cagr: float
    oos_cagr: float
    user_oos_return: float
    oos_trades: int


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    oos_equity: np.ndarray | None = None  # OOS segments chained into one curve (each fold starts where the last ended)
    mean_is_cagr: float = 0.0
    mean_oos_cagr: float = 0.0
    params_stable: bool = True
    note: str = ""


def _candidates(config: StrategyConfig) -> list[StrategyConfig]:
    if len(config.strategies) != 1:
        return [config]  # only tune single-strategy configs
    spec = config.strategies[0]
    cls = STRATEGIES[spec.id]
    out = []
    seen = set()
    for g in (*cls.grid, spec.full_params()):
        key = tuple(sorted(g.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(config.model_copy(update={"strategies": [StrategySpec(id=spec.id, params=dict(g))]}))
    return out


def _run(cfg: StrategyConfig, bars: BarArrays, budget: float, costs: Costs, start: int, end: int):
    sub = bars.head(end)
    expo, warm = compute_target_exposure(cfg, sub)
    sim = simulate(sub, expo, budget, costs, cfg.effective_risk(), start_index=max(start, warm))
    m = compute_metrics(sim.equity[start:end], budget, bars.periods_per_year, sim.trades, sim.units[start:end], sim.fees_paid)
    return sim, m


def walk_forward(
    bars: BarArrays,
    config: StrategyConfig,
    budget: float,
    costs: Costs,
    folds: int = 4,
    min_is_bars: int = 200,
) -> WalkForwardResult:
    n = len(bars)
    res = WalkForwardResult()
    seg = (n - min_is_bars) // folds if n > min_is_bars else 0
    if seg < 30:
        res.note = "Not enough history for walk-forward validation (need a few hundred bars)."
        return res
    cands = _candidates(config)
    stitched = np.full(n, np.nan)
    chosen: list[tuple] = []
    for k in range(folds):
        is_start, is_end = 0, min_is_bars + k * seg
        oos_start, oos_end = is_end, (min_is_bars + (k + 1) * seg) if k < folds - 1 else n
        scored = []
        for cand in cands:
            _, m = _run(cand, bars, budget, costs, is_start, is_end)
            scored.append((cand, m))
        enough = [x for x in scored if x[1].trades >= 3 or x[0].strategies[0].id == "dca"]
        pool = enough or scored
        best, best_m = max(pool, key=lambda x: x[1].cagr)
        sim_oos, m_oos = _run(best, bars, budget, costs, oos_start, oos_end)
        _, m_user = _run(config, bars, budget, costs, oos_start, oos_end)
        seg_eq = sim_oos.equity[oos_start:oos_end]
        prev = stitched[oos_start - 1] if k and np.isfinite(stitched[oos_start - 1]) else budget
        stitched[oos_start:oos_end] = seg_eq * (prev / budget)  # chain folds into one curve
        chosen.append(tuple(sorted(best.strategies[0].full_params().items())))
        res.folds.append(
            Fold(
                k + 1, int(bars.ts_ms[is_start]), int(bars.ts_ms[is_end - 1]), int(bars.ts_ms[oos_start]),
                int(bars.ts_ms[oos_end - 1]), best.strategies[0].full_params(), best_m.total_return,
                m_oos.total_return, best_m.cagr, m_oos.cagr, m_user.total_return, m_oos.trades,
            )
        )
    res.oos_equity = stitched
    res.mean_is_cagr = float(np.mean([f.is_cagr for f in res.folds]))
    res.mean_oos_cagr = float(np.mean([f.oos_cagr for f in res.folds]))
    res.params_stable = len(set(chosen)) <= max(1, folds // 2)
    return res
