"""Local order book maintenance with sequence checking and automatic resync.

Binance and Binance.US publish the order book as one REST snapshot plus a
stream of incremental depth updates. The documented way to keep a correct
local copy is:

1. open the diff stream and buffer events,
2. fetch the REST snapshot (``lastUpdateId``),
3. drop every buffered event whose final id ``u`` <= ``lastUpdateId``,
4. the first event applied must satisfy ``U <= lastUpdateId + 1 <= u``,
5. every later event must satisfy ``U == previous u + 1``; otherwise the
   book is out of sync and the whole procedure restarts from step 2.

``LocalBook`` is the pure, testable implementation of that state machine.
``DepthSyncer`` drives one ``LocalBook`` per symbol from a message source and
a snapshot function, emits book summaries only while the book is in sync, and
resynchronises automatically on a gap, a crossed book, or a failed periodic
verification against a fresh snapshot. ``BinanceDepthStream`` is the network
layer that feeds the syncer from the exchange's websocket.

Books that come from ccxt's own ``watch_order_book`` (other exchanges) are
checked by ``BookValidator`` for the things we can see from the outside:
non-monotonic update ids, crossed books, and empty sides.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from cryptobot.core.events import BookEvent, BookSyncEvent

log = logging.getLogger(__name__)

Levels = list[list[float]] | list[tuple[float, float]]


class LocalBook:
    """A price-level book for one symbol, updated by snapshot + diffs."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id = -1
        self.synced = False
        self._first = True
        self.applied = 0
        self.gaps = 0
        self.dropped = 0

    # -- updates -------------------------------------------------------------

    def apply_snapshot(self, bids: Levels, asks: Levels, last_update_id: int) -> None:
        self.bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if float(q) > 0}
        self.last_update_id = int(last_update_id)
        self.synced = not self.crossed() and bool(self.bids) and bool(self.asks)
        self._first = True

    def apply_diff(self, first_id: int, final_id: int, bids: Levels, asks: Levels) -> str:
        """Apply one depth update. Returns applied | stale | unsynced | gap | crossed."""
        if not self.synced:
            return "unsynced"
        first_id, final_id = int(first_id), int(final_id)
        if final_id <= self.last_update_id:
            self.dropped += 1
            return "stale"
        if self._first:
            if not (first_id <= self.last_update_id + 1 <= final_id):
                self._desync()
                return "gap"
        elif first_id != self.last_update_id + 1:
            self._desync()
            return "gap"
        for p, q in bids:
            p, q = float(p), float(q)
            if q <= 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q
        for p, q in asks:
            p, q = float(p), float(q)
            if q <= 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q
        self.last_update_id = final_id
        self._first = False
        self.applied += 1
        if self.crossed():
            self._desync()
            return "crossed"
        return "applied"

    def _desync(self) -> None:
        self.synced = False
        self.gaps += 1

    # -- queries -------------------------------------------------------------

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb >= ba

    def top(self, n: int = 20) -> tuple[list[list[float]], list[list[float]]]:
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:n]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:n]
        return [[p, q] for p, q in bids], [[p, q] for p, q in asks]

    def matches_snapshot(self, bids: Levels, asks: Levels, tolerance_pct: float = 0.05) -> bool:
        """True when our best prices are within ``tolerance_pct`` of a fresh snapshot's."""
        if not bids or not asks or not self.bids or not self.asks:
            return False
        bb, ba = float(bids[0][0]), float(asks[0][0])
        ob, oa = self.best_bid(), self.best_ask()
        if ob is None or oa is None:
            return False
        return abs(ob / bb - 1) * 100 <= tolerance_pct and abs(oa / ba - 1) * 100 <= tolerance_pct


@dataclass
class _SymState:
    book: LocalBook
    buffer: deque = field(default_factory=lambda: deque(maxlen=5000))
    snapshotting: bool = False
    last_emit_ms: int = 0
    last_verify_ms: int = 0
    resyncs: int = 0
    last_msg_ms: int = 0


SnapshotFn = Callable[[str], Awaitable[dict[str, Any]]]
EmitFn = Callable[[Any], None]


class DepthSyncer:
    """Keeps one synced ``LocalBook`` per symbol from diff messages + snapshots.

    ``snapshot(symbol)`` must return a ccxt-style order book dict with
    ``bids``, ``asks`` and ``nonce`` (the exchange's ``lastUpdateId``).
    """

    def __init__(
        self,
        exchange_id: str,
        symbols: list[str],
        emit: EmitFn,
        snapshot: SnapshotFn,
        *,
        book_interval_ms: int = 1000,
        verify_interval_ms: int = 120_000,
        top_levels: int = 20,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        max_resyncs_per_minute: int = 6,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.emit = emit
        self.snapshot = snapshot
        self.book_interval_ms = book_interval_ms
        self.verify_interval_ms = verify_interval_ms
        self.top_levels = top_levels
        self.clock_ms = clock_ms
        self.max_resyncs_per_minute = max_resyncs_per_minute
        self.state: dict[str, _SymState] = {s: _SymState(LocalBook(s)) for s in self.symbols}
        self._resync_times: dict[str, deque[int]] = {s: deque(maxlen=max_resyncs_per_minute) for s in self.symbols}

    def synced(self, symbol: str) -> bool:
        return self.state[symbol].book.synced

    async def start(self) -> None:
        await asyncio.gather(*(self.resync(s, "initial snapshot") for s in self.symbols))

    async def resync(self, symbol: str, why: str) -> None:
        st = self.state[symbol]
        if st.snapshotting:
            return
        st.snapshotting = True
        now = self.clock_ms()
        times = self._resync_times[symbol]
        if len(times) == times.maxlen and now - times[0] < 60_000:
            self.emit(BookSyncEvent(self.exchange_id, symbol, now, "unsynced", f"too many resyncs ({len(times)}/min): {why}"))
            st.snapshotting = False
            raise RuntimeError(f"{symbol}: order book cannot stay in sync ({why})")
        times.append(now)
        st.resyncs += 1
        self.emit(BookSyncEvent(self.exchange_id, symbol, now, "resync", why))
        try:
            snap = await self.snapshot(symbol)
        except Exception as e:  # noqa: BLE001 - reported, the caller decides whether to give up
            st.snapshotting = False
            self.emit(BookSyncEvent(self.exchange_id, symbol, self.clock_ms(), "unsynced", f"snapshot failed: {e}"[:200]))
            raise
        nonce = snap.get("nonce")
        if nonce is None:
            st.snapshotting = False
            self.emit(BookSyncEvent(self.exchange_id, symbol, self.clock_ms(), "unsynced", "snapshot has no update id"))
            raise RuntimeError(f"{symbol}: snapshot has no lastUpdateId; cannot sequence the depth stream")
        st.book.apply_snapshot(snap.get("bids") or [], snap.get("asks") or [], int(nonce))
        st.last_verify_ms = self.clock_ms()
        self.emit(BookSyncEvent(self.exchange_id, symbol, self.clock_ms(), "snapshot", f"lastUpdateId {nonce}", int(nonce)))
        # replay what arrived while the snapshot was in flight
        buffered = list(st.buffer)
        st.buffer.clear()
        st.snapshotting = False
        for msg in buffered:
            if self._apply(symbol, msg) == "gap":
                return  # _apply already scheduled a resync
        if st.book.synced:
            self.emit(BookSyncEvent(self.exchange_id, symbol, self.clock_ms(), "synced", f"{len(st.book.bids)} bids / {len(st.book.asks)} asks", st.book.last_update_id))
            self._maybe_emit(symbol, force=True)

    def on_message(self, symbol: str, msg: dict[str, Any]) -> str:
        """Feed one depth update (keys U, u, b, a). Returns the apply result."""
        st = self.state[symbol]
        st.last_msg_ms = self.clock_ms()
        if st.snapshotting or not st.book.synced:
            st.buffer.append(msg)
            return "buffered"
        return self._apply(symbol, msg)

    def _apply(self, symbol: str, msg: dict[str, Any]) -> str:
        st = self.state[symbol]
        res = st.book.apply_diff(msg["U"], msg["u"], msg.get("b") or [], msg.get("a") or [])
        if res in ("gap", "crossed"):
            self.emit(BookSyncEvent(self.exchange_id, symbol, self.clock_ms(), "gap", f"{res}: expected U={st.book.last_update_id + 1}, got U={msg['U']} u={msg['u']}", st.book.last_update_id))
            self._emit_unsynced(symbol)
            asyncio.ensure_future(self._resync_later(symbol, res))
        elif res == "applied":
            self._maybe_emit(symbol)
        return res

    async def _resync_later(self, symbol: str, why: str) -> None:
        try:
            await self.resync(symbol, why)
        except Exception as e:  # noqa: BLE001
            log.warning("resync %s failed: %s", symbol, e)

    def _emit_unsynced(self, symbol: str) -> None:
        st = self.state[symbol]
        bb, ba = st.book.best_bid(), st.book.best_ask()
        if bb and ba:
            self.emit(BookEvent(self.exchange_id, symbol, self.clock_ms(), bb, ba, 0.0, 0.0, 0.0, 0.0, st.book.last_update_id, False))

    def _maybe_emit(self, symbol: str, force: bool = False) -> None:
        st = self.state[symbol]
        now = self.clock_ms()
        if not force and now - st.last_emit_ms < self.book_interval_ms:
            return
        st.last_emit_ms = now
        bids, asks = st.book.top(self.top_levels)
        ev = summarize_book(self.exchange_id, symbol, now, bids, asks, seq=st.book.last_update_id, synced=st.book.synced)
        if ev is not None:
            self.emit(ev)

    async def verify(self, symbol: str) -> bool:
        """Compare the local book with a fresh snapshot; resync on a mismatch."""
        st = self.state[symbol]
        if not st.book.synced:
            return False
        try:
            snap = await self.snapshot(symbol)
        except Exception as e:  # noqa: BLE001
            log.info("verify %s: snapshot unavailable (%s)", symbol, type(e).__name__)
            return st.book.synced
        st.last_verify_ms = self.clock_ms()
        nonce = int(snap.get("nonce") or 0)
        ok = st.book.matches_snapshot(snap.get("bids") or [], snap.get("asks") or [])
        # a snapshot older than our book cannot contradict it
        if not ok and nonce and nonce < st.book.last_update_id:
            ok = True
        if ok:
            self.emit(BookSyncEvent(self.exchange_id, symbol, st.last_verify_ms, "verified", f"snapshot {nonce} agrees", st.book.last_update_id))
            return True
        self.emit(BookSyncEvent(self.exchange_id, symbol, st.last_verify_ms, "mismatch", f"local best {st.book.best_bid()}/{st.book.best_ask()} vs snapshot {snap['bids'][0][0] if snap.get('bids') else '?'}/{snap['asks'][0][0] if snap.get('asks') else '?'}", st.book.last_update_id))
        st.book.synced = False
        self._emit_unsynced(symbol)
        await self.resync(symbol, "verification mismatch")
        return st.book.synced

    def due_for_verify(self, symbol: str) -> bool:
        return self.clock_ms() - self.state[symbol].last_verify_ms >= self.verify_interval_ms


def summarize_book(exchange_id: str, symbol: str, ts_ms: int, bids, asks, *, seq: int = 0, synced: bool = True) -> BookEvent | None:
    """Reduce a depth snapshot to best prices and quote-denominated depth near the mid."""
    if not bids or not asks:
        return None
    bb, ba = float(bids[0][0]), float(asks[0][0])
    if bb <= 0 or ba <= 0 or bb >= ba:
        return None

    mid = (bb + ba) / 2.0

    def depth(levels, lo: float, hi: float) -> float:
        total = 0.0
        for lvl in levels:
            px, amt = float(lvl[0]), float(lvl[1])
            if lo <= px <= hi:
                total += px * amt
        return total

    return BookEvent(
        exchange_id, symbol, ts_ms, bb, ba,
        depth(bids, mid * 0.999, mid), depth(asks, mid, mid * 1.001),
        depth(bids, mid * 0.995, mid), depth(asks, mid, mid * 1.005),
        int(seq or 0), bool(synced),
    )


class BookValidator:
    """Sanity checks for books ccxt maintains for us (non-Binance exchanges).

    We cannot see the exchange's sequence numbers through ccxt on every venue,
    but we can see a nonce that goes backwards, a crossed book, or an empty
    side. ``check`` returns a problem string or None; after ``max_strikes``
    consecutive problems the caller should resubscribe.
    """

    def __init__(self, max_strikes: int = 3) -> None:
        self.last_nonce: dict[str, int] = {}
        self.strikes: dict[str, int] = {}
        self.max_strikes = max_strikes

    def check(self, symbol: str, book: dict[str, Any]) -> str | None:
        bids, asks = book.get("bids") or [], book.get("asks") or []
        problem = None
        if not bids or not asks:
            problem = "empty side"
        elif float(bids[0][0]) >= float(asks[0][0]):
            problem = f"crossed book {bids[0][0]} >= {asks[0][0]}"
        else:
            nonce = book.get("nonce")
            if nonce is not None:
                prev = self.last_nonce.get(symbol)
                if prev is not None and int(nonce) < prev:
                    problem = f"update id went backwards {prev} -> {nonce}"
                self.last_nonce[symbol] = int(nonce)
        if problem:
            self.strikes[symbol] = self.strikes.get(symbol, 0) + 1
        else:
            self.strikes[symbol] = 0
        return problem

    def exhausted(self, symbol: str) -> bool:
        return self.strikes.get(symbol, 0) >= self.max_strikes


# ---------------------------------------------------------------------------
# Network layer for Binance / Binance.US


WS_URLS = {
    "binanceus": "wss://stream.binance.us:9443/stream",
    "binance": "wss://stream.binance.com:9443/stream",
}


def stream_name(market_id: str, speed: str = "100ms") -> str:
    return f"{market_id.lower()}@depth@{speed}"


class BinanceDepthStream:
    """Reads the combined diff-depth websocket and feeds a ``DepthSyncer``.

    The REST snapshot comes from the ccxt exchange object (``fetch_order_book``
    with 1000 levels returns ``nonce`` = ``lastUpdateId``). Any exception
    propagates to the feed, which reconnects everything with backoff.
    """

    def __init__(
        self,
        exchange: Any,
        exchange_id: str,
        symbols: list[str],
        emit: EmitFn,
        *,
        book_interval_ms: int = 1000,
        verify_interval_ms: int = 120_000,
        ws_url: str | None = None,
        snapshot_levels: int = 1000,
        idle_timeout_s: float = 30.0,
    ) -> None:
        self.exchange = exchange
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.ws_url = ws_url or WS_URLS.get(exchange_id)
        if not self.ws_url:
            raise ValueError(f"no native depth stream for {exchange_id}")
        self.snapshot_levels = snapshot_levels
        self.idle_timeout_s = idle_timeout_s
        self.syncer = DepthSyncer(exchange_id, self.symbols, emit, self._snapshot, book_interval_ms=book_interval_ms, verify_interval_ms=verify_interval_ms, clock_ms=lambda: int(exchange.milliseconds()))
        self._ids: dict[str, str] = {}

    async def _snapshot(self, symbol: str) -> dict[str, Any]:
        return await self.exchange.fetch_order_book(symbol, self.snapshot_levels)

    def _market_ids(self) -> dict[str, str]:
        markets = getattr(self.exchange, "markets", None) or {}
        out = {}
        for s in self.symbols:
            m = markets.get(s) or {}
            out[stream_name(str(m.get("id") or s.replace("/", "")))] = s
        return out

    async def run(self) -> None:
        import aiohttp

        self._ids = self._market_ids()
        url = f"{self.ws_url}?streams={'/'.join(self._ids)}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, heartbeat=20, max_msg_size=4 * 1024 * 1024) as ws:
                await self.syncer.start()
                verify_task = asyncio.create_task(self._verify_loop())
                try:
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), timeout=self.idle_timeout_s)
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError(f"depth stream closed: {msg.type}")
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload = json.loads(msg.data)
                        data = payload.get("data") or payload
                        symbol = self._ids.get(str(payload.get("stream") or ""))
                        if symbol is None:
                            sid = str(data.get("s") or "")
                            symbol = next((s for n, s in self._ids.items() if n.startswith(sid.lower() + "@")), None)
                        if symbol is None or data.get("e") != "depthUpdate":
                            continue
                        self.syncer.on_message(symbol, data)
                finally:
                    verify_task.cancel()
                    for s in self.symbols:
                        self.syncer.emit(BookSyncEvent(self.exchange_id, s, int(self.exchange.milliseconds()), "stopped", "depth stream closed"))

    async def _verify_loop(self) -> None:
        while True:
            await asyncio.sleep(max(5.0, self.syncer.verify_interval_ms / 1000.0 / max(1, len(self.symbols))))
            for s in self.symbols:
                if self.syncer.due_for_verify(s):
                    try:
                        await self.syncer.verify(s)
                    except Exception as e:  # noqa: BLE001
                        log.warning("verify %s: %s", s, e)
