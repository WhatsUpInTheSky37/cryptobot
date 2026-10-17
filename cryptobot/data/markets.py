"""Coin picker data: rank an exchange's pairs by liquidity and quality."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ccxt

QUOTES_BY_EXCHANGE = {"kraken": ("USD",), "coinbase": ("USD",), "binanceus": ("USDT", "USD"), "binance": ("USDT",)}
STABLE_BASES = {"USDT", "USDC", "DAI", "TUSD", "USDP", "BUSD", "FDUSD", "PYUSD", "EUR", "GBP", "USD", "EURC"}


@dataclass
class MarketRow:
    symbol: str
    price: float
    volume_quote_24h: float
    change_24h_pct: float
    spread_pct: float
    range_24h_pct: float

    @property
    def suggested(self) -> bool:
        return is_suggested(self)


def is_suggested(r: MarketRow, min_volume: float = 1_000_000.0, max_spread_pct: float = 0.30) -> bool:
    return r.volume_quote_24h >= min_volume and 0 <= r.spread_pct <= max_spread_pct and r.price > 0


def rank_tickers(tickers: dict[str, dict[str, Any]], quotes: tuple[str, ...]) -> list[MarketRow]:
    rows: list[MarketRow] = []
    for sym, t in tickers.items():
        base, _, quote = sym.partition("/")
        if quote not in quotes or ":" in sym or base in STABLE_BASES:
            continue
        last = float(t.get("last") or t.get("close") or 0.0)
        bid, ask = t.get("bid"), t.get("ask")
        spread = ((float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2) * 100) if bid and ask and float(bid) > 0 else -1.0
        qv = t.get("quoteVolume")
        if qv is None and t.get("baseVolume") is not None and last:
            qv = float(t["baseVolume"]) * last
        hi, lo = t.get("high"), t.get("low")
        rng = ((float(hi) - float(lo)) / last * 100) if hi and lo and last else 0.0
        pct = t.get("percentage")
        if pct is None and t.get("open") and last:
            pct = (last / float(t["open"]) - 1) * 100
        rows.append(MarketRow(sym, last, float(qv or 0.0), float(pct or 0.0), spread, rng))
    rows.sort(key=lambda r: r.volume_quote_24h, reverse=True)
    # one row per base: prefer the most liquid quote
    seen: set[str] = set()
    out = []
    for r in rows:
        b = r.symbol.partition("/")[0]
        if b in seen:
            continue
        seen.add(b)
        out.append(r)
    return out


def fetch_market_rows(exchange_id: str) -> list[MarketRow]:
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    ex.load_markets()
    tickers = ex.fetch_tickers()
    return rank_tickers(tickers, QUOTES_BY_EXCHANGE.get(exchange_id, ("USD", "USDT")))
