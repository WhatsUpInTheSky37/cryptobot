import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import pytest

from cryptobot.core.events import (
    AlertEvent,
    BarsEvent,
    BudgetEvent,
    FillEvent,
    HeartbeatEvent,
    MarketInfoEvent,
    PositionsEvent,
    SignalEvent,
    TickEvent,
    TraderStateEvent,
)
from cryptobot.core.models import Bar, Mode, Tick
from cryptobot.db.store import Store
from cryptobot.engine.executors import LiveExecutor, SimulatedExecutor
from cryptobot.engine.risk import RiskLimits
from cryptobot.engine.trader import Trader
from cryptobot.strategy.config import RiskRules, StrategyConfig, StrategySpec

S = "BTC/USDT"
DAY = 86_400_000
T0 = 1_760_000_000_000  # some Friday in Oct 2025, UTC


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000

    def advance(self, ms):
        self.ms += ms


def daily_bars(prices, start=T0 - 400 * DAY, rng_pct=0.5):
    bars = []
    prev = prices[0]
    for i, c in enumerate(prices):
        o = prev
        h = max(o, c) * (1 + rng_pct / 100)
        l = min(o, c) * (1 - rng_pct / 100)
        bars.append(Bar(S, 86400, start + i * DAY, o, h, l, c, 10.0, 5, True))
        prev = c
    return bars


def rising(n=80, start_px=50_000.0, step=0.5):
    return [start_px * (1 + step / 100) ** i for i in range(n)]


def cfg(stop=2.0, tp=0.0, trail=0.0, thresh=5.0):
    return StrategyConfig(
        strategies=[StrategySpec(id="ema_cross", params={"fast": 5, "slow": 20})],
        timeframe_s=86400, regime_gate=False, vol_target=False,
        risk=RiskRules(stop_atr_mult=stop, take_profit_pct=tp, trailing_pct=trail, rebalance_threshold_pct=thresh),
    )


def make(store=None, limits=None, strategy=None, clock=None, **kw):
    store = store or Store(":memory:")
    events = []
    clock = clock or Clock(T0)
    t = Trader(store, events.append, "binanceus", [S], strategy or cfg(), limits or RiskLimits(budget=100, per_position_pct=100, daily_loss_pct=5, max_drawdown_pct=20), clock=clock, **kw)
    t.start()
    t.on_event(MarketInfoEvent("binanceus", S, 0.006, 0.004, 0.00001, 5.0, 0.00001, 0.01))
    return t, events, store, clock


def feed_history(t, prices):
    t.on_event(BarsEvent("binanceus", S, 86400, daily_bars(prices)))


def tick(t, price, clock):
    t.on_event(TickEvent("binanceus", Tick(S, clock.ms, price, 0.01)))


def fills(events):
    return [e for e in events if isinstance(e, FillEvent)]


def test_enters_on_signal_with_stop_attached_and_persists():
    t, ev, store, clock = make()
    feed_history(t, rising())
    f = fills(ev)
    assert len(f) == 1 and f[0].side == "buy" and f[0].reason == "signal entry" and f[0].mode == "TEST"
    p = t.ledger.position(S)
    assert p.open and p.stop is not None and p.stop < f[0].price
    assert p.basis <= 100.0 + 1e-9 and t.ledger.cash >= 0
    assert p.basis == pytest.approx(f[0].price * f[0].amount + f[0].fee)
    assert store.count_trades("TEST") == 1
    pos = store.load_positions("TEST")
    assert len(pos) == 1 and pos[0]["stop_loss"] == pytest.approx(p.stop)
    assert any(isinstance(e, SignalEvent) and "target exposure 100%" in e.text for e in ev)
    assert any(isinstance(e, PositionsEvent) and e.positions and e.positions[0].mode == "TEST" for e in ev)
    # the same history again does not trade again (already decided on that bar)
    feed_history(t, rising())
    assert len(fills(ev)) == 1


def test_stop_loss_fires_on_tick_and_blocks_reentry_until_signal_resets():
    t, ev, store, clock = make()
    feed_history(t, rising())
    p = t.ledger.position(S)
    stop = p.stop
    tick(t, stop * 0.999, clock)
    f = fills(ev)
    assert len(f) == 2 and f[1].side == "sell" and f[1].reason == "stop-loss" and f[1].pnl < 0
    assert not p.open and p.blocked and t.ledger.in_use() == 0
    assert store.load_positions("TEST") == [] and store.load_positions("TEST", "closed")[0]["pnl"] == pytest.approx(f[1].pnl)
    # next closed bar, still bullish -> no re-entry
    t.on_event(BarsEvent("binanceus", S, 86400, daily_bars(rising(81))))
    assert len(fills(ev)) == 2
    # a bearish stretch resets the signal, then a rally re-enters
    px = rising(81) + [rising(81)[-1] * (0.97 ** i) for i in range(1, 25)]
    t.on_event(BarsEvent("binanceus", S, 86400, daily_bars(px)))
    assert not p.blocked and len(fills(ev)) == 2
    px += [px[-1] * (1.02 ** i) for i in range(1, 30)]
    t.on_event(BarsEvent("binanceus", S, 86400, daily_bars(px)))
    assert len(fills(ev)) == 3 and fills(ev)[-1].side == "buy"


def test_daily_loss_limit_halts_closes_and_resume_waits_for_midnight():
    t, ev, store, clock = make(strategy=cfg(stop=0))
    feed_history(t, rising())
    entry = fills(ev)[0].price
    clock.advance(2000)
    tick(t, entry * 0.93, clock)  # -7% > 5% daily limit
    assert t.status == "halted_daily"
    f = fills(ev)
    assert f[-1].side == "sell" and f[-1].reason == "daily loss limit"
    assert not t.ledger.position(S).open
    assert any(isinstance(e, AlertEvent) and "Daily loss" in e.title for e in ev)
    assert any(isinstance(e, TraderStateEvent) and e.status == "halted_daily" for e in ev)
    assert t.resume() is False
    # a bullish bar while halted must not buy
    t.on_event(BarsEvent("binanceus", S, 86400, daily_bars(rising(81))))
    assert len(fills(ev)) == 2
    clock.ms = t.halted_until_ms + 1000
    assert t.resume() is True and t.status == "running"
    assert t.ledger.day_start_equity == pytest.approx(t.ledger.equity())


def test_drawdown_limit_halts_until_restart_with_reset_peak():
    t, ev, store, clock = make(strategy=cfg(stop=0), limits=RiskLimits(budget=100, per_position_pct=100, daily_loss_pct=90, max_drawdown_pct=20))
    feed_history(t, rising())
    entry = fills(ev)[0].price
    clock.advance(2000)
    tick(t, entry * 1.10, clock)  # new peak
    clock.advance(2000)
    tick(t, entry * 0.85, clock)  # 22.7% below peak
    assert t.status == "halted_drawdown" and fills(ev)[-1].reason == "drawdown limit"
    assert t.resume() is False
    # restarting with reset_peak clears the halt and re-bases the peak
    t2 = Trader(store, ev.append, "binanceus", [S], cfg(stop=0), RiskLimits(budget=100, per_position_pct=100, daily_loss_pct=90, max_drawdown_pct=20), clock=clock, reset_peak=True)
    t2.start()
    assert t2.status == "running" and t2.ledger.cash == pytest.approx(t.ledger.cash)


def test_emergency_stop_closes_everything_and_restart_reloads_state():
    t, ev, store, clock = make()
    feed_history(t, rising())
    assert t.ledger.position(S).open
    cash_before = t.ledger.cash
    n = t.emergency_stop()
    assert n == 1 and t.status == "stopped" and not t.ledger.position(S).open
    assert t.ledger.cash > cash_before
    # ignores events while stopped
    feed_history(t, rising(90))
    assert len(fills(ev)) == 2
    # a fresh trader on the same store continues from the persisted ledger
    t2, ev2, _, _ = make(store=store, clock=clock)
    assert t2.ledger.cash == pytest.approx(t.ledger.cash)
    assert t2.ledger.realized_pnl == pytest.approx(t.ledger.realized_pnl)


def test_restart_reloads_open_position_and_budget_change_adjusts_cash():
    t, ev, store, clock = make()
    feed_history(t, rising())
    p = t.ledger.position(S)
    t.stop()
    t2, _, _, _ = make(store=store, clock=clock, limits=RiskLimits(budget=150, per_position_pct=100))
    p2 = t2.ledger.position(S)
    assert p2.open and p2.units == pytest.approx(p.units) and p2.stop == pytest.approx(p.stop)
    assert t2.ledger.cash == pytest.approx(t.ledger.cash + 50)


def test_budget_event_and_equity_tracking():
    t, ev, store, clock = make()
    feed_history(t, rising())
    clock.advance(61_000)
    t.on_event(HeartbeatEvent("binanceus", clock.ms))
    b = [e for e in ev if isinstance(e, BudgetEvent)][-1]
    assert b.allocated == 100 and b.in_use == pytest.approx(t.ledger.in_use()) and b.available == pytest.approx(100 - b.in_use)
    assert store.load_equity("TEST")


def test_take_profit_and_trailing_stop_live():
    t, ev, store, clock = make(strategy=cfg(stop=0, tp=10))
    feed_history(t, rising())
    entry = fills(ev)[0].price
    tick(t, entry * 1.11, clock)
    assert fills(ev)[-1].reason == "take-profit" and fills(ev)[-1].pnl > 0
    t2, ev2, _, c2 = make(strategy=cfg(stop=0, trail=5))
    feed_history(t2, rising())
    e2 = fills(ev2)[0].price
    tick(t2, e2 * 1.20, c2)
    tick(t2, e2 * 1.13, c2)  # 5.8% off the high
    assert fills(ev2)[-1].reason == "stop-loss" and fills(ev2)[-1].pnl > 0


def test_per_position_limit_and_exchange_minimum_are_enforced_live():
    t, ev, store, clock = make(limits=RiskLimits(budget=100, per_position_pct=10))
    feed_history(t, rising())
    f = fills(ev)
    assert len(f) == 1 and f[0].price * f[0].amount * 1.006 <= 10.0 + 1e-9
    t2, ev2, _, _ = make(limits=RiskLimits(budget=100, per_position_pct=4), strategy=cfg(thresh=1.0))
    feed_history(t2, rising())
    assert not fills(ev2)
    assert any(isinstance(e, SignalEvent) and "below the exchange minimum" in e.text for e in ev2)


# ------------------------------------------------------- TEST mode isolation


def test_test_mode_cannot_send_a_real_order(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real order method was called in TEST mode")

    for mod in (ccxt, ccxtpro):
        for ex_id in ("binanceus", "binance", "kraken", "coinbase"):
            cls = getattr(mod, ex_id)
            for name in ("create_order", "cancel_order", "cancel_all_orders", "withdraw"):
                monkeypatch.setattr(cls, name, boom, raising=False)
    monkeypatch.setattr(LiveExecutor, "execute", boom)

    t, ev, store, clock = make()
    assert isinstance(t.executor, SimulatedExecutor)
    assert not any(isinstance(getattr(t, a), (ccxt.Exchange,)) for a in vars(t))
    feed_history(t, rising())
    tick(t, t.ledger.position(S).stop * 0.99, clock)
    t.emergency_stop()
    assert len(fills(ev)) == 2 and all(f.mode == "TEST" for f in fills(ev))
    assert all(tr["mode"] == "TEST" for tr in store.load_trades())


def test_buy_mode_requires_an_authenticated_exchange():
    with pytest.raises(ValueError, match="authenticated exchange"):
        Trader(Store(":memory:"), lambda e: None, "binanceus", [S], cfg(), RiskLimits(), mode=Mode.BUY)
