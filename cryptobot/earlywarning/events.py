"""Phase 2: find rapid upward price movements in 1-minute candles."""
from __future__ import annotations

from typing import Any

import numpy as np

from cryptobot.earlywarning.config import SurgeConfig, SurgeThreshold


def _candidates(k: np.ndarray, th: SurgeThreshold) -> list[tuple[int, int]]:
    """(start_index, trigger_index) pairs where high within ``minutes`` bars >= close*(1+pct)."""
    n = len(k)
    if n == 0:
        return []
    close, high = k["close"], k["high"]
    out = []
    w = th.minutes
    for i in range(n - 1):
        j_end = min(n, i + w + 1)
        seg = high[i + 1:j_end]
        if seg.size == 0 or close[i] <= 0:
            continue
        hit = np.where(seg >= close[i] * (1 + th.pct / 100.0))[0]
        if hit.size:
            out.append((i, i + 1 + int(hit[0])))
    return out


def detect_events(k: np.ndarray, cfg: SurgeConfig, trades: np.ndarray | None = None) -> list[dict[str, Any]]:
    """Merged surge events with realistic statistics. ``trades`` (aggTrades) are optional and only
    used to estimate the executable bid at the peak (last sell-taker print near the peak)."""
    n = len(k)
    if n < 3:
        return []
    spans: list[tuple[int, int, str]] = []
    for th in cfg.thresholds:
        for s, t in _candidates(k, th):
            spans.append((s, t, f"+{th.pct:g}%/{th.minutes}m"))
    if not spans:
        return []
    spans.sort()
    merged: list[list[Any]] = []
    for s, t, label in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], t)
            merged[-1][2].add(label)
        else:
            merged.append([s, t, {label}])
    ts, close, high, low, qv = k["ts"], k["close"], k["high"], k["low"], k["quote_volume"]
    events = []
    for s, t, labels in merged:
        # the move begins at the lowest low in the window before the trigger
        lo_from = max(0, t - max(th.minutes for th in cfg.thresholds))
        start = lo_from + int(np.argmin(low[lo_from:t + 1]))
        pre = float(close[start]) if start > 0 else float(low[start])
        end = min(n, t + cfg.max_peak_search_minutes + 1)
        seg_high = high[t:end]
        # walk forward: the peak is the highest high before the reversal (retrace of the gain)
        peak_rel = 0
        peak = float(seg_high[0])
        reversal_idx = None
        for j in range(end - t):
            h = float(high[t + j])
            if h > peak:
                peak, peak_rel = h, j
            gain = peak - pre
            if gain > 0 and float(low[t + j]) <= peak - cfg.reversal_retrace * gain and j > peak_rel:
                reversal_idx = t + j
                break
        peak_idx = t + peak_rel
        gain_pct = (peak / pre - 1.0) * 100.0 if pre > 0 else 0.0
        post_end = min(n, peak_idx + cfg.post_peak_minutes + 1)
        post_low = float(np.min(low[peak_idx:post_end])) if post_end > peak_idx else peak
        decline = (post_low / peak - 1.0) * 100.0 if peak > 0 else 0.0
        peak_bid = executable_bid(trades, int(ts[peak_idx]), peak) if trades is not None and len(trades) else None
        events.append({
            "start_ms": int(ts[start]), "trigger_ms": int(ts[t]), "peak_ms": int(ts[peak_idx]), "pre_price": pre, "peak_price": peak, "peak_bid": peak_bid,
            "gain_pct": gain_pct, "minutes_to_peak": float((ts[peak_idx] - ts[start]) / 60_000), "reversal_ms": int(ts[reversal_idx]) if reversal_idx is not None else None,
            "minutes_to_reversal": float((ts[reversal_idx] - ts[peak_idx]) / 60_000) if reversal_idx is not None else None, "max_decline_pct": decline,
            "quote_volume": float(np.sum(qv[start:peak_idx + 1])), "thresholds": sorted(labels), "start_index": int(start), "trigger_index": int(t), "peak_index": int(peak_idx),
        })
    # merge events whose surge windows still overlap after start/peak expansion
    out: list[dict[str, Any]] = []
    for e in events:
        if out and e["start_ms"] <= out[-1]["peak_ms"]:
            prev = out[-1]
            if e["peak_price"] > prev["peak_price"]:
                for key in ("peak_ms", "peak_price", "peak_bid", "minutes_to_peak", "reversal_ms", "minutes_to_reversal", "max_decline_pct", "peak_index"):
                    prev[key] = e[key]
                prev["gain_pct"] = (prev["peak_price"] / prev["pre_price"] - 1.0) * 100.0
                prev["minutes_to_peak"] = (prev["peak_ms"] - prev["start_ms"]) / 60_000
            prev["quote_volume"] += e["quote_volume"]
            prev["thresholds"] = sorted(set(prev["thresholds"]) | set(e["thresholds"]))
            continue
        out.append(e)
    return out


def executable_bid(trades: np.ndarray, at_ms: int, fallback_price: float, window_ms: int = 60_000, spread_est_pct: float = 0.2) -> float:
    """A price a seller could actually have hit near ``at_ms``: the last trade where the buyer was
    the maker (an aggressive sell into the bid) within the window; else the price less an assumed spread."""
    lo, hi = at_ms - window_ms, at_ms + window_ms
    m = (trades["ts"] >= lo) & (trades["ts"] <= hi) & (trades["buyer_maker"] == 1)
    if m.any():
        return float(trades["price"][m][-1])
    return fallback_price * (1 - spread_est_pct / 100.0)
