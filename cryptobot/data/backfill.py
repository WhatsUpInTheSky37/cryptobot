"""Historical candle backfill: planning, fetching, and merging.

Pure functions here are unit tested; ``fetch_history`` talks to ccxt.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from cryptobot.core.bars import TIMEFRAME_LABELS, bucket_ms
from cryptobot.core.models import Bar

MAX_PAGE = 720  # conservative page size supported by all four exchanges


class OhlcvSource(Protocol):
    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]: ...

    def milliseconds(self) -> int: ...


def plan_backfill(
    last_closed_ts_ms: int | None,
    now_ms: int,
    timeframe_s: int,
    *,
    default_bars: int = 300,
    max_bars: int = 5000,
) -> tuple[int, int]:
    """Decide what to fetch: returns ``(since_ms, limit)``.

    On first load we fetch ``default_bars`` of history. After a reconnect we
    fetch from the last bar we know is closed (inclusive, so an amended last
    bar is corrected) up to now, capped at ``max_bars``.
    """
    tf_ms = timeframe_s * 1000
    now_bucket = bucket_ms(now_ms, timeframe_s)
    if last_closed_ts_ms is None:
        since = now_bucket - (default_bars - 1) * tf_ms
    else:
        since = bucket_ms(last_closed_ts_ms, timeframe_s)
    n = (now_bucket - since) // tf_ms + 1
    if n > max_bars:
        n = max_bars
        since = now_bucket - (n - 1) * tf_ms
    return since, int(n)


def rows_to_bars(symbol: str, timeframe_s: int, rows: Sequence[Sequence[Any]], now_ms: int) -> list[Bar]:
    tf_ms = timeframe_s * 1000
    out: list[Bar] = []
    for r in rows:
        ts = int(r[0])
        o, h, l, c = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        v = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
        out.append(Bar(symbol, timeframe_s, ts, o, h, l, c, v, 0, ts + tf_ms <= now_ms))
    return out


def merge_bars(existing: Sequence[Bar], fetched: Sequence[Bar]) -> list[Bar]:
    """Union by open time, sorted. A fetched bar replaces an existing one unless
    the existing one is closed and the fetched one is still open."""
    by_ts: dict[int, Bar] = {b.ts_ms: b for b in existing}
    for b in fetched:
        cur = by_ts.get(b.ts_ms)
        if cur is None or b.closed or not cur.closed:
            by_ts[b.ts_ms] = b
    return [by_ts[k] for k in sorted(by_ts)]


def find_gaps(bars: Sequence[Bar], timeframe_s: int) -> list[tuple[int, int]]:
    """Inclusive ``(first_missing_ts, last_missing_ts)`` ranges in a sorted series."""
    tf_ms = timeframe_s * 1000
    gaps: list[tuple[int, int]] = []
    for prev, nxt in zip(bars, bars[1:]):
        expected = prev.ts_ms + tf_ms
        if nxt.ts_ms > expected:
            gaps.append((expected, nxt.ts_ms - tf_ms))
    return gaps


async def fetch_history(
    exchange: OhlcvSource,
    symbol: str,
    timeframe_s: int,
    since_ms: int,
    limit: int,
) -> list[Bar]:
    """Page through ``fetch_ohlcv`` until ``limit`` bars or the present."""
    label = TIMEFRAME_LABELS[timeframe_s]
    tf_ms = timeframe_s * 1000
    out: list[Bar] = []
    since = since_ms
    while len(out) < limit:
        page = min(MAX_PAGE, limit - len(out))
        rows = await exchange.fetch_ohlcv(symbol, label, since, page)
        if not rows:
            break
        now = exchange.milliseconds()
        bars = [b for b in rows_to_bars(symbol, timeframe_s, rows, now) if b.ts_ms >= since]
        if not bars:
            break
        out.extend(bars)
        last_ts = bars[-1].ts_ms
        if last_ts + tf_ms > now or len(rows) < page:
            break
        since = last_ts + tf_ms
    return merge_bars([], out)


def resample_bars(bars: Sequence[Bar], timeframe_s: int) -> list[Bar]:
    """Aggregate finer bars into ``timeframe_s`` bars (open time aligned to the bucket). A bucket is
    closed only when every finer bar in it is closed and the bucket is complete."""
    if not bars:
        return []
    src_tf = bars[0].timeframe_s
    per = max(1, timeframe_s // src_tf)
    out: dict[int, Bar] = {}
    counts: dict[int, int] = {}
    for b in sorted(bars, key=lambda b: b.ts_ms):
        key = bucket_ms(b.ts_ms, timeframe_s)
        cur = out.get(key)
        if cur is None:
            out[key] = Bar(b.symbol, timeframe_s, key, b.open, b.high, b.low, b.close, b.volume, b.trades, False)
            counts[key] = 1
        else:
            cur.high = max(cur.high, b.high)
            cur.low = min(cur.low, b.low)
            cur.close = b.close
            cur.volume += b.volume
            cur.trades += b.trades
            counts[key] += 1
        if not b.closed:
            out[key].closed = False
            counts[key] = -10**9  # an open source bar keeps the bucket open
        elif counts[key] >= per:
            out[key].closed = True
    return [out[k] for k in sorted(out)]


def supported_source_timeframe(exchange: Any, timeframe_s: int) -> int | None:
    """The timeframe to fetch for ``timeframe_s``: itself when the exchange offers it, else the largest
    offered timeframe (>= 1 minute) that divides it, else None. Exchanges without a timeframe list are
    assumed to offer everything."""
    offered = getattr(exchange, "timeframes", None)
    label = TIMEFRAME_LABELS.get(timeframe_s)
    if not offered or label is None or label in offered:
        return timeframe_s
    by_label = {v: k for k, v in TIMEFRAME_LABELS.items()}
    candidates = [by_label[lb] for lb in offered if lb in by_label and by_label[lb] >= 60 and timeframe_s % by_label[lb] == 0 and by_label[lb] < timeframe_s]
    return max(candidates) if candidates else None
