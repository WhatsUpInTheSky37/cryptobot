"""Phase 8: conservative targets.

Model A: probability of reaching +up% before -down% within each horizon, estimated for a
new setup from the k most similar past setups (component ranks), validated walk-forward.
Model B: the distribution of maximum favourable excursion (MFE) per horizon over ALL past
setups, winners and losers alike; the conservative target is a low quantile of it, checked
out of sample by coverage (how often the realised MFE reached the predicted target).
When the out-of-sample evidence is thin, the label is TARGET NOT YET RELIABLE.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from cryptobot.earlywarning.config import COMPONENTS, EWConfig
from cryptobot.earlywarning.store import EWStore

NOT_RELIABLE = "TARGET NOT YET RELIABLE"


def outcome_after(k: np.ndarray, t0_ms: int, horizon_min: int, up_pct: float, down_pct: float, entry_slip_pct: float) -> dict[str, Any] | None:
    """What happened in the ``horizon_min`` minutes after t0 for an entry at the next bar's open."""
    after = k[k["ts"] >= t0_ms]
    if len(after) < 2:
        return None
    entry = float(after["open"][0]) * (1 + entry_slip_pct / 100.0)
    seg = after[: horizon_min]
    if entry <= 0 or len(seg) != horizon_min or not 0 <= int(seg["ts"][0]) - t0_ms < 60_000 or np.any(np.diff(seg["ts"]) != 60_000):
        return None
    highs = seg["high"] / entry - 1.0
    lows = seg["low"] / entry - 1.0
    up_i = np.where(highs >= up_pct / 100.0)[0]
    dn_i = np.where(lows <= -down_pct / 100.0)[0]
    hit = bool(up_i.size) and (not dn_i.size or up_i[0] < dn_i[0])
    stopped = bool(dn_i.size) and (not up_i.size or dn_i[0] <= up_i[0])
    stop_index = int(dn_i[0]) if dn_i.size else len(seg)
    before_stop = highs[:stop_index]  # same-bar stop wins an ambiguous OHLC tie
    return {"entry": entry, "mfe_pct": float(before_stop.max() * 100.0) if len(before_stop) else 0.0, "mae_pct": float(lows.min() * 100.0), "hit": hit, "stopped": stopped, "close_pct": float((seg["close"][-1] / entry - 1.0) * 100.0), "bars": int(len(seg)),
            "high_path": highs.tolist(), "low_path": lows.tolist()}


def collect_setups(store: EWStore, symbol: str, k: np.ndarray, cfg: EWConfig, lookback_min: int = 60) -> list[dict[str, Any]]:
    """Every 60-minute window the scanner would have seen (grid, event, control) whose EAS reached the setup level, with outcomes."""
    out = []
    slip = cfg.costs.spread_estimate_pct / 2 + cfg.costs.base_slippage_pct
    rows = [w for w in store.windows(symbol) if w["kind"] == "grid"]
    for w in rows:
        if w["eas"] is None or w["eas"] < cfg.targets.setup_eas:
            continue
        comps = w["components"]
        if comps.get("status") != "ok":
            continue
        rec = {"symbol": symbol, "t0_ms": w["t0_ms"], "eas": w["eas"], "kind": w["kind"], "vec": [float(comps[c]) for c in COMPONENTS], "outcomes": {}}
        for h in cfg.horizons_min:
            o = outcome_after(k, w["t0_ms"], h, cfg.targets.up_pct, cfg.exits.stop_pct, slip)
            if o is not None:
                rec["outcomes"][h] = o
        if rec["outcomes"]:
            out.append(rec)
    return out


def _knn_p(train: list[dict[str, Any]], vec: list[float], h: int, k: int = 30) -> float | None:
    rows = [(t["vec"], t["outcomes"][h]["hit"]) for t in train if h in t["outcomes"]]
    if len(rows) < 10:
        return None
    X = np.array([r[0] for r in rows])
    sd = X.std(axis=0) + 1e-9
    d = np.sqrt((((X - np.array(vec)) / sd) ** 2).sum(axis=1))
    idx = np.argsort(d)[: min(k, len(rows))]
    return float(np.mean([rows[i][1] for i in idx]))


def _dedupe(setups: list[dict[str, Any]], overlap_min: int) -> list[dict[str, Any]]:
    """Setups on the same coin within ``overlap_min`` of each other are one observation (keep the first)."""
    out: list[dict[str, Any]] = []
    last: dict[str, int] = {}
    for s in sorted(setups, key=lambda s: s["t0_ms"]):
        if s["symbol"] in last and s["t0_ms"] - last[s["symbol"]] < overlap_min * 60_000:
            continue
        last[s["symbol"]] = s["t0_ms"]
        out.append(s)
    return out


def walk_forward_targets(setups: list[dict[str, Any]], cfg: EWConfig, *, n_folds: int = 4) -> dict[str, Any]:
    """Chronological expanding-window validation of Model A (probability) and Model B (MFE quantile target)."""
    setups = _dedupe(setups, max(cfg.horizons_min))
    report: dict[str, Any] = {"n_setups": len(setups), "horizons": {}, "policy_version": "causal-v2"}
    rt_cost = 2 * cfg.costs.fee_rate * 100 + cfg.costs.spread_estimate_pct + 2 * cfg.costs.base_slippage_pct
    if len(setups) < 20:
        for h in cfg.horizons_min:
            report["horizons"][str(h)] = {"label": NOT_RELIABLE, "reliable": False, "n_oos": 0, "reason": f"only {len(setups)} independent setups"}
        return report
    n = len(setups)
    edges = np.linspace(max(10, n // 3), n, n_folds + 1).astype(int)
    for h in cfg.horizons_min:
        preds, actual, mfes, targets_pred, policy_nets = [], [], [], [], []
        base_rates = []
        for i in range(n_folds):
            tr_end, te_end = int(edges[i]), int(edges[i + 1])
            # embargo: training setups whose horizon overlaps the test start are dropped
            cutoff = setups[tr_end]["t0_ms"] - h * 60_000 if tr_end < n else None
            train = [s for s in setups[:tr_end] if cutoff is None or s["t0_ms"] < cutoff]
            test = setups[tr_end:te_end]
            tr_mfe = np.array([s["outcomes"][h]["mfe_pct"] for s in train if h in s["outcomes"]])
            if len(tr_mfe) < 10 or not test:
                continue
            q_target = float(np.quantile(tr_mfe, cfg.targets.target_quantile))
            base = float(np.mean([s["outcomes"][h]["hit"] for s in train if h in s["outcomes"]]))
            for s in test:
                if h not in s["outcomes"]:
                    continue
                p = _knn_p(train, s["vec"], h)
                if p is None:
                    continue
                preds.append(p)
                base_rates.append(base)
                actual.append(1.0 if s["outcomes"][h]["hit"] else 0.0)
                mfes.append(s["outcomes"][h]["mfe_pct"])
                targets_pred.append(q_target)
                o = s["outcomes"][h]
                highs, lows = np.array(o.get("high_path", [])), np.array(o.get("low_path", []))
                if len(highs) == h:
                    up = np.flatnonzero(highs >= q_target / 100)
                    dn = np.flatnonzero(lows <= -cfg.exits.stop_pct / 100)
                    gross = q_target if len(up) and (not len(dn) or up[0] < dn[0]) else -cfg.exits.stop_pct if len(dn) else o["close_pct"]
                    policy_nets.append(gross - rt_cost)
        n_oos = len(actual)
        if n_oos == 0:
            report["horizons"][str(h)] = {"label": NOT_RELIABLE, "reliable": False, "n_oos": 0, "reason": "no out-of-sample setups"}
            continue
        a, p, b, m, t = (np.array(x) for x in (actual, preds, base_rates, mfes, targets_pred))
        brier_model = float(np.mean((p - a) ** 2))
        brier_base = float(np.mean((b - a) ** 2))
        coverage = float(np.mean(m >= t))
        all_mfe = np.array([s["outcomes"][h]["mfe_pct"] for s in setups if h in s["outcomes"]])
        target_gross = float(np.quantile(all_mfe, cfg.targets.target_quantile))
        prob_ok = n_oos >= cfg.targets.min_setups and brier_model < brier_base * 0.98  # must beat the base rate, not tie it
        target_ok = n_oos >= cfg.targets.min_setups and coverage >= cfg.targets.min_coverage
        net = target_gross - rt_cost
        policy_ev = float(np.mean(policy_nets)) if policy_nets else None
        tradeable = target_ok and net > 0 and policy_ev is not None and policy_ev > 0
        if not target_ok:
            label = NOT_RELIABLE
        elif tradeable:
            label = f"target {net:+.2f}% net (p{int(cfg.targets.target_quantile * 100)} of MFE, {coverage:.0%} OOS coverage)"
        else:
            label = f"validated but NOT tradeable: p{int(cfg.targets.target_quantile * 100)} target {target_gross:+.2f}% gross is {net:+.2f}% after costs"
        report["horizons"][str(h)] = {
            "n_oos": n_oos, "oos_hit_rate": float(a.mean()), "oos_model_mean_p": float(p.mean()), "brier_model": brier_model, "brier_base": brier_base,
            "probability_validated": bool(prob_ok), "target_gross_pct": target_gross, "target_net_pct": net, "coverage": coverage,
            "mfe_quantiles": {"p10": float(np.quantile(all_mfe, 0.1)), "p25": float(np.quantile(all_mfe, 0.25)), "p50": float(np.quantile(all_mfe, 0.5)), "p75": float(np.quantile(all_mfe, 0.75))},
            "reliable": bool(target_ok), "tradeable": bool(tradeable), "label": label,
            "first_passage_net_pct": policy_ev, "stop_pct": cfg.exits.stop_pct, "horizon_min": h,
            "reason": "" if target_ok else f"{n_oos} OOS setups (need {cfg.targets.min_setups}), coverage {coverage:.0%} (need {cfg.targets.min_coverage:.0%})",
        }
    return report


def threshold_sweep(setups: list[dict[str, Any]], cfg: EWConfig, spreads: dict[str, float | None] | None = None, *, horizon_min: int | None = None, step: float = 5.0, min_n: int = 30) -> dict[str, Any]:
    """Pick the EAS threshold by expected value, not by eye: for each level from the setup EAS upward, the
    independent setups at or above it, their hit rate, and the mean net return at the horizon after a
    round trip's costs (the pair's observed spread where known). In-sample over all setups; the best
    level needs ``min_n`` setups. Nothing here is a profit claim."""
    h = horizon_min or max(cfg.horizons_min)
    setups = _dedupe(setups, max(cfg.horizons_min))
    rows = []
    top = max((s["eas"] for s in setups), default=cfg.targets.setup_eas)
    thr = cfg.targets.setup_eas
    while thr <= min(100.0, top + 1e-9):
        sub = [s for s in setups if s["eas"] >= thr and h in s["outcomes"]]
        if not sub:
            break
        nets = []
        for s in sub:
            sp = (spreads or {}).get(s["symbol"])
            sp = cfg.costs.spread_estimate_pct if sp is None else sp
            rt = 2 * cfg.costs.fee_rate * 100 + sp + 2 * cfg.costs.base_slippage_pct
            nets.append(s["outcomes"][h]["close_pct"] - rt)
        rows.append({"threshold": thr, "n": len(sub), "hit_rate": float(np.mean([s["outcomes"][h]["hit"] for s in sub])), "mean_net_pct": float(np.mean(nets)), "median_net_pct": float(np.median(nets)),
                     "share_positive": float(np.mean([x > 0 for x in nets])), "mean_mfe_pct": float(np.mean([s["outcomes"][h]["mfe_pct"] for s in sub]))})
        thr += step
    ok = [r for r in rows if r["n"] >= min_n]
    best = max(ok, key=lambda r: r["mean_net_pct"]) if ok else None
    return {"horizon_min": h, "rows": rows, "best": best, "min_n": min_n, "note": "in-sample: every setup, no hold-out; use it to choose where to look, not as a result"}


class TargetEstimator:
    """Live use of a validated report: returns None where the report says the target is not reliable."""

    def __init__(self, report: dict[str, Any] | None, setups: list[dict[str, Any]] | None = None) -> None:
        self.report = report or {"horizons": {}}
        self.setups = setups or []

    def target(self, horizon_min: int) -> tuple[float | None, str]:
        """(net target % if validated AND positive after costs, label). None means: do not trade on it."""
        if self.report.get("policy_version") != "causal-v2":
            return None, NOT_RELIABLE + (": rebuild research with causal-v2" if self.report.get("horizons") else "")
        h = self.report.get("horizons", {}).get(str(horizon_min))
        if not h or not h.get("reliable"):
            return None, NOT_RELIABLE
        if not h.get("tradeable"):
            return None, h["label"]
        return float(h["target_net_pct"]), h["label"]

    def gross_target(self, horizon_min: int) -> float | None:
        if self.report.get("policy_version") != "causal-v2":
            return None
        h = self.report.get("horizons", {}).get(str(horizon_min))
        return float(h["target_gross_pct"]) if h and h.get("reliable") else None

    def probability(self, horizon_min: int, vec: list[float]) -> float | None:
        if self.report.get("policy_version") != "causal-v2":
            return None
        h = self.report.get("horizons", {}).get(str(horizon_min))
        if not h or not h.get("probability_validated"):
            return None
        return _knn_p(self.setups, vec, horizon_min)

