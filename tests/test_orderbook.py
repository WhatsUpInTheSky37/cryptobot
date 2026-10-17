"""Order book: snapshot + diff sequencing, gap detection, resync, validation."""
import asyncio

import pytest

from cryptobot.core.events import BookEvent, BookSyncEvent
from cryptobot.data.orderbook import BookValidator, DepthSyncer, LocalBook, stream_name


def test_local_book_follows_binance_sequencing_rules():
    b = LocalBook("BTC/USDT")
    assert b.apply_diff(1, 2, [], []) == "unsynced"
    b.apply_snapshot([[100, 1], [99, 2]], [[101, 1], [102, 3]], last_update_id=10)
    assert b.synced and b.best_bid() == 100 and b.best_ask() == 101
    assert b.apply_diff(5, 9, [[100, 5]], []) == "stale"  # u <= lastUpdateId: dropped
    assert b.bids[100] == 1
    # first event must bracket lastUpdateId + 1
    assert b.apply_diff(12, 13, [], []) == "gap" and not b.synced and b.gaps == 1
    b.apply_snapshot([[100, 1], [99, 2]], [[101, 1], [102, 3]], last_update_id=10)
    assert b.apply_diff(8, 11, [[100, 0], [99.5, 4]], [[101, 2]]) == "applied"
    assert 100 not in b.bids and b.bids[99.5] == 4 and b.asks[101] == 2 and b.last_update_id == 11
    # every later event must start at previous u + 1
    assert b.apply_diff(12, 12, [[99.5, 1]], []) == "applied"
    assert b.apply_diff(14, 15, [], []) == "gap"
    assert not b.synced


def test_local_book_detects_crossed_book_and_top_levels():
    b = LocalBook("X")
    b.apply_snapshot([[100, 1]], [[101, 1]], 1)
    assert b.apply_diff(2, 2, [[101.5, 1]], []) == "crossed" and not b.synced
    b.apply_snapshot([[100, 1], [99, 1], [98, 1]], [[101, 1], [103, 1], [102, 1]], 1)
    bids, asks = b.top(2)
    assert bids == [[100, 1], [99, 1]] and asks == [[101, 1], [102, 1]]
    assert b.matches_snapshot([[100.01, 1]], [[100.99, 1]])
    assert not b.matches_snapshot([[90, 1]], [[101, 1]])


class FakeSnapshots:
    def __init__(self, snaps):
        self.snaps = list(snaps)
        self.calls = 0

    async def __call__(self, symbol):
        self.calls += 1
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_syncer_buffers_until_snapshot_then_emits_only_synced_books():
    events = []
    clock = {"ms": 1_000}
    snaps = FakeSnapshots([{"bids": [[100, 1], [99, 1]], "asks": [[101, 1], [102, 1]], "nonce": 10}])
    sy = DepthSyncer("binanceus", ["BTC/USDT"], events.append, snaps, book_interval_ms=0, clock_ms=lambda: clock["ms"])
    # messages arrive before the snapshot: buffered
    assert sy.on_message("BTC/USDT", {"U": 9, "u": 11, "b": [[100, 2]], "a": []}) == "buffered"
    assert sy.on_message("BTC/USDT", {"U": 12, "u": 12, "b": [], "a": [[101, 0]]}) == "buffered"
    run(sy.start())
    book = sy.state["BTC/USDT"].book
    assert book.synced and book.bids[100] == 2 and 101 not in book.asks and book.last_update_id == 12
    kinds = [e.status for e in events if isinstance(e, BookSyncEvent)]
    assert kinds[:3] == ["resync", "snapshot", "synced"]
    books = [e for e in events if isinstance(e, BookEvent)]
    assert books and books[-1].synced and books[-1].seq == 12 and books[-1].best_ask == 102
    clock["ms"] += 1000
    assert sy.on_message("BTC/USDT", {"U": 13, "u": 14, "b": [[99.5, 3]], "a": []}) == "applied"
    last = [e for e in events if isinstance(e, BookEvent)][-1]
    assert last.seq == 14 and last.best_bid == 100 and last.best_ask == 102 and last.synced


def test_syncer_resyncs_on_gap_and_marks_book_unsynced():
    events = []
    snaps = FakeSnapshots([
        {"bids": [[100, 1]], "asks": [[101, 1]], "nonce": 10},
        {"bids": [[100, 1]], "asks": [[101, 1]], "nonce": 20},
    ])

    async def go():
        sy = DepthSyncer("binanceus", ["BTC/USDT"], events.append, snaps, book_interval_ms=0, clock_ms=lambda: 5_000)
        await sy.start()
        assert sy.on_message("BTC/USDT", {"U": 11, "u": 11, "b": [], "a": []}) == "applied"
        assert sy.on_message("BTC/USDT", {"U": 15, "u": 16, "b": [], "a": []}) == "gap"  # skipped 12..14
        assert not sy.synced("BTC/USDT")
        await asyncio.sleep(0)  # let the scheduled resync run
        await asyncio.sleep(0)
        return sy

    sy = run(go())
    assert snaps.calls == 2 and sy.synced("BTC/USDT") and sy.state["BTC/USDT"].book.last_update_id == 20
    statuses = [e.status for e in events if isinstance(e, BookSyncEvent)]
    assert "gap" in statuses and statuses.count("resync") == 2
    unsynced = [e for e in events if isinstance(e, BookEvent) and not e.synced]
    assert unsynced, "consumers must be told the book is not trustworthy"


def test_syncer_verification_mismatch_triggers_resync_and_too_many_resyncs_raises():
    events = []
    snaps = FakeSnapshots([
        {"bids": [[100, 1]], "asks": [[101, 1]], "nonce": 10},
        {"bids": [[200, 1]], "asks": [[201, 1]], "nonce": 30},  # verification: wildly different
        {"bids": [[200, 1]], "asks": [[201, 1]], "nonce": 31},
    ])

    async def go():
        sy = DepthSyncer("binanceus", ["BTC/USDT"], events.append, snaps, book_interval_ms=0, clock_ms=lambda: 5_000, max_resyncs_per_minute=3)
        await sy.start()
        ok = await sy.verify("BTC/USDT")
        assert ok and sy.state["BTC/USDT"].book.best_bid() == 200
        await sy.resync("BTC/USDT", "third within a minute is still allowed")
        with pytest.raises(RuntimeError):
            await sy.resync("BTC/USDT", "fourth is not")

    run(go())
    assert "mismatch" in [e.status for e in events if isinstance(e, BookSyncEvent)]


def test_syncer_refuses_snapshot_without_update_id():
    snaps = FakeSnapshots([{"bids": [[1, 1]], "asks": [[2, 1]]}])
    sy = DepthSyncer("binanceus", ["X"], lambda e: None, snaps, clock_ms=lambda: 0)
    with pytest.raises(RuntimeError):
        run(sy.start())


def test_book_validator_for_ccxt_books():
    v = BookValidator(max_strikes=2)
    assert v.check("X", {"bids": [[100, 1]], "asks": [[101, 1]], "nonce": 5}) is None
    assert v.check("X", {"bids": [[100, 1]], "asks": [[101, 1]], "nonce": 4}) is not None  # went backwards
    assert "crossed" in v.check("X", {"bids": [[102, 1]], "asks": [[101, 1]], "nonce": 6})
    assert v.exhausted("X")
    assert v.check("X", {"bids": [[100, 1]], "asks": [[101, 1]], "nonce": 7}) is None and not v.exhausted("X")
    assert v.check("Y", {"bids": [], "asks": [[1, 1]]}) == "empty side"


def test_stream_name():
    assert stream_name("BTCUSDT") == "btcusdt@depth@100ms"
