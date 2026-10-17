"""Live market data over WebSocket with automatic reconnect and gap backfill.

One ``MarketFeed`` serves one exchange and any number of symbols. It emits
events through a callback so the engine can forward them to the GUI and the
database without this module knowing about either.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Callable, Iterable, Sequence

import ccxt
import ccxt.pro as ccxtpro

from cryptobot.core.bars import TIMEFRAME_LABELS, BarAggregator
from cryptobot.core.events import (
    BarsEvent,
    BookEvent,
    ConnectionEvent,
    Event,
    HeartbeatEvent,
    MarketInfoEvent,
    TickEvent,
)
from cryptobot.core.models import Bar, Tick
from cryptobot.data.backfill import fetch_history, plan_backfill

log = logging.getLogger(__name__)

Emit = Callable[[Event], None]
ClosedBarsSink = Callable[[Sequence[Bar]], None]
ExchangeFactory = Callable[[str], object]


class FeedFatal(Exception):
    """A condition that retrying will not fix (bad symbol, geo-block, auth)."""


def is_geo_block(exc: BaseException) -> bool:
    s = str(exc).lower()
    return " 451" in s or "restricted location" in s or "unavailable from a restricted" in s


def default_exchange_factory(exchange_id: str) -> object:
    cls = getattr(ccxtpro, exchange_id)
    ex = cls({"enableRateLimit": True, "newUpdates": True})
    return ex


class MarketFeed:
    def __init__(
        self,
        exchange_id: str,
        symbols: Iterable[str],
        emit: Emit,
        *,
        timeframes_s: Sequence[int] = (1, 60, 300),
        history_bars: int = 300,
        on_closed_bars: ClosedBarsSink | None = None,
        exchange_factory: ExchangeFactory = default_exchange_factory,
        max_backoff_s: float = 30.0,
        roll_lag_ms: int = 750,
        heartbeat_s: float = 1.0,
        order_book: bool = True,
        book_interval_ms: int = 1000,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self._emit = emit
        self.timeframes_s = tuple(timeframes_s)
        self.history_bars = history_bars
        self._on_closed = on_closed_bars
        self._factory = exchange_factory
        self.max_backoff_s = max_backoff_s
        self.roll_lag_ms = roll_lag_ms
        self.heartbeat_s = heartbeat_s
        self.order_book = order_book
        self.book_interval_ms = book_interval_ms
        self._aggs: dict[str, dict[int, BarAggregator]] = {
            s: {tf: BarAggregator(s, tf) for tf in self.timeframes_s} for s in self.symbols
        }
        self._last_closed: dict[tuple[str, int], int] = {}
        self._seen: dict[str, tuple[deque[str], set[str]]] = {
            s: (deque(maxlen=4000), set()) for s in self.symbols
        }
        self.reconnects = 0

    # -- lifecycle ----------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            ex = self._factory(self.exchange_id)
            try:
                self._emit(
                    ConnectionEvent(
                        self.exchange_id,
                        "connecting" if attempt == 0 else "reconnecting",
                        attempt=attempt,
                    )
                )
                await ex.load_markets()  # type: ignore[attr-defined]
                self._check_symbols(ex)
                self._emit_market_info(ex)
                await self._load_history(ex)
                self._emit(ConnectionEvent(self.exchange_id, "connected", attempt=attempt))
                if attempt:
                    self.reconnects += 1
                attempt = 0
                await self._watch(ex, stop)
            except asyncio.CancelledError:
                raise
            except FeedFatal as e:
                log.error("feed fatal: %s", e)
                self._emit(ConnectionEvent(self.exchange_id, "error", str(e)))
                break
            except (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.BadSymbol) as e:
                log.error("feed fatal: %s", e)
                self._emit(ConnectionEvent(self.exchange_id, "error", f"{type(e).__name__}: {e}"[:300]))
                break
            except Exception as e:  # noqa: BLE001 - any other failure is retried
                if is_geo_block(e):
                    msg = (
                        f"{self.exchange_id} is not reachable from your location (HTTP 451). "
                        "Choose a different exchange."
                    )
                    log.error(msg)
                    self._emit(ConnectionEvent(self.exchange_id, "blocked", msg))
                    break
                attempt += 1
                delay = min(self.max_backoff_s, 2.0 ** (attempt - 1)) * (0.75 + 0.5 * random.random())
                log.warning("feed error (%s), retry %d in %.1fs: %s", type(e).__name__, attempt, delay, e)
                self._emit(
                    ConnectionEvent(
                        self.exchange_id,
                        "reconnecting",
                        f"{type(e).__name__}: {e}"[:300],
                        attempt=attempt,
                        retry_in_s=delay,
                    )
                )
                await self._sleep(stop, delay)
            finally:
                try:
                    await ex.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
        self._emit(ConnectionEvent(self.exchange_id, "stopped"))

    # -- steps --------------------------------------------------------------

    def _check_symbols(self, ex) -> None:
        markets = getattr(ex, "markets", None) or {}
        missing = [s for s in self.symbols if s not in markets]
        if missing:
            raise FeedFatal(f"{self.exchange_id} does not list {', '.join(missing)}")

    def _emit_market_info(self, ex) -> None:
        markets = getattr(ex, "markets", None) or {}
        try:
            from ccxt.base.decimal_to_precision import TICK_SIZE
        except Exception:  # noqa: BLE001
            TICK_SIZE = 4  # noqa: N806
        tick_mode = getattr(ex, "precisionMode", None) == TICK_SIZE

        def step(v) -> float:
            if v is None:
                return 0.0
            return float(v) if tick_mode else 10.0 ** (-int(v))

        for s in self.symbols:
            m = markets.get(s) or {}
            lim = m.get("limits") or {}
            prec = m.get("precision") or {}
            self._emit(
                MarketInfoEvent(
                    self.exchange_id,
                    s,
                    float(m.get("taker") or 0.0),
                    float(m.get("maker") or 0.0),
                    float((lim.get("amount") or {}).get("min") or 0.0),
                    float((lim.get("cost") or {}).get("min") or 0.0),
                    step(prec.get("amount")),
                    step(prec.get("price")),
                )
            )

    async def _load_history(self, ex) -> None:
        """Fetch history for every REST-capable timeframe; after a reconnect this fills the gap."""
        now = ex.milliseconds()
        for symbol in self.symbols:
            for tf in self.timeframes_s:
                if tf < 60 or tf not in TIMEFRAME_LABELS:
                    continue
                since, limit = plan_backfill(
                    self._last_closed.get((symbol, tf)), now, tf, default_bars=self.history_bars
                )
                bars = await fetch_history(ex, symbol, tf, since, limit)
                if not bars:
                    continue
                self._publish(symbol, tf, bars)
                agg = self._aggs[symbol][tf]
                agg.seed(bars[-1])

    async def _watch(self, ex, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._watch_symbol(ex, s), name=f"watch:{s}") for s in self.symbols]
        if self.order_book and getattr(ex, "has", {}).get("watchOrderBook"):
            tasks += [asyncio.create_task(self._watch_book(ex, s), name=f"book:{s}") for s in self.symbols]
        tasks.append(asyncio.create_task(self._heartbeat(ex), name="heartbeat"))
        stopper = asyncio.create_task(stop.wait(), name="stop")
        try:
            done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is stopper:
                    continue
                exc = t.exception()
                if exc is not None:
                    raise exc
        finally:
            for t in [*tasks, stopper]:
                t.cancel()
            await asyncio.gather(*tasks, stopper, return_exceptions=True)

    async def _watch_symbol(self, ex, symbol: str) -> None:
        aggs = self._aggs[symbol]
        order, seen = self._seen[symbol]
        while True:
            trades = await ex.watch_trades(symbol)
            for t in trades:
                tid = t.get("id")
                if tid is not None:
                    tid = str(tid)
                    if tid in seen:
                        continue
                    if len(order) == order.maxlen:
                        seen.discard(order[0])
                    order.append(tid)
                    seen.add(tid)
                ts = t.get("timestamp") or ex.milliseconds()
                price = t.get("price")
                if price is None:
                    continue
                tick = Tick(symbol, int(ts), float(price), float(t.get("amount") or 0.0), tid, t.get("side"))
                self._emit(TickEvent(self.exchange_id, tick))
                for tf, agg in aggs.items():
                    changed = agg.add_tick(tick)
                    if changed:
                        self._publish(symbol, tf, changed)

    async def _watch_book(self, ex, symbol: str) -> None:
        last = 0
        while True:
            book = await ex.watch_order_book(symbol, 20)
            now = ex.milliseconds()
            if now - last < self.book_interval_ms:
                continue
            last = now
            ev = summarize_book(self.exchange_id, symbol, now, book.get("bids") or [], book.get("asks") or [])
            if ev is not None:
                self._emit(ev)

    async def _heartbeat(self, ex) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            now = ex.milliseconds() - self.roll_lag_ms
            for symbol, aggs in self._aggs.items():
                for tf, agg in aggs.items():
                    changed = agg.roll(now)
                    if changed:
                        self._publish(symbol, tf, changed)
            self._emit(HeartbeatEvent(self.exchange_id, now))

    # -- helpers ------------------------------------------------------------

    def _publish(self, symbol: str, tf: int, bars: Sequence[Bar]) -> None:
        self._emit(BarsEvent(self.exchange_id, symbol, tf, list(bars)))
        if tf < 60:
            return
        closed = [b for b in bars if b.closed]
        if not closed:
            return
        key = (symbol, tf)
        latest = max(b.ts_ms for b in closed)
        if latest > self._last_closed.get(key, -1):
            self._last_closed[key] = latest
        if self._on_closed is not None:
            try:
                self._on_closed(closed)
            except Exception:  # noqa: BLE001 - persistence must never kill the feed
                log.exception("closed-bar sink failed")

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def summarize_book(exchange_id: str, symbol: str, ts_ms: int, bids, asks) -> BookEvent | None:
    """Reduce a depth snapshot to best prices and quote-denominated depth near the mid."""
    if not bids or not asks:
        return None
    bb, ba = float(bids[0][0]), float(asks[0][0])
    if bb <= 0 or ba <= 0:
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
    )
