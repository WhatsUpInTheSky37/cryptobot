"""Fault-injection regressions for the trading-logic review."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import ccxt
import numpy as np
import pytest
from pydantic import ValidationError

from cryptobot.core.events import BarsEvent, BookEvent, ConnectionEvent, HeartbeatEvent
from cryptobot.core.models import Bar
from cryptobot.db.store import Store
from cryptobot.earlywarning.config import EWConfig
from cryptobot.earlywarning.engine import EWPaperBook, _Position, _Sym
from cryptobot.earlywarning.paper import simulate_trade
from cryptobot.earlywarning.store import EWStore, klines_array
from cryptobot.earlywarning.targets import outcome_after
from cryptobot.engine.executors import Fill, LiveExecutor, OrderUncertain
from cryptobot.engine.ledger import Ledger
from cryptobot.engine.risk import Approval, MarketInfo, OrderIntent, Rejection, RiskContext, RiskLimits, RiskManager
from cryptobot.engine.worker import TraderWorker
from cryptobot.pulse.memory import MarketMemory, MemoryRow
from cryptobot.pulse.ml import auc
from cryptobot.pulse.outcomes import OutcomeTracker, Pending
from cryptobot.pulse.config import PulseConfig
from cryptobot.reports import completed_trades
from cryptobot.strategy.config import RiskRules, StrategyConfig, StrategySpec
from tests.test_live import FakeLive, approval, live, make_buy
from tests.test_trader import DAY, S, T0, daily_bars, feed_history, fills, make, rising, tick


def test_cancel_failure_never_reports_canceled_or_reissues():
    exchange = FakeLive(fill_fraction=0)
    executor = live(exchange, timeout=0)
    exchange.cancel_order = lambda *args: (_ for _ in ()).throw(ccxt.NetworkError("offline"))
    exchange.fetch_order = lambda *args: (_ for _ in ()).throw(ccxt.NetworkError("offline"))
    with pytest.raises(OrderUncertain):
        executor.execute(approval(executor.risk), 80_000, 1)
    assert exchange.create_calls == 1


def test_ambiguous_submission_is_not_retried_when_lookup_fails():
    exchange = FakeLive(fail_first=ccxt.NetworkError("lost acknowledgment"))
    executor = live(exchange)
    executor._find_by_client_id = lambda *args: None
    with pytest.raises(OrderUncertain):
        executor.execute(approval(executor.risk), 80_000, 1)
    assert exchange.create_calls == len(exchange.orders) == 1


def test_commission_asset_deltas_and_value_are_distinct():
    ledger = Ledger(100)
    ledger.apply_fill(Fill(1, "X/USD", "buy", 100, 1, 1, "buy", "BUY", quote_fee=0, base_fee=.01))
    assert ledger.cash == 0 and ledger.position("X/USD").units == .99
    assert ledger.equity() == 99
    assert ledger.position("X/USD").basis == 100


def test_third_asset_commission_is_valued_instead_of_disappearing():
    exchange = FakeLive()
    executor = live(exchange)
    exchange.fetch_ticker = lambda *args: {"last": 600}
    total, quote, base, fees, _ = executor._settle_fees({"filled": 1, "fees": [{"cost": .01, "currency": "BNB"}]}, "X/USD", 100, .001)
    assert (total, quote, base) == (6, 0, 0)
    ledger = Ledger(100)
    ledger.apply_fill(Fill(1, "X/USD", "buy", 100, 1, total, "buy", "BUY", quote_fee=quote, base_fee=base, fees=tuple(fees)))
    assert ledger.cash == 0 and ledger.equity() == 94


def test_unknown_commission_conversion_keeps_order_unresolved():
    executor = live(FakeLive())
    with pytest.raises(OrderUncertain):
        executor._settle_fees({"fees": [{"cost": 1, "currency": "BNB"}]}, "X/USD", 100, .001)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_nonfinite_and_negative_amounts_are_rejected(value):
    manager = RiskManager(RiskLimits(), RiskRules(stop_atr_mult=0))
    ctx = RiskContext(100, 0, 0, 0, False, MarketInfo("X/USD"), T0)
    assert isinstance(manager.approve(OrderIntent("X/USD", "buy", value, 100, "test"), ctx), Rejection)


def test_pending_approvals_reserve_cash_and_inventory():
    manager = RiskManager(RiskLimits(per_position_pct=100), RiskRules(stop_atr_mult=0))
    ctx = RiskContext(100, 0, 0, 0, False, MarketInfo("X/USD", taker_fee=0), T0)
    first = manager.approve(OrderIntent("X/USD", "buy", 1, 100, "first"), ctx)
    assert isinstance(first, Approval)
    assert isinstance(manager.approve(OrderIntent("X/USD", "buy", 1, 100, "second"), ctx), Rejection)
    with pytest.raises(PermissionError, match="expired"):
        manager.consume(first.token, T0 + 30_001)


def test_loss_at_stop_caps_include_costs_and_existing_risk():
    manager = RiskManager(RiskLimits(budget=100, per_position_pct=100, risk_per_trade_pct=1, max_open_risk_pct=2), RiskRules())
    ctx = RiskContext(100, 0, 0, 0, False, MarketInfo("X/USD", taker_fee=.01), T0, open_risk=1.5)
    order = manager.approve(OrderIntent("X/USD", "buy", 1, 100, "entry", stop_price=90), ctx)
    assert isinstance(order, Approval)
    worst_loss = order.amount * (100 * 1.0005 * 1.01 - 90 * .9995 * .99)
    assert worst_loss <= .5 + 1e-9


def test_pending_approvals_reserve_aggregate_stop_risk():
    manager = RiskManager(RiskLimits(budget=1000, per_position_pct=100, risk_per_trade_pct=5, max_open_risk_pct=5), RiskRules())
    ctx = RiskContext(1000, 0, 0, 0, False, MarketInfo("X/USD", taker_fee=0), T0)
    order = OrderIntent("X/USD", "buy", 1, 100, "entry", stop_price=50)
    assert isinstance(manager.approve(order, ctx), Approval)
    assert isinstance(manager.approve(order, ctx), Rejection)


def test_fill_and_ledger_checkpoint_roll_back_together(monkeypatch):
    trader, _, store, _ = make()
    before = trader.ledger.to_dict()
    monkeypatch.setattr(store, "insert_trade", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        trader._apply_fill(Fill(T0, S, "buy", 100, .1, .01, "entry", "TEST"))
    assert trader.ledger.to_dict() == before
    assert store.load_positions("TEST") == []


def test_applied_execution_is_idempotent():
    trader, _, store, _ = make()
    fill = Fill(T0, S, "buy", 100, .1, .01, "entry", "TEST", execution_key="venue:order-1")
    trader._apply_fill(fill)
    cash = trader.ledger.cash
    trader._apply_fill(fill)
    assert trader.ledger.cash == cash and store.count_trades("TEST") == 1


def test_failed_liquidation_keeps_retrying_until_remaining_units_close(monkeypatch):
    trader, _, _, clock = make()
    feed_history(trader, rising())
    original = trader._sell
    monkeypatch.setattr(trader, "_sell", lambda *args, **kwargs: False)
    trader._halt("halted_daily", "test failure")
    assert trader.status == "liquidating" and trader.ledger.position(S).open
    monkeypatch.setattr(trader, "_sell", original)
    clock.advance(6_000)
    tick(trader, trader.ledger.position(S).last_price, clock)
    trader.on_event(HeartbeatEvent("binanceus", clock.ms))
    assert not trader.ledger.position(S).open and trader.status == "halted_daily"


def test_native_stop_survives_pause_and_known_journal_is_recoverable():
    trader, events, store, exchange, clock = make_buy()
    feed_history(trader, rising())
    p = trader.ledger.position(S)
    assert p.protection_client_id
    stops = [o for o in exchange.orders.values() if o.get("stopLossPrice")]
    assert stops and stops[0]["status"] == "open"
    trader.stop()
    assert trader.status == "paused" and stops[0]["status"] == "open"
    restored, _, _, _, _ = make_buy(store=store, ex=exchange, clock=clock)
    assert restored.ledger.position(S).protection_client_id == p.protection_client_id


def test_live_requires_fresh_book_and_ignores_another_venue():
    trader, events, _, _, _ = make_buy()
    trader._quotes.clear()
    trader.on_event(ConnectionEvent("binanceus", "reconnecting"))
    feed_history(trader, rising())
    assert not fills(events)
    trader.on_event(BarsEvent("other", S, 86400, daily_bars(rising())))
    assert not fills(events)


def test_dca_schedule_uses_elapsed_running_clock_not_history_length():
    config = StrategyConfig(strategies=[StrategySpec(id="dca", params={"interval": 1, "purchases": 12})], regime_gate=False, vol_target=False, risk=RiskRules(rebalance_threshold_pct=0))
    trader, _, store, clock = make(strategy=config)
    trader.on_event(BarsEvent("binanceus", S, 86400, daily_bars([100.] * 600, start=T0 - 600 * DAY)))
    assert trader.ledger.position(S).held_e == pytest.approx(1 / 12, abs=.001)
    clock.advance(DAY)
    trader.on_event(BarsEvent("binanceus", S, 86400, daily_bars([100.] * 601, start=T0 - 600 * DAY)))
    assert trader.ledger.position(S).held_e == pytest.approx(2 / 12, abs=.001)
    assert trader.ledger.position(S).schedule_started_ms == T0


def test_outcome_restore_preserves_first_hit_and_exact_horizon():
    store = Store(":memory:")
    eid = store.insert_pulse_event(ts_ms=T0, exchange="x", symbol=S, direction="long", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=.1, spread_pct=0, mid=100,
                                   target_pct=1, stop_pct=1, cost_pct=.2, status="signal", horizon_ms=60_000)
    tracker = OutcomeTracker(store)
    tracker.add(Pending(eid, S, "long", T0, 100, 1, 1, .2, 60_000))
    tracker.on_price(S, 102, T0 + 10_000)
    tracker.flush(T0 + 10_000)
    restored = OutcomeTracker(store)
    restored.restore(T0 + 20_000)
    assert restored.pending[0].first_hit == "target" and restored.pending[0].horizon_ms == 60_000
    restored.on_price(S, 98, T0 + 60_000)
    assert store.pulse_event(eid)["outcome"] == "target"


def test_unobserved_gap_is_censored():
    store = Store(":memory:")
    eid = store.insert_pulse_event(ts_ms=T0, exchange="x", symbol=S, direction="long", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=.1, spread_pct=0, mid=100, target_pct=1, stop_pct=1, cost_pct=.2, status="signal")
    tracker = OutcomeTracker(store)
    tracker.add(Pending(eid, S, "long", T0, 100, 1, 1, .2, 60_000))
    tracker.on_price(S, 103, T0 + 31_000)
    assert store.pulse_event(eid)["outcome"] == "abandoned"


def test_memory_rejects_evidence_for_another_stop_or_venue():
    memory = MarketMemory(Store(":memory:"), min_n=1)
    memory.add(MemoryRow(S, T0, "long", "range", [0.] * 12, True, False, .1, 1000, 1, 1, .2, 60_000, "v2", "x"))
    assert not memory.estimate("long", {}, target_pct=1, stop_pct=.4, exchange="x").sufficient
    assert not memory.estimate("long", {}, target_pct=1, stop_pct=.2, exchange="y").sufficient
    assert memory.estimate("long", {}, target_pct=1, stop_pct=.2, exchange="x").sufficient


def test_auc_ties_are_half_wins():
    assert auc(np.array([1., 0.]), np.array([.5, .5])) == .5


def test_ew_entry_candle_stop_and_incomplete_horizon():
    cfg = EWConfig()
    cfg.costs.base_slippage_pct = cfg.costs.spread_estimate_pct = cfg.costs.fee_rate = 0
    k = klines_array([(T0 + i * 60_000, 100, 101, 50 if i == 1 else 99, 100, 100, 10000, 100, 50, 5000) for i in range(5)])
    trade = simulate_trade(k, None, "X/USD", T0 + 60_000, "A", "fixed", cfg)
    assert trade and trade.reason == "stop" and trade.pnl < 0
    assert outcome_after(k, T0, 60, 3, 2, 0) is None


def test_ew_partial_trade_statistics_count_the_whole_position():
    cfg = EWConfig()
    cfg.costs.base_slippage_pct = cfg.costs.spread_estimate_pct = cfg.costs.fee_rate = 0
    book = EWPaperBook(EWStore(":memory:"), cfg, lambda: T0)
    book.cash = 0
    book.positions["X/USD"] = _Position("X/USD", 1, 100, 100, 97.5, 110, 120, T0, 0, highest=100)
    book.on_price("X/USD", _Sym(bid=110, state="QUIET"), T0 + 1000, 10000)
    book.on_price("X/USD", _Sym(bid=99, state="QUIET"), T0 + 2000, 10000)
    assert book.realized_today == 4.5
    assert book.wins == 1 and book.losses == 0 and book.gross_profit == 4.5


def test_partial_sell_is_not_a_completed_trade():
    base = dict(mode="TEST", exchange="x", symbol="X/USD", position_id=1, fee=0)
    trades = [dict(base, ts_ms=1, side="buy", amount=1, pnl=None), dict(base, ts_ms=2, side="sell", amount=.5, pnl=5, position_closed=False)]
    assert completed_trades(trades) == []
    trades.append(dict(base, ts_ms=3, side="sell", amount=.5, pnl=-.5, position_closed=True))
    assert completed_trades(trades)[0][1] == 4.5


def test_nested_pulse_risk_configuration_is_not_overwritten():
    config = PulseConfig(risk={"daily_loss_pct": .4, "risk_per_trade_pct": .1})
    assert config.risk.daily_loss_pct == .4 and config.risk.risk_per_trade_pct == .1


def test_order_worker_keeps_the_async_feed_responsive():
    entered, release = threading.Event(), threading.Event()
    class Slow:
        symbols = ["X/USD"]
        def start(self): pass
        def stop(self): pass
        def on_event(self, _): entered.set(); release.wait(2)
    async def scenario():
        worker = await TraderWorker(Slow).wait_ready()
        worker.on_event("tick")
        await asyncio.to_thread(entered.wait, 1)
        # The order worker is blocked; this heartbeat still runs immediately.
        await asyncio.wait_for(asyncio.sleep(.01), .2)
        release.set()
        await asyncio.wrap_future(worker.stop())
        await worker.close()
    asyncio.run(scenario())


def test_full_market_queue_still_accepts_emergency_commands():
    entered, release = threading.Event(), threading.Event()
    stopped = threading.Event()
    class Slow:
        def start(self): pass
        def stop(self): pass
        def on_event(self, _): entered.set(); release.wait(2)
        def emergency_stop(self): stopped.set()
    async def scenario():
        worker = await TraderWorker(Slow).wait_ready()
        worker.on_event("first")
        await asyncio.to_thread(entered.wait, 1)
        for i in range(20_001):
            worker.on_event(i)
        assert worker._overloaded
        emergency = worker.emergency_stop()
        release.set()
        await asyncio.wait_for(asyncio.wrap_future(emergency), 1)
        assert stopped.is_set()
        await worker.close()
    asyncio.run(scenario())


def test_midnight_does_not_charge_yesterdays_unrealized_loss_again():
    now = int(datetime(2026, 10, 18, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    cfg = EWConfig()
    book = EWPaperBook(EWStore(":memory:"), cfg, lambda: now)
    book.cash = 0
    book.positions["X/USD"] = _Position("X/USD", 1, 100, 100, 80, 110, 120, now - DAY, 0)
    book.marks["X/USD"] = 90
    book.day_key = "2026-10-17"
    assert not book.daily_loss_hit(now)
    assert book.day_start_equity == 90


def test_historical_scores_do_not_change_when_future_windows_are_added():
    from cryptobot.earlywarning.study import score_windows
    store, cfg = EWStore(":memory:"), EWConfig()
    def add(i, value):
        feats = dict(rel_volume=value, imbalance=value, freq_change=value, compression=value, absorption=value, persistence=value)
        store.put_window("X/USD", "grid", 0, T0 + i * 60_000, 60, feats, None, {})
    for i in range(10):
        add(i, i + 1)
    score_windows(store, "X/USD", cfg)
    before = [(r["t0_ms"], r["eas"]) for r in store.windows("X/USD")]
    for i in range(10, 20):
        add(i, 10000)
    score_windows(store, "X/USD", cfg)
    assert [(r["t0_ms"], r["eas"]) for r in store.windows("X/USD")][:10] == before


def test_funded_replay_does_not_spend_the_same_cash_on_overlapping_setups():
    from cryptobot.earlywarning.portfolio import replay
    cfg = EWConfig()
    cfg.risk.risk_per_trade_pct = 100
    cfg.risk.max_positions = 3
    cfg.costs.fee_rate = cfg.costs.spread_estimate_pct = cfg.costs.base_slippage_pct = 0
    cfg.exits.max_hold_min = 10
    k = klines_array([(T0 + i * 60_000, 100, 101, 99, 100, 100, 100000, 100, 50, 50000) for i in range(5)])
    data = {s: (k, None) for s in ("A/USD", "B/USD", "C/USD")}
    summary, trades = replay([dict(symbol=s, t0_ms=T0) for s in data], data, cfg, "A", "fixed", None)
    assert len(trades) == 2
    assert summary["funded_portfolio"] and summary["final_cash"] == 100


@pytest.mark.parametrize("venue,expected_type", [("coinbase", "limit"), ("binanceus", "limit"), ("kraken", "stop-loss")])
def test_native_protection_uses_venue_specific_supported_order_types(venue, expected_type):
    exchange = FakeLive()
    executor = live(exchange)
    executor.exchange_id = venue
    requests = []
    def place(*args):
        requests.append(args)
        return dict(id="stop-1", status="open", filled=0, remaining=args[3])
    exchange.create_order = place
    executor.protect(approval(executor.risk, side="sell", units=.001), 70000, 1)
    assert requests[0][1] == expected_type
    if venue == "kraken":
        assert "cl_ord_id" in requests[0][-1] and "userref" not in requests[0][-1]
    else:
        assert requests[0][-1]["stopLossPrice"] == 70000


def test_old_leaky_research_report_cannot_enable_paper_trading():
    from cryptobot.earlywarning.targets import TargetEstimator
    old = TargetEstimator({"horizons": {"60": dict(reliable=True, tradeable=True, target_net_pct=5)}})
    assert old.target(60)[0] is None and old.gross_target(60) is None


def test_unresolved_commission_still_protects_known_bought_inventory():
    exchange = FakeLive(balances={"USDT": 500})
    original = exchange._make
    def make_order(*args):
        order = original(*args)
        order["fee"] = {"cost": .01, "currency": "BNB"}
        return order
    exchange._make = make_order
    trader, events, store, _, _ = make_buy(ex=exchange)
    feed_history(trader, rising())
    assert trader.status == "reconcile_required"
    assert any(o.get("stopLossPrice") and o["status"] == "open" for o in exchange.orders.values())
    assert any(r["status"] == "unknown" for r in store.pending_orders(trader.scope))


def test_terminal_fill_recovers_after_crash_before_local_accounting():
    trader, _, store, exchange, clock = make_buy()
    ctx = RiskContext(500, 0, 0, 0, False, MarketInfo(S), clock.ms)
    approved = trader.risk.approve(OrderIntent(S, "buy", .001, 80000, "entry", stop_price=70000), ctx)
    assert isinstance(approved, Approval)
    fill = trader.executor.execute(approved, 80000, clock.ms)
    assert fill and trader.ledger.position(S).units == 0
    restored, _, _, _, _ = make_buy(store=store, ex=exchange, clock=clock)
    assert restored.ledger.position(S).units == pytest.approx(fill.amount)
    assert restored.ledger.position(S).protection_client_id
    restored._recover_orders()
    assert store.count_trades("BUY") == 1


def test_scanner_restores_state_for_a_held_coin_outside_the_top_list():
    from cryptobot.earlywarning.engine import EarlyWarningEngine
    engine = EarlyWarningEngine(EWStore(":memory:"), lambda _: None, "binanceus", EWConfig(), clock=lambda: T0 / 1000)
    engine.paper.positions["HELD/USD"] = _Position("HELD/USD", 1, 100, 100, 95, 110, 120, T0, 0)
    assert engine.scan({}, []) == ["HELD/USD"]
    assert "HELD/USD" in engine.sym


def test_desktop_lock_prevents_a_second_owner(tmp_path):
    qt = pytest.importorskip("PySide6.QtCore")
    first, second = qt.QLockFile(str(tmp_path / "book.lock")), qt.QLockFile(str(tmp_path / "book.lock"))
    assert first.tryLock(0) and not second.tryLock(0)
    first.unlock()
    assert second.tryLock(0)
    second.unlock()


@pytest.mark.parametrize("strategy_id", ["ema_cross", "macd", "rsi_reversion", "bollinger_reversion", "donchian_breakout", "grid", "dca"])
@pytest.mark.parametrize("modifiers", [False, True])
def test_streaming_checkpoint_preserves_batch_signals_past_600_bars(strategy_id, modifiers):
    from cryptobot.strategy.online import OnlineSignals
    from cryptobot.strategy.signals import compute_target_exposure
    from tests.test_strategies import synthetic
    arrays = synthetic(900, seed=11)
    config = StrategyConfig(strategies=[StrategySpec(id=strategy_id)], regime_gate=modifiers, vol_target=modifiers)
    arrays.timeframe_s = config.timeframe_s
    expected, _ = compute_target_exposure(config, arrays)
    runtime = OnlineSignals(config)
    actual = []
    for i in range(len(arrays)):
        bar = Bar("X/USD", config.timeframe_s, int(arrays.ts_ms[i]), arrays.open[i], arrays.high[i], arrays.low[i], arrays.close[i], arrays.volume[i], 0, True)
        value, _ = runtime.evaluate([bar])
        actual.append(np.nan if value is None else value)
        if i == 599:
            runtime = OnlineSignals(config, state=runtime.to_dict())
    np.testing.assert_allclose(actual, expected, atol=1e-9, equal_nan=True)
    assert len(runtime.history) <= 600
