"""Turn a stream of trades into fixed-interval OHLCV bars.

The same aggregator is used for the 1-second display candles and for the
strategy timeframes, and it is deterministic so it can be unit tested.
"""
from __future__ import annotations

from cryptobot.core.models import Bar, Tick

TIMEFRAME_LABELS: dict[int, str] = {
    1: "1s",
    60: "1m",
    300: "5m",
    900: "15m",
    3600: "1h",
    14400: "4h",
    86400: "1d",
}


def bucket_ms(ts_ms: int, timeframe_s: int) -> int:
    """Open time of the bar that contains ``ts_ms``."""
    tf_ms = timeframe_s * 1000
    return ts_ms - (ts_ms % tf_ms)


class BarAggregator:
    """Builds bars for one symbol and one timeframe.

    ``add_tick`` and ``roll`` both return the list of bars that changed, so a
    consumer can simply upsert them by open time. A closed bar may be returned
    a second time if a late trade for it arrives; that is an amendment.

    When trades are sparse, the gap between the last bar and the next trade is
    filled with flat, zero-volume bars (open = high = low = close = previous
    close) so the series stays contiguous. ``max_gap_fill`` caps how many such
    bars are produced after a long outage; beyond that the series restarts.
    """

    def __init__(
        self,
        symbol: str,
        timeframe_s: int,
        *,
        fill_gaps: bool = True,
        max_gap_fill: int = 600,
    ) -> None:
        if timeframe_s <= 0:
            raise ValueError("timeframe_s must be positive")
        self.symbol = symbol
        self.timeframe_s = timeframe_s
        self.fill_gaps = fill_gaps
        self.max_gap_fill = max_gap_fill
        self.current: Bar | None = None
        self.last_closed: Bar | None = None
        self.late_ticks = 0
        self.dropped_ticks = 0

    # -- public API ---------------------------------------------------------

    def add_tick(self, tick: Tick) -> list[Bar]:
        b = bucket_ms(tick.ts_ms, self.timeframe_s)
        cur = self.current
        if cur is None:
            self.current = self._new_bar(b, tick.price)
            self.current.apply(tick.price, tick.amount)
            return [self.current]
        if b == cur.ts_ms:
            cur.apply(tick.price, tick.amount)
            return [cur]
        if b > cur.ts_ms:
            changed = self._advance_to(b)
            self.current = self._new_bar(b, tick.price)
            self.current.apply(tick.price, tick.amount)
            changed.append(self.current)
            return changed
        # Late tick for an earlier bar.
        self.late_ticks += 1
        lc = self.last_closed
        if lc is not None and b == lc.ts_ms:
            lc.apply(tick.price, tick.amount)
            return [lc]
        self.dropped_ticks += 1
        return []

    def roll(self, now_ms: int) -> list[Bar]:
        """Close any bar whose period has ended by ``now_ms`` even with no trades.

        Called on a heartbeat so the chart keeps moving when a market is quiet.
        """
        cur = self.current
        if cur is None:
            return []
        b = bucket_ms(now_ms, self.timeframe_s)
        if b <= cur.ts_ms:
            return []
        changed = self._advance_to(b)
        self.current = self._placeholder(b, self.last_closed.close)  # type: ignore[union-attr]
        changed.append(self.current)
        return changed

    def seed(self, bar: Bar) -> None:
        """Adopt an externally fetched bar (from REST backfill) as the current bar."""
        if bar.timeframe_s != self.timeframe_s or bar.symbol != self.symbol:
            raise ValueError("seed bar does not match aggregator")
        if bar.closed:
            self.last_closed = bar
            self.current = None
        else:
            self.current = bar

    def reset(self) -> None:
        self.current = None
        self.last_closed = None

    # -- internals ----------------------------------------------------------

    def _new_bar(self, ts_ms: int, price: float) -> Bar:
        return Bar(self.symbol, self.timeframe_s, ts_ms, price, price, price, price)

    def _placeholder(self, ts_ms: int, price: float) -> Bar:
        return Bar(self.symbol, self.timeframe_s, ts_ms, price, price, price, price, 0.0, 0, False)

    def _advance_to(self, new_bucket_ms: int) -> list[Bar]:
        """Close the current bar and emit gap fillers up to (not including) ``new_bucket_ms``."""
        cur = self.current
        assert cur is not None
        cur.closed = True
        self.last_closed = cur
        out: list[Bar] = [cur]
        if not self.fill_gaps:
            return out
        tf_ms = self.timeframe_s * 1000
        missing = (new_bucket_ms - cur.end_ms) // tf_ms
        if missing <= 0:
            return out
        if missing > self.max_gap_fill:
            # Too long an outage to pretend the market was flat; leave a hole.
            return out
        t = cur.end_ms
        while t < new_bucket_ms:
            filler = self._placeholder(t, cur.close)
            filler.closed = True
            out.append(filler)
            self.last_closed = filler
            t += tf_ms
        return out


def resample(bars: list[Bar], timeframe_s: int) -> list[Bar]:
    """Combine finer bars into coarser ones. Input must be sorted by ``ts_ms``."""
    out: list[Bar] = []
    for b in bars:
        if b.timeframe_s > timeframe_s:
            raise ValueError("cannot resample to a finer timeframe")
        bucket = bucket_ms(b.ts_ms, timeframe_s)
        if out and out[-1].ts_ms == bucket:
            o = out[-1]
            o.high = max(o.high, b.high)
            o.low = min(o.low, b.low)
            o.close = b.close
            o.volume += b.volume
            o.trades += b.trades
            o.closed = b.closed and b.end_ms >= o.end_ms
        else:
            out.append(
                Bar(
                    b.symbol,
                    timeframe_s,
                    bucket,
                    b.open,
                    b.high,
                    b.low,
                    b.close,
                    b.volume,
                    b.trades,
                    b.closed and b.end_ms >= bucket + timeframe_s * 1000,
                )
            )
    return out
