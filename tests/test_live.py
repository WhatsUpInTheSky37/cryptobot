"""Milestone 5: real-order executor, BUY-mode trader, mode gate, reports."""
import math
import ccxt
import pytest

from cryptobot.core.events import AlertEvent, FillEvent
from cryptobot.core.models import Mode
from cryptobot.db.store import Store
from cryptobot.engine.executors import ExecutionError, LiveExecutor
from cryptobot.engine.modes import FIRST_MONTH_CAP, MIN_TEST_DAYS, buy_gate, simulation_stats
from cryptobot.engine.risk import Approval, MarketInfo, OrderIntent, RiskContext, RiskLimits, RiskManager
from cryptobot.engine.trader import Trader
from cryptobot.reports import comparison, export_csv, export_tax_csv, period_summary, tax_lots
from cryptobot.strategy.config import RiskRules
from tests.test_trader import DAY, S, T0, Clock, cfg, daily_bars, feed_history, fills, rising, tick

INFO = MarketInfo(S, taker_fee=0.006, min_amount=0.00001, min_cost=5.0, amount_step=0.00001)


class FakeLive:
    """A tiny exchange: records orders, fills per a script."""

    id = "binanceus"
    has = {"fetchOrder": True, "fetchTradingFee": True}

    def __init__(self, fill_fraction=1.0, fail_first=None, fee_currency="quote", balances=None):
        self.orders: dict[str, dict] = {}
        self.create_calls = 0
        self.cancelled: list[str] = []
        self.fill_fraction = fill_fraction
        self.fail_first = fail_first
        self.fee_currency = fee_currency
        self.balances = balances or {"USDT": 1000.0, "BTC": 0.0}
        self._n = 0

    def amount_to_precision(self, sym, a):
        return str(math.floor(a * 100000) / 100000)

    def price_to_precision(self, sym, p):
        return f"{p:.2f}"

    def feature_value(self, *args):
        return True

    def fetch_trading_fee(self, sym):
        return {"taker": 0.0045, "maker": 0.003}

    def create_order(self, sym, otype, side, amount, price=None, params=None):
        self.create_calls += 1
        if self.fail_first and self.create_calls == 1:
            self._n += 1
            oid = f"o{self._n}"
            self.orders[oid] = self._make(oid, sym, otype, side, amount, price, params)  # it DID go through
            raise self.fail_first
        self._n += 1
        oid = f"o{self._n}"
        if (params or {}).get("stopLossPrice") is not None:
            self.orders[oid] = {"id": oid, "clientOrderId": (params or {}).get("clientOrderId"), "symbol": sym, "type": otype, "side": side,
                                "amount": amount, "price": None, "filled": 0, "remaining": amount, "status": "open", "stopLossPrice": params["stopLossPrice"]}
        else:
            self.orders[oid] = self._make(oid, sym, otype, side, amount, price, params)
        return dict(self.orders[oid])

    def _make(self, oid, sym, otype, side, amount, price, params):
        px = price if otype == "limit" else 80_000.0
        filled = amount * self.fill_fraction
        base, _, quote = sym.partition("/")
        fee_cost = filled * px * 0.0045
        fee = {"cost": fee_cost / px, "currency": base} if self.fee_currency == "base" else {"cost": fee_cost, "currency": quote}
        o = {
            "id": oid, "clientOrderId": (params or {}).get("clientOrderId"), "symbol": sym, "type": otype, "side": side,
            "amount": amount, "price": px, "average": px, "filled": filled, "remaining": amount - filled,
            "status": "closed" if filled >= amount else "open", "fee": fee, "info": {},
        }
        if side == "buy":
            self.balances[base] = self.balances.get(base, 0.0) + filled
        else:
            self.balances[base] = self.balances.get(base, 0.0) - filled
        return o

    def fetch_order(self, oid, sym=None, params=None):
        if oid is None and params and params.get("clientOrderId"):
            for o in self.orders.values():
                if o["clientOrderId"] == params["clientOrderId"]:
                    return dict(o)
            raise ccxt.OrderNotFound("none")
        return dict(self.orders[oid])

    def fetch_open_orders(self, sym=None, since=None, limit=None):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    def fetch_closed_orders(self, sym=None, since=None, limit=None):
        return [dict(o) for o in self.orders.values() if o["status"] != "open"]

    def cancel_order(self, oid, sym=None):
        self.orders[oid]["status"] = "canceled"
        self.cancelled.append(oid)
        return dict(self.orders[oid])

    def fetch_balance(self):
        return {"total": dict(self.balances), "free": dict(self.balances)}


def approval(rm, side="buy", amount=0.001, price=80_000.0, units=0.0):
    intent = OrderIntent(S, side, amount, price, "t", 79_000.0 if side == "buy" else None)
    ctx = RiskContext(1000.0, 0.0, 0.0, units, False, INFO, 1)
    a = rm.approve(intent, ctx)
    assert isinstance(a, Approval)
    return a


def rm():
    return RiskManager(RiskLimits(budget=1000, per_position_pct=100, risk_per_trade_pct=100, max_open_risk_pct=100), RiskRules(stop_atr_mult=2), 0.05)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def live(ex, **kw):
    ck = FakeClock()
    return LiveExecutor(rm(), ex, exchange_id="binanceus", fill_timeout_s=kw.pop("timeout", 5.0), poll_s=1.0, sleep=ck.sleep, clock=ck, **kw)


def test_live_full_fill_reports_real_price_fee_and_id():
    ex = FakeLive()
    lx = live(ex)
    a = approval(lx.risk)
    f = lx.execute(a, 80_000.0, 123)
    assert f is not None and f.mode == "BUY" and f.status == "filled" and f.order_id == "o1"
    assert f.price == pytest.approx(80_000.0 * 1.0025)  # marketable limit: never worse than the slippage cap
    assert f.slippage_pct == pytest.approx(0.25)
    assert f.fee == pytest.approx(f.notional * 0.0045)
    o = ex.orders["o1"]
    assert o["type"] == "limit" and o["clientOrderId"].startswith("cb")
    with pytest.raises(PermissionError):
        lx.execute(a, 80_000.0, 124)  # approval is single use


def test_live_fee_in_base_currency_is_converted_to_quote():
    ex = FakeLive(fee_currency="base")
    lx = live(ex)
    f = lx.execute(approval(lx.risk), 80_000.0, 1)
    assert f.fee == pytest.approx(f.notional * 0.0045, rel=1e-6)


def test_live_partial_fill_cancels_remainder_after_timeout():
    ex = FakeLive(fill_fraction=0.4)
    lx = live(ex, timeout=3.0)
    f = lx.execute(approval(lx.risk, amount=0.001), 80_000.0, 1)
    assert f is not None and f.status == "partial" and f.amount == pytest.approx(0.0004)
    assert ex.cancelled == ["o1"]


def test_live_unfilled_order_is_cancelled_and_returns_none():
    ex = FakeLive(fill_fraction=0.0)
    lx = live(ex, timeout=2.0)
    assert lx.execute(approval(lx.risk), 80_000.0, 1) is None
    assert ex.cancelled == ["o1"]


def test_live_retry_after_network_error_does_not_duplicate_order():
    ex = FakeLive(fail_first=ccxt.NetworkError("timeout"))
    lx = live(ex)
    f = lx.execute(approval(lx.risk), 80_000.0, 1)
    assert f is not None and f.order_id == "o1"
    assert len(ex.orders) == 1 and ex.create_calls == 1


def test_live_rejections_become_execution_errors():
    ex = FakeLive(fail_first=ccxt.InsufficientFunds("no money"))
    lx = live(ex)
    with pytest.raises(ExecutionError, match="insufficient funds"):
        lx.execute(approval(lx.risk), 80_000.0, 1)


def test_live_urgent_uses_market_order_and_cancel_ours_only_touches_our_orders():
    ex = FakeLive(fill_fraction=0.0)
    ex.orders["theirs"] = {"id": "theirs", "clientOrderId": "manual", "status": "open", "filled": 0, "remaining": 1, "symbol": S}
    lx = live(ex, timeout=1.0)
    lx.execute(approval(lx.risk, side="sell", units=0.001), 80_000.0, 1, urgent=True)
    assert ex.orders["o1"]["type"] == "market"
    ex.orders["o1"]["status"] = "open"
    assert lx.cancel_ours(S) == 1 and "theirs" not in lx.cancel_ours.__self__.exchange.cancelled


# ------------------------------------------------------------- BUY-mode trader


def make_buy(store=None, ex=None, clock=None, budget=100.0, **kw):
    store = store or Store(":memory:")
    store.set_setting("test.started_ms", T0 - 20 * DAY)
    ex = ex or FakeLive(balances={"USDT": 500.0, "BTC": 0.0})
    events = []
    clock = clock or Clock(T0)
    t = Trader(store, events.append, "binanceus", [S], cfg(**kw), RiskLimits(budget=budget, per_position_pct=100, daily_loss_pct=5, max_drawdown_pct=20, risk_per_trade_pct=100, max_open_risk_pct=100), mode=Mode.BUY, clock=clock, exchange=ex, fill_timeout_s=1.0)
    ck = FakeClock()
    t.executor._sleep = ck.sleep
    t.executor._clock = ck
    t.start()
    from cryptobot.core.events import MarketInfoEvent

    t.on_event(MarketInfoEvent("binanceus", S, 0.006, 0.004, 0.00001, 5.0, 0.00001, 0.01))
    from cryptobot.core.events import BookEvent, ConnectionEvent
    t.on_event(ConnectionEvent("binanceus", "connected"))
    px = rising()[-1]
    t.on_event(BookEvent("binanceus", S, clock.ms, px, px, 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1, True))
    return t, events, store, ex, clock


def test_buy_mode_trades_for_real_with_recorded_order_ids_and_actual_fees():
    t, ev, store, ex, clock = make_buy()
    feed_history(t, rising())
    f = fills(ev)
    assert len(f) == 1 and f[0].mode == "BUY" and f[0].order_id == "o1"
    row = store.load_trades("BUY")[0]
    assert row["order_id"] == "o1" and row["fill_price"] == pytest.approx(f[0].price) and row["slippage"] == pytest.approx(0.25, abs=1e-3)
    assert row["price"] == pytest.approx(f[0].price / 1.0025)  # expected vs actual recorded separately
    assert t.ledger.position(S).basis <= 100.0 + 1e-6  # fees, cap slippage, and real fee tier all inside the budget
    assert t._fee_override[S] == 0.0045  # the real fee tier was read from the exchange
    assert store.get_setting("buy.first_started_ms") == T0
    assert ex.balances["BTC"] == pytest.approx(t.ledger.position(S).units)


def test_buy_mode_partial_fill_tops_up_next_bar():
    t, ev, store, ex, clock = make_buy(ex=FakeLive(fill_fraction=0.5, balances={"USDT": 500.0}))
    feed_history(t, rising())
    p = t.ledger.position(S)
    assert fills(ev)[0].status == "partial" and p.held_e == pytest.approx(0.5, abs=0.02)
    ex.fill_fraction = 1.0
    t.on_event(__import__("cryptobot.core.events", fromlist=["BarsEvent"]).BarsEvent("binanceus", S, 86400, daily_bars(rising(81))))
    assert len(fills(ev)) == 2 and fills(ev)[1].reason == "signal add"
    assert p.basis <= 100.0 + 1e-6 and p.held_e == pytest.approx(1.0, abs=0.02)


def test_buy_mode_first_month_budget_cap_is_enforced():
    with pytest.raises(ValueError, match="first 30 days"):
        make_buy(budget=FIRST_MONTH_CAP + 1)


def test_buy_mode_reconciles_with_exchange_balance_on_restart():
    t, ev, store, ex, clock = make_buy()
    feed_history(t, rising())
    units = t.ledger.position(S).units
    t.stop()
    ex.balances["BTC"] = units * 0.5  # user sold half by hand
    t2, ev2, _, _, _ = make_buy(store=store, ex=ex, clock=clock)
    p2 = t2.ledger.position(S)
    assert p2.units == pytest.approx(units)  # do not invent a missing sale's proceeds/basis
    assert t2.status == "reconcile_required"
    assert any(isinstance(e, AlertEvent) and "reconciliation" in e.title.lower() for e in ev2)
    assert store.load_positions("BUY")


def test_buy_mode_emergency_stop_cancels_open_orders_then_market_sells():
    t, ev, store, ex, clock = make_buy()
    feed_history(t, rising())
    ex.orders["stale"] = {"id": "stale", "clientOrderId": "cbstale", "status": "open", "filled": 0, "remaining": 1, "symbol": S}
    n = t.emergency_stop()
    assert n == 1
    assert any(o.get("stopLossPrice") for o in ex.orders.values())
    sell = [o for o in ex.orders.values() if o.get("side") == "sell"][-1]
    assert sell["type"] == "market"
    assert not t.ledger.position(S).open and t.status == "stopped"


def test_buy_mode_order_failure_is_reported_not_fatal():
    ex = FakeLive(fail_first=ccxt.InsufficientFunds("broke"), balances={"USDT": 0.0})
    t, ev, store, _, clock = make_buy(ex=ex)
    feed_history(t, rising())
    assert not fills(ev) and any(isinstance(e, AlertEvent) and "reconciliation" in e.title.lower() for e in ev)
    assert t.status == "reconcile_required"


# ------------------------------------------------------------------ mode gate


def test_buy_gate_reasons_and_stats():
    store = Store(":memory:")
    now = T0
    ok, reasons, st = buy_gate(store, now, 100.0, has_key=False, key_can_trade=None)
    assert not ok and len(reasons) == 2 and "14 days" in reasons[0] and "No API key" in reasons[1]
    store.set_setting("test.started_ms", now - (MIN_TEST_DAYS + 1) * DAY)
    ok, reasons, st = buy_gate(store, now, 300.0, has_key=True, key_can_trade=True)
    assert not ok and reasons == [f"For the first 30 days of BUY mode the budget may not exceed ${FIRST_MONTH_CAP:,.0f}. Lower it on the Risk screen."]
    ok, reasons, st = buy_gate(store, now, 250.0, has_key=True, key_can_trade=None)
    assert ok and st.days_in_test == pytest.approx(15.0)
    ok, reasons, _ = buy_gate(store, now, 100.0, has_key=True, key_can_trade=False)
    assert not ok and "cannot trade" in reasons[0]
    # stats reflect TEST trades and equity
    store.insert_trade(mode="TEST", exchange="x", symbol=S, side="buy", ts_ms=now, price=1, amount=1, fee=0.1, status="filled")
    store.insert_trade(mode="TEST", exchange="x", symbol=S, side="sell", ts_ms=now + 1, price=1, amount=1, fee=0.1, pnl=2.0, status="filled")
    store.insert_trade(mode="TEST", exchange="x", symbol=S, side="sell", ts_ms=now + 2, price=1, amount=1, fee=0.1, pnl=-1.0, status="filled")
    store.add_equity("TEST", now, 100, 100, 0)
    store.add_equity("TEST", now + 1, 90, 90, 0)
    store.add_equity("TEST", now + 2, 101, 101, 0)
    st = simulation_stats(store, now)
    assert st.trades == 2 and st.win_rate == 0.5 and st.total_pnl == 1.0 and st.max_drawdown == pytest.approx(0.1) and st.equity == 101


# -------------------------------------------------------------------- reports


def _t(mode, side, ts, px, amt, fee=0.0, pnl=None, status="filled", slippage=0.0, tid=None):
    return {"id": tid, "mode": mode, "exchange": "binanceus", "symbol": S, "side": side, "ts_ms": ts, "price": px, "fill_price": px, "amount": amt, "fee": fee, "fee_currency": "USDT", "slippage": slippage, "reason": "t", "pnl": pnl, "position_id": None, "order_id": None, "status": status}


def test_period_summary_and_comparison():
    d0 = 1_760_000_000_000
    trades = [_t("TEST", "buy", d0, 100, 1, 0.5), _t("TEST", "sell", d0 + 3600_000, 110, 1, 0.5, pnl=9.0), _t("TEST", "buy", d0 + 2 * DAY, 100, 1, 0.5), _t("TEST", "sell", d0 + 3 * DAY, 95, 1, 0.5, pnl=-6.0)]
    daily = period_summary(trades, "daily")
    assert len(daily) == 3 and daily[0]["round_trips"] == 1 and daily[0]["pnl"] == 9.0 and daily[0]["win_rate"] == 1.0
    monthly = period_summary(trades, "monthly")
    assert len(monthly) == 1 and monthly[0]["pnl"] == 3.0 and monthly[0]["fees"] == 2.0
    buys = [_t("BUY", "buy", d0 + 2 * DAY, 100.5, 1, 0.6, slippage=0.3), _t("BUY", "sell", d0 + 3 * DAY, 94.7, 1, 0.6, pnl=-7.0, slippage=0.2, status="partial")]
    c = comparison(trades, buys)
    assert c["since_ms"] == d0 + 2 * DAY
    assert c["TEST"]["round_trips"] == 1 and c["TEST"]["pnl"] == -6.0
    assert c["BUY"]["pnl"] == -7.0 and c["BUY"]["avg_slippage_pct"] == pytest.approx(0.25) and c["BUY"]["partial_fills"] == 1


def test_tax_lots_fifo_and_exports(tmp_path):
    d0 = 1_700_000_000_000
    trades = [
        _t("BUY", "buy", d0, 100, 1.0, fee=1.0, tid=1),
        _t("BUY", "buy", d0 + DAY, 200, 1.0, fee=2.0, tid=2),
        _t("BUY", "sell", d0 + 400 * DAY, 300, 1.5, fee=3.0, tid=3),
    ]
    lots = tax_lots(trades)
    assert len(lots) == 2
    first, second = lots
    assert first["amount"] == 1.0 and first["cost_basis"] == pytest.approx(101.0) and first["term"] == "long"
    assert first["proceeds"] == pytest.approx((300 * 1.5 - 3.0) * (1.0 / 1.5))
    assert second["amount"] == 0.5 and second["cost_basis"] == pytest.approx(101.0) and second["term"] == "long"
    assert first["gain"] + second["gain"] == pytest.approx((300 * 1.5 - 3.0) - 101.0 - 101.0)
    n = export_tax_csv(trades, tmp_path / "tax.csv")
    assert n == 2 and (tmp_path / "tax.csv").read_text().startswith("asset,amount,date_acquired,date_sold,proceeds,cost_basis,gain,term,exchange")
    assert export_csv(trades, tmp_path / "all.csv") == 3
    assert "time_local" in (tmp_path / "all.csv").read_text().splitlines()[0]

