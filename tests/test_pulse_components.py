"""Pulse components in isolation: health, costs, regime, memory rows, outcome tracker, analysis, store migration."""
import sqlite3

import pytest

from cryptobot.core.models import Bar
from cryptobot.db.store import Store
from cryptobot.pulse import analysis
from cryptobot.pulse.config import HealthConfig, PulseConfig
from cryptobot.pulse.costs import assess, expected_move_pct, net_return_pct, plan_exits, round_trip_costs, slippage_per_side_pct
from cryptobot.pulse.health import INSUFFICIENT, SymbolHealth
from cryptobot.pulse.outcomes import OutcomeTracker, Pending
from cryptobot.pulse.regime import classify

S = "ETH/USDT"
T0 = 1_760_000_000_000


# ---------------------------------------------------------------- health


def test_health_defaults_tolerate_a_thin_exchange():
    """Binance.US quiet pairs print well under a trade a minute; the defaults must not exclude them outright."""
    h = SymbolHealth(HealthConfig())
    h.min_obs = 0
    h.set_connected(True)
    for i in range(6):
        h.on_tick(T0 + i * 120_000)  # one trade every two minutes
        h.on_book(T0 + i * 120_000, True, i)
    now = T0 + 5 * 120_000 + 100_000
    h.on_book(now, True, 9)
    assert h.evaluate(now).ok


def test_health_states_in_order():
    h = SymbolHealth(HealthConfig(stale_trade_s=60, stale_book_s=10, min_trades_per_min=5, activity_window_s=120, inactive_cooldown_s=300))
    h.min_obs = 10
    assert h.evaluate(T0).status == "disconnected"
    h.set_connected(True)
    assert h.evaluate(T0).status == "no_data"
    for i in range(20):
        h.on_tick(T0 + i * 1000)
    assert h.evaluate(T0 + 20_000).status == "no_book"
    h.on_book(T0 + 20_000, synced=False, seq=1)
    assert h.evaluate(T0 + 20_000).status == "book_unsynced"
    h.on_sync("synced", "")
    h.on_book(T0 + 20_000, synced=True, seq=2)
    s = h.evaluate(T0 + 20_000)
    assert s.status == "insufficient_data" and s.label == INSUFFICIENT and "0/10" in s.detail and "min to go" in s.detail
    h.observations = 10
    assert h.evaluate(T0 + 20_000).ok
    assert h.evaluate(T0 + 35_000).status == "stale_book"
    h.on_book(T0 + 35_000, True, 3)
    assert h.evaluate(T0 + 90_000).status == "stale_trades"
    # back to healthy, then the market goes thin: 1 trade / 30 s
    for i in range(10):
        h.on_tick(T0 + 90_000 + i * 30_000)
        h.on_book(T0 + 90_000 + i * 30_000, True, 4 + i)
    s = h.evaluate(T0 + 90_000 + 9 * 30_000)
    assert s.status == "inactive" and "excluded" in s.detail
    assert h.evaluate(T0 + 90_000 + 9 * 30_000 + 1000).status == "inactive"  # cooldown sticks
    h.set_connected(False)
    assert h.evaluate(T0).status == "disconnected"


# ----------------------------------------------------------------- costs


def test_slippage_model_scales_with_size_versus_depth():
    assert slippage_per_side_pct(0, 10_000) == 0.02
    assert slippage_per_side_pct(1000, 0) == pytest.approx(0.12)  # no visible depth: a full band
    assert slippage_per_side_pct(5000, 10_000) == pytest.approx(0.02 + 0.1 * 0.5 / 2)
    assert slippage_per_side_pct(20_000, 10_000) == pytest.approx(0.02 + 0.05 + 0.1)
    assert slippage_per_side_pct(1_000_000, 10_000) == pytest.approx(0.02 + 0.05 + 0.4)  # capped


def test_round_trip_costs_add_fees_spread_and_slippage():
    c = round_trip_costs(0.001, 0.02, 500, 50_000, 50_000)
    assert c.fee_pct == pytest.approx(0.2) and c.spread_pct == pytest.approx(0.02)
    assert c.slippage_pct == pytest.approx(2 * (0.02 + 0.1 * 0.01 / 2))
    assert c.total_pct == pytest.approx(c.fee_pct + c.spread_pct + c.slippage_pct)
    assert net_return_pct(0.5, c) == pytest.approx(0.5 - c.total_pct)


def test_expected_move_and_exits_never_inflate_target_to_cover_fees():
    assert expected_move_pct(0.1, 10) == pytest.approx(0.1 * 10 ** 0.5)
    assert expected_move_pct(0.1, 10, evidence_move_pct=0.5) == 0.5
    assert expected_move_pct(0, 10) == 0.0
    kw = dict(stop_vol_mult=1.0, min_stop_pct=0.15, max_stop_pct=0.6, target_capture=0.6, max_target_pct=1.5)
    target, stop = plan_exits(0.3, 0.9, **kw)
    assert stop == pytest.approx(0.3) and target == pytest.approx(0.54)
    target, stop = plan_exits(0.01, 0.03, **kw)  # quiet market: stop floors, target stays tiny
    assert stop == 0.15 and target == pytest.approx(0.018)
    assert plan_exits(5.0, 50, **kw) == (1.5, 0.6)
    assert plan_exits(0.3, 0.9, adaptive=False, fixed_target=0.4, fixed_stop=0.2, **kw) == (0.4, 0.2)


def test_assess_verdicts():
    c = round_trip_costs(0.001, 0.02, 500, 50_000, 50_000)  # ~0.26%
    a = assess(0.2, 0.2, c, 0.9, min_ev_pct=0.02, min_rr=1.0)
    assert a.verdict == "target_below_costs"
    a = assess(0.6, 0.2, c, None, min_ev_pct=0.02, min_rr=1.0)
    assert a.verdict == "insufficient_evidence" and a.ev_pct is None and "INSUFFICIENT HISTORICAL EVIDENCE" in a.reason
    a = assess(0.6, 0.2, c, 0.5, min_ev_pct=0.02, min_rr=1.0)
    assert a.verdict == "ev_below_margin" and a.ev_pct < 0.02
    a = assess(0.6, 0.2, c, 0.9, min_ev_pct=0.02, min_rr=1.0)
    assert a.verdict == "rr_below_min"  # (0.6-0.26)/(0.2+0.26) = 0.74
    a = assess(1.0, 0.2, c, 0.9, min_ev_pct=0.02, min_rr=1.0)
    assert a.verdict == "ok" and a.rr_after_costs > 1.5 and a.ev_pct == pytest.approx(0.9 * 1.0 - 0.1 * 0.2 - c.total_pct)
    assert a.break_even_p == pytest.approx((0.2 + c.total_pct) / 1.2)


# ---------------------------------------------------------------- regime


def bars(prices, rng=0.002):
    return [Bar(S, 60, T0 + i * 60_000, p, p * (1 + rng), p * (1 - rng), p, 1, 1, True) for i, p in enumerate(prices)]


def test_regime_classification():
    assert classify(bars([100] * 10)).name == "unknown"
    assert classify(bars([100] * 60)).name == "range"
    up = [100 * (1.002 ** i) for i in range(60)]
    assert classify(bars(up)).name == "trend_up"
    assert classify(bars(list(reversed(up)))).name == "trend_down"
    calm = bars([100] * 50) + bars([100] * 10, rng=0.01)
    for i, b in enumerate(calm):
        b.ts_ms = T0 + i * 60_000
    assert classify(calm).name == "high_vol"
    quiet = bars([100] * 50, rng=0.01) + bars([100] * 10, rng=0.001)
    for i, b in enumerate(quiet):
        b.ts_ms = T0 + i * 60_000
    assert classify(quiet).name == "quiet"


# ------------------------------------------------------------- outcomes


def test_outcome_tracker_horizons_and_persistence():
    store = Store(":memory:")
    eid = store.insert_pulse_event(ts_ms=T0, exchange="x", symbol=S, direction="long", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=0.1, spread_pct=0, mid=100.0,
                                   target_pct=0.5, stop_pct=0.3, cost_pct=0.2, status="signal")
    done = []
    tr = OutcomeTracker(store, done.append)
    tr.add(Pending(eid, S, "long", T0, 100.0, 0.5, 0.3, 0.2, 0))
    tr.on_price(S, 100.2, T0 + 30_000)
    tr.on_price(S, 99.8, T0 + 60_000)
    tr.on_price(S, 99.69, T0 + 90_000)  # stop first
    tr.on_price(S, 101.0, T0 + 300_000)  # later target does not undo the stop
    tr.flush(T0 + 300_000)
    row = store.pulse_event(eid)
    assert row["outcome"] == "pending" and row["first_hit"] == "stop" and row["ret_1m"] == pytest.approx(-0.2) and row["mfe_pct"] == pytest.approx(1.0)
    tr.on_price(S, 100.5, T0 + 1_200_000)
    row = store.pulse_event(eid)
    assert done and row["outcome"] == "stop" and row["net_pct"] == pytest.approx(-0.3 - 0.2) and row["ret_20m"] == pytest.approx(0.5)
    assert row["time_to_mae_ms"] == 90_000 and row["time_to_mfe_ms"] == 300_000
    # short direction: moves are inverted
    eid2 = store.insert_pulse_event(ts_ms=T0, exchange="x", symbol=S, direction="short", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=0.1, spread_pct=0, mid=100.0,
                                    target_pct=0.5, stop_pct=0.3, cost_pct=0.2, status="research")
    tr.add(Pending(eid2, S, "short", T0, 100.0, 0.5, 0.3, 0.2, 0))
    tr.on_price(S, 99.4, T0 + 10_000)
    assert tr.pending[0].first_hit == "target"
    # restore after a "restart"
    tr2 = OutcomeTracker(store)
    restored, abandoned = tr2.restore(T0 + 100_000)
    assert restored == 1 and abandoned == 0 and tr2.pending[0].event_id == eid2
    restored, abandoned = OutcomeTracker(store).restore(T0 + 2_000_000)
    assert restored == 0 and abandoned == 1 and store.pulse_event(eid2)["outcome"] == "abandoned"


# ---------------------------------------------------------------- store


def test_store_migrates_old_pulse_table(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE pulse_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, exchange VARCHAR(32), symbol VARCHAR(32), direction VARCHAR(5), prs FLOAT, f FLOAT, a FLOAT, w FLOAT, r FLOAT, c FLOAT,
        imbalance FLOAT, vol_pct FLOAT, spread_pct FLOAT, mid FLOAT, target_pct FLOAT, stop_pct FLOAT, cost_pct FLOAT, similar_n INTEGER, similar_p FLOAT, ev_pct FLOAT, status VARCHAR(10), reason TEXT,
        outcome VARCHAR(8), mfe_pct FLOAT, mae_pct FLOAT, resolved_ts_ms INTEGER, trade_pnl FLOAT)""")
    con.execute("INSERT INTO pulse_events (ts_ms, exchange, symbol, direction, prs, f, a, w, r, c, imbalance, vol_pct, spread_pct, mid, target_pct, stop_pct, cost_pct, similar_n, similar_p, ev_pct, status, reason, outcome) "
                "VALUES (1, 'binanceus', 'ETH/USDT', 'long', 90, 1, 1, 1, 1, 1, 0, 0.1, 0, 3000, 0.4, 0.2, 0.3, 0, 0, 0, 'entered', 'x', 'target')")
    con.commit()
    con.close()
    store = Store(db)
    assert "pulse_events.regime" in store.migrated and "pulse_events.net_pct" in store.migrated
    row = store.load_pulse_events(limit=1)[0]
    assert row["regime"] == "unknown" and row["net_pct"] is None and row["confirmed"] in (0, False)
    assert store.count_pulse_events(resolved_only=True) == 1
    assert Store(db).migrated == []  # idempotent
    did = store.insert_decision(event_id=row["id"], ts_ms=1, mode="A", symbol="ETH/USDT", direction="long", accepted=False, code="weak_signal", reason="PRS 60")
    assert store.load_decisions(mode="A")[0]["id"] == did


def test_store_rejects_updates_to_signal_time_columns():
    store = Store(":memory:")
    eid = store.insert_pulse_event(ts_ms=1, exchange="x", symbol=S, direction="long", prs=90, f=1, a=1, w=1, r=1, c=1, imbalance=0, vol_pct=0.1, spread_pct=0, mid=1, target_pct=0.4, stop_pct=0.2, cost_pct=0.3, status="signal")
    for bad in ({"prs": 1}, {"mid": 2}, {"features_json": "{}"}, {"regime": "x"}, {"similar_p": 0.9}):
        with pytest.raises(ValueError):
            store.update_pulse_event(eid, **bad)
    store.update_pulse_event(eid, outcome="target", net_pct=0.1)
    assert store.pulse_event(eid)["outcome"] == "target"


# ------------------------------------------------------------- analysis


def test_mode_stats_profit_factor_drawdown_and_profitable_days():
    day = 86_400_000
    trades = [
        {"side": "buy", "ts_ms": T0, "fee": 0.1, "pnl": None},
        {"side": "sell", "ts_ms": T0 + 1000, "fee": 0.1, "pnl": 2.0},
        {"side": "sell", "ts_ms": T0 + day, "fee": 0.1, "pnl": -1.0},
        {"side": "sell", "ts_ms": T0 + 2 * day, "fee": 0.1, "pnl": 3.0},
    ]
    eq = [(T0, 100.0), (T0 + 1000, 102.0), (T0 + day, 101.0), (T0 + 2 * day, 104.0)]
    s = analysis.mode_stats(trades, eq, 100.0)
    assert s["trades"] == 3 and s["wins"] == 2 and s["profit_factor"] == pytest.approx(5.0) and s["net_pnl"] == pytest.approx(4.0)
    assert s["max_drawdown_pct"] == pytest.approx(1 / 102 * 100) and s["profitable_days_pct"] == pytest.approx(200 / 3)
    assert s["fees"] == pytest.approx(0.4) and s["return_pct"] == pytest.approx(4.0)


def test_rejected_analysis_counts_only_realistic_net_returns():
    events = {
        1: {"id": 1, "outcome": "target", "first_hit": "target", "net_pct": 0.2},
        2: {"id": 2, "outcome": "target", "first_hit": "target", "net_pct": -0.05},  # hit the target but costs ate it
        3: {"id": 3, "outcome": "pending"},
    }
    decisions = [
        {"event_id": 1, "mode": "A", "accepted": False, "code": "ev_below_margin"},
        {"event_id": 2, "mode": "A", "accepted": False, "code": "ev_below_margin"},
        {"event_id": 3, "mode": "A", "accepted": False, "code": "ev_below_margin"},
        {"event_id": 1, "mode": "B", "accepted": True, "code": "ok"},
    ]
    rows = analysis.rejected_analysis(decisions, events)
    assert len(rows) == 1
    r = rows[0]
    assert r["mode"] == "A" and r["count"] == 3 and r["resolved"] == 2 and r["hit_rate"] == 1.0 and r["profitable"] == 1
    assert r["reason"] == "Expected value below margin"
    g = analysis.group_outcomes(list(events.values()) + [{"id": 4, "outcome": "stop", "first_hit": "stop", "net_pct": -0.5, "symbol": "X"}], "symbol")
    assert any(row["symbol"] == "X" and row["hit_rate"] == 0.0 for row in g)


def test_config_normalises_weights_and_keeps_open_risk_cap_consistent():
    c = PulseConfig(weights={"f": 2, "a": 2, "sx": -1}, risk_per_trade_pct=1.0)
    w = c.weights_normalized()
    assert sum(abs(v) for v in w.values()) == pytest.approx(1.0) and w["sx"] < 0
    assert c.risk.max_open_risk_pct >= 1.0
    assert all(m.budget == c.budget for m in c.modes)


def test_indicator_edge_ranks_indicators_by_separation():
    from cryptobot.pulse.analysis import indicator_edge

    ev = []
    for i in range(40):
        hit = i % 2 == 0
        ev.append({"direction": "long", "outcome": "target" if hit else "stop", "first_hit": "target" if hit else "stop",
                   "f": 90 if hit else 40, "a": 50, "w": 50, "r": 50, "c": 50, "acc": 30 if hit else 80, "vpr": 50, "sx": 50, "vx": 50})
    ev.append({"direction": "short", "outcome": "target", "f": 1})
    ev.append({"direction": "long", "outcome": "pending", "f": 1})
    edge = indicator_edge(ev)
    assert edge["resolved"] == 40 and edge["hits"] == 20
    by = {r["indicator"]: r["auc"] for r in edge["rows"]}
    assert by["f"] == 1.0 and by["acc"] == 0.0 and by["a"] == 0.5
    assert edge["rows"][0]["indicator"] in ("f", "acc") and edge["note"]
    assert indicator_edge([])["resolved"] == 0


def test_memory_requires_evidence_spanning_market_time():
    from cryptobot.db.store import Store
    from cryptobot.pulse.memory import MarketMemory, MemoryRow

    store = Store(":memory:")
    m = MarketMemory(store, k=10, min_n=5, min_span_ms=24 * 3_600_000)
    vec = [90, 90, 90, 90, 90, 50, 50, 50, 50, 0.2, 0.01, 0.5]
    for i in range(8):  # eight independent setups, all inside one afternoon
        m.add(MemoryRow(f"C{i}/USD", 1_750_000_000_000 + i * 3_600_000, "long", "range", vec, True, False, 0.1, 60_000, 0.4))
    est = m.estimate("long", dict(zip(("f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx", "vol_pct", "spread_pct", "imbalance"), vec)))
    assert not est.sufficient and "span only" in est.note and "need 24 h" in est.note
    m.add(MemoryRow("C9/USD", 1_750_000_000_000 + 30 * 3_600_000, "long", "range", vec, True, False, 0.1, 60_000, 0.4))
    assert m.estimate("long", dict(zip(("f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx", "vol_pct", "spread_pct", "imbalance"), vec))).sufficient

