"""Phase 6-7-9-12 live: market-wide screening, detailed streams for the strongest candidates,
state classification, signal bookkeeping (true/false positives, missed surges) and an
independent paper book. Runs inside the engine process; owns its own market feed.

Nothing here can place an order. There is no exchange object with credentials.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np

from cryptobot.core.events import BookEvent, ConnectionEvent, EWCandidateView, EWStatusEvent, HeartbeatEvent, TickEvent
from cryptobot.earlywarning.config import COMPONENTS, EWConfig
from cryptobot.earlywarning.data import BinanceUSData
from cryptobot.earlywarning.features import price_changes, window_features
from cryptobot.earlywarning.paper import slip_pct
from cryptobot.earlywarning.scanner import Candidate, screen, snapshot
from cryptobot.earlywarning.score import INSUFFICIENT, EASModel, components_from_features
from cryptobot.earlywarning.states import classify
from cryptobot.earlywarning.store import EWStore, klines_array
from cryptobot.earlywarning.targets import TargetEstimator

log = logging.getLogger("earlywarning")
SIGNAL_STATES = ("POSSIBLE ACCUMULATION", "ACCUMULATION CONFIRMED", "BREAKOUT DEVELOPING")
NO_ENTRY_STATES = ("POSSIBLE DISTRIBUTION", "REVERSAL WARNING", "SURGE ACTIVE", "NO DATA")
SIGNAL_COOLDOWN_MS = 15 * 60_000
SIGNAL_HORIZON_MS = 60 * 60_000
SURGE_PCT = 5.0  # a live signal "was right" when price gained this much within the horizon


@dataclass
class _LiveBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    quote: float = 0.0
    trades: int = 0
    tb_base: float = 0.0
    tb_quote: float = 0.0

    def row(self) -> tuple:
        return (self.ts, self.open, self.high, self.low, self.close, self.volume, self.quote, self.trades, self.tb_base, self.tb_quote)


@dataclass
class _Sym:
    history: list[tuple] = field(default_factory=list)  # stored 1m rows (ts ascending)
    bars: dict[int, _LiveBar] = field(default_factory=dict)
    bid: float = 0.0
    ask: float = 0.0
    bid_depth: float = 0.0
    last_tick_ms: int = 0
    last_book_ms: int = 0
    state: str = "NO DATA"
    eas: float | None = None
    comps: dict[str, Any] = field(default_factory=dict)
    feats: dict[str, Any] | None = None
    changes: dict[int, float | None] = field(default_factory=dict)
    last_signal_ms: int = 0
    data_status: str = "no data yet"
    reasons: list[str] = field(default_factory=list)
    updated_ms: int = 0
    breakout_level: float = 0.0
    retest_seen: bool = False


@dataclass
class _Pending:
    signal_id: int
    symbol: str
    start_ms: int
    price: float
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class _Position:
    symbol: str
    units: float
    entry_px: float
    cost: float
    stop: float
    t1: float
    t2: float
    entry_ms: int
    signal_id: int
    took1: bool = False
    took2: bool = False
    highest: float = 0.0


class EWPaperBook:
    """One virtual portfolio. Same costs as the research simulator; fills at the live bid/ask."""

    BOOK = "EW"

    def __init__(self, store: EWStore, cfg: EWConfig, clock_ms: Callable[[], int], tz: str = "America/New_York") -> None:
        self.store, self.cfg, self.clock_ms = store, cfg, clock_ms
        self._tz = ZoneInfo(tz)
        self.cash = cfg.risk.budget
        self.positions: dict[str, _Position] = {}
        self.day_key = ""
        self.day_start_equity = cfg.risk.budget
        self.realized_today = 0.0
        self.consecutive_losses = 0
        self.paused_until_ms = 0
        self.trades = self.wins = self.losses = 0
        self.gross_profit = self.gross_loss = self.fees = 0.0
        self.marks: dict[str, float] = {}
        self.rejections: dict[str, int] = {}
        self.restore()

    # -- persistence
    def to_state(self) -> dict[str, Any]:
        return {"budget": self.cfg.risk.budget, "cash": self.cash, "positions": {s: p.__dict__ for s, p in self.positions.items()}, "day_key": self.day_key, "day_start_equity": self.day_start_equity,
                "realized_today": self.realized_today, "consecutive_losses": self.consecutive_losses, "paused_until_ms": self.paused_until_ms, "trades": self.trades, "wins": self.wins, "losses": self.losses,
                "gross_profit": self.gross_profit, "gross_loss": self.gross_loss, "fees": self.fees, "rejections": self.rejections}

    def save(self) -> None:
        self.store.set("paper.state", self.to_state())

    def restore(self) -> None:
        st = self.store.get("paper.state")
        if not st or st.get("budget") != self.cfg.risk.budget:
            return
        self.cash = float(st["cash"])
        self.positions = {s: _Position(**p) for s, p in st.get("positions", {}).items()}
        for k in ("day_key",):
            setattr(self, k, st.get(k, ""))
        for k in ("day_start_equity", "realized_today", "gross_profit", "gross_loss", "fees"):
            setattr(self, k, float(st.get(k, 0.0)))
        for k in ("consecutive_losses", "paused_until_ms", "trades", "wins", "losses"):
            setattr(self, k, int(st.get(k, 0)))
        self.rejections = dict(st.get("rejections", {}))

    # -- accounting
    def equity(self) -> float:
        return self.cash + sum(p.units * self.marks.get(s, p.entry_px) for s, p in self.positions.items())

    def unrealized(self) -> float:
        return sum(p.units * self.marks.get(s, p.entry_px) - p.cost for s, p in self.positions.items())

    def _roll_day(self, now_ms: int) -> None:
        key = datetime.fromtimestamp(now_ms / 1000, self._tz).date().isoformat()
        if key != self.day_key:
            self.day_key, self.realized_today, self.day_start_equity = key, 0.0, self.equity()

    def daily_loss_hit(self, now_ms: int) -> bool:
        self._roll_day(now_ms)
        return self.realized_today + self.unrealized() <= -self.day_start_equity * self.cfg.risk.daily_loss_pct / 100.0

    def _reject(self, code: str) -> str:
        self.rejections[code] = self.rejections.get(code, 0) + 1
        return code

    # -- decisions
    def consider(self, sym: str, st: _Sym, target_net_pct: float | None, target_gross_pct: float | None, now_ms: int, quote_vol_1m: float) -> str:
        """Returns 'entered' or the rejection code."""
        r = self.cfg.risk
        if sym in self.positions:
            return self._reject("already_in_position")
        if st.state in NO_ENTRY_STATES:
            return self._reject("state_not_eligible")
        em = self.cfg.entry_method
        want = {"A": ("ACCUMULATION CONFIRMED",), "B": ("BREAKOUT DEVELOPING",), "C": ("BREAKOUT DEVELOPING",)}[em]
        if st.state not in want:
            return self._reject("waiting_for_entry_state")
        if em == "C" and not st.retest_seen:
            return self._reject("waiting_for_retest")
        if st.data_status != "ok":
            return self._reject("data_invalid")
        if target_net_pct is None or target_net_pct <= 0:
            return self._reject("target_not_reliable")
        if st.bid <= 0 or st.ask <= 0:
            return self._reject("no_book")
        spread = (st.ask - st.bid) / ((st.ask + st.bid) / 2) * 100
        if spread > r.max_spread_pct:
            return self._reject("spread_too_high")
        if now_ms < self.paused_until_ms:
            return self._reject("paused_after_losses")
        if self.daily_loss_hit(now_ms):
            return self._reject("daily_loss_limit")
        if len(self.positions) >= r.max_positions:
            return self._reject("max_positions")
        eq = self.equity()
        stop_pct = self.cfg.exits.stop_pct
        notional = min(eq * r.risk_per_trade_pct / 100.0 / (stop_pct / 100.0), self.cash, eq * 0.5)
        liq_cap = quote_vol_1m * r.max_position_share_of_minute_volume
        if liq_cap < 5.0:
            return self._reject("insufficient_liquidity")
        notional = min(notional, liq_cap)
        slip = slip_pct(notional, quote_vol_1m, self.cfg)
        rt_cost = 2 * self.cfg.costs.fee_rate * 100 + spread + 2 * slip
        gross = target_gross_pct if target_gross_pct is not None else target_net_pct + rt_cost
        if gross - rt_cost <= 0:
            return self._reject("expected_net_not_positive")
        px = st.ask * (1 + slip / 100.0)
        units = notional / px
        fee = units * px * self.cfg.costs.fee_rate
        self.cash -= units * px + fee
        self.fees += fee
        self.positions[sym] = _Position(sym, units, px, units * px + fee, px * (1 - stop_pct / 100.0), px * (1 + gross / 100.0), px * (1 + gross * self.cfg.exits.t2_multiple / 100.0), now_ms, 0, highest=px)
        self.store.insert_paper_trade(book=self.BOOK, symbol=sym, side="buy", ts_ms=now_ms, price=st.ask, fill_price=px, amount=units, fee=fee, reason=f"entry {em} in {st.state}, EAS {st.eas:.0f}" if st.eas is not None else f"entry {em}")
        self.save()
        return "entered"

    def on_price(self, sym: str, st: _Sym, now_ms: int, quote_vol_1m: float) -> str | None:
        pos = self.positions.get(sym)
        if st.bid > 0:
            self.marks[sym] = st.bid
        if pos is None or st.bid <= 0:
            return None
        ex = self.cfg.exits
        pos.highest = max(pos.highest, st.bid)
        reason = None
        qty = pos.units
        if st.bid <= pos.stop:
            reason = "stop"
        elif st.state in ("POSSIBLE DISTRIBUTION", "REVERSAL WARNING"):
            reason = "distribution warning"
        elif now_ms - pos.entry_ms >= ex.max_hold_min * 60_000:
            reason = "time exit"
        elif ex.method == "partial":
            if not pos.took1 and st.bid >= pos.t1:
                reason, qty = "target 1 (partial)", pos.units * ex.partial_shares[0] / (1 - 0)
                pos.took1 = True
                pos.stop = max(pos.stop, pos.entry_px)
            elif pos.took1 and not pos.took2 and st.bid >= pos.t2:
                reason, qty = "target 2 (partial)", pos.units * ex.partial_shares[1] / (ex.partial_shares[1] + ex.partial_shares[2])
                pos.took2 = True
            elif pos.took2 and st.bid <= pos.highest * (1 - ex.trailing_pct / 100.0):
                reason = "trailing"
        elif ex.method == "trailing":
            if pos.highest > pos.entry_px and st.bid <= pos.highest * (1 - ex.trailing_pct / 100.0):
                reason = "trailing"
        elif st.bid >= pos.t1:
            reason = "target"
        if reason is None:
            return None
        qty = min(qty, pos.units)
        slip = slip_pct(qty * st.bid, quote_vol_1m, self.cfg)
        px = st.bid * (1 - slip / 100.0)
        fee = qty * px * self.cfg.costs.fee_rate
        share = qty / pos.units
        basis = pos.cost * share
        pnl = qty * px - fee - basis
        self.cash += qty * px - fee
        self.fees += fee
        pos.units -= qty
        pos.cost -= basis
        self._roll_day(now_ms)
        self.realized_today += pnl
        self.store.insert_paper_trade(book=self.BOOK, symbol=sym, side="sell", ts_ms=now_ms, price=st.bid, fill_price=px, amount=qty, fee=fee, pnl=pnl, reason=reason)
        if pos.units <= 1e-12 or reason in ("stop", "distribution warning", "time exit", "trailing", "target"):
            if pos.units > 1e-12:  # close the remainder at the same bid
                rem = pos.units
                px2 = st.bid * (1 - slip_pct(rem * st.bid, quote_vol_1m, self.cfg) / 100.0)
                fee2 = rem * px2 * self.cfg.costs.fee_rate
                pnl2 = rem * px2 - fee2 - pos.cost
                self.cash += rem * px2 - fee2
                self.fees += fee2
                self.realized_today += pnl2
                pnl += pnl2
                self.store.insert_paper_trade(book=self.BOOK, symbol=sym, side="sell", ts_ms=now_ms, price=st.bid, fill_price=px2, amount=rem, fee=fee2, pnl=pnl2, reason=reason + " (remainder)")
            self.positions.pop(sym, None)
            self.trades += 1
            if pnl > 0:
                self.wins += 1
                self.gross_profit += pnl
                self.consecutive_losses = 0
            else:
                self.losses += 1
                self.gross_loss += -pnl
                self.consecutive_losses += 1
                if self.consecutive_losses >= self.cfg.risk.max_consecutive_losses:
                    self.paused_until_ms = now_ms + self.cfg.risk.pause_minutes * 60_000
        self.save()
        return reason

    def status(self, now_ms: int) -> dict[str, Any]:
        self._roll_day(now_ms)
        return {"book": self.BOOK, "budget": self.cfg.risk.budget, "equity": self.equity(), "cash": self.cash, "unrealized": self.unrealized(), "realized_today": self.realized_today,
                "trades": self.trades, "wins": self.wins, "losses": self.losses, "net_pnl": self.gross_profit - self.gross_loss, "fees": self.fees, "consecutive_losses": self.consecutive_losses,
                "paused_until_ms": self.paused_until_ms, "halted": self.daily_loss_hit(now_ms), "positions": [dict(p.__dict__, mark=self.marks.get(s, p.entry_px)) for s, p in self.positions.items()],
                "rejections": dict(self.rejections), "entry_method": self.cfg.entry_method, "exit_method": self.cfg.exits.method}


class EarlyWarningEngine:
    def __init__(self, store: EWStore, emit: Callable[[Any], None], exchange_id: str, cfg: EWConfig, *, data: BinanceUSData | None = None,
                 feed_factory: Callable[[list[str], Callable[[Any], None]], Any] | None = None, clock: Callable[[], float] = time.time, timezone: str = "America/New_York") -> None:
        self.store, self.emit, self.exchange_id, self.cfg = store, emit, exchange_id, cfg
        self.data = data
        self.feed_factory = feed_factory
        self.clock = clock
        self.sym: dict[str, _Sym] = {}
        self.model = EASModel(cfg.weights_normalized(), min_obs=cfg.score.min_history_obs)
        self.targets = TargetEstimator(store.get_study("targets"), [dict(s, outcomes={int(h): o for h, o in s["outcomes"].items()}, vec=s["vec"]) for s in (store.get_study("setups") or [])])
        self.paper = EWPaperBook(store, cfg, self._now_ms, timezone)
        self.pending: list[_Pending] = []
        self.candidates: list[Candidate] = []
        self.prev_snapshot: dict[str, dict[str, Any]] | None = None
        self.eligible: list[dict[str, Any]] = []
        self.running = False
        self.connected = False
        self.message = ""
        self.last_scan_ms = 0
        self._last_eval_ms = 0
        self._last_status_ms = 0
        self._feed_task: asyncio.Task | None = None
        self._feed_stop: asyncio.Event | None = None
        self._seed_from_research()

    # ------------------------------------------------------------- helpers

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _seed_from_research(self) -> None:
        """Use the research grid windows as the live score distribution where they exist."""
        for m in self.store.markets():
            rows = [components_from_features(w["features"]) for w in self.store.windows(m["symbol"], kind="grid")]
            rows = [r for r in rows if r]
            if rows:
                self.model.seed(m["symbol"], rows)

    def selected(self) -> list[str]:
        return [c.symbol for c in self.candidates if not c.excluded][: self.cfg.scanner.top_n]

    # ------------------------------------------------------------ scanning

    def scan(self, tickers: dict[str, dict[str, Any]] | None = None, eligible: list[dict[str, Any]] | None = None) -> list[str]:
        """Phase 6 screening; blocking REST when tickers are not supplied. Returns the symbols to stream."""
        now = self._now_ms()
        if tickers is None or eligible is None:
            assert self.data is not None, "no data client"
            eligible = self.data.eligible_markets(tuple(self.cfg.scanner.quotes))
            tickers = self.data.tickers()
        self.eligible = eligible
        self.candidates = screen(tickers, eligible, self.cfg.scanner, self.prev_snapshot)
        self.prev_snapshot = snapshot(tickers)
        self.last_scan_ms = now
        chosen = self.selected()
        for s in chosen:
            self.sym.setdefault(s, _Sym())
            self.sym[s].reasons = next((c.reasons for c in self.candidates if c.symbol == s), [])
        for s in list(self.sym):
            if s not in chosen and s not in self.paper.positions:
                self.sym.pop(s)
        return chosen

    def ensure_history(self, symbol: str, days: int = 8) -> int:
        """Blocking REST: make sure the store has recent 1m candles (for baselines), then load them."""
        n = 0
        if self.data is not None:
            mid = next((m["market_id"] for m in self.eligible if m["symbol"] == symbol), symbol.replace("/", ""))
            try:
                n = self.data.download_klines(symbol, mid, "1m", days, now_ms=self._now_ms())
            except Exception as e:  # noqa: BLE001
                log.warning("history for %s failed: %s", symbol, e)
        k = self.store.klines(symbol, "1m", start_ms=self._now_ms() - days * 86_400_000)
        self.sym.setdefault(symbol, _Sym()).history = [tuple(r) for r in k.tolist()]
        return n

    # --------------------------------------------------------------- feed

    def on_feed_event(self, ev: Any) -> None:
        if isinstance(ev, ConnectionEvent):
            self.connected = ev.status == "connected"
            if not self.connected:
                for st in self.sym.values():
                    st.data_status = f"connection {ev.status}"
            return
        if isinstance(ev, TickEvent) and ev.tick.symbol in self.sym:
            st = self.sym[ev.tick.symbol]
            t = ev.tick
            st.last_tick_ms = max(st.last_tick_ms, t.ts_ms)
            ts = t.ts_ms - t.ts_ms % 60_000
            b = st.bars.get(ts)
            if b is None:
                b = st.bars[ts] = _LiveBar(ts, t.price, t.price, t.price, t.price)
                for old in [k for k in st.bars if k < ts - 400 * 60_000]:
                    st.bars.pop(old)
            b.high, b.low, b.close = max(b.high, t.price), min(b.low, t.price), t.price
            q = t.price * t.amount
            b.volume += t.amount
            b.quote += q
            b.trades += 1
            if t.side == "buy":
                b.tb_base += t.amount
                b.tb_quote += q
            elif t.side is None:
                b.tb_base += t.amount / 2
                b.tb_quote += q / 2
            if st.bid == 0:
                st.bid = st.ask = t.price
        elif isinstance(ev, BookEvent) and ev.symbol in self.sym and ev.synced:
            st = self.sym[ev.symbol]
            st.bid, st.ask, st.bid_depth, st.last_book_ms = ev.best_bid, ev.best_ask, ev.bid_depth_near, ev.ts_ms
        elif isinstance(ev, HeartbeatEvent):
            self.tick_clock(ev.ts_ms)

    def tick_clock(self, now_ms: int) -> None:
        if now_ms - self._last_eval_ms >= 60_000:
            self._last_eval_ms = now_ms
            self.evaluate_all(now_ms)
        else:
            for s, st in self.sym.items():
                self._track(s, st, now_ms)
        self._publish(now_ms)

    # ---------------------------------------------------------- evaluation

    def _klines(self, st: _Sym, now_ms: int) -> np.ndarray:
        cur = now_ms - now_ms % 60_000
        live = [b.row() for ts, b in sorted(st.bars.items()) if ts < cur]  # closed live bars only
        hist = st.history
        if live:
            first_live = live[0][0]
            hist = [r for r in hist if r[0] < first_live]
        return klines_array(hist + live)

    def evaluate_all(self, now_ms: int) -> None:
        for s, st in self.sym.items():
            self.evaluate(s, st, now_ms)

    def evaluate(self, sym: str, st: _Sym, now_ms: int) -> None:
        sc = self.cfg.scanner
        # data validity first
        if not self.connected:
            st.data_status = "connection down"
        elif st.last_tick_ms == 0:
            st.data_status = "no trades yet"
        elif now_ms - st.last_tick_ms > sc.stale_s * 1000:
            st.data_status = f"trades stale ({(now_ms - st.last_tick_ms) // 1000}s)"
        elif st.last_book_ms and now_ms - st.last_book_ms > sc.stale_s * 1000:
            st.data_status = "order book stale"
        elif st.bid > 0 and st.ask > 0 and (st.ask - st.bid) / ((st.ask + st.bid) / 2) * 100 > sc.max_spread_pct:
            st.data_status = f"spread {(st.ask - st.bid) / ((st.ask + st.bid) / 2) * 100:.2f}% too wide"
        else:
            st.data_status = "ok"
        k = self._klines(st, now_ms)
        st.feats = window_features(k, now_ms - now_ms % 60_000, 60, self.cfg.score.baseline_minutes, None, large_pct=self.cfg.score.large_trade_pct) if len(k) else None
        comps = components_from_features(st.feats)
        if st.data_status == "ok" and comps is not None:
            self.model.add(sym, comps)
            st.eas, st.comps = self.model.score(sym, comps)
        else:
            st.eas, st.comps = None, {"status": INSUFFICIENT, "detail": st.data_status if st.data_status != "ok" else "features unavailable (baseline too short)"}
        st.changes = price_changes(k, now_ms) if len(k) else {m: None for m in (5, 15, 30, 60)}
        new_state = classify(st.eas, st.comps, st.changes, imbalance=(st.feats or {}).get("imbalance"), rel_volume=(st.feats or {}).get("rel_volume"), data_ok=st.data_status == "ok", cfg=self.cfg.score)
        if new_state == "BREAKOUT DEVELOPING" and st.state != "BREAKOUT DEVELOPING":
            st.breakout_level = max((b.high for ts, b in st.bars.items() if ts >= now_ms - 30 * 60_000), default=st.ask)
            st.retest_seen = False
        if st.breakout_level and st.bid and st.bid <= st.breakout_level * 1.005 and st.state in ("BREAKOUT DEVELOPING", "SURGE ACTIVE"):
            st.retest_seen = True
        if new_state != st.state:
            self.store.insert_transition(now_ms, sym, st.state, new_state, st.eas, st.data_status)
            if st.state in ("QUIET", "NO DATA", "POSSIBLE ACCUMULATION") and new_state == "SURGE ACTIVE" and now_ms - st.last_signal_ms > SIGNAL_HORIZON_MS:
                self.store.insert_transition(now_ms, sym, "MISSED", "SURGE ACTIVE", st.eas, "surge without a prior warning")
            st.state = new_state
        st.updated_ms = now_ms
        if st.state in SIGNAL_STATES and now_ms - st.last_signal_ms >= SIGNAL_COOLDOWN_MS and st.data_status == "ok":
            sid = self.store.insert_signal(now_ms, sym, st.eas, st.comps, st.state, st.bid or float(k["close"][-1]) if len(k) else 0.0, True, "; ".join(st.reasons))
            st.last_signal_ms = now_ms
            self.pending.append(_Pending(sid, sym, now_ms, st.bid or (float(k["close"][-1]) if len(k) else 0.0)))
        qv1 = float(np.mean(k["quote_volume"][-5:])) if len(k) else 0.0
        self.paper.consider(sym, st, *self.targets.target(60)[:1], self.targets.gross_target(60), now_ms, qv1)
        self._track(sym, st, now_ms)

    def _track(self, sym: str, st: _Sym, now_ms: int) -> None:
        price = st.bid or 0.0
        if price <= 0:
            return
        k_last = None
        qv1 = float(np.mean([b.quote for b in list(st.bars.values())[-5:]])) if st.bars else 0.0
        self.paper.on_price(sym, st, now_ms, qv1)
        done = []
        for p in self.pending:
            if p.symbol != sym or p.price <= 0:
                continue
            mv = (price / p.price - 1) * 100
            p.mfe, p.mae = max(p.mfe, mv), min(p.mae, mv)
            if now_ms - p.start_ms >= SIGNAL_HORIZON_MS:
                self.store.resolve_signal(p.signal_id, "surge" if p.mfe >= SURGE_PCT else "no_surge", p.mfe, p.mae, now_ms)
                done.append(p)
        for p in done:
            self.pending.remove(p)
        del k_last

    # --------------------------------------------------------------- stats

    def stats(self) -> dict[str, Any]:
        rows = self.store.signals(limit=5000)
        resolved = [r for r in rows if r["outcome"] in ("surge", "no_surge")]
        tp = sum(1 for r in resolved if r["outcome"] == "surge")
        missed = sum(1 for t in self.store.transitions(limit=5000) if t["from_state"] == "MISSED")
        return {"signals": len(rows), "pending": len(self.pending), "resolved": len(resolved), "true_positives": tp, "false_positives": len(resolved) - tp,
                "precision": tp / len(resolved) if resolved else None, "missed_surges": missed, "recall": tp / (tp + missed) if (tp + missed) else None,
                "avg_mfe_pct": float(np.mean([r["mfe_pct"] for r in resolved])) if resolved else None}

    # -------------------------------------------------------------- status

    def status(self) -> EWStatusEvent:
        now = self._now_ms()
        views = []
        for s in self.selected() + [p for p in self.paper.positions if p not in self.selected()]:
            st = self.sym.get(s)
            if st is None:
                continue
            f = st.feats or {}
            warnings = []
            spread = (st.ask - st.bid) / ((st.ask + st.bid) / 2) * 100 if st.bid > 0 and st.ask > 0 else None
            if spread is not None and spread > self.cfg.risk.max_spread_pct:
                warnings.append(f"spread {spread:.2f}%")
            qv1 = float(np.mean([b.quote for b in list(st.bars.values())[-5:]])) if st.bars else None
            if qv1 is not None and qv1 * self.cfg.risk.max_position_share_of_minute_volume < 5.0:
                warnings.append("thin: cannot size a position")
            if st.data_status != "ok":
                warnings.append(st.data_status)
            tnet, tlabel = self.targets.target(60)
            vec = [float(st.comps[c]) for c in COMPONENTS] if st.comps.get("status") == "ok" else None
            p = self.targets.probability(60, vec) if vec else None
            est_net = tnet if tnet is not None else None
            sim = {"n": len(self.targets.setups), "p": p, "note": "validated" if p is not None else "not validated"}
            views.append(EWCandidateView(s, st.state if self.running else "NO DATA", st.eas, dict(st.comps), st.data_status, st.bid or 0.0, dict(st.changes), f.get("rel_volume"),
                                         ((1 + f["imbalance"]) / max(1e-9, 1 - f["imbalance"])) if f.get("imbalance") is not None else None, f.get("freq_change"), sim, p, tlabel, tnet, est_net,
                                         warnings, spread, qv1, list(st.reasons), st.updated_ms, ""))
        scanner = {"eligible": len(self.eligible), "excluded": sum(1 for c in self.candidates if c.excluded), "selected": self.selected(), "last_scan_ms": self.last_scan_ms,
                   "next_scan_ms": self.last_scan_ms + self.cfg.scanner.rescan_s * 1000 if self.last_scan_ms else 0, "top": [(c.symbol, round(c.score, 2), c.reasons, c.excluded) for c in self.candidates[:15]]}
        research = {"has_report": bool(self.store.get_study("run")), "targets": {h: v.get("label") for h, v in (self.store.get_study("targets") or {}).get("horizons", {}).items()}, "demo": bool((self.store.get_study("run") or {}).get("demo"))}
        return EWStatusEvent(self.running, self.connected, self.message, scanner, views, self.paper.status(now), self.stats(), research, self.store.transitions(limit=20))

    def _publish(self, now_ms: int, force: bool = False) -> None:
        if not force and now_ms - self._last_status_ms < 2000:
            return
        self._last_status_ms = now_ms
        self.emit(self.status())

    # ------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        """Scan, stream the strongest candidates, rescan on a schedule; restart the feed when the set changes."""
        self.running = True
        self.message = "Scanning Binance.US markets…"
        loop = asyncio.get_running_loop()
        current: list[str] = []
        try:
            while not stop.is_set():
                try:
                    chosen = await loop.run_in_executor(None, self.scan)
                    for s in chosen:
                        if not self.sym[s].history:
                            await loop.run_in_executor(None, self.ensure_history, s)
                    self.message = f"Streaming {len(chosen)} of {len(self.eligible)} eligible markets; next scan in {self.cfg.scanner.rescan_s // 60} min."
                except Exception as e:  # noqa: BLE001
                    log.warning("scan failed: %s", e)
                    self.message = f"Scan failed: {e}"[:200]
                    chosen = current
                if chosen != current:
                    await self._restart_feed(chosen)
                    current = chosen
                self._publish(self._now_ms(), force=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.scanner.rescan_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_feed()
            self.paper.save()
            self.running = False
            self._publish(self._now_ms(), force=True)

    async def _restart_feed(self, symbols: list[str]) -> None:
        await self._stop_feed()
        if not symbols:
            return
        if self.feed_factory is None:
            from cryptobot.data.feed import MarketFeed

            feed = MarketFeed(self.exchange_id, symbols, self.on_feed_event, timeframes_s=(60,), history_bars=120)
        else:
            feed = self.feed_factory(symbols, self.on_feed_event)
        self._feed_stop = asyncio.Event()
        self._feed_task = asyncio.create_task(feed.run(self._feed_stop), name="ew-feed")

    async def _stop_feed(self) -> None:
        if self._feed_stop is not None:
            self._feed_stop.set()
        if self._feed_task is not None:
            try:
                await asyncio.wait_for(self._feed_task, timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._feed_task.cancel()
        self._feed_task, self._feed_stop = None, None
