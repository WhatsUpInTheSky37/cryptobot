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
