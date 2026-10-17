"""The five Pulse signals, computed from live trades and order-book summaries.

Every raw measurement is turned into a 0..100 score by ranking it against the
same coin's own recent history, so "unusual" means unusual *for that coin*.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass

from cryptobot.core.events import BookEvent
from cryptobot.core.models import Tick


@dataclass(slots=True)
class Observation:
    ts_ms: int
    mid: float
    imb60: float
    spread_pct: float
    vol_pct: float
    # raw, long-side oriented
    f_raw: float
    a_raw: float
    w_raw: float
    r_raw: float
    c_raw: float
    # raw, short-side oriented
    fs_raw: float
    ws_raw: float
    rs_raw: float
    cs_raw: float


@dataclass(slots=True)
class Scores:
    f: float
    a: float
    w: float
    r: float
    c: float
    prs: float


def percentile_rank(history: list[float], x: float) -> float:
    """0..100: share of history strictly below x (history kept sorted)."""
    if not history:
        return 50.0
    return 100.0 * bisect_left(history, x) / len(history)


class _Ranked:
    """A bounded history that answers percentile queries."""

    def __init__(self, maxlen: int) -> None:
        self._q: deque[float] = deque(maxlen=maxlen)
        self._sorted: list[float] = []

    def add(self, x: float) -> None:
        if len(self._q) == self._q.maxlen:
            old = self._q[0]
            i = bisect_left(self._sorted, old)
            if i < len(self._sorted) and self._sorted[i] == old:
                self._sorted.pop(i)
        self._q.append(x)
        self._sorted.insert(bisect_left(self._sorted, x), x)

    def rank(self, x: float) -> float:
        return percentile_rank(self._sorted, x)

    def __len__(self) -> int:
        return len(self._q)


class SymbolFeatures:
    def __init__(self, symbol: str, window_s: int = 60, sub_s: int = 5, history_obs: int = 1440, min_obs: int = 60) -> None:
        self.symbol = symbol
        self.window_ms = window_s * 1000
        self.sub_ms = sub_s * 1000
        self.min_obs = min_obs
        self._trades: deque[Tick] = deque()
        self._books: deque[BookEvent] = deque()
        self._mids: deque[tuple[int, float]] = deque()
        self.vol_pct: float = 0.0  # 1-minute volatility (ATR as % of price), set from bars
        self.last_mid: float = 0.0
        self.last_book: BookEvent | None = None
        self._hist = {k: _Ranked(history_obs) for k in ("f", "a", "w", "r", "c", "fs", "ws", "rs", "cs")}
        self.observations = 0

    # -- inputs ---------------------------------------------------------------

    def add_tick(self, t: Tick) -> None:
        self._trades.append(t)
        if self.last_book is None:
            self.last_mid = t.price
            self._mids.append((t.ts_ms, t.price))
        self._trim(t.ts_ms)

    def add_book(self, b: BookEvent) -> None:
        self.last_book = b
        self.last_mid = b.mid
        self._books.append(b)
        self._mids.append((b.ts_ms, b.mid))
        self._trim(b.ts_ms)

    def _trim(self, now_ms: int) -> None:
        keep = now_ms - 10 * 60_000
        while self._trades and self._trades[0].ts_ms < keep:
            self._trades.popleft()
        while self._books and self._books[0].ts_ms < keep:
            self._books.popleft()
        while self._mids and self._mids[0][0] < keep:
            self._mids.popleft()

    # -- primitives -----------------------------------------------------------

    def flow(self, now_ms: int, window_ms: int) -> tuple[float, float]:
        """(buy quote volume, sell quote volume) over the window; unknown sides split evenly."""
        b = s = 0.0
        lo = now_ms - window_ms
        for t in reversed(self._trades):
            if t.ts_ms < lo:
                break
            v = t.price * t.amount
            if t.side == "buy":
                b += v
            elif t.side == "sell":
                s += v
            else:
                b += v / 2
                s += v / 2
        return b, s

    def imbalance(self, now_ms: int, window_ms: int | None = None) -> float:
        b, s = self.flow(now_ms, window_ms or self.window_ms)
        return (b - s) / (b + s) if (b + s) > 0 else 0.0

    def mid_at(self, ts_ms: int) -> float | None:
        best = None
        for t, m in self._mids:
            if t <= ts_ms:
                best = m
            else:
                break
        return best

    def recent_high_mid(self, now_ms: int, seconds: int) -> float:
        lo = now_ms - seconds * 1000
        vals = [m for t, m in self._mids if t >= lo]
        return max(vals) if vals else self.last_mid

    def book_at(self, ts_ms: int) -> BookEvent | None:
        best = None
        for b in self._books:
            if b.ts_ms <= ts_ms:
                best = b
            else:
                break
        return best

    def _depth_avg(self, now_ms: int, window_ms: int, side: str) -> float:
        lo = now_ms - window_ms
        vals = [(b.ask_depth_near if side == "ask" else b.bid_depth_near) for b in self._books if b.ts_ms >= lo]
        return sum(vals) / len(vals) if vals else 0.0

    # -- observation ----------------------------------------------------------

    def observe(self, now_ms: int, related_imb: float | None) -> Observation | None:
        if not self.last_mid:
            return None
        n_sub = max(1, self.window_ms // self.sub_ms)
        subs = []
        for k in range(n_sub):
            end = now_ms - k * self.sub_ms
            b, s = self.flow(end, self.sub_ms)
            subs.append((b - s) / (b + s) if (b + s) > 0 else 0.0)
        f_raw = sum(subs) / len(subs)
        bq, sq = self.flow(now_ms, self.window_ms)
        vol60 = bq + sq
        m0 = self.mid_at(now_ms - self.window_ms) or self.last_mid
        move_pct = abs(self.last_mid - m0) / m0 * 100.0 if m0 else 0.0
        a_raw = math.log1p(vol60 / max(move_pct, 0.005)) if vol60 > 0 else 0.0
        book_now = self.last_book
        book_10 = self.book_at(now_ms - 10_000)
        w_raw = ws_raw = 0.0
        if book_now and book_10:
            if book_10.ask_depth_near > 0:
                w_raw = (book_10.ask_depth_near - book_now.ask_depth_near) / book_10.ask_depth_near
            if book_10.bid_depth_near > 0:
                ws_raw = (book_10.bid_depth_near - book_now.bid_depth_near) / book_10.bid_depth_near
        r_raw = rs_raw = 0.0
        if book_now:
            aa = self._depth_avg(now_ms, self.window_ms, "ask")
            ba = self._depth_avg(now_ms, self.window_ms, "bid")
            r_raw = (aa - book_now.ask_depth_near) / aa if aa > 0 else 0.0
            rs_raw = (ba - book_now.bid_depth_near) / ba if ba > 0 else 0.0
        c_raw = related_imb if related_imb is not None else 0.0
        obs = Observation(
            now_ms, self.last_mid, self.imbalance(now_ms), book_now.spread_pct if book_now else 0.0, self.vol_pct,
            f_raw, a_raw, w_raw, r_raw, c_raw, -f_raw, ws_raw, rs_raw, -c_raw,
        )
        for k, v in (("f", f_raw), ("a", a_raw), ("w", w_raw), ("r", r_raw), ("c", c_raw), ("fs", -f_raw), ("ws", ws_raw), ("rs", rs_raw), ("cs", -c_raw)):
            self._hist[k].add(v)
        self.observations += 1
        return obs

    @property
    def warmed_up(self) -> bool:
        return self.observations >= self.min_obs

    def scores(self, obs: Observation, weights: dict[str, float], related_available: bool) -> tuple[Scores, Scores]:
        if not self.warmed_up:
            neutral = Scores(50, 50, 50, 50, 50, 50)
            return neutral, neutral

        def sc(key: str, x: float) -> float:
            return self._hist[key].rank(x)

        c_long = sc("c", obs.c_raw) if related_available else 50.0
        c_short = sc("cs", obs.cs_raw) if related_available else 50.0
        long_ = Scores(sc("f", obs.f_raw), sc("a", obs.a_raw), sc("w", obs.w_raw), sc("r", obs.r_raw), c_long, 0.0)
        short = Scores(sc("fs", obs.fs_raw), sc("a", obs.a_raw), sc("ws", obs.ws_raw), sc("rs", obs.rs_raw), c_short, 0.0)
        for s in (long_, short):
            s.prs = weights["f"] * s.f + weights["a"] * s.a + weights["w"] * s.w + weights["r"] * s.r + weights["c"] * s.c
        return long_, short
