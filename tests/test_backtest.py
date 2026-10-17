import numpy as np
import pytest

from cryptobot.backtest.benchmarks import buy_and_hold, dca
from cryptobot.backtest.engine import Costs, simulate
from cryptobot.backtest.metrics import compute_metrics, max_drawdown
from cryptobot.backtest.report import run_backtest
from cryptobot.backtest.walkforward import walk_forward
from cryptobot.core.models import Bar
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import RiskRules, StrategyConfig, StrategySpec
from tests.test_strategies import synthetic

NO_RISK = RiskRules(stop_atr_mult=0, trailing_pct=0, take_profit_pct=0, rebalance_threshold_pct=0)


def flat_then_crash(n_flat=40, crash_to=50.0) -> BarArrays:
    bars = []
    for i in range(n_flat):
        bars.append(Bar("X/USD", 86400, i * 86_400_000, 100, 101, 99, 100, 10, 5, True))
    bars.append(Bar("X/USD", 86400, n_flat * 86_400_000, 100, 100, crash_to, crash_to, 10, 5, True))
    for i in range(n_flat + 1, n_flat + 20):
        bars.append(Bar("X/USD", 86400, i * 86_400_000, crash_to, crash_to + 1, crash_to - 1, crash_to, 10, 5, True))
    return BarArrays.from_bars(bars)


def test_always_long_no_costs_matches_price_ratio():
    b = synthetic(200)
    ones = np.ones(200)
    sim = simulate(b, ones, 100.0, Costs(0, 0), NO_RISK, start_index=10)
    assert sim.equity[-1] == pytest.approx(100.0 * b.close[-1] / b.open[11])
    assert len(sim.fills) == 1


def test_fees_and_slippage_reduce_equity_by_known_amount():
    b = synthetic(200)
    ones = np.ones(200)
    c = Costs(0.5, 0.1)
    sim = simulate(b, ones, 100.0, c, NO_RISK, start_index=10)
    units = (100.0 / (1 + c.fee)) / (b.open[11] * (1 + c.slip))
    assert sim.equity[-1] == pytest.approx(units * b.close[-1])
    assert sim.fees_paid == pytest.approx(100.0 / (1 + c.fee) * c.fee)


def test_budget_is_a_hard_cap_and_cash_never_negative():
    b = synthetic(400, seed=11, drift=0.01)  # strong uptrend: equity grows well past the budget
    rng = np.random.default_rng(1)
    expo = np.where(rng.uniform(0, 1, 400) < 0.15, 0.0, rng.uniform(0, 1, 400))  # goes flat now and then
    sim = simulate(b, expo, 100.0, Costs(0.4, 0.05), RiskRules(stop_atr_mult=0, rebalance_threshold_pct=1), start_index=5)
    assert np.nanmin(sim.cash) >= -1e-9
    assert sim.equity[-1] > 120  # profit happened
    # the money at risk (cost basis of the open position, fees included) never exceeds
    # the budget, even though profits took equity well above it and the position was
    # scaled up and down many times
    assert sim.trades
    assert sim.max_in_use <= 100.0 + 1e-6
    assert np.max(sim.in_use) <= 100.0 + 1e-6
    assert any(f.reason == "signal add" for f in sim.fills) and any(f.reason == "signal reduce" for f in sim.fills)


def test_stop_loss_fires_and_blocks_reentry_until_flat():
    b = flat_then_crash()
    ones = np.ones(len(b))
    sim = simulate(b, ones, 100.0, Costs(0, 0), RiskRules(stop_atr_mult=2.0, atr_n=14, rebalance_threshold_pct=0), start_index=20)
    reasons = [f.reason for f in sim.fills]
    assert reasons == ["signal entry", "stop-loss"]
    stop_fill = sim.fills[1]
    assert stop_fill.price == pytest.approx(min(100.0, 100.0 - 2 * 2.0))  # ATR of flat bars = 2
    assert sim.units[-1] == 0 and sim.equity[-1] == pytest.approx(96.0)
    assert sim.stop_outs == 1 and len(sim.trades) == 1 and sim.trades[0].exit_reason == "stop-loss"


def test_take_profit_and_trailing_stop():
    bars = []
    px = 100.0
    for i in range(60):
        px *= 1.02 if i < 40 else 0.97
        bars.append(Bar("X/USD", 86400, i * 86_400_000, px / 1.02 if i < 40 else px / 0.97, px * 1.005, px * 0.995, px, 10, 5, True))
    b = BarArrays.from_bars(bars)
    ones = np.ones(60)
    tp = simulate(b, ones, 100.0, Costs(0, 0), RiskRules(stop_atr_mult=0, take_profit_pct=20, rebalance_threshold_pct=0), start_index=5)
    assert tp.take_profits == 1 and tp.trades[0].pnl_pct == pytest.approx(0.20, abs=0.01)
    tr = simulate(b, ones, 100.0, Costs(0, 0), RiskRules(stop_atr_mult=0, trailing_pct=5, rebalance_threshold_pct=0), start_index=5)
    assert tr.stop_outs == 1
    peak = max(bb.high for bb in bars[:41])
    assert tr.trades[0].exit_price == pytest.approx(peak * 0.95, rel=0.03)


def test_metrics_known_values():
    eq = np.array([100.0, 110.0, 99.0, 120.0])
    assert max_drawdown(eq) == pytest.approx(0.10)
    m = compute_metrics(eq, 100.0, 365.0)
    assert m.total_return == pytest.approx(0.20)
    assert m.max_drawdown == pytest.approx(0.10)
    flat = compute_metrics(np.array([100.0, 100.0, 100.0]), 100.0, 365.0)
    assert flat.sharpe == 0 and flat.total_return == 0


def test_benchmarks():
    b = synthetic(300)
    bh = buy_and_hold(b, 100.0, Costs(0, 0), 10)
    assert bh[-1] == pytest.approx(100.0 * b.close[-1] / b.open[11])
    assert np.isnan(bh[9]) and bh[10] == 100.0
    d = dca(b, 100.0, Costs(0, 0), 10, 7)
    assert d[10] == 100.0
    assert np.all(np.isfinite(d[10:]))
    # fully invested by the end: no cash left beyond rounding
    buys = list(range(11, 300, 7))
    units = sum((100.0 / len(buys)) / b.open[i] for i in buys)
    assert d[-1] == pytest.approx(units * b.close[-1])


def test_walk_forward_folds_are_ordered_and_disjoint():
    b = synthetic(1200, seed=4)
    cfg = StrategyConfig(strategies=[StrategySpec(id="ema_cross")])
    wf = walk_forward(b, cfg, 100.0, Costs(0.4, 0.05), folds=4)
    assert len(wf.folds) == 4
    for k, f in enumerate(wf.folds):
        assert f.is_start_ms < f.is_end_ms < f.oos_start_ms <= f.oos_end_ms
        if k:
            assert f.oos_start_ms > wf.folds[k - 1].oos_end_ms
            assert f.is_end_ms == wf.folds[k - 1].oos_end_ms
    assert wf.oos_equity is not None
    first = int(np.searchsorted(b.ts_ms, wf.folds[0].oos_start_ms))
    assert np.all(np.isnan(wf.oos_equity[:first])) and np.all(np.isfinite(wf.oos_equity[first:]))
    short = walk_forward(synthetic(150), cfg, 100.0, Costs(0.4, 0.05))
    assert short.note and not short.folds


@pytest.mark.parametrize("sid", ["ema_cross", "rsi_reversion", "grid", "dca"])
def test_full_report_runs_and_explains(sid):
    b = synthetic(900, seed=8)
    rep = run_backtest(b, StrategyConfig(strategies=[StrategySpec(id=sid)]), 100.0, Costs(0.4, 0.05))
    assert rep.metrics.final_equity > 0
    assert rep.warnings and all(w.level in ("ok", "warn", "bad") for w in rep.warnings)
    assert any("holding" in w.text for w in rep.warnings)
    if sid == "dca":
        assert rep.metrics.exposure_time > 0.9  # the baseline stays invested; stops do not eject it
