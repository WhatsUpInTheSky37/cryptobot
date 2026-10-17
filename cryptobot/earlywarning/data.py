"""Binance.US public market data: exchange info, klines (with taker-buy volume and trade
counts), aggregated trades. Rate-limit aware, resumable, cached in the EW store.

Individual historical trades (``/api/v3/historicalTrades``) need an API key on
Binance.US; this module never uses credentials, so it records that limitation
and uses aggregated trades, which carry the taker side (``isBuyerMaker``).
Historical order-book snapshots are not available from the exchange at all.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Protocol

from cryptobot.earlywarning.store import EWStore

log = logging.getLogger("earlywarning.data")

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
WEIGHT_LIMIT_PER_MIN = 1200  # Binance.US request weight limit
WEIGHT_SOFT = 1000


class RestClient(Protocol):
    """The subset of a ccxt binanceus instance we use (so tests can fake it)."""

    def load_markets(self) -> dict[str, Any]: ...
    def publicGetExchangeInfo(self, params: dict[str, Any] | None = None) -> dict[str, Any]: ...  # noqa: N802
    def publicGetKlines(self, params: dict[str, Any]) -> list[list[Any]]: ...  # noqa: N802
    def publicGetAggTrades(self, params: dict[str, Any]) -> list[dict[str, Any]]: ...  # noqa: N802
    def fetch_tickers(self) -> dict[str, dict[str, Any]]: ...
    def milliseconds(self) -> int: ...


def make_client(exchange_id: str = "binanceus") -> Any:
    import ccxt

    return getattr(ccxt, exchange_id)({"enableRateLimit": True})


class BinanceUSData:
    def __init__(self, client: Any, store: EWStore, *, sleep: Callable[[float], None] = time.sleep, exchange_id: str = "binanceus") -> None:
        self.client = client
        self.store = store
        self.sleep = sleep
        self.exchange_id = exchange_id
        self._markets: dict[str, Any] | None = None
        self.requests = 0

    # -- rate limiting -----------------------------------------------------------

    def _throttle(self) -> None:
        """ccxt spaces requests; additionally back off when the exchange reports high used weight."""
        self.requests += 1
        hdrs = getattr(self.client, "last_response_headers", None) or {}
        used = None
        for k, v in hdrs.items():
            if str(k).lower().startswith("x-mbx-used-weight"):
                try:
                    used = max(used or 0, int(v))
                except (TypeError, ValueError):
                    pass
        if used is not None and used >= WEIGHT_SOFT:
            wait = 60.0 * (used - WEIGHT_SOFT + 50) / WEIGHT_LIMIT_PER_MIN + 1.0
            log.info("used weight %s: sleeping %.1fs", used, wait)
            self.sleep(wait)

    # -- markets -----------------------------------------------------------------

    def exchange_info(self) -> dict[str, dict[str, Any]]:
        """market id -> raw symbol info (status, permissions, base, quote)."""
        info = self.client.publicGetExchangeInfo()
        self._throttle()
        out = {}
        for s in info.get("symbols", []):
            out[str(s.get("symbol"))] = s
        return out

    def verify_symbols(self, symbols: list[str], *, quote_fallback: tuple[str, ...] = ("USDT", "USDC", "USD")) -> list[dict[str, Any]]:
        """Check exact symbols against exchangeInfo. Never substitutes a similarly named coin.

        When the exact pair is missing or not trading but the SAME base asset trades against
        another allowed quote (e.g. MXC/USD is BREAK while MXC/USDT is TRADING, which is the
        normal state of Binance.US since it lost USD rails), the record carries ``fallback``
        with that pair; the report shows both so the substitution is never silent."""
        info = self.exchange_info()
        by_id = {k: v for k, v in info.items()}

        def _perms(x: dict[str, Any]) -> list[str]:
            return x.get("permissions") or [p for ps in (x.get("permissionSets") or []) for p in ps]

        def _trading(x: dict[str, Any]) -> bool:
            return str(x.get("status")) == "TRADING" and (("SPOT" in _perms(x)) or bool(x.get("isSpotTradingAllowed")))

        out = []
        for sym in symbols:
            base, _, quote = sym.partition("/")
            mid = f"{base}{quote}"
            s = by_id.get(mid)
            similar = sorted(k for k, v in by_id.items() if k != mid and str(v.get("baseAsset")) != base and (base in k or str(v.get("baseAsset")) in base))[:5]
            alts = [v for k, v in by_id.items() if k != mid and str(v.get("baseAsset")) == base and str(v.get("quoteAsset")) in quote_fallback and _trading(v)]
            alts.sort(key=lambda v: quote_fallback.index(str(v["quoteAsset"])))
            fallback = f"{alts[0]['baseAsset']}/{alts[0]['quoteAsset']}" if alts else None
            if s is None:
                note = "not listed on Binance.US" + (f"; similarly named (NOT the same coin): {', '.join(similar)}" if similar else "")
                rec = dict(symbol=sym, market_id=mid, status="MISSING", available=False, spot=False, base=base, quote=quote, note=note)
            else:
                spot = ("SPOT" in _perms(s)) or bool(s.get("isSpotTradingAllowed"))
                status = str(s.get("status", "UNKNOWN"))
                available = status == "TRADING" and spot
                note = "" if available else f"status {status}, spot {'yes' if spot else 'no'}"
                rec = dict(symbol=sym, market_id=mid, status=status, available=available, spot=spot, base=str(s.get("baseAsset", base)), quote=str(s.get("quoteAsset", quote)), note=note)
            if not rec["available"] and fallback:
                rec["fallback"] = fallback
                rec["note"] = (rec["note"] + "; " if rec["note"] else "") + f"same asset trades as {fallback} (TRADING): researched instead"
            self.store.put_market(**{k: v for k, v in rec.items() if k != "fallback"})
            out.append(rec)
        return out

    def eligible_markets(self, quotes: tuple[str, ...] = ("USD", "USDT")) -> list[dict[str, Any]]:
        """All spot markets currently TRADING in the wanted quotes (for the scanner)."""
        info = self.exchange_info()
        out = []
        for mid, s in info.items():
            perms = s.get("permissions") or [p for ps in (s.get("permissionSets") or []) for p in ps]
            if str(s.get("status")) != "TRADING" or not (("SPOT" in perms) or s.get("isSpotTradingAllowed")):
                continue
            if str(s.get("quoteAsset")) not in quotes:
                continue
            out.append({"symbol": f"{s['baseAsset']}/{s['quoteAsset']}", "market_id": mid, "base": s["baseAsset"], "quote": s["quoteAsset"]})
        return out

    # -- klines ------------------------------------------------------------------

    def fetch_klines(self, market_id: str, interval: str, start_ms: int, end_ms: int) -> list[tuple]:
        """Raw klines as EW rows: (ts, o, h, l, c, v, qv, trades, taker_buy_base, taker_buy_quote)."""
        step = INTERVAL_MS[interval]
        out: list[tuple] = []
        cur = start_ms
        while cur < end_ms:
            raw = self.client.publicGetKlines({"symbol": market_id, "interval": interval, "startTime": int(cur), "endTime": int(end_ms - 1), "limit": 1000})
            self._throttle()
            if not raw:
                break
            for k in raw:
                out.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]), int(k[8]), float(k[9]), float(k[10])))
            last = int(raw[-1][0])
            if last + step <= cur:
                break
            cur = last + step
            if len(raw) < 1000:
                break
        return out

    def download_klines(self, symbol: str, market_id: str, interval: str, days: int, now_ms: int | None = None) -> int:
        now_ms = now_ms or int(self.client.milliseconds())
        start = now_ms - days * 86_400_000
        rng = self.store.kline_range(symbol, interval)
        if rng and rng[1] >= start:
            start = max(start, rng[1])  # resume from the last stored bar (re-fetched: it may have been open)
        rows = self.fetch_klines(market_id, interval, start, now_ms)
        n = self.store.upsert_klines(symbol, interval, rows)
        rng = self.store.kline_range(symbol, interval)
        if rng:
            self.store.put_download(symbol, f"klines_{interval}", rng[0], rng[1], rng[2])
        return n

    # -- aggregated trades -------------------------------------------------------

    def fetch_agg_trades(self, market_id: str, start_ms: int, end_ms: int, *, max_rows: int | None = None, empty_windows: int = 72) -> list[tuple]:
        """(agg_id, ts, price, qty, buyer_maker). Pages by fromId after the first time-window request.

        Binance answers a time-window request with the trades of at most one hour. On a thin pair, or
        before the pair was listed, that first hour can be empty, so the window walks forward hour by
        hour (up to ``empty_windows`` hours) until it finds the first trade; from there paging by id
        continues across any gap."""
        out: list[tuple] = []
        window = int(start_ms)
        raw: list[Any] = []
        for _ in range(max(1, empty_windows)):
            if window >= end_ms:
                break
            raw = self.client.publicGetAggTrades({"symbol": market_id, "startTime": window, "endTime": int(min(end_ms, window + 3_600_000)), "limit": 1000})
            self._throttle()
            if raw:
                break
            window += 3_600_000
        while raw:
            for t in raw:
                ts = int(t["T"])
                if ts >= end_ms:
                    return out
                out.append((int(t["a"]), ts, float(t["p"]), float(t["q"]), 1 if t.get("m") else 0))
            if max_rows and len(out) >= max_rows:
                break
            if len(raw) < 1000:
                # the hour window may simply have ended: continue from the last id
                raw = self.client.publicGetAggTrades({"symbol": market_id, "fromId": int(raw[-1]["a"]) + 1, "limit": 1000})
                self._throttle()
                if raw and int(raw[0]["T"]) >= end_ms:
                    break
                continue
            raw = self.client.publicGetAggTrades({"symbol": market_id, "fromId": int(raw[-1]["a"]) + 1, "limit": 1000})
            self._throttle()
        return out

    def download_agg_trades(self, symbol: str, market_id: str, days: int, now_ms: int | None = None, *, max_rows: int | None = None) -> int:
        now_ms = now_ms or int(self.client.milliseconds())
        start = now_ms - days * 86_400_000
        rng = self.store.aggtrade_range(symbol)
        if rng and rng[1] >= start:
            start = rng[1] + 1
        else:
            # seed the first time window from the first candle that actually traded at or after the start
            # (the pair may be younger than the requested history, or simply idle for hours at a time)
            k = self.store.klines(symbol, "1m", start_ms=start)
            traded = k[k["trades"] > 0] if len(k) else k
            if len(traded):
                start = max(start, int(traded["ts"][0]))
        rows = self.fetch_agg_trades(market_id, start, now_ms, max_rows=max_rows)
        n = self.store.upsert_aggtrades(symbol, rows)
        rng = self.store.aggtrade_range(symbol)
        if rng:
            self.store.put_download(symbol, "aggtrades", rng[0], rng[1], rng[3], "individual trades need an API key on Binance.US; aggregated trades used instead")
        return n

    # -- scanner -----------------------------------------------------------------

    def tickers(self) -> dict[str, dict[str, Any]]:
        t = self.client.fetch_tickers()
        self._throttle()
        return t


def download_all(data: BinanceUSData, symbols: list[str], days: int, *, trades: bool = True, progress: Callable[[str], None] = log.info, max_trade_rows: int | None = None) -> list[dict[str, Any]]:
    """Phase 1: verify, then download 1m and 5m klines and aggregated trades for each available symbol."""
    recs = data.verify_symbols(symbols)
    for r in list(recs):
        if not r["available"] and r.get("fallback"):
            fb = r["fallback"]
            progress(f"{r['symbol']}: unavailable ({r['note']})")
            if not any(x["symbol"] == fb for x in recs):
                base, _, quote = fb.partition("/")
                sub = dict(symbol=fb, market_id=f"{base}{quote}", status="TRADING", available=True, spot=True, base=base, quote=quote, note=f"substituted for {r['symbol']} ({r['status']}): same asset, different quote")
                data.store.put_market(**sub)
                recs.append(sub)
    for r in recs:
        if not r["available"]:
            if not r.get("fallback"):
                progress(f"{r['symbol']}: unavailable ({r['note']})")
            continue
        for interval in ("1m", "5m"):
            n = data.download_klines(r["symbol"], r["market_id"], interval, days)
            progress(f"{r['symbol']}: {n} new {interval} candles")
        if trades:
            n = data.download_agg_trades(r["symbol"], r["market_id"], days, max_rows=max_trade_rows)
            progress(f"{r['symbol']}: {n} new aggregated trades")
    return recs
