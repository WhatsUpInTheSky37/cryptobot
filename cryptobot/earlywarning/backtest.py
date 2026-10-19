"""Compare entry and exit methods on the recorded setups, strictly out of sample.

Nothing is fitted on the test share: thresholds come from the config, Model B's
target from the training share only. Results on the training share are shown
for reference and never used to pick a method automatically.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from cryptobot.earlywarning.config import EWConfig
from cryptobot.earlywarning.paper import ENTRY_METHODS, EXIT_METHODS, simulate_trade, summarize
from cryptobot.earlywarning.store import EWStore
from cryptobot.earlywarning.targets import _dedupe, collect_setups
from cryptobot.earlywarning.portfolio import replay


def run_methods(store: EWStore, symbols: list[str], cfg: EWConfig, *, lookback_min: int = 60, spreads: dict[str, float | None] | None = None) -> dict[str, Any]:
    """``spreads``: observed spread % per symbol (from taker prints); where known it replaces the assumed spread in fills and the cost gate."""
    data: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}
    cfg_for: dict[str, EWConfig] = {}
    for sym, sp in (spreads or {}).items():
        if sp is not None:
            c = cfg.model_copy(deep=True)
            c.costs.spread_estimate_pct = float(sp)
            cfg_for[sym] = c
    setups: list[dict[str, Any]] = []
    for s in symbols:
        k = store.klines(s, "1m")
        if len(k) == 0:
            continue
        rng = store.aggtrade_range(s)
        tr = store.aggtrades(s, rng[0], rng[1] + 1) if rng else None
        data[s] = (k, tr)
        setups += collect_setups(store, s, k, cfg, lookback_min)
    setups = _dedupe(setups, max(cfg.horizons_min))
    if not setups:
        return {"n_setups": 0, "results": {}, "note": "no setups reached the EAS setup level"}
    cut = int(len(setups) * cfg.train_share)
    train, test = setups[:cut], setups[cut:]
    if test:
        cutoff = test[0]["t0_ms"] - max(cfg.horizons_min) * 60_000
        train = [s for s in train if s["t0_ms"] < cutoff]
    h = str(max(cfg.horizons_min))
    tr_mfe = [s["outcomes"][int(h)]["mfe_pct"] for s in train if int(h) in s["outcomes"]]
    pct_target = float(np.quantile(tr_mfe, cfg.targets.target_quantile)) if len(tr_mfe) >= 10 else None
    results: dict[str, Any] = {}
    for em in ENTRY_METHODS:
        for xm in EXIT_METHODS:
            key = f"{em}/{xm}"
            res = {}
            for name, subset in (("train", train), ("test", test)):
                res[name], trades = replay(subset, data, cfg, em, xm, pct_target, cfg_for)
                res[name]["by_symbol"] = {sym: summarize([t for t in trades if t.symbol == sym], cfg.risk.budget)["net_pct"] for sym in {t.symbol for t in trades}}
            results[key] = res
    return {"n_setups": len(setups), "n_train": len(train), "n_test": len(test), "n_embargoed": len(setups) - len(train) - len(test), "percentile_target_pct": pct_target, "results": results,
            "note": "test = chronologically later setups never used to set anything; compare methods on 'test' only"}

