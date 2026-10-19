import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cryptobot.engine.executors import Fill, ReadOnlyExchange, SimulatedExecutor
from cryptobot.engine.ledger import Ledger
from cryptobot.engine.risk import Approval, MarketInfo, OrderIntent, Rejection, RiskContext, RiskLimits, RiskManager, floor_to_step
from cryptobot.strategy.config import RiskRules

S = "BTC/USDT"
INFO = MarketInfo(S, taker_fee=0.006, min_amount=0.00001, min_cost=5.0, amount_step=0.00001)


def ctx(cash=100.0, in_use=0.0, basis=0.0, units=0.0, halted=False, info=INFO):
    return RiskContext(cash, in_use, basis, units, halted, info, 1_000)


def rm(budget=100.0, per_pos=100.0, stop=2.0):
    return RiskManager(RiskLimits(budget=budget, per_position_pct=per_pos, risk_per_trade_pct=100, max_open_risk_pct=100), RiskRules(stop_atr_mult=stop))


def buy(amount, price=50_000.0, stop=49_000.0, reason="t"):
    return OrderIntent(S, "buy", amount, price, reason, stop)


def test_buy_within_limits_is_approved_and_sized_with_fees_inside_budget():
    r = rm()
    a = r.approve(buy(0.002), ctx())  # $100 requested
    assert isinstance(a, Approval)
    assert a.notional * 1.0005 * (1 + 0.006) <= 100.0 + 1e-9  # fee and assumed slippage fit inside the cap
    assert a.amount == floor_to_step(100.0 / 1.006 / (50_000 * 1.0005), 0.00001)


def test_buy_is_capped_by_remaining_budget_and_per_position_limit():
    r = rm(per_pos=10.0)
    a = r.approve(buy(0.002), ctx())
    assert isinstance(a, Approval) and a.notional * 1.006 <= 10.0 + 1e-9
    rej = r.approve(buy(0.002), ctx(in_use=10.0, basis=10.0, units=0.0002))
    assert isinstance(rej, Rejection) and "per-position limit" in rej.reason or "budget" in rej.reason
    rej2 = rm().approve(buy(0.002), ctx(cash=100.0, in_use=100.0, basis=100.0, units=0.002))
    assert isinstance(rej2, Rejection)


def test_buy_below_exchange_minimum_is_rejected_with_plain_reason():
    r = rm(budget=4.0)
    rej = r.approve(buy(0.00008), ctx(cash=4.0))
    assert isinstance(rej, Rejection) and "below the exchange minimum" in rej.reason


def test_stop_loss_is_mandatory_when_configured():
    r = rm(stop=2.0)
    assert isinstance(r.approve(OrderIntent(S, "buy", 0.001, 50_000, "t", None), ctx()), Rejection)
    assert isinstance(r.approve(OrderIntent(S, "buy", 0.001, 50_000, "t", 51_000), ctx()), Rejection)
    assert isinstance(rm(stop=0).approve(OrderIntent(S, "buy", 0.001, 50_000, "t", None), ctx()), Approval)


def test_halted_blocks_buys_but_allows_sells():
    r = rm()
    assert isinstance(r.approve(buy(0.001), ctx(halted=True)), Rejection)
    a = r.approve(OrderIntent(S, "sell", 0.001, 50_000, "stop"), ctx(units=0.001, halted=True))
    assert isinstance(a, Approval) and a.amount == 0.001


def test_sell_closes_dust_and_never_exceeds_units():
    r = rm()
    a = r.approve(OrderIntent(S, "sell", 1.0, 50_000, "exit"), ctx(units=0.0012345678))
    assert isinstance(a, Approval) and a.amount == 0.00123
    r.consume(a.token)  # a pending full close correctly reserves the inventory
    b = r.approve(OrderIntent(S, "sell", 0.0005, 50_000, "reduce"), ctx(units=0.0012345678))
    assert isinstance(b, Approval) and b.amount == pytest.approx(0.0005)


def test_approval_token_is_single_use_and_executor_refuses_forgeries():
    r = rm()
    a = r.approve(buy(0.001), ctx())
    ex = SimulatedExecutor(r, slippage_pct=0.1)
    f = ex.execute(a, 50_000.0, 1123)
    assert isinstance(f, Fill) and f.price == pytest.approx(50_050.0) and f.fee == pytest.approx(f.notional * 0.006)
    with pytest.raises(PermissionError):
        ex.execute(a, 50_000.0, 124)  # reuse
    forged = Approval(a.intent, a.amount, 0.0, "deadbeef", 0)
    with pytest.raises(PermissionError):
        ex.execute(forged, 50_000.0, 125)


def test_read_only_exchange_blocks_every_order_method():
    class Fake:
        def fetch_balance(self):
            return {"USDT": 1}

        def create_order(self, *a, **k):
            raise AssertionError("must never be called")

        def cancel_all_orders(self, *a, **k):
            raise AssertionError("must never be called")

        def withdraw(self, *a, **k):
            raise AssertionError("must never be called")

    ro = ReadOnlyExchange(Fake())
    assert ro.fetch_balance() == {"USDT": 1}
    for name in ("create_order", "create_market_buy_order", "cancel_all_orders", "withdraw", "transfer"):
        with pytest.raises(PermissionError):
            getattr(ro, name)


# --------------------------------------------------------------- property test


@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["buy", "sell"]),
            st.floats(0.0, 3.0),  # requested spend as a multiple of the budget (buys) / fraction of units (sells)
            st.floats(1_000.0, 100_000.0),  # price
        ),
        min_size=1,
        max_size=60,
    ),
    st.floats(1.0, 100.0),
)
def test_money_at_risk_never_exceeds_budget(actions, per_pos):
    budget = 100.0
    r = RiskManager(RiskLimits(budget=budget, per_position_pct=per_pos, risk_per_trade_pct=100, max_open_risk_pct=100), RiskRules(stop_atr_mult=0))
    ex = SimulatedExecutor(r, slippage_pct=0.05)
    led = Ledger(cash=budget)
    info = MarketInfo(S, taker_fee=0.006, min_amount=0.0, min_cost=0.0, amount_step=0.0)
    for side, x, price in actions:
        p = led.position(S)
        c = RiskContext(led.cash, led.in_use(), p.basis, p.units, False, info, 1)
        if side == "buy":
            intent = OrderIntent(S, "buy", x * budget / price, price, "t", None)
        else:
            if p.units <= 0:
                continue
            intent = OrderIntent(S, "sell", p.units * min(1.0, x), price, "t")
        res = r.approve(intent, c)
        if isinstance(res, Rejection):
            continue
        led.apply_fill(ex.execute(res, price, 1))
        assert led.cash >= -1e-6
        assert led.in_use() <= budget + 1e-6
        assert led.position(S).basis <= budget * per_pos / 100 + 1e-6

