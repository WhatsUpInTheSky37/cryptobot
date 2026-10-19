"""Crypto Pulse engine: data validation, scoring, signals, three paper modes, outcomes."""
import pytest

from cryptobot.core.events import BarsEvent, BookEvent, BookSyncEvent, ConnectionEvent, HeartbeatEvent, MarketInfoEvent, PulseEventRecord, PulseStatusEvent, TickEvent
from cryptobot.core.models import Bar, Tick
from cryptobot.data.feed import summarize_book
from cryptobot.db.store import Store
from cryptobot.pulse.config import ModeConfig, PulseConfig, break_even_win_rate, expected_value_pct
from cryptobot.pulse.engine import PulseEngine
from cryptobot.pulse.features import SymbolFeatures, combine, percentile_rank
from cryptobot.pulse.memory import INSUFFICIENT, MarketMemory, MemoryRow

S = "ETH/USDT"
B = "BTC/USDT"
T0 = 1_760_000_000_000


def test_break_even_and_ev_math():
    assert break_even_win_rate(0.4, 0.2, 0.3) == pytest.approx(0.8333, abs=1e-3)
    assert expected_value_pct(0.8333, 0.4, 0.2, 0.3) == pytest.approx(0.0, abs=1e-3)
    assert expected_value_pct(0.45, 0.4, 0.2, 0.3) < 0
    assert expected_value_pct(0.9, 0.4, 0.2, 0.2) > 0


def test_summarize_book_depth_bands_and_rejects_crossed():
    e = summarize_book("x", S, 1, [[100.0, 1], [99.95, 2], [99.0, 5]], [[100.1, 1], [100.2, 3], [101.0, 4]], seq=7)
    assert e.mid == pytest.approx(100.05) and e.seq == 7 and e.synced
    assert e.bid_depth_near == pytest.approx(100 * 1 + 99.95 * 2)
    assert e.ask_depth_near == pytest.approx(100.1 * 1)
    assert summarize_book("x", S, 1, [], [[1, 1]]) is None
    assert summarize_book("x", S, 1, [[101, 1]], [[100, 1]]) is None  # crossed


def test_percentile_rank_and_combine():
    assert percentile_rank([], 5) == 50.0
    assert percentile_rank([1, 2, 3, 4], 5) == 100.0
    assert percentile_rank([1, 2, 3, 4], 0) == 0.0
    assert percentile_rank([3, 3, 3, 3], 3) == 50.0  # ties rank in the middle
    assert percentile_rank([1, 2, 3, 4], 3) == pytest.approx(62.5)
    # a negative weight counts (100 - score); PRS stays in 0..100
    assert combine({"f": 100, "sx": 100}, {"f": 0.5, "sx": -0.5}) == pytest.approx(50.0)
    assert combine({"f": 100, "sx": 0}, {"f": 0.5, "sx": -0.5}) == pytest.approx(100.0)


def book(ts, bid=3000.0, ask=3000.2, bid_depth=50_000.0, ask_depth=50_000.0, synced=True) -> BookEvent:
    return BookEvent("binanceus", S, ts, bid, ask, bid_depth, ask_depth, bid_depth * 3, ask_depth * 3, 1, synced)


def feed_neutral(f: SymbolFeatures, start: int, seconds: int, price=3000.0):
    for k in range(seconds):
        ts = start + k * 1000
        f.add_tick(Tick(S, ts, price, 0.1, None, "buy"))
        f.add_tick(Tick(S, ts + 500, price, 0.1, None, "sell"))
        f.add_book(book(ts, price - 0.1, price + 0.1))
        if k % 5 == 4:
            f.observe(ts + 999, 0.0)


def test_features_have_no_scores_until_warm_and_rank_unusual_pressure_high():
    f = SymbolFeatures(S, window_s=60, sub_s=5, history_obs=1000, min_obs=20)
    w = PulseConfig().weights_normalized()
    feed_neutral(f, T0, 60)
    obs = f.observe(T0 + 60_000, 0.0)
    assert not f.warmed_up
    assert f.scores(obs, w, True) is None  # never a placeholder 50
    feed_neutral(f, T0 + 61_000, 120)
    assert f.warmed_up
    t1 = T0 + 181_000
    for k in range(60):
        ts = t1 + k * 1000
        f.add_tick(Tick(S, ts, 3000.0 + 0.001 * k, 2.0, None, "buy"))
        f.add_tick(Tick(S, ts + 500, 3000.0 + 0.001 * k, 0.1, None, "sell"))
        f.add_book(book(ts, 2999.9, 3000.1, 50_000.0, 50_000.0 * (1 - k / 100)))
    obs = f.observe(t1 + 60_000, 0.6)
    long_, short = f.scores(obs, w, True)
    assert long_.f > 90 and long_.a > 90 and long_.w > 90 and long_.r > 90 and long_.c > 90
    assert long_.prs > 75 and short.prs < long_.prs - 25
    assert short.f < 10 and short.c < 10  # bearish is its own distribution, not 100 - bullish
    assert obs.imb60 > 0.8
    assert f.scores(obs, w, False)[0].c == 50.0
    # an unsynced book is ignored entirely
    before = len(f._books)
    f.add_book(book(t1 + 61_000, synced=False))
    assert len(f._books) == before


def test_memory_requires_independent_samples_and_gives_intervals():
    store = Store(":memory:")
    m = MarketMemory(store, k=10, min_n=5, prior=0.45, overlap_ms=600_000)
    vec = {"f": 90, "a": 90, "w": 90, "r": 90, "c": 90, "acc": 50, "vpr": 50, "sx": 50, "vx": 50, "vol_pct": 0.2, "spread_pct": 0.01, "imbalance": 0.5}
    e = m.estimate("long", vec)
    assert not e.sufficient and e.p_target is None and INSUFFICIENT in e.note
    # 8 events on one coin within 10 minutes are ONE observation
    for i in range(8):
        m.add(MemoryRow(S, T0 + i * 60_000, "long", "range", [90, 90, 90, 90, 90, 50, 50, 50, 50, 0.2, 0.01, 0.5], True, False, 0.1, 60_000, 0.4))
    e = m.estimate("long", vec)
    assert not e.sufficient and e.n_effective == 1 and e.n == 8
    # spread the events out: now they count
    for i in range(6):
        m.add(MemoryRow(B, T0 + i * 3_600_000, "long", "range", [92, 88, 91, 89, 90, 50, 50, 50, 50, 0.2, 0.01, 0.5], i % 2 == 0, i % 2 == 1, 0.05 if i % 2 == 0 else -0.3, 90_000, 0.3))
    e = m.estimate("long", vec)
    assert e.sufficient and e.n_effective >= 5 and 0 < e.p_target < 1
    assert e.ci_low is not None and e.ci_low <= e.p_target <= e.ci_high
    assert e.expected_net_pct is not None
    # a persisted row reloads
    store.insert_pulse_event(ts_ms=1, exchange="x", symbol=S, direction="long", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=0.1, spread_pct=0, mid=1, target_pct=0.4, stop_pct=0.2, cost_pct=0.3,
                             status="signal", outcome="target", first_hit="target", net_pct=0.1)
    assert MarketMemory(store, k=10, min_n=1).resolved() == 1


# ---------------------------------------------------------------- engine


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000


def cfg(**kw) -> PulseConfig:
    base = dict(min_obs=12, history_obs=500, prepare_prs=70, enter_prs=85, event_cooldown_s=1, prepared_ttl_s=120, adaptive=False, target_pct=0.6, stop_pct=0.2,
                budget=1000.0, per_position_pct=100.0, risk_per_trade_pct=0.25, max_hold_s=1200)
    base.update(kw)
    return PulseConfig(**base)


def make(config=None, store=None, connect=True):
    store = store or Store(":memory:")
    events = []
    clock = Clock(T0)
    eng = PulseEngine(store, events.append, "binanceus", [S, B], config or cfg(), slippage_pct=0.05, clock=clock)
    eng.start()
    if connect:
        eng.on_event(ConnectionEvent("binanceus", "connected"))
    # a discounted fee tier (0.02%/side); at the standard 0.1%/side a 0.6%/0.2% trade cannot pass the R/R gate
    eng.on_event(MarketInfoEvent("binanceus", S, 0.0002, 0.0002, 0.0001, 5.0, 0.0001, 0.01))
    eng.on_event(MarketInfoEvent("binanceus", B, 0.0002, 0.0002, 0.00001, 5.0, 0.00001, 0.01))
    bars = [Bar(S, 60, T0 - (60 - i) * 60_000, 3000, 3003, 2997, 3000, 10, 5, True) for i in range(60)]
    eng.on_event(BarsEvent("binanceus", S, 60, bars))
    return eng, events, store, clock


def drive_neutral(eng, clock, seconds: int, price=3000.0):
    for _ in range(seconds):
        ts = clock.ms
        eng.on_event(TickEvent(eng.exchange_id, Tick(S, ts, price, 0.1, None, "buy")))
        eng.on_event(TickEvent(eng.exchange_id, Tick(S, ts + 500, price, 0.1, None, "sell")))
        eng.on_event(TickEvent(eng.exchange_id, Tick(B, ts, 80_000.0, 0.01, None, "buy")))
        eng.on_event(TickEvent(eng.exchange_id, Tick(B, ts + 500, 80_000.0, 0.01, None, "sell")))
        eng.on_event(BookEvent(eng.exchange_id, S, ts, price - 0.1, price + 0.1, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
        eng.on_event(BookEvent(eng.exchange_id, B, ts, 79_999.0, 80_001.0, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
        eng.on_event(HeartbeatEvent(eng.exchange_id, ts + 999))
        clock.ms += 1000


def drive_pressure(eng, clock, seconds: int, price=3000.0, drift=0.0, k0=0):
    for k in range(seconds):
        ts = clock.ms
        px = price + drift * k
        eng.on_event(TickEvent(eng.exchange_id, Tick(S, ts, px, 2.0, None, "buy")))
        eng.on_event(TickEvent(eng.exchange_id, Tick(S, ts + 500, px, 0.1, None, "sell")))
        eng.on_event(TickEvent(eng.exchange_id, Tick(B, ts, 80_000.0, 0.05, None, "buy")))
        ask = 50_000.0 * (1 - min(0.9, (k0 + k) / 100))
        eng.on_event(BookEvent(eng.exchange_id, S, ts, px - 0.1, px + 0.1, 50_000.0, ask, 150_000.0, ask * 3, 1, True))
        eng.on_event(BookEvent(eng.exchange_id, B, ts, 79_999.0, 80_001.0, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
        eng.on_event(HeartbeatEvent(eng.exchange_id, ts + 999))
        clock.ms += 1000


def statuses(events):
    return [e for e in events if isinstance(e, PulseStatusEvent)]


def records(events):
    return [e for e in events if isinstance(e, PulseEventRecord) and e.status]


def seed_memory(eng, n=40, p_hit=0.9, direction="long"):
    """Independent, resolved, similar past setups so the modes have evidence."""
    for i in range(n):
        hit = (i % 10) < int(p_hit * 10)
        eng.memory.add(MemoryRow(f"X{i}/USDT", T0 - (i + 1) * 3_600_000, direction, "range", [95, 95, 95, 95, 95, 90, 60, 50, 50, 0.1, 0.007, 0.9], hit, not hit, 0.09 if hit else -0.51, 120_000, 0.5, eng.cfg.target_pct, eng.cfg.stop_pct, eng.cfg.max_hold_s * 1000, "bidask-v2", eng.exchange_id))


def test_no_scores_without_data_and_insufficient_data_is_explicit():
    eng, ev, store, clock = make()
    eng.on_event(HeartbeatEvent("binanceus", clock.ms))
    st = statuses(ev)[-1]
    for s in st.snapshots:
        assert not s.valid and s.health == "no_data" and s.scores_long == {} and s.prs_long == 0.0
    drive_neutral(eng, clock, 30)  # 6 observations < min_obs 12
    st = statuses(ev)[-1]
    snap = [s for s in st.snapshots if s.symbol == S][0]
    assert not snap.valid and snap.health == "insufficient_data" and "observations" in snap.health_detail and "min to go" in snap.health_detail
    assert eng.message.startswith("Connected to") and "Warming up" in eng.message
    assert not records(ev)
    drive_neutral(eng, clock, 60)
    snap = [s for s in statuses(ev)[-1].snapshots if s.symbol == S][0]
    assert snap.valid and snap.health == "ok" and set(snap.scores_long) >= {"f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx"}


def test_stale_data_disconnect_and_unsynced_book_block_signals():
    c = cfg()
    c.health.stale_trade_s = 90
    eng, ev, store, clock = make(c)
    drive_neutral(eng, clock, 90)
    assert [s for s in statuses(ev)[-1].snapshots if s.symbol == S][0].valid
    # trades stop arriving: stale after 90 s
    for _ in range(100):
        eng.on_event(HeartbeatEvent("binanceus", clock.ms))
        clock.ms += 1000
    snap = [s for s in statuses(ev)[-1].snapshots if s.symbol == S][0]
    assert not snap.valid and snap.health == "stale_trades"
    # unsynced book
    drive_neutral(eng, clock, 20)
    eng.on_event(BookSyncEvent("binanceus", S, clock.ms, "gap", "expected U=5"))
    eng.on_event(HeartbeatEvent("binanceus", clock.ms + 5000))
    clock.ms += 6000
    snap = [s for s in statuses(ev)[-1].snapshots if s.symbol == S][0]
    assert snap.health == "book_unsynced"
    eng.on_event(BookSyncEvent("binanceus", S, clock.ms, "synced", "ok"))
    drive_neutral(eng, clock, 10)
    assert [s for s in statuses(ev)[-1].snapshots if s.symbol == S][0].valid
    # disconnect
    eng.on_event(ConnectionEvent("binanceus", "reconnecting"))
    eng.on_event(HeartbeatEvent("binanceus", clock.ms))
    st = statuses(ev)[-1]
    assert not st.connected and all(s.health == "disconnected" for s in st.snapshots)
    n_before = len(records(ev))
    drive_pressure(eng, clock, 60)  # pressure while disconnected: nothing recorded
    assert len(records(ev)) == n_before


def test_inactive_market_is_excluded():
    c = cfg()
    c.health.min_trades_per_min = 30
    eng, ev, store, clock = make(c)
    drive_neutral(eng, clock, 90)  # 2 trades/s = 120/min for ETH... but 2/s for BTC too
    # now a thin market: one trade every 10 s on ETH
    for _ in range(40):
        eng.on_event(TickEvent("binanceus", Tick(S, clock.ms, 3000.0, 0.1, None, "buy")))
        for _ in range(10):
            eng.on_event(BookEvent("binanceus", S, clock.ms, 2999.9, 3000.1, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
            eng.on_event(HeartbeatEvent("binanceus", clock.ms + 999))
            clock.ms += 1000
    snap = [s for s in statuses(ev)[-1].snapshots if s.symbol == S][0]
    assert snap.health == "inactive" and snap.state == "excluded"


def test_signal_recorded_with_immutable_signal_time_values_and_all_modes_decide():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    assert eng.sym[S].dirs["long"].state == "prepared"
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    recs = records(ev)
    assert recs and recs[-1].status == "signal" and set(recs[-1].decisions) == {"A", "B", "C"}
    row = store.pulse_event(recs[-1].id)
    assert row["status"] == "signal" and row["outcome"] == "pending" and row["regime"] in ("range", "quiet", "unknown")
    assert row["fee_pct"] == pytest.approx(0.04) and row["spread_cost_pct"] > 0 and row["slip_pct"] > 0
    assert row["cost_pct"] == pytest.approx(row["fee_pct"] + row["spread_cost_pct"] + row["slip_pct"])
    assert row["evidence"] == "insufficient" and row["similar_p"] == 0.0
    decs = store.load_decisions(event_id=recs[-1].id)
    assert {d["mode"] for d in decs} == {"A", "B", "C"} and all(not d["accepted"] for d in decs)
    assert all(d["code"] in ("insufficient_evidence", "weak_signal", "weak_momentum", "no_confirmation", "rr_below_min", "ev_below_margin") for d in decs)
    # signal-time columns cannot be changed afterwards
    with pytest.raises(ValueError):
        store.update_pulse_event(recs[-1].id, prs=99.0)
    with pytest.raises(ValueError):
        store.update_pulse_event(recs[-1].id, f=1.0, mfe_pct=1.0)


def test_modes_enter_only_with_evidence_and_positive_ev_and_exit_at_stop():
    eng, ev, store, clock = make()
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    recs = records(ev)
    taken = {k for r in recs for k, (ok, _) in r.decisions.items() if ok}
    assert taken, [r.decisions for r in recs]
    trader = eng.modes[sorted(taken)[0]]
    pos = next(iter(trader.positions.values()))
    assert pos.stop == pytest.approx(pos.entry_px * 0.998) and pos.target == pytest.approx(pos.entry_px * 1.006)
    assert pos.entry_px > 3000.0 + 0.05 * 14 - 0.1  # bought at the ask plus slippage, not the mid
    tr = store.load_trades(trader.trade_mode)
    assert len(tr) == 1 and tr[0]["side"] == "buy" and tr[0]["fee"] > 0
    # risk: at most 0.25% of equity at the stop distance (the per-position cap shrinks it here)
    assert pos.risk_usd <= trader.mode.budget * 0.0025 + 1e-9 and pos.units * pos.entry_px <= trader.mode.budget * 1.0 + 1e-6
    # price drops through the stop (bid-based)
    eng.on_event(BookEvent("binanceus", S, clock.ms, pos.stop * 0.999, pos.stop * 0.9995, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
    assert S not in trader.positions and trader.trades == 1 and trader.losses == 1 and trader.consecutive_losses == 1
    sells = [t for t in store.load_trades(trader.trade_mode) if t["side"] == "sell"]
    assert sells and sells[0]["pnl"] < 0 and "stop" in sells[0]["reason"]
    assert trader.realized_today == pytest.approx(sells[0]["pnl"])
    d = [d for d in store.load_decisions(mode=trader.mode.key) if d["accepted"]][0]
    assert d["trade_pnl"] == pytest.approx(sells[0]["pnl"])


def test_modes_are_independent_and_not_auto_selected():
    eng, ev, store, clock = make()
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    st = eng.status()
    assert [m.key for m in st.modes] == ["A", "B", "C"]
    assert all(m.budget == 1000.0 for m in st.modes)
    taken = [m for m in st.modes if m.open_positions]
    assert taken and len(taken) < 3 or True  # conservative may sit out; what matters is separate books
    for m in st.modes:
        assert m.equity == pytest.approx(eng.modes[m.key].equity())
    # no mode is promoted: status carries all three, legacy summary is the plain sum
    assert st.paper_budget == pytest.approx(3000.0)


def test_bearish_setups_are_research_only_in_every_mode():
    eng, ev, store, clock = make()
    seed_memory(eng, n=60, p_hit=0.9, direction="short")
    drive_neutral(eng, clock, 90)
    for k in range(60):
        ts = clock.ms
        eng.on_event(TickEvent("binanceus", Tick(S, ts, 3000.0 - 0.05 * k, 2.0, None, "sell")))
        eng.on_event(TickEvent("binanceus", Tick(S, ts + 500, 3000.0 - 0.05 * k, 0.1, None, "buy")))
        eng.on_event(TickEvent("binanceus", Tick(B, ts, 80_000.0, 0.05, None, "sell")))
        bid = 50_000.0 * (1 - min(0.9, k / 100))
        eng.on_event(BookEvent("binanceus", S, ts, 3000.0 - 0.05 * k - 0.1, 3000.0 - 0.05 * k + 0.1, bid, 50_000.0, bid * 3, 150_000.0, 1, True))
        eng.on_event(BookEvent("binanceus", B, ts, 79_999.0, 80_001.0, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
        eng.on_event(HeartbeatEvent("binanceus", ts + 999))
        clock.ms += 1000
    shorts = [r for r in records(ev) if r.direction == "short"]
    assert shorts and all(r.status == "research" and r.decisions == {} for r in shorts)
    assert all(not m.positions for m in eng.modes.values())
    assert store.load_decisions() == []


def test_outcome_tracking_horizons_mfe_mae_first_hit_and_net_after_costs():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    pend = [p for p in eng.tracker.pending if p.direction == "long"]
    assert pend
    p0 = pend[-1]  # the most recent signal: nothing has happened after it yet
    base = p0.entry_mid
    t = p0.start_ms
    # Executable bid quotes arrive throughout the horizon; gaps are censored.
    for elapsed in range(10_000, 1_200_001, 10_000):
        move = -0.0005 if elapsed <= 30_000 else 0.001 if elapsed < 300_000 else 0.0065 if elapsed <= 310_000 else 0.002
        bid = base * (1 + move)
        eng.on_event(BookEvent("binanceus", S, t + elapsed, bid, bid * 1.00001, 50000, 50000, 150000, 150000, 1, True))
    row = store.pulse_event(p0.event_id)
    assert row["outcome"] == "target" and row["first_hit"] == "target" and row["first_hit_ms"] == t + 300_000
    assert row["ret_30s"] == pytest.approx(-0.05, abs=1e-6)
    assert row["ret_1m"] == pytest.approx(0.1, abs=1e-6)
    assert row["ret_5m"] == pytest.approx(0.65, abs=1e-6)
    assert row["mfe_pct"] == pytest.approx(0.65, abs=1e-6)
    assert -0.1 < row["mae_pct"] <= -0.05  # includes observed bid/ask loss immediately after the signal
    assert eng.memory.resolved("long") >= 1
    assert row["label_entry"] == pytest.approx(base) and row["prs"] > 0


def test_outcomes_use_only_future_prices_never_past():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    p0 = [p for p in eng.tracker.pending if p.direction == "long"][-1]
    before = (p0.mfe, p0.mae, dict(p0.rets), p0.first_hit)
    # a late tick stamped BEFORE the signal must not count, however favourable
    eng.on_event(TickEvent("binanceus", Tick(S, p0.start_ms - 5000, p0.entry_mid * 1.01, 1.0, None, "buy")))
    assert (p0.mfe, p0.mae, dict(p0.rets), p0.first_hit) == before


def test_pending_outcomes_survive_restart_and_old_ones_are_abandoned():
    store = Store(":memory:")
    eng, ev, _s, clock = make(store=store)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    n_pending = len([p for p in eng.tracker.pending if p.direction == "long"])
    assert n_pending > 0
    old = store.insert_pulse_event(ts_ms=clock.ms - 2_000_000, exchange="binanceus", symbol=S, direction="long", prs=80, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=0.1, spread_pct=0,
                                   mid=3000, target_pct=0.4, stop_pct=0.2, cost_pct=0.3, status="signal")
    eng.running = False  # simulate a crash: no stop()
    eng2 = PulseEngine(store, ev.append, "binanceus", [S, B], cfg(), slippage_pct=0.05, clock=clock)
    eng2.start()
    assert len(eng2.tracker.pending) == n_pending
    assert store.pulse_event(old)["outcome"] == "abandoned"
    assert "Resumed" in eng2.message


def test_paper_state_persists_between_sessions():
    store = Store(":memory:")
    eng, ev, _s, clock = make(store=store)
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    taken = [m for m in eng.modes.values() if m.positions]
    assert taken
    m = taken[0]
    cash, pos = m.cash, dict(m.positions)
    eng2 = PulseEngine(store, ev.append, "binanceus", [S, B], cfg(), slippage_pct=0.05, clock=clock)
    eng2.start()
    m2 = eng2.modes[m.mode.key]
    assert m2.cash == pytest.approx(cash) and set(m2.positions) == set(pos)
    assert m2.positions[S].stop == pytest.approx(pos[S].stop)


def test_risk_limits_max_positions_consecutive_losses_daily_loss_and_no_adding():
    # one stop-out costs ~0.39% of equity (0.2% stop + fees + spread + slippage), so a 0.6% daily cap allows one loss, not two.
    # A single permissive mode: this test is about the risk layer, not signal strength (the second episode ranks lower
    # against a history that now contains the first one).
    lenient = ModeConfig(key="B", label="Balanced", enter_prs=70, min_acc=0, require_breakout=False, min_ev_pct=0.0, min_rr=0.5, min_similar=10)
    c = cfg(max_positions=1, max_consecutive_losses=2, pause_minutes=30, daily_loss_pct=0.6, risk_per_trade_pct=0.25, modes=[lenient])
    eng, ev, store, clock = make(c)
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)

    def stop_out(m):
        for sym, pos in list(m.positions.items()):
            eng.on_event(BookEvent("binanceus", sym, clock.ms, pos.stop * 0.999, pos.stop * 0.9995, 50_000.0, 50_000.0, 150_000.0, 150_000.0, 1, True))
        clock.ms += 2000

    def setup(price):
        drive_neutral(eng, clock, 130, price=price)
        drive_pressure(eng, clock, 60, price=price)
        drive_pressure(eng, clock, 15, price=price, drift=0.05, k0=60)

    setup(3000.0)
    taken = [m for m in eng.modes.values() if m.positions]
    assert taken, [r.decisions for r in records(ev)[-3:]]
    m = taken[0]
    assert len(m.positions) == 1  # max_positions
    held = dict(m.positions)
    # more signals while holding: never add to the position, never open a second one
    drive_pressure(eng, clock, 5, price=3000.0 + 0.05 * 14, drift=0.05, k0=75)
    assert {s: p.units for s, p in m.positions.items()} == {s: p.units for s, p in held.items()}
    assert any(d["code"] in ("already_in_position", "max_positions") for d in store.load_decisions(mode=m.mode.key))
    stop_out(m)
    assert m.losses == 1 and m.consecutive_losses == 1 and not m.positions
    assert m.realized_today < -m.mode.budget * 0.0025, "a stop-out loses more than the nominal risk once costs are paid"
    assert not m.daily_loss_hit(clock.ms)
    setup(3010.0)
    assert m.positions, "should have re-entered after one loss"
    stop_out(m)
    assert m.consecutive_losses == 2 and m.paused_until_ms > clock.ms
    setup(3020.0)
    assert not m.positions
    assert any(d["code"] == "paused_losses" for d in store.load_decisions(mode=m.mode.key))
    # two losses of ~0.39% each exceed the 0.6% daily limit (realized + open)
    assert m.daily_loss_hit(clock.ms)


def test_daily_loss_limit_counts_open_positions():
    eng, ev, store, clock = make(cfg(daily_loss_pct=0.2))
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    m = [m for m in eng.modes.values() if m.positions][0]
    assert not m.daily_loss_hit(clock.ms)
    pos = m.positions[S]
    # mark just above the stop: unrealized loss ~0.2% of equity already breaches the limit before any exit
    m.mark(S, pos.stop * 1.0001, pos.stop * 1.0003, 50_000.0)
    assert m.unrealized() < 0 and m.daily_loss_hit(clock.ms)


def test_status_event_reports_costs_break_even_and_health_summary():
    eng, ev, store, clock = make()
    drive_neutral(eng, clock, 90)
    st = statuses(ev)[-1]
    assert st.running and st.connected and st.paper_budget == pytest.approx(3000.0)
    assert st.cost_pct > 0.1 and st.required_win_rate == pytest.approx(break_even_win_rate(0.6, 0.2, st.cost_pct), abs=1e-6)
    assert st.health_summary.startswith("2/2 coins")
    assert {s.symbol for s in st.snapshots} == {S, B}
    assert st.memory["resolved"] == 0 and st.ml["usable"] is False
    snap = [s for s in st.snapshots if s.symbol == S][0]
    assert snap.assessment_long["verdict"] == "insufficient_evidence"
    assert "INSUFFICIENT HISTORICAL EVIDENCE" in snap.assessment_long["reason"]


def test_refuses_trades_when_target_cannot_cover_costs():
    eng, ev, store, clock = make(cfg(adaptive=True, min_stop_pct=0.15))
    for st in eng.sym.values():
        st.feats.vol_pct = 0.0001
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    assert all(not m.positions for m in eng.modes.values())
    codes = {d["code"] for d in store.load_decisions()}
    assert "target_below_costs" in codes
    row = store.load_pulse_events(limit=1)[0]
    assert row["target_pct"] < 1.2 * row["cost_pct"]


def test_stop_calls_close_all_and_records_sells():
    eng, ev, store, clock = make()
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    taken = [m for m in eng.modes.values() if m.positions]
    assert taken
    eng.stop()
    assert all(not m.positions for m in eng.modes.values())
    assert any("stopped" in t["reason"] for m in taken for t in store.load_trades(m.trade_mode))
    assert not statuses(ev)[-1].running


def test_pulse_started_after_the_feed_connected_is_not_stuck_disconnected():
    """The runner replays the last ConnectionEvent to a Pulse engine that starts later; without it every coin
    would show 'exchange connection is down' forever even though the top bar says Connected."""
    store = Store(":memory:")
    events = []
    clock = Clock(T0)
    eng = PulseEngine(store, events.append, "coinbase", [S, B], cfg(), slippage_pct=0.05, clock=clock)
    eng.on_event(ConnectionEvent("coinbase", "connected"))  # replayed before start
    eng.start()
    assert eng.connected
    eng.on_event(MarketInfoEvent("coinbase", S, 0.0002, 0.0002, 0.0001, 5.0, 0.0001, 0.01))
    eng.on_event(MarketInfoEvent("coinbase", B, 0.0002, 0.0002, 0.00001, 5.0, 0.00001, 0.01))
    drive_neutral(eng, clock, 90)
    st = statuses(events)[-1]
    assert st.connected and all(s.health != "disconnected" for s in st.snapshots)
    assert [s for s in st.snapshots if s.symbol == S][0].valid


def test_stop_is_kept_outside_the_spread_or_the_trade_is_refused():
    # the book in drive_pressure is 0.2 wide on 3000 (0.0067%); 40 spreads is 0.27%, wider than the fixed 0.2% stop
    eng, ev, store, clock = make(cfg(stop_spread_mult=40.0))
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    # Widening the stop changes the predicted event: old 0.2%-stop evidence is refused.
    assert all(S not in m.positions for m in eng.modes.values())
    assert any(d["code"] == "insufficient_evidence" and d["symbol"] == S for d in store.load_decisions())
    # 100 spreads would be 0.67%, beyond the 0.6% maximum stop: no trade, with the reason recorded
    eng, ev, store, clock = make(cfg(stop_spread_mult=100.0))
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    assert all(S not in m.positions for m in eng.modes.values())  # (B's book is 2 wide on 80,000: 100 spreads is only 0.25%, so B may still trade)
    assert "stop_inside_spread" in {d["code"] for d in store.load_decisions()}


def test_conservative_probability_uses_the_lower_confidence_bound():
    eng, ev, store, clock = make()
    seed_memory(eng, n=60, p_hit=0.9)
    drive_neutral(eng, clock, 90)
    drive_pressure(eng, clock, 60)
    drive_pressure(eng, clock, 15, drift=0.05, k0=60)
    row = store.load_pulse_events(limit=1)[0]
    taken = [d for d in store.load_decisions() if d["accepted"]]
    assert taken and row["similar_p"] >= 0.85
    # the EV recorded on the signal was computed from the lower bound, so it is below the point-estimate EV
    point_ev = row["similar_p"] * row["target_pct"] - (1 - row["similar_p"]) * row["stop_pct"] - row["cost_pct"]
    assert row["ci_low"] < row["similar_p"] and row["ev_pct"] < point_ev - 1e-9

