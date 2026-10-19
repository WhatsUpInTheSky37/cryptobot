"""Phase 3-4: event windows versus control windows. Which pre-surge patterns are real,
how often they appear when nothing happens, and how early they show up."""
from __future__ import annotations

from typing import Any

import numpy as np

from cryptobot.earlywarning.config import EWConfig
from cryptobot.earlywarning.events import tradeable
from cryptobot.earlywarning.features import FEATURE_KEYS, window_features
from cryptobot.earlywarning.score import EASModel, components_from_features
from cryptobot.earlywarning.store import EWStore


def auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    """Probability a random event value exceeds a random control value (0.5 = no information)."""
    if len(pos) == 0 or len(neg) == 0:
        return None
    allv = np.concatenate([pos, neg])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    # average ranks for ties
    sorted_v = allv[order]
    i = 0
    while i < len(sorted_v):
        j = i
        while j + 1 < len(sorted_v) and sorted_v[j + 1] == sorted_v[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def control_times(k: np.ndarray, events: list[dict[str, Any]], n: int, *, exclusion_before_min: int, exclusion_after_min: int, min_gap_min: int, seed: int = 7) -> list[int]:
    """Random t0 times that do not overlap any event (before or after) and not each other."""
    if len(k) == 0 or n <= 0:
        return []
    rng = np.random.default_rng(seed)
    ts = k["ts"]
    bad = [(e["start_ms"] - exclusion_before_min * 60_000, (e.get("reversal_ms") or e["peak_ms"]) + exclusion_after_min * 60_000) for e in events]
    lo, hi = int(ts[0]) + exclusion_before_min * 60_000, int(ts[-1]) - exclusion_after_min * 60_000
    if hi <= lo:
        return []
    out: list[int] = []
    tries = 0
    while len(out) < n and tries < n * 50:
        tries += 1
        t = int(rng.integers(lo, hi)) // 60_000 * 60_000
        if any(a <= t <= b for a, b in bad) or any(abs(t - o) < min_gap_min * 60_000 for o in out):
            continue
        out.append(t)
    return sorted(out)


LEAD_LOOKBACK_MIN = 60  # fixed window length used to ask "was the warning visible N minutes before the start?"
GRID_STEP_MIN = 15


def build_windows(store: EWStore, symbol: str, k: np.ndarray, trades: np.ndarray | None, events: list[dict[str, Any]], cfg: EWConfig, *, grid_step_min: int = GRID_STEP_MIN, exclude_events: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Compute and store features for: every event at every lookback ("event"); fixed 60-minute windows
    ending N minutes before each event ("lead"); random non-surge periods ("control"); and a regular
    time grid over the whole history ("grid", what a live scanner would have seen). Returns counts."""
    lookbacks = sorted(cfg.lookbacks_min, reverse=True)
    max_lb = max(lookbacks)
    for kind in ("event", "control", "lead", "grid"):
        store.clear_windows(symbol, kind)
    stored = store.events(symbol)
    by_start = {e["start_ms"]: e["id"] for e in stored}
    n_ev = n_lead = n_grid = 0
    for e in events:
        eid = by_start.get(e["start_ms"], 0)
        for lb in lookbacks:
            f = window_features(k, e["start_ms"], lb, cfg.score.baseline_minutes, trades, large_pct=cfg.score.large_trade_pct)
            if f is None:
                continue
            store.put_window(symbol, "event", eid, e["start_ms"], lb, f, None, {})
            n_ev += 1
        for off in lookbacks:
            t0 = e["start_ms"] - off * 60_000
            f = window_features(k, t0, LEAD_LOOKBACK_MIN, cfg.score.baseline_minutes, trades, large_pct=cfg.score.large_trade_pct)
            if f is None:
                continue
            store.put_window(symbol, "lead", eid, t0, off, f, None, {})  # lookback_min column holds the OFFSET for lead rows
            n_lead += 1
    if len(k):
        first = int(k["ts"][0]) + (cfg.score.baseline_minutes + LEAD_LOOKBACK_MIN) * 60_000
        t0 = first - first % (grid_step_min * 60_000) + grid_step_min * 60_000
        while t0 <= int(k["ts"][-1]):
            f = window_features(k, t0, LEAD_LOOKBACK_MIN, cfg.score.baseline_minutes, trades, large_pct=cfg.score.large_trade_pct)
            if f is not None:
                store.put_window(symbol, "grid", 0, t0, LEAD_LOOKBACK_MIN, f, None, {})
                n_grid += 1
            t0 += grid_step_min * 60_000
    n_ctrl = max(20, cfg.control_windows_per_event * len(events))
    surge_after = max(th.minutes for th in cfg.surge.thresholds) + cfg.surge.post_peak_minutes
    # controls stay clear of EVERY surge, tradeable or not, so "ordinary" never contains a thin-pair spike
    ctrl = control_times(k, exclude_events or events, n_ctrl, exclusion_before_min=max_lb, exclusion_after_min=surge_after, min_gap_min=max(60, max_lb // 4))
    n_c = 0
    for t0 in ctrl:
        for lb in lookbacks:
            f = window_features(k, t0, lb, cfg.score.baseline_minutes, trades, large_pct=cfg.score.large_trade_pct)
            if f is None:
                continue
            store.put_window(symbol, "control", 0, t0, lb, f, None, {})
            n_c += 1
    store.commit()
    return {"event_windows": n_ev, "control_windows": n_c, "controls": len(ctrl), "lead_windows": n_lead, "grid_windows": n_grid}


def _dist_key(symbol: str, r: dict[str, Any]) -> str:
    """Lead and grid windows are 60-minute windows: they share the 60-minute distribution."""
    lb = LEAD_LOOKBACK_MIN if r["kind"] in ("lead", "grid") else r["lookback_min"]
    return f"{symbol}@{lb}"


def score_windows(store: EWStore, symbol: str, cfg: EWConfig) -> EASModel:
    """Normalise each window's components against the coin's whole window history (per window length)
    and store the EAS. The grid dominates the 60-minute distribution, so "unusual" means unusual against
    ordinary time, not against a hand-picked set of events."""
    model = EASModel(cfg.weights_normalized(), min_obs=1)
    rows = store.windows(symbol)
    comps_list = [(r, components_from_features(r["features"])) for r in rows]
    for r, c in sorted(comps_list, key=lambda rc: rc[0]["t0_ms"]):
        eas, ranked = model.score(_dist_key(symbol, r), c)
        store.put_window(symbol, r["kind"], r["event_id"], r["t0_ms"], r["lookback_min"], r["features"], eas, ranked)
        if c and (r["kind"] == "grid" or r["lookback_min"] != LEAD_LOOKBACK_MIN):
            model.add(_dist_key(symbol, r), c)
    store.commit()
    return model


def compare(store: EWStore, symbol: str, cfg: EWConfig) -> dict[str, Any]:
    """Phase 4 statistics for one coin: per lookback, per feature effect sizes and AUC; EAS true/false-positive
    rates at the configured thresholds; and how early the score crossed them before each event."""
    rows = store.windows(symbol)
    ev = [r for r in rows if r["kind"] == "event"]
    ct = [r for r in rows if r["kind"] == "control"]
    out: dict[str, Any] = {"symbol": symbol, "events": len({r["event_id"] for r in ev}), "controls": len({r["t0_ms"] for r in ct}), "by_lookback": {}}
    for lb in sorted(cfg.lookbacks_min, reverse=True):
        e_lb = [r for r in ev if r["lookback_min"] == lb]
        c_lb = [r for r in ct if r["lookback_min"] == lb]
        feats: dict[str, Any] = {}
        for key in FEATURE_KEYS:
            pe = np.array([r["features"][key] for r in e_lb if r["features"].get(key) is not None], dtype=float)
            pc = np.array([r["features"][key] for r in c_lb if r["features"].get(key) is not None], dtype=float)
            if len(pe) == 0 or len(pc) == 0:
                continue
            feats[key] = {"event_median": float(np.median(pe)), "control_median": float(np.median(pc)), "event_mean": float(pe.mean()), "control_mean": float(pc.mean()), "auc": auc(pe, pc), "n_event": int(len(pe)), "n_control": int(len(pc))}
        ea = np.array([r["eas"] for r in e_lb if r["eas"] is not None], dtype=float)
        ca = np.array([r["eas"] for r in c_lb if r["eas"] is not None], dtype=float)
        thresholds = {}
        for name, thr in (("possible", cfg.score.possible_eas), ("confirmed", cfg.score.confirmed_eas)):
            tp = float(np.mean(ea >= thr)) if len(ea) else None
            fp = float(np.mean(ca >= thr)) if len(ca) else None
            thresholds[name] = {"threshold": thr, "true_positive_rate": tp, "false_positive_rate": fp,
                                "precision_at_base_rate": (tp * out["events"]) / (tp * out["events"] + fp * out["controls"]) if tp is not None and fp is not None and (tp * out["events"] + fp * out["controls"]) > 0 else None}
        out["by_lookback"][str(lb)] = {"features": feats, "eas_auc": auc(ea, ca), "eas_event_median": float(np.median(ea)) if len(ea) else None, "eas_control_median": float(np.median(ca)) if len(ca) else None, "thresholds": thresholds}
    # lead time: 60-minute windows ending N minutes before the start; the warning "began" at the largest
    # offset of the unbroken run of EAS >= possible that reaches the start (offset 1 min)
    lead_rows = [r for r in rows if r["kind"] == "lead"]
    leads: list[int | None] = []
    lead_profile: dict[int, list[float]] = {}
    for eid in {r["event_id"] for r in ev}:
        by_off = {r["lookback_min"]: r["eas"] for r in lead_rows if r["event_id"] == eid and r["eas"] is not None}
        for off, v in by_off.items():
            lead_profile.setdefault(off, []).append(v)
        run = None
        for off in sorted(by_off):  # 1, 5, 15, ... outward
            if by_off[off] >= cfg.score.possible_eas:
                run = off
            else:
                break
        leads.append(run)
    detected = [l for l in leads if l is not None]
    out["lead_time"] = {"events": len(leads), "detected": len(detected), "share_detected": len(detected) / len(leads) if leads else None,
                        "median_lead_min": float(np.median(detected)) if detected else None, "mean_lead_min": float(np.mean(detected)) if detected else None,
                        "by_event": leads, "median_eas_by_offset": {str(o): float(np.median(v)) for o, v in sorted(lead_profile.items())}}
    # grid: what a live scanner would have flagged, and how often nothing followed
    grid = [r for r in rows if r["kind"] == "grid" and r["eas"] is not None]
    surge_after_min = max(th.minutes for th in cfg.surge.thresholds)
    ev_starts = sorted(e["start_ms"] for e in tradeable(store.events(symbol), cfg.surge))
    def followed(t0: int) -> bool:
        i = np.searchsorted(ev_starts, t0)
        return i < len(ev_starts) and ev_starts[i] - t0 <= surge_after_min * 60_000
    grid_stats = {}
    for name, thr in (("possible", cfg.score.possible_eas), ("confirmed", cfg.score.confirmed_eas)):
        flagged = [r for r in grid if r["eas"] >= thr]
        tp = sum(1 for r in flagged if followed(r["t0_ms"]))
        hit_events = {e for e in ev_starts if any(0 <= e - r["t0_ms"] <= surge_after_min * 60_000 for r in flagged)}
        grid_stats[name] = {"threshold": thr, "flagged": len(flagged), "flag_rate": len(flagged) / len(grid) if grid else None, "true_positives": tp, "false_positives": len(flagged) - tp,
                            "false_positive_rate": (len(flagged) - tp) / len(flagged) if flagged else None, "precision": tp / len(flagged) if flagged else None,
                            "recall": len(hit_events) / len(ev_starts) if ev_starts else None, "missed_surges": len(ev_starts) - len(hit_events)}
    out["grid"] = {"windows": len(grid), "surge_window_min": surge_after_min, **grid_stats}
    # strongest patterns: features with the highest AUC at the 60-minute lookback (fall back to the longest available)
    best_lb = "60" if "60" in out["by_lookback"] else (next(iter(out["by_lookback"])) if out["by_lookback"] else None)
    if best_lb:
        f = out["by_lookback"][best_lb]["features"]
        ranked = sorted(((k, v["auc"]) for k, v in f.items() if v["auc"] is not None), key=lambda kv: -abs(kv[1] - 0.5))
        out["strongest"] = [{"feature": k, "auc": a, "lookback_min": int(best_lb)} for k, a in ranked[:5]]
    return out


def pooled_compare(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool per-coin results: average AUC per feature weighted by events, pooled FPR/TPR."""
    pooled: dict[str, Any] = {"coins": [r["symbol"] for r in results], "events": sum(r["events"] for r in results), "controls": sum(r["controls"] for r in results), "features": {}, "thresholds": {}}
    for lb in ("60", "30", "15", "180"):
        for r in results:
            bl = r["by_lookback"].get(lb)
            if not bl:
                continue
            for k, v in bl["features"].items():
                if v["auc"] is None:
                    continue
                d = pooled["features"].setdefault(f"{k}@{lb}", {"auc_sum": 0.0, "w": 0})
                d["auc_sum"] += v["auc"] * r["events"]
                d["w"] += r["events"]
            for name, t in bl["thresholds"].items():
                d = pooled["thresholds"].setdefault(f"{name}@{lb}", {"tp": 0.0, "fp": 0.0, "ne": 0, "nc": 0})
                if t["true_positive_rate"] is not None:
                    d["tp"] += t["true_positive_rate"] * r["events"]
                    d["ne"] += r["events"]
                if t["false_positive_rate"] is not None:
                    d["fp"] += t["false_positive_rate"] * r["controls"]
                    d["nc"] += r["controls"]
    pooled["features"] = {k: (v["auc_sum"] / v["w"] if v["w"] else None) for k, v in pooled["features"].items()}
    pooled["thresholds"] = {k: {"true_positive_rate": v["tp"] / v["ne"] if v["ne"] else None, "false_positive_rate": v["fp"] / v["nc"] if v["nc"] else None} for k, v in pooled["thresholds"].items()}
    leads = [l for r in results for l in r.get("lead_time", {}).get("by_event", []) if l is not None]
    pooled["lead_time"] = {"median_lead_min": float(np.median(leads)) if leads else None, "detected": len(leads), "events": pooled["events"]}
    return pooled

