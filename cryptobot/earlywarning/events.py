"""Phase 2: find rapid upward price movements in 1-minute candles."""
from __future__ import annotations

from typing import Any

import numpy as np

from cryptobot.earlywarning.config import SurgeConfig, SurgeThreshold


def trailing_range_pct(k: np.ndarray, bars: int = 60) -> np.ndarray:
    """Per bar: (highest high - lowest low) / close over the previous ``bars`` bars, in percent (0 where unknown)."""
    n = len(k)
    out = np.zeros(n)
    if n == 0:
        return out
    high, low, close = k["high"], k["low"], k["close"]
    for i in range(n):
        lo = max(0, i - bars)
        if i - lo < 5 or close[i] <= 0:
            continue
        out[i] = (float(high[lo:i].max()) - float(low[lo:i].min())) / float(close[i]) * 100.0
    return out


def _candidates(k: np.ndarray, th: SurgeThreshold, *, confirm_on_close: bool = True, vol_multiple: float = 0.0, rng_pct: np.ndarray | None = None) -> list[tuple[int, int]]:
    """(start_index, trigger_index) pairs where price within ``minutes`` bars reaches close*(1+pct).

    With ``confirm_on_close`` the CLOSE of a later bar must reach the level (a wick does not count).
    With ``vol_multiple`` > 0 the level is at least ``vol_multiple`` times the trailing 60-minute range."""
    n = len(k)
    if n == 0:
        return []
    close = k["close"]
    ref = close if confirm_on_close else k["high"]
    out = []
    w = th.minutes
    for i in range(n - 1):
        j_end = min(n, i + w + 1)
        seg = ref[i + 1:j_end]
        if seg.size == 0 or close[i] <= 0:
            continue
        pct = th.pct
        if vol_multiple > 0 and rng_pct is not None:
            pct = max(pct, vol_multiple * float(rng_pct[i]))
        hit = np.where(seg >= close[i] * (1 + pct / 100.0))[0]
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
    rng_pct = trailing_range_pct(k) if cfg.vol_multiple > 0 else None
    for th in cfg.thresholds:
        for s, t in _candidates(k, th, confirm_on_close=cfg.confirm_on_close, vol_multiple=cfg.vol_multiple, rng_pct=rng_pct):
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
    ts, close, high, low, qv, ntr = k["ts"], k["close"], k["high"], k["low"], k["quote_volume"], k["trades"]
    events = []
    for s, t, labels in merged:
        # the move begins at the lowest low in the window before the trigger
        lo_from = max(0, t - max(th.minutes for th in cfg.thresholds))
        seg_low = low[lo_from:t + 1]
        start = lo_from + int(len(seg_low) - 1 - np.argmin(seg_low[::-1]))  # the LAST time price sat at the low: where the move left from
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
        # volume and trades DURING the surge (start to the bar that crossed the threshold): what a trader had to trade through
        n_trades = int(np.sum(ntr[start:t + 1]))
        quote_volume = float(np.sum(qv[start:t + 1]))
        events.append({
            "start_ms": int(ts[start]), "trigger_ms": int(ts[t]), "peak_ms": int(ts[peak_idx]), "pre_price": pre, "peak_price": peak, "peak_bid": peak_bid,
            "gain_pct": gain_pct, "minutes_to_peak": float((ts[peak_idx] - ts[start]) / 60_000), "reversal_ms": int(ts[reversal_idx]) if reversal_idx is not None else None,
            "minutes_to_reversal": float((ts[reversal_idx] - ts[peak_idx]) / 60_000) if reversal_idx is not None else None, "max_decline_pct": decline,
            "quote_volume": quote_volume, "trades": n_trades, "thresholds": sorted(labels), "start_index": int(start), "trigger_index": int(t), "peak_index": int(peak_idx),
            # the peak sits at the end of the search window: price was still rising when we stopped looking
            "peak_truncated": bool(peak_idx >= end - 1 and reversal_idx is None),
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
            prev["trades"] += e["trades"]
            prev["peak_truncated"] = prev["peak_truncated"] or e["peak_truncated"]
            prev["thresholds"] = sorted(set(prev["thresholds"]) | set(e["thresholds"]))
            continue
        out.append(e)
    for e in out:
        e["tradeable"] = is_tradeable(e, cfg)
    return out


def is_tradeable(e: dict[str, Any], cfg: SurgeConfig) -> bool:
    if e.get("quote_volume") is None or e.get("trades") is None:
        return bool(e.get("tradeable", True))
    return bool(e["quote_volume"] >= cfg.tradeable_quote_volume and e["trades"] >= cfg.tradeable_min_trades)


def tradeable(events: list[dict[str, Any]], cfg: SurgeConfig) -> list[dict[str, Any]]:
    """The events the study should learn from: all of them, or only the tradeable ones when configured."""
    return [e for e in events if is_tradeable(e, cfg)] if cfg.study_tradeable_only else list(events)


def executable_bid(trades: np.ndarray, at_ms: int, fallback_price: float, window_ms: int = 60_000, spread_est_pct: float = 0.2) -> float:
    """A price a seller could actually have hit near ``at_ms``: the last trade where the buyer was
    the maker (an aggressive sell into the bid) within the window; else the price less an assumed spread."""
    lo, hi = at_ms - window_ms, at_ms + window_ms
    m = (trades["ts"] >= lo) & (trades["ts"] <= hi) & (trades["buyer_maker"] == 1)
    if m.any():
        return float(trades["price"][m][-1])
    return fallback_price * (1 - spread_est_pct / 100.0)


def observed_spread_pct(trades: np.ndarray | None, *, min_minutes: int = 50) -> float | None:
    """Median bid/ask spread in percent, estimated from aggregated trades: within each minute that has
    both an aggressive buy (at the ask) and an aggressive sell (at the bid), the gap between the last of
    each. None with fewer than ``min_minutes`` such minutes. A conservative stand-in for the order book
    the exchange does not keep."""
    if trades is None or len(trades) == 0:
        return None
    minute = trades["ts"] // 60_000
    buy = trades["buyer_maker"] == 0
    spreads = []
    for m in np.unique(minute):
        sel = minute == m
        b = trades["price"][sel & buy]
        a = trades["price"][sel & ~buy]
        if b.size and a.size:
            ask, bid = float(b[-1]), float(a[-1])
            if ask >= bid > 0:
                spreads.append((ask - bid) / ((ask + bid) / 2) * 100.0)
    if len(spreads) < min_minutes:
        return None
    return float(np.median(spreads))
