"""Full backtest run + plain-English verdicts."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptobot.backtest.benchmarks import buy_and_hold, dca
from cryptobot.backtest.engine import Costs, SimResult, simulate
from cryptobot.backtest.metrics import Metrics, compute_metrics
from cryptobot.backtest.walkforward import WalkForwardResult, walk_forward
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import StrategyConfig
from cryptobot.strategy.signals import compute_target_exposure


@dataclass
class Warning_:
    level: str  # "bad" | "warn" | "ok"
    text: str


@dataclass
class BacktestReport:
    config: StrategyConfig
    costs: Costs
    budget: float
    bars: BarArrays
    exposure: np.ndarray
    start_index: int
    sim: SimResult
    metrics: Metrics
    bh_equity: np.ndarray
    bh_metrics: Metrics
    dca_equity: np.ndarray
    dca_metrics: Metrics
    walk: WalkForwardResult
    warnings: list[Warning_] = field(default_factory=list)


def run_backtest(bars: BarArrays, config: StrategyConfig, budget: float, costs: Costs, folds: int = 4, limits=None) -> BacktestReport:
    exposure, warm = compute_target_exposure(config, bars)
    start = min(warm, max(0, len(bars) - 2))
    sim = simulate(bars, exposure, budget, costs, config.effective_risk(), start_index=start, limits=limits)
    ppy = bars.periods_per_year
    m = compute_metrics(sim.equity, budget, ppy, sim.trades, sim.units, sim.fees_paid)
    bench_fee = budget * costs.fee / (1 + costs.fee)  # the whole budget is bought once
    bh = buy_and_hold(bars, budget, costs, start)
    bhm = compute_metrics(bh, budget, ppy, fees_paid=bench_fee)
    every = max(1, int(round(7 * 86400 / bars.timeframe_s)))
    dc = dca(bars, budget, costs, start, every)
    dcm = compute_metrics(dc, budget, ppy, fees_paid=bench_fee)
    wf = walk_forward(bars, config, budget, costs, folds=folds, limits=limits)
    rep = BacktestReport(config, costs, budget, bars, exposure, start, sim, m, bh, bhm, dc, dcm, wf)
    rep.warnings = verdicts(rep)
    return rep


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def verdicts(r: BacktestReport) -> list[Warning_]:
    out: list[Warning_] = []
    m, bh, dc = r.metrics, r.bh_metrics, r.dca_metrics
    years = m.days / 365.0

    if m.total_return < bh.total_return:
        lvl = "bad" if m.max_drawdown >= bh.max_drawdown * 0.9 else "warn"
        out.append(
            Warning_(
                lvl,
                f"The strategy made {pct(m.total_return)} over {years:.1f} years while simply holding made "
                f"{pct(bh.total_return)}. "
                + (
                    f"Its worst drawdown was {m.max_drawdown:.0%} vs {bh.max_drawdown:.0%} for holding, so it did "
                    "lose less in the bad stretches."
                    if lvl == "warn"
                    else f"Its worst drawdown ({m.max_drawdown:.0%}) was not meaningfully better than holding "
                    f"({bh.max_drawdown:.0%}) either. On this evidence it is worse than doing nothing."
                ),
            )
        )
    else:
        out.append(
            Warning_(
                "ok",
                f"The strategy made {pct(m.total_return)} vs {pct(bh.total_return)} for holding, with a worst "
                f"drawdown of {m.max_drawdown:.0%} vs {bh.max_drawdown:.0%}. Past performance does not predict "
                "future results; check the walk-forward section before believing it.",
            )
        )
    if m.total_return < dc.total_return:
        out.append(Warning_("warn", f"Weekly dollar-cost averaging would have made {pct(dc.total_return)}, more than the strategy."))
    if m.trades < 20:
        out.append(Warning_("warn", f"Only {m.trades} round-trip trades. Too few to tell skill from luck; treat every number here as noise."))
    if m.fees_paid > 0 and (m.final_equity - r.budget + m.fees_paid) > 0:
        gross = m.final_equity - r.budget + m.fees_paid
        share = m.fees_paid / gross
        if share > 0.25:
            out.append(Warning_("bad" if share > 0.6 else "warn", f"Fees and slippage consumed {share:.0%} of gross profit (${m.fees_paid:,.2f}). A faster timeframe or a pricier exchange would make this worse."))
    if m.total_return > 0 and m.sharpe < 0.5:
        out.append(Warning_("warn", f"Risk-adjusted return is poor (Sharpe {m.sharpe:.2f}). The gains came with a lot of volatility."))
    if m.exposure_time < 0.10 and m.trades > 0:
        out.append(Warning_("warn", f"The strategy was in the market only {m.exposure_time:.0%} of the time. Results depend on a few short windows."))
    if years < 2:
        out.append(Warning_("warn", f"Only {years:.1f} years of data. Crypto has multi-year cycles; test across at least one full bull and bear market."))

    wf = r.walk
    if wf.note:
        out.append(Warning_("warn", wf.note))
    elif wf.folds:
        if wf.mean_is_cagr > 0 and wf.mean_oos_cagr < 0:
            out.append(Warning_("bad", f"Overfit: parameters that looked best in-sample ({wf.mean_is_cagr:+.0%}/yr) LOST money out-of-sample ({wf.mean_oos_cagr:+.0%}/yr)."))
        elif wf.mean_is_cagr > 0 and wf.mean_oos_cagr < 0.5 * wf.mean_is_cagr:
            out.append(Warning_("warn", f"Likely overfit: out-of-sample return ({wf.mean_oos_cagr:+.0%}/yr) is less than half of in-sample ({wf.mean_is_cagr:+.0%}/yr)."))
        else:
            out.append(Warning_("ok", f"Walk-forward: in-sample {wf.mean_is_cagr:+.0%}/yr, out-of-sample {wf.mean_oos_cagr:+.0%}/yr. The parameters held up reasonably across periods."))
        if not wf.params_stable:
            out.append(Warning_("warn", "The best parameters changed from period to period. The strategy's edge, if any, is not stable."))
    return out

