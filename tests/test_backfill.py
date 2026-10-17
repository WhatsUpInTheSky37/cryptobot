import asyncio

import ccxt
import pytest

from cryptobot.core.events import BarsEvent, ConnectionEvent
from cryptobot.core.models import Bar
from cryptobot.data.backfill import fetch_history, find_gaps, merge_bars, plan_backfill
from cryptobot.data.feed import MarketFeed, is_geo_block

S = "BTC/USD"
MIN = 60_000


def bar(ts: int, close: float = 1.0, closed: bool = True, tf: int = 60) -> Bar:
    return Bar(S, tf, ts, close, close, close, close, 1.0, 1, closed)


def test_plan_first_load_fetches_default_window():
    now = 100 * MIN + 30_000
    since, n = plan_backfill(None, now, 60, default_bars=10)
    assert n == 10
    assert since == 91 * MIN  # 10 bars ending at the current (partial) bar


def test_plan_after_gap_refetches_from_last_closed():
    now = 100 * MIN + 5_000
    since, n = plan_backfill(90 * MIN, now, 60)
    assert since == 90 * MIN and n == 11


def test_plan_caps_long_outage():
    now = 10_000 * MIN
    since, n = plan_backfill(0, now, 60, max_bars=100)
    assert n == 100 and since == now - 99 * MIN


def test_merge_dedupes_sorts_and_prefers_closed():
    existing = [bar(0), bar(MIN, closed=False, close=5.0)]
    fetched = [bar(MIN, close=6.0), bar(2 * MIN, close=7.0, closed=False), bar(-MIN)]
    out = merge_bars(existing, fetched)
    assert [b.ts_ms for b in out] == [-MIN, 0, MIN, 2 * MIN]
    assert out[2].close == 6.0 and out[2].closed
    # a closed existing bar is not replaced by an open fetched bar
    out2 = merge_bars([bar(0, close=1.0)], [bar(0, close=2.0, closed=False)])
    assert out2[0].close == 1.0


def test_find_gaps():
    bars = [bar(0), bar(MIN), bar(4 * MIN), bar(5 * MIN), bar(8 * MIN)]
    assert find_gaps(bars, 60) == [(2 * MIN, 3 * MIN), (6 * MIN, 7 * MIN)]
    assert find_gaps(merge_bars(bars, [bar(2 * MIN), bar(3 * MIN), bar(6 * MIN), bar(7 * MIN)]), 60) == []


class FakeOhlcv:
    def __init__(self, first_ts: int, n: int, now: int):
        self.rows = [[first_ts + i * MIN, 1, 2, 0.5, 1.5, 10] for i in range(n)]
        self.now = now
        self.calls: list[tuple] = []

    def milliseconds(self):
        return self.now

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        rows = [r for r in self.rows if r[0] >= since]
        return rows[:limit]


async def test_fetch_history_paginates_and_marks_closed():
    now = 2000 * MIN + 1000
    src = FakeOhlcv(0, 2001, now)
    bars = await fetch_history(src, S, 60, 0, 1500)
    assert len(bars) == 1500
    assert len(src.calls) == 3  # 720 + 720 + 60
    assert all(b.closed for b in bars)
    assert find_gaps(bars, 60) == []
    tail = await fetch_history(src, S, 60, 1999 * MIN, 5)
    assert [b.closed for b in tail] == [True, False]


def test_geo_block_detection():
    assert is_geo_block(ccxt.ExchangeNotAvailable("binance GET https://api.binance.com 451 Service unavailable from a restricted location"))
    assert not is_geo_block(ccxt.NetworkError("timeout"))


# --- reconnect + gap backfill end to end ------------------------------------


class FakeExchange:
    """First session: one batch of trades then a network error. Second: trades until stopped."""

    sessions = 0

    def __init__(self, script: dict):
        self.script = script
        self.markets = {S: {}}
        self.now = script["now"]
        self.fetch_calls: list[tuple] = []
        self.session = FakeExchange.sessions
        FakeExchange.sessions += 1
        self._batches = list(script["batches"][self.session])
        self.closed = False

    def milliseconds(self):
        return self.now

    async def load_markets(self):
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.fetch_calls.append((symbol, timeframe, since, limit))
        self.script["fetch_calls"].append((self.session, since, limit))
        rows = [[ts, 1, 2, 0.5, 1.5, 1] for ts in range(0, self.now, MIN) if ts >= since]
        return rows[:limit]

    async def watch_trades(self, symbol):
        if not self._batches:
            if self.session == 0:
                raise ccxt.NetworkError("socket closed")
            await asyncio.sleep(3600)
        trades, advance = self._batches.pop(0)
        self.now += advance
        return trades

    async def close(self):
        self.closed = True


def trade(i: int, ts: int, price: float):
    return {"id": str(i), "timestamp": ts, "price": price, "amount": 0.1}


async def test_feed_reconnects_and_backfills_gap():
    FakeExchange.sessions = 0
    start_now = 1000 * MIN + 500
    script = {
        "now": start_now,
        "fetch_calls": [],
        "batches": [
            [([trade(1, start_now, 100.0), trade(2, start_now + 100, 101.0)], 0)],
            [([trade(3, start_now + 5 * MIN, 103.0)], 0)],
        ],
    }
    events = []
    made = []

    def factory(_id):
        ex = FakeExchange(script)
        if made:  # the second session happens 5 minutes later
            ex.now = start_now + 5 * MIN
        made.append(ex)
        return ex

    feed = MarketFeed(
        "fake", [S], events.append, timeframes_s=(1, 60), history_bars=10,
        exchange_factory=factory, max_backoff_s=0.01, heartbeat_s=10,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))

    async def wait_for(pred, timeout=5.0):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met")

    await wait_for(lambda: sum(1 for e in events if isinstance(e, ConnectionEvent) and e.status == "connected") >= 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    statuses = [e.status for e in events if isinstance(e, ConnectionEvent)]
    assert statuses[:2] == ["connecting", "connected"]
    assert "reconnecting" in statuses
    assert statuses.count("connected") == 2
    assert statuses[-1] == "stopped"
    assert feed.reconnects == 1
    assert all(ex.closed for ex in made)

    # First history load: 10 bars. Second: from the last closed 1m bar, i.e. the gap only.
    calls = script["fetch_calls"]
    assert calls[0][0] == 0 and calls[0][2] == 10
    first_load_last_closed = 999 * MIN  # bar 1000*MIN was still open at the first load
    assert calls[1][0] == 1
    assert calls[1][1] == first_load_last_closed
    assert calls[1][2] == 7  # 999 .. 1005 inclusive

    # The 1-minute series seen by the GUI is contiguous across the outage.
    series: dict[int, Bar] = {}
    for e in events:
        if isinstance(e, BarsEvent) and e.timeframe_s == 60:
            for b in e.bars:
                series[b.ts_ms] = b
    bars = [series[k] for k in sorted(series)]
    assert find_gaps(bars, 60) == []
    assert bars[-1].ts_ms == 1005 * MIN and not bars[-1].closed
    assert bars[-1].close == 103.0  # live trade applied on top of the backfilled bar

    # No duplicate ticks were processed.
    assert sum(1 for e in events if type(e).__name__ == "TickEvent") == 3


async def test_feed_stops_on_geo_block():
    class Blocked(FakeExchange):
        async def load_markets(self):
            raise ccxt.ExchangeNotAvailable("GET https://api.binance.com/api/v3/exchangeInfo 451 restricted location")

    FakeExchange.sessions = 0
    events = []
    feed = MarketFeed("binance", [S], events.append, exchange_factory=lambda _i: Blocked({"now": 0, "fetch_calls": [], "batches": [[], []]}), max_backoff_s=0.01)
    await asyncio.wait_for(feed.run(asyncio.Event()), 5)
    statuses = [e.status for e in events if isinstance(e, ConnectionEvent)]
    assert statuses == ["connecting", "blocked", "stopped"]


async def test_feed_reports_unknown_symbol():
    class NoSymbol(FakeExchange):
        def __init__(self, s):
            super().__init__(s)
            self.markets = {}

    FakeExchange.sessions = 0
    events = []
    feed = MarketFeed("kraken", ["NOPE/USD"], events.append, exchange_factory=lambda _i: NoSymbol({"now": 0, "fetch_calls": [], "batches": [[], []]}))
    await asyncio.wait_for(feed.run(asyncio.Event()), 5)
    statuses = [e.status for e in events if isinstance(e, ConnectionEvent)]
    assert statuses == ["connecting", "error", "stopped"]
    assert "NOPE/USD" in [e for e in events if isinstance(e, ConnectionEvent)][1].message
