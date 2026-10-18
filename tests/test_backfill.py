import asyncio

import ccxt
import pytest

from cryptobot.core.events import BarsEvent, BookEvent, ConnectionEvent, TickEvent
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


async def test_feed_drops_an_unlisted_symbol_and_keeps_the_rest():
    from cryptobot.core.events import SymbolDroppedEvent

    FakeExchange.sessions = 0
    events = []
    feed = MarketFeed("coinbase", [S, "SHIB/USB"], events.append, exchange_factory=lambda _i: FakeExchange({"now": 1000 * MIN + 500, "fetch_calls": [], "batches": [[], []]}), max_backoff_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    for _ in range(200):
        if any(isinstance(e, ConnectionEvent) and e.status == "connected" for e in events):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    dropped = [e for e in events if isinstance(e, SymbolDroppedEvent)]
    assert [d.symbol for d in dropped] == ["SHIB/USB"] and "does not list SHIB/USB" in dropped[0].reason
    assert feed.symbols == [S]
    assert any(isinstance(e, ConnectionEvent) and e.status == "connected" for e in events), "the feed connected without the bad coin"


def test_resample_bars_builds_four_hour_candles_from_hourly_ones():
    from cryptobot.data.backfill import resample_bars, supported_source_timeframe

    H = 3_600_000
    hourly = [Bar(S, 3600, i * H, 10 + i, 12 + i, 9 + i, 11 + i, 1.0, 5, closed=True) for i in range(9)]
    hourly[-1].closed = False
    four = resample_bars(hourly, 14_400)
    assert [b.ts_ms for b in four] == [0, 4 * H, 8 * H] and all(b.timeframe_s == 14_400 for b in four)
    first = four[0]
    assert (first.open, first.high, first.low, first.close, first.volume, first.trades) == (10, 15, 9, 14, 4.0, 20)
    assert first.closed and four[1].closed and not four[2].closed, "the last bucket is incomplete and its source bar is open"

    class Coinbase:
        timeframes = {"1m": 1, "5m": 1, "15m": 1, "30m": 1, "1h": 1, "2h": 1, "6h": 1, "1d": 1}

    class NoList:
        pass

    assert supported_source_timeframe(Coinbase(), 14_400) == 3_600  # 4h from the largest divisor the app knows how to label (1h; 2h is not in the table)
    assert supported_source_timeframe(Coinbase(), 3_600) == 3_600
    assert supported_source_timeframe(NoList(), 14_400) == 14_400
    assert supported_source_timeframe(Coinbase(), 60) == 60


async def test_feed_fetches_a_supported_candle_size_and_resamples():
    class Coinbase(FakeExchange):
        timeframes = {"1m": 1, "1h": 1}

    FakeExchange.sessions = 0
    events = []
    feed = MarketFeed("coinbase", [S], events.append, timeframes_s=(1, 60, 14_400), history_bars=3,
                      exchange_factory=lambda _i: Coinbase({"now": 1000 * MIN + 500, "fetch_calls": [], "batches": [[], []]}), max_backoff_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    for _ in range(200):
        if any(isinstance(e, ConnectionEvent) and e.status == "connected" for e in events):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    labels = {tf for e in events if isinstance(e, BarsEvent) for tf in [e.timeframe_s]}
    assert 14_400 in labels, "4h bars were published although the exchange has no 4h candles"
    assert all(b.timeframe_s == 14_400 for e in events if isinstance(e, BarsEvent) and e.timeframe_s == 14_400 for b in e.bars)


async def test_feed_batches_subscriptions_when_the_exchange_allows():
    class Batched(FakeExchange):
        has = {"watchTradesForSymbols": True, "watchOrderBookForSymbols": True, "watchOrderBook": True}

        def __init__(self, script):
            super().__init__(script)
            self.markets = {S: {}, "ETH/USD": {}}

        async def watch_trades_for_symbols(self, symbols):
            self.batched_calls = getattr(self, "batched_calls", 0) + 1
            trades = await self.watch_trades(symbols[0])
            return [dict(t, symbol=symbols[i % len(symbols)]) for i, t in enumerate(trades)]

        async def watch_order_book_for_symbols(self, symbols, limit=None):
            await asyncio.sleep(0.01)
            return {"symbol": symbols[0], "bids": [[1.0, 5.0]], "asks": [[1.1, 5.0]], "nonce": 1}

    FakeExchange.sessions = 0
    now = 1000 * MIN + 500
    script = {"now": now, "fetch_calls": [], "batches": [[([trade(1, now, 1.0), trade(2, now, 1.1)], 10)], []]}
    events = []
    feed = MarketFeed("coinbase", [S, "ETH/USD"], events.append, exchange_factory=lambda _i: Batched(script), max_backoff_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    for _ in range(300):
        if sum(1 for e in events if isinstance(e, TickEvent)) >= 2 and any(isinstance(e, BookEvent) for e in events):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    ticks = [e for e in events if isinstance(e, TickEvent)]
    assert {t.tick.symbol for t in ticks} == {S, "ETH/USD"}, "trades from one subscription are routed to their own symbols"
    assert any(isinstance(e, BookEvent) and e.symbol == S for e in events)
    assert not any(isinstance(e, ConnectionEvent) and e.status == "error" for e in events)


def test_plan_pulse_feed_shares_extends_or_owns():
    from cryptobot.engine.runner import plan_pulse_feed

    market = ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert plan_pulse_feed("coinbase", ["BTC/USD", "SOL/USD"], "coinbase", market) == ("shared", [])
    assert plan_pulse_feed("coinbase", ["BTC/USD", "SHIB/USD"], "coinbase", market) == ("extra", ["SHIB/USD"])
    assert plan_pulse_feed("kraken", ["BTC/USD"], "coinbase", market) == ("own", ["BTC/USD"])
