"""Phase 3: reconstruct buying activity in the windows before a point in time.

Works from 1-minute klines alone (they carry taker-buy volume and trade counts)
and uses aggregated trades when they are available for trade-size statistics.
Every relative metric compares the window with the ``baseline`` period that
precedes it, so a value of 1.0 means "normal for this coin at that time".
"""
from __future__ import annotations

from typing import Any

import numpy as np

FEATURE_KEYS = ("rel_volume", "imbalance", "freq_change", "median_size_change", "p90_size_change", "large_cluster", "absorption", "compression", "activity_change", "persistence", "move_pct")
FEATURE_NAMES = {
    "rel_volume": "Relative volume",
    "imbalance": "Aggressive buy/sell imbalance",
    "freq_change": "Trade frequency change",
    "median_size_change": "Median trade size change",
    "p90_size_change": "Upper-percentile trade size change",
    "large_cluster": "Large-trade clusters",
    "absorption": "Buying volume per % of price move",
    "compression": "Volatility compression",
    "activity_change": "Trading activity change",
    "persistence": "Persistence of buying pressure",
    "move_pct": "Price move in window",
}
MIN_BASELINE_BARS = 60


def _slice(k: np.ndarray, lo_ms: int, hi_ms: int) -> np.ndarray:
    """Rows with lo <= ts < hi; ``k`` is sorted by ts so this is a binary search, not a scan."""
    i, j = np.searchsorted(k["ts"], lo_ms, side="left"), np.searchsorted(k["ts"], hi_ms, side="left")
    return k[i:j]


def _rolling_range_pct(k: np.ndarray, bars: int) -> np.ndarray:
    """Range (high-low)/close over consecutive blocks of ``bars`` bars, in %."""
    n = len(k) // bars
    if n == 0:
        return np.zeros(0)
    out = []
    for i in range(n):
        seg = k[i * bars:(i + 1) * bars]
        c = float(seg["close"][-1]) or 1.0
        out.append((float(seg["high"].max()) - float(seg["low"].min())) / c * 100.0)
    return np.array(out)


def window_features(k: np.ndarray, t0_ms: int, lookback_min: int, baseline_min: int, trades: np.ndarray | None = None, *, large_pct: float = 99.0) -> dict[str, Any] | None:
    """Features of the window [t0 - lookback, t0) relative to the baseline before it. None when the
    window has no bars or the baseline is too short to say what "normal" is."""
    lb_ms = lookback_min * 60_000
    w = _slice(k, t0_ms - lb_ms, t0_ms)
    b = _slice(k, t0_ms - lb_ms - baseline_min * 60_000, t0_ms - lb_ms)
    if len(w) == 0 or len(b) < MIN_BASELINE_BARS:
        return None
    w_min = max(1.0, (float(w["ts"][-1]) - float(w["ts"][0])) / 60_000 + 1.0)
    b_min = max(1.0, (float(b["ts"][-1]) - float(b["ts"][0])) / 60_000 + 1.0)
    wq, bq = float(w["quote_volume"].sum()), float(b["quote_volume"].sum())
    rel_volume = (wq / w_min) / (bq / b_min) if bq > 0 else None
    wbuy = float(w["taker_buy_quote"].sum())
    imbalance = (2 * wbuy - wq) / wq if wq > 0 else 0.0
    wt, bt = float(w["trades"].sum()), float(b["trades"].sum())
    freq_change = (wt / w_min) / (bt / b_min) if bt > 0 else None
    source = "klines"
    median_size_change = p90_size_change = large_cluster = None
    if trades is not None and len(trades):
        tw = _slice(trades, t0_ms - lb_ms, t0_ms)
        tb = _slice(trades, t0_ms - lb_ms - baseline_min * 60_000, t0_ms - lb_ms)
        if len(tw) >= 5 and len(tb) >= 50:
            sw, sb = tw["price"] * tw["qty"], tb["price"] * tb["qty"]
            median_size_change = float(np.median(sw) / max(1e-12, np.median(sb)))
            p90_size_change = float(np.percentile(sw, 90) / max(1e-12, np.percentile(sb, 90)))
            big = float(np.percentile(sb, large_pct))
            large_cluster = float(np.sum(sw > big) / w_min) / max(1e-9, float(np.sum(sb > big)) / b_min)
            buy_w = float(sw[tw["buyer_maker"] == 0].sum())
            if sw.sum() > 0:
                imbalance = (2 * buy_w - float(sw.sum())) / float(sw.sum())
            source = "aggtrades"
    if median_size_change is None:
        # kline fallback: average trade size per bar
        aw = w["quote_volume"][w["trades"] > 0] / np.maximum(1, w["trades"][w["trades"] > 0])
        ab = b["quote_volume"][b["trades"] > 0] / np.maximum(1, b["trades"][b["trades"] > 0])
        if len(aw) and len(ab) >= 20:
            median_size_change = float(np.median(aw) / max(1e-12, np.median(ab)))
            p90_size_change = float(np.percentile(aw, 90) / max(1e-12, np.percentile(ab, 90)))
            big = float(np.percentile(ab, large_pct))
            large_cluster = float(np.sum(aw > big) / w_min) / max(1e-9, float(np.sum(ab > big)) / b_min)
    c0, c1 = float(w["open"][0]), float(w["close"][-1])
    move_pct = (c1 / c0 - 1.0) * 100.0 if c0 > 0 else 0.0
    rel_buy = (wbuy / w_min) / ((float(b["taker_buy_quote"].sum()) / b_min) or 1e-12) if bq > 0 else 0.0
    absorption = rel_buy / max(abs(move_pct), 0.05)
    bars = max(1, len(w))
    w_range = (float(w["high"].max()) - float(w["low"].min())) / (c1 or 1.0) * 100.0
    b_ranges = _rolling_range_pct(b, bars)
    compression = float(np.median(b_ranges) / w_range) if len(b_ranges) and w_range > 0 else None
    active_w = float(np.mean(w["trades"] > 0))
    active_b = float(np.mean(b["trades"] > 0)) or 1e-9
    activity_change = active_w / active_b
    parts = np.array_split(w, min(4, len(w))) if len(w) >= 2 else [w]
    pos = 0
    for p in parts:
        q = float(p["quote_volume"].sum())
        if q > 0 and 2 * float(p["taker_buy_quote"].sum()) > q:
            pos += 1
    persistence = pos / len(parts)
    return {
        "rel_volume": rel_volume, "imbalance": imbalance, "freq_change": freq_change, "median_size_change": median_size_change, "p90_size_change": p90_size_change,
        "large_cluster": large_cluster, "absorption": absorption, "compression": compression, "activity_change": activity_change, "persistence": persistence,
        "move_pct": move_pct, "n_bars": int(len(w)), "n_baseline_bars": int(len(b)), "source": source, "quote_volume": wq,
    }


def price_changes(k: np.ndarray, t0_ms: int, minutes: tuple[int, ...] = (5, 15, 30, 60)) -> dict[int, float | None]:
    """% change of the close over the last N minutes ending at t0 (exclusive)."""
    out: dict[int, float | None] = {}
    upto = k[: np.searchsorted(k["ts"], t0_ms, side="left")]
    if len(upto) == 0:
        return {m: None for m in minutes}
    last = float(upto["close"][-1])
    for m in minutes:
        before = upto[: np.searchsorted(upto["ts"], t0_ms - m * 60_000, side="left")]
        out[m] = (last / float(before["close"][-1]) - 1.0) * 100.0 if len(before) and before["close"][-1] > 0 else None
    return out
