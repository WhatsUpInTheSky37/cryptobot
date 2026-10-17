"""Early Warning: data client (faked), events, features, study, score, states, targets, paper, scanner, store, engine, CLI."""
import asyncio

import numpy as np
import pytest

from cryptobot.core.events import BookEvent, ConnectionEvent, EWStatusEvent, HeartbeatEvent, TickEvent
from cryptobot.core.models import Tick
from cryptobot.earlywarning import backtest, study
from cryptobot.config import AppPaths
from cryptobot.earlywarning.cli import ew_db_path, research
from cryptobot.earlywarning.config import COMPONENTS, EWConfig, SurgeConfig, SurgeThreshold
from cryptobot.earlywarning.data import BinanceUSData, download_all
from cryptobot.earlywarning.engine import EarlyWarningEngine
from cryptobot.earlywarning.events import detect_events, executable_bid
from cryptobot.earlywarning.features import price_changes, window_features
from cryptobot.earlywarning.paper import simulate_trade, slip_pct, summarize
from cryptobot.earlywarning.report import build_report
from cryptobot.earlywarning.scanner import screen, snapshot
from cryptobot.earlywarning.score import INSUFFICIENT, EASModel, components_from_features
from cryptobot.earlywarning.states import classify
from cryptobot.earlywarning.store import EWStore, klines_array
from cryptobot.earlywarning.synth import synthetic_aggtrades, synthetic_klines
from cryptobot.earlywarning.targets import NOT_RELIABLE, TargetEstimator, outcome_after, walk_forward_targets

T0 = 1_750_000_000_000


# ------------------------------------------------------------------ data


class FakeBinanceUS:
    """Just enough of ccxt.binanceus for the data client, with recorded rate-limit headers."""

    def __init__(self, k, trades_rows, *, symbols=("PONDUSD", "COMPUSD"), weight=100):
        self.k, self.trades_rows = k, trades_rows
        self.symbols = symbols
        self.last_response_headers = {"x-mbx-used-weight-1m": str(weight)}
        self.calls = []

    def milliseconds(self):
        return int(self.k["ts"][-1]) + 60_000

    def publicGetExchangeInfo(self, params=None):  # noqa: N802
        syms = [{"symbol": s, "status": "TRADING", "baseAsset": s[:-3], "quoteAsset": "USD", "permissions": ["SPOT"]} for s in self.symbols]
        syms.append({"symbol": "MXCUSDT", "status": "TRADING", "baseAsset": "MXC", "quoteAsset": "USDT", "permissions": ["SPOT"]})
        syms.append({"symbol": "FLUXUSD", "status": "BREAK", "baseAsset": "FLUX", "quoteAsset": "USD", "permissions": ["SPOT"]})
        return {"symbols": syms}

    def publicGetKlines(self, params):  # noqa: N802
        self.calls.append(("klines", params))
        start, end, limit = int(params["startTime"]), int(params.get("endTime", 1 << 62)), int(params["limit"])
        rows = [r for r in self.k if start <= r["ts"] <= end][:limit]
        return [[int(r["ts"]), str(r["open"]), str(r["high"]), str(r["low"]), str(r["close"]), str(r["volume"]), int(r["ts"]) + 59_999, str(r["quote_volume"]), int(r["trades"]), str(r["taker_buy_base"]), str(r["taker_buy_quote"]), "0"] for r in rows]

    def publicGetAggTrades(self, params):  # noqa: N802
        self.calls.append(("agg", params))
        limit = int(params.get("limit", 1000))
        if "fromId" in params:
            rows = [r for r in self.trades_rows if r[0] >= int(params["fromId"])][:limit]
        else:
            s, e = int(params["startTime"]), int(params["endTime"])
            rows = [r for r in self.trades_rows if s <= r[1] <= e][:limit]
        return [{"a": a, "T": ts, "p": str(p), "q": str(q), "m": bool(m)} for a, ts, p, q, m in rows]

    def fetch_tickers(self):
        return {}


@pytest.fixture(scope="module")
def synth():
    k, starts = synthetic_klines(days=12, seed=3, surges=3)
    tr, rows = synthetic_aggtrades(k, per_bar=2)
    return k, starts, tr, rows


def test_verify_symbols_exact_match_and_similar_names_are_not_substituted(synth):
    k, _s, _tr, rows = synth
    store = EWStore(":memory:")
    data = BinanceUSData(FakeBinanceUS(k, rows), store, sleep=lambda s: None)
    recs = data.verify_symbols(["MXC/USD", "POND/USD", "FLUX/USD", "COMP/USD", "MXCX/USD"])
    by = {r["symbol"]: r for r in recs}
    # the exact pair is missing, but the SAME asset trades as MXC/USDT: recorded as a fallback, never silently
    assert not by["MXC/USD"]["available"] and by["MXC/USD"]["fallback"] == "MXC/USDT" and "same asset trades as MXC/USDT" in by["MXC/USD"]["note"]
    assert by["POND/USD"]["available"] and by["COMP/USD"]["available"] and "fallback" not in by["POND/USD"]
    assert not by["FLUX/USD"]["available"] and "BREAK" in by["FLUX/USD"]["note"] and "fallback" not in by["FLUX/USD"]
    # a look-alike name is a different asset: named as such, never used
    assert not by["MXCX/USD"]["available"] and "NOT the same coin" in by["MXCX/USD"]["note"] and "fallback" not in by["MXCX/USD"]
    assert {m["symbol"] for m in store.markets()} == set(by)


def test_download_is_resumable_and_backs_off_on_weight(synth):
    k, _s, _tr, rows = synth
    store = EWStore(":memory:")
    slept = []
    client = FakeBinanceUS(k, rows, weight=1150)
    data = BinanceUSData(client, store, sleep=slept.append)
    n = data.download_klines("POND/USD", "PONDUSD", "1m", days=3)
    assert n == 3 * 1440 and slept, "high used weight must trigger a sleep"
    first_calls = len(client.calls)
    n2 = data.download_klines("POND/USD", "PONDUSD", "1m", days=3)
    assert n2 <= 2 and len(client.calls) - first_calls <= 2  # resumed from the last stored bar
    rng = store.kline_range("POND/USD", "1m")
    assert rng[2] == 3 * 1440
    n3 = data.download_agg_trades("POND/USD", "PONDUSD", days=1)
    assert n3 > 0 and store.aggtrade_range("POND/USD")[3] == n3
    assert data.download_agg_trades("POND/USD", "PONDUSD", days=1) == 0
    assert any(d["kind"] == "aggtrades" and "API key" in d["note"] for d in store.downloads())
    recs = download_all(data, ["POND/USD", "MXC/USD"], 2, progress=lambda m: None)
    assert [(r["symbol"], r["available"]) for r in recs] == [("POND/USD", True), ("MXC/USD", False), ("MXC/USDT", True)]
    assert "substituted for MXC/USD" in [m for m in store.markets() if m["symbol"] == "MXC/USDT"][0]["note"]


def test_agg_trades_seed_from_first_traded_candle_and_walk_empty_hours(synth):
    k, _s, _tr, rows = synth
    k = k.copy()
    day5 = int(k["ts"][0]) + 5 * 86_400_000
    k["trades"][k["ts"] < day5] = 0  # the pair did not trade for its first five days (e.g. listed later than the requested history)
    late = [r for r in rows if r[1] > day5 + 2 * 3_600_000]  # and the first two hours after that were idle too
    store = EWStore(":memory:")
    client = FakeBinanceUS(k, late)
    data = BinanceUSData(client, store, sleep=lambda s: None)
    data.download_klines("POND/USD", "PONDUSD", "1m", days=12)
    n = data.download_agg_trades("POND/USD", "PONDUSD", days=12)
    assert n == len(late) > 0
    first = [p for kind, p in client.calls if kind == "agg" and "startTime" in p]
    assert first[0]["startTime"] == day5, "the first time window starts at the first traded candle, not at the requested start"
    assert len(first) == 3, "two empty hours were walked before the first trade was found"
    assert store.aggtrade_range("POND/USD")[3] == len(late)


def test_demo_runs_never_mix_with_real_rows_and_a_real_run_purges_them(synth, tmp_path):
    k, _s, _tr, rows = synth
    store = EWStore(tmp_path / "ew.db")
    store.put_market("COMP/USDT", "COMPUSDT", "TRADING", True, True, "COMP", "USDT", note="substituted for COMP/USD (BREAK)")
    store.upsert_klines("COMP/USDT", "1m", [tuple(r) for r in k.tolist()])
    cfg = EWConfig(days=12)
    cfg.seed_symbols = ["POND/USD"]
    summary = research(store, cfg, demo=True, progress=lambda m: None)
    assert set(summary["events"]) == {"POND/USD"} and set(store.get_study("compare")) == {"POND/USD"}, "a demo run analyses only its own synthetic markets"
    assert {m["symbol"]: m["note"] for m in store.markets()} == {"COMP/USDT": "substituted for COMP/USD (BREAK)", "POND/USD": "synthetic"}
    lines = []
    data = BinanceUSData(FakeBinanceUS(k, rows), store, sleep=lambda s: None)
    research(store, cfg, data=data, demo=False, progress=lines.append)
    assert any("removed synthetic demo data" in m for m in lines)
    assert all(m["note"] != "synthetic" for m in store.markets()) and all(d["note"] != "synthetic" for d in store.downloads())
    assert not store.get_study("run")["demo"] and set(store.get_study("compare")) == {"POND/USD"}
    text = build_report(store, cfg)
    assert "SYNTHETIC DATA" not in text and "Median daily volume" in text and "THIN" in text, "a thin pair is named as such in the report"
    assert ew_db_path(AppPaths(tmp_path, tmp_path), demo=True).name == "earlywarning-demo.db" and ew_db_path(AppPaths(tmp_path, tmp_path)).name == "earlywarning.db"


# ---------------------------------------------------------------- events


def test_detect_events_thresholds_merge_and_statistics(synth):
    k, starts, tr, _rows = synth
    ev = detect_events(k, SurgeConfig(), tr)
    assert len(ev) == len(starts)
    for e, s in zip(ev, starts):
        assert abs(e["start_index"] - s) <= 95  # start = lowest low before the trigger, inside the accumulation
        assert e["gain_pct"] > 10 and e["peak_ms"] > e["start_ms"] and e["minutes_to_peak"] > 0
        assert e["max_decline_pct"] < 0 and e["quote_volume"] > 0 and e["peak_bid"] is not None and e["peak_bid"] <= e["peak_price"]
        assert e["minutes_to_reversal"] is not None
    # a single strict threshold finds nothing in a flat series; overlapping windows do not duplicate
    flat = klines_array([(T0 + i * 60_000, 1, 1.001, 0.999, 1, 1, 1, 1, 0.5, 0.5) for i in range(500)])
    assert detect_events(flat, SurgeConfig(thresholds=[SurgeThreshold(pct=5, minutes=5)])) == []
    assert len(detect_events(k, SurgeConfig(thresholds=[SurgeThreshold(pct=10, minutes=15), SurgeThreshold(pct=10, minutes=16)]))) == len(ev)


def test_executable_bid_uses_last_sell_taker_print():
    tr = np.array([(T0, 1.00, 1, 0), (T0 + 1000, 0.98, 1, 1), (T0 + 2000, 1.05, 1, 0)], dtype=[("ts", "i8"), ("price", "f8"), ("qty", "f8"), ("buyer_maker", "i1")])
    assert executable_bid(tr, T0 + 2000, 1.05) == 0.98
    assert executable_bid(tr, T0 + 10_000_000, 1.05) == pytest.approx(1.05 * 0.998)


# -------------------------------------------------------------- features


def test_window_features_relative_to_baseline_and_insufficient_baseline(synth):
    k, starts, tr, _rows = synth
    t0 = int(k["ts"][starts[1]])
    f = window_features(k, t0, 60, 7 * 1440, tr)
    assert f is not None and f["source"] == "aggtrades"
    assert f["rel_volume"] > 1.5 and f["imbalance"] > 0.2 and f["freq_change"] > 1.5 and f["persistence"] >= 0.75
    assert f["median_size_change"] is not None and f["large_cluster"] is not None and f["compression"] is not None
    g = window_features(k, t0, 60, 7 * 1440, None)
    assert g["source"] == "klines" and g["median_size_change"] is not None
    assert window_features(k, int(k["ts"][30]), 60, 7 * 1440) is None  # baseline too short
    ch = price_changes(k, t0)
    assert set(ch) == {5, 15, 30, 60} and all(v is not None for v in ch.values())


# ---------------------------------------------------------------- score


def test_eas_insufficient_data_then_ranks():
    m = EASModel({"rel_volume": 1, "imbalance": 1, "freq_accel": 1, "compression": 1, "absorption": 1, "persistence": 1}, min_obs=5)
    comps = {c: 1.0 for c in COMPONENTS}
    eas, info = m.score("X", comps)
    assert eas is None and info["status"] == INSUFFICIENT and "0/5" in info["detail"]
    assert m.score("X", None)[1]["status"] == INSUFFICIENT
    for i in range(5):
        m.add("X", {c: float(i) for c in COMPONENTS})
    eas, info = m.score("X", {c: 10.0 for c in COMPONENTS})
    assert eas == pytest.approx(100.0) and info["status"] == "ok"
    eas, info = m.score("X", {c: -1.0 for c in COMPONENTS})
    assert eas == 0.0
    assert components_from_features({"rel_volume": 1, "imbalance": 0, "freq_change": 1, "compression": None, "absorption": 1, "persistence": 1}) is None


def test_states():
    cfg = EWConfig().score
    ok = {c: 80.0 for c in COMPONENTS} | {"status": "ok"}
    flat = {5: 0.1, 15: 0.2, 30: 0.3, 60: 0.4}
    assert classify(None, None, flat, imbalance=0, rel_volume=1, data_ok=True, cfg=cfg) == "NO DATA"
    assert classify(80, ok, flat, imbalance=0.3, rel_volume=2, data_ok=False, cfg=cfg) == "NO DATA"
    assert classify(80, ok, flat, imbalance=0.3, rel_volume=2, data_ok=True, cfg=cfg) == "ACCUMULATION CONFIRMED"
    assert classify(65, ok, flat, imbalance=0.3, rel_volume=2, data_ok=True, cfg=cfg) == "POSSIBLE ACCUMULATION"
    assert classify(40, ok, flat, imbalance=0.3, rel_volume=2, data_ok=True, cfg=cfg) == "QUIET"
    assert classify(80, ok, {5: 1.2, 15: 1.0, 30: 1.0, 60: 1.0}, imbalance=0.3, rel_volume=2, data_ok=True, cfg=cfg) == "BREAKOUT DEVELOPING"
    assert classify(80, ok, {5: 3.5, 15: 4.0, 30: 4.0, 60: 4.0}, imbalance=0.3, rel_volume=2, data_ok=True, cfg=cfg) == "SURGE ACTIVE"
    assert classify(80, ok, {5: 0.0, 15: 1.0, 30: 3.0, 60: 6.0}, imbalance=-0.4, rel_volume=2, data_ok=True, cfg=cfg) == "POSSIBLE DISTRIBUTION"
    assert classify(80, ok, {5: -2.5, 15: 1.0, 30: 3.0, 60: 6.0}, imbalance=0.0, rel_volume=1, data_ok=True, cfg=cfg) == "REVERSAL WARNING"


# ---------------------------------------------------------------- study


def test_study_events_vs_controls_grid_and_lead_time(synth):
    k, starts, tr, rows = synth
    cfg = EWConfig()
    store = EWStore(":memory:")
    store.upsert_klines("POND/USD", "1m", [tuple(r) for r in k.tolist()])
    ev = detect_events(k, cfg.surge, tr)
    store.replace_events("POND/USD", ev)
    counts = study.build_windows(store, "POND/USD", k, tr, ev, cfg, grid_step_min=60)
    assert counts["event_windows"] > 0 and counts["controls"] >= 15 and counts["lead_windows"] > 0 and counts["grid_windows"] > 50
    study.score_windows(store, "POND/USD", cfg)
    assert all(w["eas"] is not None for w in store.windows("POND/USD", kind="grid"))
    r = study.compare(store, "POND/USD", cfg)
    assert r["events"] == len(ev) and r["controls"] >= 15
    f60 = r["by_lookback"]["60"]["features"]
    assert f60["rel_volume"]["auc"] > 0.9 and f60["imbalance"]["auc"] > 0.8
    assert r["by_lookback"]["60"]["eas_auc"] > 0.9
    th = r["by_lookback"]["60"]["thresholds"]["confirmed"]
    assert th["true_positive_rate"] is not None and th["false_positive_rate"] is not None and th["false_positive_rate"] < th["true_positive_rate"]
    g = r["grid"]["confirmed"]
    assert g["flagged"] >= g["true_positives"] and g["false_positive_rate"] is not None and 0 <= g["precision"] <= 1 and g["recall"] is not None
    lt = r["lead_time"]
    assert lt["detected"] >= 1 and lt["median_lead_min"] >= 15 and "60" in lt["median_eas_by_offset"]
    # controls never overlap an event window
    ctrl = {w["t0_ms"] for w in store.windows("POND/USD", kind="control")}
    for t in ctrl:
        for e in ev:
            assert not (e["start_ms"] - 1440 * 60_000 <= t <= e["peak_ms"])
    pooled = study.pooled_compare([r, r])
    assert pooled["events"] == 2 * len(ev) and pooled["features"]


def test_auc_handles_ties_and_edges():
    assert study.auc(np.array([2, 2]), np.array([1, 1])) == 1.0
    assert study.auc(np.array([1, 1]), np.array([1, 1])) == 0.5
    assert study.auc(np.array([]), np.array([1])) is None


# -------------------------------------------------------------- targets


def test_outcome_after_and_not_reliable_without_setups(synth):
    k, starts, _tr, _rows = synth
    o = outcome_after(k, int(k["ts"][starts[0]]), 60, 3.0, 2.0, 0.1)
    assert o["hit"] and not o["stopped"] and o["mfe_pct"] > 3 and o["bars"] == 60
    rep = walk_forward_targets([], EWConfig())
    assert all(v["label"] == NOT_RELIABLE for v in rep["horizons"].values())
    est = TargetEstimator(rep)
    assert est.target(60) == (None, NOT_RELIABLE) and est.probability(60, [50] * 6) is None


def test_walk_forward_targets_validates_only_with_coverage_and_net_positive():
    cfg = EWConfig()
    cfg.targets.min_setups = 20
    rng = np.random.default_rng(1)
    setups = []
    for i in range(120):
        mfe = float(rng.uniform(2.0, 6.0))  # every setup moves at least 2%: p25 ≈ 3% gross
        setups.append({"symbol": "A/USD", "t0_ms": T0 + i * 3_600_000, "eas": 70, "kind": "grid", "vec": [50 + rng.normal(0, 10) for _ in COMPONENTS],
                       "outcomes": {h: {"hit": mfe >= 3.0, "stopped": False, "mfe_pct": mfe, "mae_pct": -0.5, "entry": 1.0, "close_pct": 1.0, "bars": h} for h in cfg.horizons_min}})
    rep = walk_forward_targets(setups, cfg)
    h60 = rep["horizons"]["60"]
    assert h60["n_oos"] >= 20 and h60["reliable"] and h60["coverage"] >= 0.7 and h60["tradeable"] and h60["target_net_pct"] > 0
    est = TargetEstimator(rep, setups)
    assert est.target(60)[0] == pytest.approx(h60["target_net_pct"])
    # overlapping setups (same coin, 10 min apart) collapse to one observation
    dense = [dict(s, t0_ms=T0 + i * 600_000) for i, s in enumerate(setups)]
    assert walk_forward_targets(dense, cfg)["n_setups"] < 30
    # negative after costs: validated but not tradeable
    small = [dict(s, outcomes={h: dict(o, mfe_pct=0.2) for h, o in s["outcomes"].items()}) for s in setups]
    r2 = walk_forward_targets(small, cfg)["horizons"]["60"]
    assert r2["reliable"] and not r2["tradeable"] and "NOT tradeable" in r2["label"] and TargetEstimator({"horizons": {"60": r2}}).target(60)[0] is None


# ---------------------------------------------------------------- paper


def test_simulate_trade_bid_side_fees_partials_and_stop_not_at_level(synth):
    k, starts, tr, _rows = synth
    cfg = EWConfig()
    t0 = int(k["ts"][starts[0]])
    tA = simulate_trade(k, tr, "POND/USD", t0, "A", "partial", cfg)
    assert tA is not None and tA.fills[0].side == "buy" and tA.fills[0].price > float(k["open"][starts[0] + 1])  # paid the ask plus slippage
    assert len(tA.fills) >= 2 and tA.fees > 0 and tA.pnl_pct > 0 and tA.mfe_pct > 5
    assert any("partial" in f.reason for f in tA.fills)
    tB = simulate_trade(k, tr, "POND/USD", t0, "B", "fixed", cfg)
    assert tB is not None and tB.entry_ms > tA.entry_ms  # breakout confirmation enters later
    # a stop fills at the worse of the level and the next open, never better than the level
    crash = klines_array([(T0 + i * 60_000, 1.0 if i < 5 else 0.9, 1.001 if i < 5 else 0.9, 0.999 if i < 5 else 0.85, 1.0 if i < 5 else 0.86, 1000, 1000, 10, 500, 500) for i in range(60)])
    tc = simulate_trade(crash, None, "X/USD", T0 + 2 * 60_000, "A", "fixed", cfg)
    assert tc is not None and tc.reason == "stop" and tc.fills[-1].price < tc.entry_price * (1 - cfg.exits.stop_pct / 100)
    # target below costs: no trade
    cfg2 = EWConfig()
    cfg2.exits.fixed_target_pct = 0.2
    assert simulate_trade(k, tr, "POND/USD", t0, "A", "fixed", cfg2) is None
    # thin market: cannot size a position
    thin = klines_array([(T0 + i * 60_000, 1.0, 1.001, 0.999, 1.0, 1, 1.0, 1, 0.5, 0.5) for i in range(60)])
    assert simulate_trade(thin, None, "X/USD", T0 + 2 * 60_000, "A", "fixed", cfg) is None
    assert slip_pct(100, 10_000, cfg) == cfg.costs.base_slippage_pct and slip_pct(5_000, 10_000, cfg) > cfg.costs.base_slippage_pct
    s = summarize([tA, tB], 100.0)
    assert s["trades"] == 2 and s["fees"] > 0 and "max_drawdown_pct" in s


def test_backtest_methods_out_of_sample(synth):
    k, starts, tr, rows = synth
    cfg = EWConfig()
    store = EWStore(":memory:")
    store.upsert_klines("POND/USD", "1m", [tuple(r) for r in k.tolist()])
    store.upsert_aggtrades("POND/USD", rows)
    ev = detect_events(k, cfg.surge, tr)
    store.replace_events("POND/USD", ev)
    study.build_windows(store, "POND/USD", k, tr, ev, cfg, grid_step_min=60)
    study.score_windows(store, "POND/USD", cfg)
    bt = backtest.run_methods(store, ["POND/USD"], cfg)
    assert bt["n_setups"] > 0 and bt["n_train"] + bt["n_test"] == bt["n_setups"]
    assert "A/partial" in bt["results"] and "test" in bt["results"]["A/partial"] and "train" in bt["results"]["A/partial"]


# -------------------------------------------------------------- scanner


def test_scanner_eligibility_and_unusual_activity():
    cfg = EWConfig().scanner
    elig = [{"symbol": s, "market_id": s.replace("/", ""), "base": s[:-4], "quote": "USD"} for s in ("AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD")]
    t1 = {"AAA/USD": {"last": 1.0, "quoteVolume": 1_000_000, "bid": 0.999, "ask": 1.0, "percentage": 1.0, "timestamp": T0, "info": {"count": 10000}},
          "BBB/USD": {"last": 1.0, "quoteVolume": 50_000, "bid": 0.999, "ask": 1.0, "percentage": 1.0, "timestamp": T0, "info": {"count": 100}},
          "CCC/USD": {"last": 1.0, "quoteVolume": 500_000, "bid": 0.95, "ask": 1.0, "percentage": 1.0, "timestamp": T0, "info": {"count": 1000}},
          "DDD/USD": {"last": 1.0, "quoteVolume": 500_000, "bid": 0.999, "ask": 1.0, "percentage": 1.0, "timestamp": T0, "info": {"count": 1000}}}
    c = screen(t1, elig, cfg)
    by = {x.symbol: x for x in c}
    assert "volume" in by["BBB/USD"].excluded and "spread" in by["CCC/USD"].excluded and not by["AAA/USD"].excluded
    prev = snapshot(t1)
    t2 = dict(t1)
    t2["DDD/USD"] = dict(t1["DDD/USD"], quoteVolume=560_000, last=1.005, timestamp=T0 + 300_000, info={"count": 1150})  # 60k in 5 min vs 21k/5min average
    t2["AAA/USD"] = dict(t1["AAA/USD"], quoteVolume=1_002_000, timestamp=T0 + 300_000, info={"count": 10020})
    c2 = screen(t2, elig, cfg, prev)
    assert c2[0].symbol == "DDD/USD" and any("volume" in r for r in c2[0].reasons) and "price firm" in " ".join(c2[0].reasons)
    assert [x.symbol for x in c2 if x.excluded] == ["CCC/USD", "BBB/USD"] or set(x.symbol for x in c2 if x.excluded) == {"CCC/USD", "BBB/USD"}


# ---------------------------------------------------------------- store


def test_store_roundtrips_and_persists(tmp_path):
    db = tmp_path / "ew.db"
    s = EWStore(db)
    s.set("k", {"a": 1})
    s.put_study("x", [1, 2])
    sid = s.insert_signal(T0, "A/USD", 70.0, {"status": "ok"}, "POSSIBLE ACCUMULATION", 1.0, True, "")
    s.resolve_signal(sid, "surge", 6.0, -1.0, T0 + 3_600_000)
    s.insert_transition(T0, "A/USD", "QUIET", "POSSIBLE ACCUMULATION", 70.0)
    s.insert_paper_trade(book="EW", symbol="A/USD", side="buy", ts_ms=T0, price=1, fill_price=1.001, amount=10, fee=0.01)
    s.add_equity("EW", T0, 100.0, 90.0)
    s2 = EWStore(db)
    assert s2.get("k") == {"a": 1} and s2.get_study("x") == [1, 2]
    assert s2.signals()[0]["outcome"] == "surge" and s2.transitions()[0]["to_state"] == "POSSIBLE ACCUMULATION"
    assert s2.paper_trades("EW")[0]["fill_price"] == 1.001 and s2.equity("EW") == [(T0, 100.0)]


# --------------------------------------------------------------- engine


def make_engine(k, store=None, **cfg_kw):
    cfg = EWConfig()
    cfg.score.min_history_obs = 5
    for key, v in cfg_kw.items():
        setattr(cfg, key, v)
    store = store or EWStore(":memory:")
    store.upsert_klines("POND/USD", "1m", [tuple(r) for r in k.tolist()])
    clock = {"ms": int(k["ts"][-1]) + 60_000}
    events = []
    eng = EarlyWarningEngine(store, events.append, "binanceus", cfg, data=None, clock=lambda: clock["ms"] / 1000)
    tickers = {"POND/USD": {"last": float(k["close"][-1]), "quoteVolume": 500_000, "bid": 0.999, "ask": 1.0, "percentage": 2.0, "timestamp": clock["ms"], "info": {"count": 5000}},
               "TINY/USD": {"last": 1.0, "quoteVolume": 5_000, "bid": 0.9, "ask": 1.0, "percentage": 0.0, "timestamp": clock["ms"], "info": {"count": 50}}}
    elig = [{"symbol": "POND/USD", "market_id": "PONDUSD", "base": "POND", "quote": "USD"}, {"symbol": "TINY/USD", "market_id": "TINYUSD", "base": "TINY", "quote": "USD"}]
    return eng, events, store, clock, tickers, elig


def drive(eng, clock, minutes, px, *, buy_every=3, book=True, amount=100.0):
    for _m in range(minutes):
        for s in range(60):
            ts = clock["ms"]
            eng.on_feed_event(TickEvent("binanceus", Tick("POND/USD", ts, px, amount, None, "buy" if s % buy_every else "sell")))
            if book:
                eng.on_feed_event(BookEvent("binanceus", "POND/USD", ts, px * 0.999, px * 1.001, 5000.0, 5000.0, 15000.0, 15000.0, 1, True))
            eng.on_feed_event(HeartbeatEvent("binanceus", ts))
            clock["ms"] += 1000


def test_engine_scans_streams_scores_states_signals_and_stale_data():
    k, _starts = synthetic_klines(days=9, seed=5, surges=1)
    eng, events, store, clock, tickers, elig = make_engine(k)
    chosen = eng.scan(tickers, elig)
    assert chosen == ["POND/USD"] and any(c.excluded for c in eng.candidates)
    eng.ensure_history("POND/USD")
    assert len(eng.sym["POND/USD"].history) > 7 * 1440
    eng.running = True
    eng.on_feed_event(ConnectionEvent("binanceus", "connected"))
    px = float(k["close"][-1])
    drive(eng, clock, 3, px)
    st = eng.sym["POND/USD"]
    assert st.eas is None and st.state == "NO DATA" and st.comps["status"] == INSUFFICIENT  # not enough live observations yet
    drive(eng, clock, 8, px, amount=400.0)  # heavy buying, flat price
    assert st.data_status == "ok" and st.eas is not None and st.state in ("ACCUMULATION CONFIRMED", "POSSIBLE ACCUMULATION", "QUIET")
    ev = [e for e in events if isinstance(e, EWStatusEvent)][-1]
    c = ev.candidates[0]
    assert c.symbol == "POND/USD" and c.eas == st.eas and c.target_label == NOT_RELIABLE and c.surge_probability is None and c.scanner_reasons
    assert all(v is not None for v in c.changes.values()) and c.rel_volume is not None and c.buy_sell_ratio is not None
    assert ev.scanner["selected"] == ["POND/USD"] and ev.paper["trades"] == 0 and "target_not_reliable" in ev.paper["rejections"] or ev.paper["rejections"]
    assert ev.stats["signals"] >= (1 if st.state != "QUIET" else 0)
    assert store.transitions()  # at least NO DATA -> something
    # stale: no ticks for 3 minutes
    clock["ms"] += 180_000
    eng.on_feed_event(HeartbeatEvent("binanceus", clock["ms"]))
    assert st.state == "NO DATA" and "stale" in st.data_status
    # disconnect
    eng.on_feed_event(ConnectionEvent("binanceus", "reconnecting"))
    eng.on_feed_event(HeartbeatEvent("binanceus", clock["ms"] + 60_000))
    assert not eng.connected and st.data_status.startswith("connection")


def test_engine_paper_book_trades_only_with_validated_target_and_respects_limits():
    k, _starts = synthetic_klines(days=9, seed=6, surges=1)
    store = EWStore(":memory:")
    # a validated, tradeable 60-minute target from "research"
    store.put_study("targets", {"n_setups": 100, "horizons": {"60": {"reliable": True, "tradeable": True, "target_net_pct": 2.5, "target_gross_pct": 3.0, "probability_validated": False, "label": "target +2.50% net"}}})
    eng, events, store, clock, tickers, elig = make_engine(k, store=store)
    eng.scan(tickers, elig)
    eng.ensure_history("POND/USD")
    eng.running = True
    eng.on_feed_event(ConnectionEvent("binanceus", "connected"))
    px = float(k["close"][-1])
    drive(eng, clock, 12, px, amount=400.0)
    st = eng.sym["POND/USD"]
    assert st.state == "ACCUMULATION CONFIRMED", (st.state, st.eas, st.data_status)
    assert "POND/USD" in eng.paper.positions, eng.paper.rejections
    pos = eng.paper.positions["POND/USD"]
    assert pos.entry_px > px and pos.stop < pos.entry_px and pos.t1 > pos.entry_px
    assert eng.paper.cash < eng.cfg.risk.budget and pos.units * pos.entry_px <= eng.cfg.risk.budget * 0.5 + 1e-9
    # no adding, no second position in the same coin
    drive(eng, clock, 2, px, amount=400.0)
    assert eng.paper.rejections.get("already_in_position")
    # stop: bid drops through the stop -> filled at the bid, not the stop level
    crash = pos.stop * 0.99
    eng.on_feed_event(BookEvent("binanceus", "POND/USD", clock["ms"], crash, crash * 1.001, 5000.0, 5000.0, 15000.0, 15000.0, 1, True))
    eng.on_feed_event(HeartbeatEvent("binanceus", clock["ms"]))
    assert "POND/USD" not in eng.paper.positions and eng.paper.losses == 1
    sells = [t for t in store.paper_trades("EW") if t["side"] == "sell"]
    assert sells and sells[0]["fill_price"] < pos.stop and sells[0]["reason"] == "stop"
    # state persists across a restart
    eng2 = EarlyWarningEngine(store, events.append, "binanceus", eng.cfg, data=None, clock=lambda: clock["ms"] / 1000)
    assert eng2.paper.losses == 1 and eng2.paper.cash == pytest.approx(eng.paper.cash)


def test_engine_run_loop_with_fake_feed():
    k, _starts = synthetic_klines(days=9, seed=7, surges=1)
    eng, events, store, clock, tickers, elig = make_engine(k)
    eng.cfg.scanner.rescan_s = 1
    eng.scan = lambda: eng.__class__.scan(eng, tickers, elig)  # no network

    class FakeFeed:
        def __init__(self, symbols, emit):
            self.symbols, self.emit = symbols, emit

        async def run(self, stop):
            self.emit(ConnectionEvent("binanceus", "connected"))
            await stop.wait()

    eng.feed_factory = FakeFeed

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(eng.run(stop))
        await asyncio.sleep(0.3)
        assert eng.running and eng.connected and eng._feed_task is not None
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(go())
    assert not eng.running and any(isinstance(e, EWStatusEvent) and not e.running for e in events)


# ------------------------------------------------------------------ cli


def test_research_demo_end_to_end_and_report(tmp_path):
    store = EWStore(tmp_path / "ew.db")
    cfg = EWConfig(days=20)
    cfg.seed_symbols = ["POND/USD", "COMP/USD"]
    lines = []
    summary = research(store, cfg, demo=True, progress=lines.append)
    assert summary["demo"] and set(summary["events"]) == {"POND/USD", "COMP/USD"} and all(v > 0 for v in summary["events"].values())
    assert store.get_study("compare") and store.get_study("targets")["horizons"] and store.get_study("methods")["results"]
    text = build_report(store, cfg, demo=True)
    assert "SYNTHETIC DATA" in text and "## 2. Surge events" in text and "False-positive" in text or "false-positive rate" in text
    assert "TARGET NOT YET RELIABLE" in text or "net" in text
    assert "## 9-10" in text and "A/partial" in text
