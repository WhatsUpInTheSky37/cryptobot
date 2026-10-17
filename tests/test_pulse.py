import pytest

from cryptobot.core.events import BookEvent, HeartbeatEvent, MarketInfoEvent, PulseEventRecord, PulseStatusEvent, TickEvent
from cryptobot.core.models import Tick
from cryptobot.data.feed import summarize_book
from cryptobot.db.store import Store
from cryptobot.pulse.config import PulseConfig, break_even_win_rate, expected_value_pct
from cryptobot.pulse.engine import PulseEngine
from cryptobot.pulse.features import SymbolFeatures, percentile_rank
from cryptobot.pulse.memory import MarketMemory

S = "ETH/USDT"
T0 = 1_760_000_000_000


def test_break_even_and_ev_math():
    # 0.4% target, 0.2% stop, 0.3% round-trip cost: need p = 0.5/0.6 = 83%
    assert break_even_win_rate(0.4, 0.2, 0.3) == pytest.approx(0.8333, abs=1e-3)
    assert expected_value_pct(0.8333, 0.4, 0.2, 0.3) == pytest.approx(0.0, abs=1e-3)
    assert expected_value_pct(0.45, 0.4, 0.2, 0.3) < 0  # the proposal's own example loses at these costs
    assert expected_value_pct(0.9, 0.4, 0.2, 0.2) > 0


def test_summarize_book_depth_bands():
    e = summarize_book("x", S, 1, [[100.0, 1], [99.95, 2], [99.0, 5]], [[100.1, 1], [100.2, 3], [101.0, 4]])
    assert e.mid == pytest.approx(100.05)
    assert e.bid_depth_near == pytest.approx(100 * 1 + 99.95 * 2)
    assert e.ask_depth_near == pytest.approx(100.1 * 1)
    assert e.ask_depth_wide == pytest.approx(100.1 + 100.2 * 3)
    assert summarize_book("x", S, 1, [], [[1, 1]]) is None


def test_percentile_rank():
    assert percentile_rank([], 5) == 50.0
    assert percentile_rank([1, 2, 3, 4], 5) == 100.0
    assert percentile_rank([1, 2, 3, 4], 0) == 0.0
    assert percentile_rank([1, 2, 3, 4], 3) == 50.0


def book(ts, bid=3000.0, ask=3000.2, bid_depth=50_000.0, ask_depth=50_000.0) -> BookEvent:
    return BookEvent("binanceus", S, ts, bid, ask, bid_depth, ask_depth, bid_depth * 3, ask_depth * 3)


def feed_neutral(f: SymbolFeatures, start: int, seconds: int, price=3000.0):
    """Balanced flow, flat book, one observation every 5 s."""
    for k in range(seconds):
        ts = start + k * 1000
        f.add_tick(Tick(S, ts, price, 0.1, None, "buy"))
        f.add_tick(Tick(S, ts + 500, price, 0.1, None, "sell"))
        f.add_book(book(ts, price - 0.1, price + 0.1))
        if k % 5 == 4:
            f.observe(ts + 999, 0.0)


def test_features_rank_unusual_pressure_high_and_warm_up_is_neutral():
    f = SymbolFeatures(S, window_s=60, sub_s=5, history_obs=1000, min_obs=20)
    w = PulseConfig().weights()
    feed_neutral(f, T0, 60)
    obs = f.observe(T0 + 60_000, 0.0)
    assert not f.warmed_up
    long_, short = f.scores(obs, w, True)
    assert long_.prs == 50 and short.prs == 50  # neutral until warmed up
    feed_neutral(f, T0 + 61_000, 120)
    assert f.warmed_up
    # now: heavy aggressive buying, price barely moves, asks withdrawing
    t1 = T0 + 181_000
    for k in range(60):
        ts = t1 + k * 1000
        f.add_tick(Tick(S, ts, 3000.0 + 0.001 * k, 2.0, None, "buy"))
        f.add_tick(Tick(S, ts + 500, 3000.0 + 0.001 * k, 0.1, None, "sell"))
        f.add_book(book(ts, 2999.9, 3000.1, 50_000.0, 50_000.0 * (1 - k / 100)))
    obs = f.observe(t1 + 60_000, 0.6)
    long_, short = f.scores(obs, w, True)
    assert long_.f > 90 and long_.a > 90 and long_.w > 90 and long_.r > 90 and long_.c > 90
    assert long_.prs > 90 and short.prs < 40
    assert obs.imb60 > 0.8
    # with no related market the confirmation score is neutral
    assert f.scores(obs, w, False)[0].c == 50.0


def test_memory_prior_until_enough_then_similarity():
    store = Store(":memory:")
    m = MarketMemory(store, k=10, min_n=5, prior=0.45)
    assert m.estimate("long", {"f": 90, "a": 90, "w": 90, "r": 90, "c": 90, "vol_pct": 0.2}) == (0.45, 0)
    for i in range(8):
        strong = i < 4
        vec = {"f": 95 if strong else 60, "a": 95 if strong else 60, "w": 95 if strong else 60, "r": 95 if strong else 60, "c": 95 if strong else 60, "vol_pct": 0.2}
        m.add("long", vec, hit_target=strong)
    p, n = m.estimate("long", {"f": 94, "a": 94, "w": 94, "r": 94, "c": 94, "vol_pct": 0.2})
    assert n == 8 and p > 0.4
    assert m.hit_rate("long") == 0.5
    # persisted rows reload
    store.insert_pulse_event(ts_ms=1, exchange="x", symbol=S, direction="long", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=0.1, spread_pct=0, mid=1, target_pct=0.4, stop_pct=0.2, cost_pct=0.3, status="research", outcome="target")
    m2 = MarketMemory(store, k=10, min_n=1, prior=0.45)
    assert m2.resolved() == 1


# ---------------------------------------------------------------- engine


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000


def cfg(**kw) -> PulseConfig:
    base = dict(min_obs=12, history_obs=500, prepare_prs=70, enter_prs=85, event_cooldown_s=1, prepared_ttl_s=120, adaptive=False, target_pct=0.4, stop_pct=0.2, budget=1000.0, per_position_pct=100.0, risk_per_trade_pct=1.0, research_mode=True, max_hold_s=1200)
    base.update(kw)
    return PulseConfig(**base)


def make(config=None, store=None):
    store = store or Store(":memory:")
    events = []
    clock = Clock(T0)
    eng = PulseEngine(store, events.append, "binanceus", [S, "BTC/USDT"], config or cfg(), slippage_pct=0.05, clock=clock)
    eng.start()
    eng.on_event(MarketInfoEvent("binanceus", S, 0.001, 0.001, 0.0001, 5.0, 0.0001, 0.01))
    return eng, events, store, clock


def drive_neutral(eng, clock, seconds: int, price=3000.0):
    for _ in range(seconds):
        ts = clock.ms
        eng.on_event(TickEvent("binanceus", Tick(S, ts, price, 0.1, None, "buy")))
        eng.on_event(TickEvent("binanceus", Tick(S, ts + 500, price, 0.1, None, "sell")))
        eng.on_event(TickEvent("binanceus", Tick("BTC/USDT", ts, 80_000.0, 0.01, None, "buy")))
        eng.on_event(TickEvent("binanceus", Tick("BTC/USDT", ts + 500, 80_000.0, 0.01, None, "sell")))
        eng.on_event(BookEvent("binanceus", S, ts, price - 0.1, price + 0.1, 50_000.0, 50_000.0, 150_000.0, 150_000.0))
        eng.on_event(HeartbeatEvent("binanceus", ts + 999))
        clock.ms += 1000


def drive_pressure(eng, clock, seconds: int, price=3000.0, drift=0.0, k0=0):
    """Aggressive buying, flat (or drifting) price, asks withdrawing; BTC agrees. k0 continues an earlier decay."""
    for k in range(seconds):
        ts = clock.ms
        px = price + drift * k
        eng.on_event(TickEvent("binanceus", Tick(S, ts, px, 2.0, None, "buy")))
        eng.on_event(TickEvent("binanceus", Tick(S, ts + 500, px, 0.1, None, "sell")))
        eng.on_event(TickEvent("binanceus", Tick("BTC/USDT", ts, 80_000.0, 0.05, None, "buy")))
        ask = 50_000.0 * (1 - min(0.9, (k0 + k) / 100))
        eng.on_event(BookEvent("binanceus", S, ts, px - 0.1, px + 0.1, 50_000.0, ask, 150_000.0, ask * 3))
        eng.on_event(HeartbeatEvent("binanceus", ts + 999))
        clock.ms += 1000


def statuses(events):
    return [e for e in events if isinstance(e, PulseStatusEvent)]


def records(events):
    return [e for e in events if isinstance(e, PulseEventRecord) and e.status]


def test_engine_prepares_then_enters_only_after_price_confirmation_and_exits_at_stop():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)  # warm-up: 18 observations
    assert eng.sym[S].state in ("idle", "monitor")
    drive_pressure(eng, clock, 60)  # pressure but price flat: should PREPARE, not enter
    assert eng.sym[S].state == "prepared"
    assert not eng.positions and store.count_trades("PULSE") == 0
    # release: price breaks above the absorption high while buying continues
    drive_pressure(eng, clock, 15, price=3000.0, drift=0.05, k0=60)
    assert S in eng.positions, eng.sym[S].note
    pos = eng.positions[S]
    assert pos.stop == pytest.approx(pos.entry_px * 0.998) and pos.target == pytest.approx(pos.entry_px * 1.004)
    assert pos.cost <= 1000.0 * 1.0 + 1e-6
    recs = records(ev)
    assert recs[-1].status == "entered" and "research mode" in recs[-1].reason  # EV is negative with no history
    tr = store.load_trades("PULSE")
    assert len(tr) == 1 and tr[0]["side"] == "buy"
    # price drops through the stop
    eng.on_event(TickEvent("binanceus", Tick(S, clock.ms, pos.stop * 0.999, 1.0, None, "sell")))
    assert S not in eng.positions and eng.trades == 1 and eng.trade_pnl < 0 and eng.consecutive_losses == 1
    assert store.load_trades("PULSE")[0]["reason"] == "pulse stop"
    row = store.load_pulse_events(limit=5)[0]
    assert row["status"] == "entered" and row["outcome"] == "stop" and row["trade_pnl"] == pytest.approx(eng.trade_pnl)
    assert eng.memory.resolved("long") >= 1


def test_engine_rejects_when_ev_negative_and_research_mode_off():
    eng, ev, store, clock = make(cfg(research_mode=False))
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    assert not eng.positions
    rej = [r for r in records(ev) if r.status == "rejected"]
    assert rej and "EV" in rej[-1].reason and "break-even" in rej[-1].reason
    assert store.load_pulse_events(limit=5)[0]["status"] == "rejected"


def test_engine_records_bearish_setups_but_never_trades_them():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)
    # heavy selling, flat price, bids withdrawing
    for k in range(60):
        ts = clock.ms
        eng.on_event(TickEvent("binanceus", Tick(S, ts, 3000.0, 2.0, None, "sell")))
        eng.on_event(TickEvent("binanceus", Tick(S, ts + 500, 3000.0, 0.1, None, "buy")))
        eng.on_event(TickEvent("binanceus", Tick("BTC/USDT", ts, 80_000.0, 0.05, None, "sell")))
        bid = 50_000.0 * (1 - min(0.9, k / 100))
        eng.on_event(BookEvent("binanceus", S, ts, 2999.9, 3000.1, bid, 50_000.0, bid * 3, 150_000.0))
        eng.on_event(HeartbeatEvent("binanceus", ts + 999))
        clock.ms += 1000
    shorts = [r for r in records(ev) if r.direction == "short"]
    assert shorts and all(r.status == "research" for r in shorts)
    assert not eng.positions and store.count_trades("PULSE") == 0


def test_engine_outcome_tracking_target_mfe_mae_and_expiry():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    pos = eng.positions[S]
    entry_mid = [p for p in eng.pending if p.direction == "long"][-1].entry_mid
    eng.on_event(TickEvent("binanceus", Tick(S, clock.ms, entry_mid * 0.9995, 1.0, None, "sell")))  # small dip
    eng.on_event(TickEvent("binanceus", Tick(S, clock.ms + 1, pos.target * 1.0001, 1.0, None, "buy")))
    assert S not in eng.positions and eng.trade_pnl > 0 and eng.consecutive_losses == 0
    row = [r for r in store.load_pulse_events(limit=10) if r["direction"] == "long" and r["status"] == "entered"][0]
    assert row["outcome"] == "target" and row["mfe_pct"] >= 0.4 and row["mae_pct"] <= -0.04
    # a research event that goes nowhere expires after the horizon
    eng2, ev2, store2, clock2 = make(cfg(max_hold_s=10))
    drive_neutral(eng2, clock2, 90)
    drive_pressure(eng2, clock2, 60)
    long_pending = [p for p in eng2.pending if p.direction == "long"]
    clock2.ms += 11_000
    eng2.on_event(TickEvent("binanceus", Tick(S, clock2.ms, 3000.0, 1.0, None, "buy")))
    assert all(store2.load_pulse_events(limit=50)[i]["outcome"] != "pending" for i in range(min(2, len(long_pending))))


def test_engine_risk_rules_max_positions_consecutive_losses_and_daily_loss():
    eng, ev, store, clock = make(cfg(max_positions=1, max_consecutive_losses=2, pause_minutes=30, daily_loss_pct=0.8, risk_per_trade_pct=1.0))
    drive_neutral(eng, clock, 90)

    def cycle(stop_out: bool, price: float):
        drive_neutral(eng, clock, 130, price=price)  # long enough for the previous setup to fade completely
        drive_pressure(eng, clock, 60, price=price)
        drive_pressure(eng, clock, 15, price=price, drift=0.05, k0=60)
        assert S in eng.positions, eng.sym[S].note
        pos = eng.positions[S]
        px = pos.stop * 0.999 if stop_out else pos.target * 1.001
        eng.on_event(TickEvent("binanceus", Tick(S, clock.ms, px, 1.0, None, "sell" if stop_out else "buy")))
        clock.ms += 2000

    cycle(True, 3000.0)
    cycle(True, 3010.0)
    assert eng.consecutive_losses == 2 and eng.paused_until_ms > clock.ms
    drive_neutral(eng, clock, 130, price=3020.0)
    drive_pressure(eng, clock, 60, price=3020.0)
    drive_pressure(eng, clock, 15, price=3020.0, drift=0.05, k0=60)
    assert not eng.positions
    assert "paused" in [r for r in records(ev) if r.status == "rejected"][-1].reason
    # daily loss: two stop-outs at 1% risk each exceed the 0.8% daily limit (the per-position cap shrinks the real risk to ~0.5% each)
    assert eng._daily_loss_hit()


def test_status_event_reports_costs_and_break_even():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 20)
    st = statuses(ev)[-1]
    assert st.running and st.paper_budget == 1000.0
    assert st.required_win_rate == pytest.approx(break_even_win_rate(0.4, 0.2, 2 * (0.1 + 0.05)), abs=1e-6)
    assert st.ev_pct < 0  # with the prior p=0.45, negative at these costs
    assert {s.symbol for s in st.snapshots} == {S, "BTC/USDT"}


def test_engine_refuses_trades_when_target_cannot_cover_costs():
    # adaptive exits with near-zero volatility collapse to the 0.15% floor: a 0.30% target vs 0.30% costs
    eng, ev, store, clock = make(cfg(adaptive=True, min_stop_pct=0.15))
    for st in eng.sym.values():
        st.feats.vol_pct = 0.0001
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    assert not eng.positions
    rej = [r for r in records(ev) if r.status == "rejected"]
    assert rej and "cannot cover costs" in rej[-1].reason
    st = statuses(ev)[-1]
    assert st.target_pct == pytest.approx(0.30) and st.cost_pct == pytest.approx(0.30) and st.required_win_rate >= 1.0
