"""Download and cache historical candles for backtesting (synchronous ccxt, run in a worker thread)."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import ccxt

from cryptobot.core.bars import TIMEFRAME_LABELS
from cryptobot.core.models import Bar
from cryptobot.data.backfill import merge_bars, rows_to_bars
from cryptobot.db.store import Store

log = logging.getLogger(__name__)

PAGE = {"coinbase": 300, "kraken": 720, "binanceus": 1000, "binance": 1000}
# Public, key-free, deep daily history reachable from the US. Independent of the trading exchange.
DEFAULT_HISTORY_SOURCE = "coinbase"
HISTORY_SOURCES = {
    "coinbase": "Coinbase (deep history, USD pairs)",
    "binanceus": "Binance.US (from 2019, USDT pairs)",
    "kraken": "Kraken (last 2 years only)",
}


def history_symbol(source: str, symbol: str) -> str:
    base, _, quote = symbol.partition("/")
    if source in ("coinbase", "kraken") and quote in ("USDT", "USDC"):
        return f"{base}/USD"
    if source in ("binance", "binanceus") and quote == "USD":
        return f"{base}/USDT"
    return symbol


class HistoryDownloader:
    def __init__(self, store: Store | None = None, progress: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.progress = progress or (lambda _m: None)

    def download(self, source: str, symbol: str, timeframe_s: int, years: float, until_ms: int | None = None) -> tuple[list[Bar], str]:
        """Return (bars, note). ``note`` is non-empty when less than requested was available."""
        label = TIMEFRAME_LABELS[timeframe_s]
        tf_ms = timeframe_s * 1000
        now = until_ms or int(time.time() * 1000)
        since = now - int(years * 365.25 * 86400 * 1000)
        since -= since % tf_ms
        sym = history_symbol(source, symbol)

        cached: list[Bar] = []
        if self.store is not None:
            cached = [b for b in self.store.load_bars(source, sym, timeframe_s, limit=100_000) if b.ts_ms >= since]
        fetch_from = since
        if cached and _contiguous(cached, tf_ms) and cached[0].ts_ms <= since + tf_ms:
            fetch_from = cached[-1].ts_ms  # refresh the last cached bar and append new ones
            self.progress(f"Using {len(cached)} cached {label} bars; fetching the rest…")

        ex = getattr(ccxt, source)({"enableRateLimit": True})
        ex.load_markets()
        if sym not in ex.markets:
            raise ValueError(f"{HISTORY_SOURCES.get(source, source)} does not list {sym}")
        page = PAGE.get(source, 500)
        fetched: list[Bar] = []
        cursor = fetch_from
        while cursor < now:
            rows = ex.fetch_ohlcv(sym, label, cursor, page)
            if not rows:
                break
            bars = rows_to_bars(sym, timeframe_s, rows, ex.milliseconds())
            bars = [b for b in bars if b.ts_ms >= cursor]
            if not bars:
                break
            fetched.extend(bars)
            self.progress(f"Downloaded {len(cached) + len(fetched)} {label} bars of {sym} from {source}…")
            last = bars[-1].ts_ms
            if last + tf_ms > now or len(rows) < page:
                break
            cursor = last + tf_ms
        all_bars = merge_bars(cached, fetched)
        for b in all_bars:
            b.symbol = sym
        if self.store is not None and fetched:
            self.store.upsert_bars(source, [b for b in fetched if b.closed])
        note = ""
        if all_bars and all_bars[0].ts_ms > since + 2 * tf_ms:
            got_years = (all_bars[-1].ts_ms - all_bars[0].ts_ms) / (365.25 * 86400 * 1000)
            note = f"{HISTORY_SOURCES.get(source, source)} only provided {got_years:.1f} years of {label} data (you asked for {years:g})."
        return all_bars, note


def _contiguous(bars: list[Bar], tf_ms: int) -> bool:
    return all(b.ts_ms - a.ts_ms == tf_ms for a, b in zip(bars, bars[1:]))
