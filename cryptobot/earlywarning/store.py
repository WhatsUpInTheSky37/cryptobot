"""Early Warning's own SQLite database (separate file, separate tables, nothing shared with Pulse or the trader)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS ew_markets (symbol TEXT PRIMARY KEY, market_id TEXT, status TEXT, available INTEGER, spot INTEGER, base TEXT, quote TEXT, note TEXT, checked_ms INTEGER);
CREATE TABLE IF NOT EXISTS ew_klines (symbol TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL, trades INTEGER, taker_buy_base REAL, taker_buy_quote REAL, PRIMARY KEY (symbol, interval, ts));
CREATE TABLE IF NOT EXISTS ew_aggtrades (symbol TEXT, agg_id INTEGER, ts INTEGER, price REAL, qty REAL, buyer_maker INTEGER, PRIMARY KEY (symbol, agg_id));
CREATE INDEX IF NOT EXISTS ix_ew_aggtrades_ts ON ew_aggtrades (symbol, ts);
CREATE TABLE IF NOT EXISTS ew_downloads (symbol TEXT, kind TEXT, first_ms INTEGER, last_ms INTEGER, rows INTEGER, updated_ms INTEGER, note TEXT, PRIMARY KEY (symbol, kind));
CREATE TABLE IF NOT EXISTS ew_events (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, start_ms INTEGER, trigger_ms INTEGER, peak_ms INTEGER, pre_price REAL, peak_price REAL, peak_bid REAL, gain_pct REAL, minutes_to_peak REAL, reversal_ms INTEGER, minutes_to_reversal REAL, max_decline_pct REAL, quote_volume REAL, thresholds TEXT, UNIQUE (symbol, start_ms));
CREATE TABLE IF NOT EXISTS ew_windows (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, kind TEXT, event_id INTEGER, t0_ms INTEGER, lookback_min INTEGER, features TEXT, eas REAL, components TEXT, UNIQUE (symbol, kind, event_id, t0_ms, lookback_min));
CREATE INDEX IF NOT EXISTS ix_ew_windows ON ew_windows (symbol, kind, lookback_min);
CREATE TABLE IF NOT EXISTS ew_study (key TEXT PRIMARY KEY, value TEXT, updated_ms INTEGER);
CREATE TABLE IF NOT EXISTS ew_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, symbol TEXT, eas REAL, components TEXT, state TEXT, price REAL, data_ok INTEGER, note TEXT, outcome TEXT, mfe_pct REAL, mae_pct REAL, resolved_ms INTEGER);
CREATE INDEX IF NOT EXISTS ix_ew_signals ON ew_signals (symbol, ts_ms);
CREATE TABLE IF NOT EXISTS ew_transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, symbol TEXT, from_state TEXT, to_state TEXT, eas REAL, note TEXT);
CREATE TABLE IF NOT EXISTS ew_paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT, symbol TEXT, side TEXT, ts_ms INTEGER, price REAL, fill_price REAL, amount REAL, fee REAL, pnl REAL, reason TEXT, signal_id INTEGER);
CREATE TABLE IF NOT EXISTS ew_equity (book TEXT, ts_ms INTEGER, equity REAL, cash REAL, PRIMARY KEY (book, ts_ms));
CREATE TABLE IF NOT EXISTS ew_settings (key TEXT PRIMARY KEY, value TEXT);
"""

KLINE_COLS = ("ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote")


class EWStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL") if str(path) != ":memory:" else None
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    # -- settings / study ----------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        row = self.con.execute("SELECT value FROM ew_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.con.execute("INSERT OR REPLACE INTO ew_settings (key, value) VALUES (?, ?)", (key, json.dumps(value, default=float)))
        self.con.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (SQLite has no IF NOT EXISTS for columns)."""
        have = {r[1] for r in self.con.execute("PRAGMA table_info(ew_events)").fetchall()}
        for col, decl in (("trades", "INTEGER"), ("tradeable", "INTEGER"), ("peak_truncated", "INTEGER")):
            if col not in have:
                self.con.execute(f"ALTER TABLE ew_events ADD COLUMN {col} {decl}")
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def put_study(self, key: str, value: Any) -> None:
        self.con.execute("INSERT OR REPLACE INTO ew_study (key, value, updated_ms) VALUES (?, ?, ?)", (key, json.dumps(value, default=float), int(time.time() * 1000)))
        self.con.commit()

    def get_study(self, key: str, default: Any = None) -> Any:
        row = self.con.execute("SELECT value FROM ew_study WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # -- markets -------------------------------------------------------------

    def put_market(self, symbol: str, market_id: str, status: str, available: bool, spot: bool, base: str, quote: str, note: str = "") -> None:
        self.con.execute("INSERT OR REPLACE INTO ew_markets VALUES (?,?,?,?,?,?,?,?,?)", (symbol, market_id, status, int(available), int(spot), base, quote, note, int(time.time() * 1000)))
        self.con.commit()

    def markets(self) -> list[dict[str, Any]]:
        cur = self.con.execute("SELECT symbol, market_id, status, available, spot, base, quote, note, checked_ms FROM ew_markets ORDER BY symbol")
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    def delete_symbol(self, symbol: str) -> None:
        """Remove a market and every row of data derived from it (candles, trades, events, windows, downloads)."""
        for table in ("ew_markets", "ew_klines", "ew_aggtrades", "ew_downloads", "ew_events", "ew_windows"):
            self.con.execute(f"DELETE FROM {table} WHERE symbol=?", (symbol,))
        self.con.commit()

    def daily_quote_volumes(self, symbol: str, interval: str = "1m") -> list[float]:
        """Quote volume summed per UTC day, oldest first (days with at least one candle)."""
        cur = self.con.execute("SELECT ts / 86400000 AS d, SUM(quote_volume) FROM ew_klines WHERE symbol=? AND interval=? GROUP BY d ORDER BY d", (symbol, interval))
        return [float(r[1] or 0.0) for r in cur.fetchall()]

    # -- klines --------------------------------------------------------------

    def upsert_klines(self, symbol: str, interval: str, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        self.con.executemany("INSERT OR REPLACE INTO ew_klines VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [(symbol, interval, *r) for r in rows])
        self.con.commit()
        return len(rows)

    def klines(self, symbol: str, interval: str = "1m", start_ms: int | None = None, end_ms: int | None = None) -> np.ndarray:
        """Structured array sorted by ts with KLINE_COLS fields (empty array if none)."""
        q = "SELECT ts, open, high, low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote FROM ew_klines WHERE symbol=? AND interval=?"
        args: list[Any] = [symbol, interval]
        if start_ms is not None:
            q += " AND ts>=?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND ts<?"
            args.append(end_ms)
        rows = self.con.execute(q + " ORDER BY ts", args).fetchall()
        return klines_array(rows)

    def kline_range(self, symbol: str, interval: str = "1m") -> tuple[int, int, int] | None:
        r = self.con.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM ew_klines WHERE symbol=? AND interval=?", (symbol, interval)).fetchone()
        return (int(r[0]), int(r[1]), int(r[2])) if r and r[0] is not None else None

    # -- trades --------------------------------------------------------------

    def upsert_aggtrades(self, symbol: str, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        self.con.executemany("INSERT OR IGNORE INTO ew_aggtrades VALUES (?,?,?,?,?,?)", [(symbol, *r) for r in rows])
        self.con.commit()
        return len(rows)

    def aggtrades(self, symbol: str, start_ms: int, end_ms: int) -> np.ndarray:
        rows = self.con.execute("SELECT ts, price, qty, buyer_maker FROM ew_aggtrades WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts", (symbol, start_ms, end_ms)).fetchall()
        return trades_array(rows)

    def aggtrade_range(self, symbol: str) -> tuple[int, int, int, int] | None:
        r = self.con.execute("SELECT MIN(ts), MAX(ts), MAX(agg_id), COUNT(*) FROM ew_aggtrades WHERE symbol=?", (symbol,)).fetchone()
        return (int(r[0]), int(r[1]), int(r[2]), int(r[3])) if r and r[0] is not None else None

    def put_download(self, symbol: str, kind: str, first_ms: int, last_ms: int, rows: int, note: str = "") -> None:
        self.con.execute("INSERT OR REPLACE INTO ew_downloads VALUES (?,?,?,?,?,?,?)", (symbol, kind, first_ms, last_ms, rows, int(time.time() * 1000), note))
        self.con.commit()

    def downloads(self) -> list[dict[str, Any]]:
        cur = self.con.execute("SELECT symbol, kind, first_ms, last_ms, rows, updated_ms, note FROM ew_downloads ORDER BY symbol, kind")
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    # -- events / windows ----------------------------------------------------

    def replace_events(self, symbol: str, events: list[dict[str, Any]]) -> None:
        self.con.execute("DELETE FROM ew_windows WHERE symbol=? AND kind='event'", (symbol,))
        self.con.execute("DELETE FROM ew_events WHERE symbol=?", (symbol,))
        for e in events:
            self.con.execute(
                "INSERT INTO ew_events (symbol, start_ms, trigger_ms, peak_ms, pre_price, peak_price, peak_bid, gain_pct, minutes_to_peak, reversal_ms, minutes_to_reversal, max_decline_pct, quote_volume, thresholds, trades, tradeable, peak_truncated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, e["start_ms"], e["trigger_ms"], e["peak_ms"], e["pre_price"], e["peak_price"], e.get("peak_bid"), e["gain_pct"], e["minutes_to_peak"], e.get("reversal_ms"), e.get("minutes_to_reversal"), e.get("max_decline_pct"), e.get("quote_volume"), json.dumps(e.get("thresholds", [])),
                 e.get("trades"), int(bool(e.get("tradeable", True))), int(bool(e.get("peak_truncated", False)))),
            )
        self.con.commit()

    def events(self, symbol: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM ew_events" + (" WHERE symbol=?" if symbol else "") + " ORDER BY symbol, start_ms"
        cur = self.con.execute(q, (symbol,) if symbol else ())
        out = []
        for r in cur.fetchall():
            d = dict(zip([c[0] for c in cur.description], r))
            d["thresholds"] = json.loads(d["thresholds"] or "[]")
            d["tradeable"] = bool(d["tradeable"]) if d.get("tradeable") is not None else True
            d["peak_truncated"] = bool(d.get("peak_truncated"))
            out.append(d)
        return out

    def put_window(self, symbol: str, kind: str, event_id: int | None, t0_ms: int, lookback_min: int, features: dict[str, Any], eas: float | None, components: dict[str, Any]) -> None:
        self.con.execute("INSERT OR REPLACE INTO ew_windows (symbol, kind, event_id, t0_ms, lookback_min, features, eas, components) VALUES (?,?,?,?,?,?,?,?)",
                         (symbol, kind, event_id or 0, t0_ms, lookback_min, json.dumps(features, default=float), eas, json.dumps(components, default=float)))

    def commit(self) -> None:
        self.con.commit()

    def windows(self, symbol: str | None = None, kind: str | None = None, lookback_min: int | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM ew_windows WHERE 1=1", []
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if lookback_min is not None:
            q += " AND lookback_min=?"
            args.append(lookback_min)
        cur = self.con.execute(q + " ORDER BY t0_ms", args)
        out = []
        for r in cur.fetchall():
            d = dict(zip([c[0] for c in cur.description], r))
            d["features"] = json.loads(d["features"] or "{}")
            d["components"] = json.loads(d["components"] or "{}")
            out.append(d)
        return out

    def clear_windows(self, symbol: str, kind: str) -> None:
        self.con.execute("DELETE FROM ew_windows WHERE symbol=? AND kind=?", (symbol, kind))
        self.con.commit()

    # -- live signals / transitions / paper -------------------------------------

    def insert_signal(self, ts_ms: int, symbol: str, eas: float | None, components: dict[str, Any], state: str, price: float, data_ok: bool, note: str) -> int:
        cur = self.con.execute("INSERT INTO ew_signals (ts_ms, symbol, eas, components, state, price, data_ok, note, outcome) VALUES (?,?,?,?,?,?,?,?,'pending')",
                               (ts_ms, symbol, eas, json.dumps(components, default=float), state, price, int(data_ok), note))
        self.con.commit()
        return int(cur.lastrowid)

    def resolve_signal(self, signal_id: int, outcome: str, mfe_pct: float, mae_pct: float, resolved_ms: int) -> None:
        self.con.execute("UPDATE ew_signals SET outcome=?, mfe_pct=?, mae_pct=?, resolved_ms=? WHERE id=?", (outcome, mfe_pct, mae_pct, resolved_ms, signal_id))
        self.con.commit()

    def signals(self, symbol: str | None = None, limit: int = 1000, pending_only: bool = False) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM ew_signals WHERE 1=1", []
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        if pending_only:
            q += " AND outcome='pending'"
        cur = self.con.execute(q + " ORDER BY ts_ms DESC LIMIT ?", (*args, limit))
        out = []
        for r in cur.fetchall():
            d = dict(zip([c[0] for c in cur.description], r))
            d["components"] = json.loads(d["components"] or "{}")
            out.append(d)
        return out

    def insert_transition(self, ts_ms: int, symbol: str, from_state: str, to_state: str, eas: float | None, note: str = "") -> None:
        self.con.execute("INSERT INTO ew_transitions (ts_ms, symbol, from_state, to_state, eas, note) VALUES (?,?,?,?,?,?)", (ts_ms, symbol, from_state, to_state, eas, note))
        self.con.commit()

    def transitions(self, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q = "SELECT ts_ms, symbol, from_state, to_state, eas, note FROM ew_transitions" + (" WHERE symbol=?" if symbol else "") + " ORDER BY ts_ms DESC LIMIT ?"
        cur = self.con.execute(q, ((symbol, limit) if symbol else (limit,)))
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    def insert_paper_trade(self, **f: Any) -> int:
        cur = self.con.execute("INSERT INTO ew_paper_trades (book, symbol, side, ts_ms, price, fill_price, amount, fee, pnl, reason, signal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (f["book"], f["symbol"], f["side"], f["ts_ms"], f["price"], f["fill_price"], f["amount"], f["fee"], f.get("pnl"), f.get("reason", ""), f.get("signal_id")))
        self.con.commit()
        return int(cur.lastrowid)

    def paper_trades(self, book: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        q = "SELECT * FROM ew_paper_trades" + (" WHERE book=?" if book else "") + " ORDER BY ts_ms DESC LIMIT ?"
        cur = self.con.execute(q, ((book, limit) if book else (limit,)))
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    def add_equity(self, book: str, ts_ms: int, equity: float, cash: float) -> None:
        self.con.execute("INSERT OR REPLACE INTO ew_equity VALUES (?,?,?,?)", (book, ts_ms, equity, cash))
        self.con.commit()

    def equity(self, book: str, limit: int = 20_000) -> list[tuple[int, float]]:
        rows = self.con.execute("SELECT ts_ms, equity FROM ew_equity WHERE book=? ORDER BY ts_ms DESC LIMIT ?", (book, limit)).fetchall()
        rows.reverse()
        return [(int(t), float(e)) for t, e in rows]


def klines_array(rows: list[tuple]) -> np.ndarray:
    dt = np.dtype([("ts", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("volume", "f8"), ("quote_volume", "f8"), ("trades", "i8"), ("taker_buy_base", "f8"), ("taker_buy_quote", "f8")])
    if not rows:
        return np.zeros(0, dtype=dt)
    return np.array([tuple(r) for r in rows], dtype=dt)


def trades_array(rows: list[tuple]) -> np.ndarray:
    dt = np.dtype([("ts", "i8"), ("price", "f8"), ("qty", "f8"), ("buyer_maker", "i1")])
    if not rows:
        return np.zeros(0, dtype=dt)
    return np.array([tuple(r) for r in rows], dtype=dt)
