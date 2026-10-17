"""The Pulse indicators, computed from live trades and order-book summaries.

Every raw measurement is turned into a 0..100 score by ranking it against the
same coin's own recent history, so "unusual" means unusual *for that coin*.
Long and short sides are ranked separately: a strong bearish reading is not
the mirror image of a bullish one, it is its own distribution.

Until a coin has ``min_obs`` observations there are no scores at all
(``scores`` returns None); nothing downstream may treat that as neutral.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import asdict, dataclass

from cryptobot.core.events import BookEvent
from cryptobot.core.models import Tick
from cryptobot.pulse.config import INDICATORS

# raw keys that have a separately ranked short-side version
DIRECTIONAL = ("f", "w", "r", "c", "acc", "vpr")


@dataclass(slots=True)
class Observation:
    ts_ms: int
    mid: float
    bid: float
    ask: float
    bid_depth: float
    ask_depth: float
    imb60: float
    spread_pct: float
    vol_pct: float  # 1-minute range from bars, %
    vol60_quote: float
    raw_long: dict[str, float]
    raw_short: dict[str, float]


@dataclass(slots=True)
class Scores:
    f: float
    a: float
    w: float
    r: float
    c: float
    acc: float
    vpr: float
    sx: float
    vx: float
    prs: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def percentile_rank(history: list[float], x: float) -> float:
    """0..100: mid-rank of x in the (sorted) history, so ties count half. A value equal
    to every past value ranks 50, not 0."""
    if not history:
        return 50.0
    lo, hi = bisect_left(history, x), bisect_right(history, x)
    return 100.0 * (lo + 0.5 * (hi - lo)) / len(history)


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


def combine(scores: dict[str, float], weights: dict[str, float]) -> float:
    """PRS in 0..100. Negative weights count (100 - score)."""
    num = den = 0.0
    for k, w in weights.items():
        s = scores.get(k)
        if s is None or w == 0:
            continue
        num += abs(w) * (s if w > 0 else 100.0 - s)
        den += abs(w)
    return num / den if den > 0 else 0.0


class SymbolFeatures:
    def __init__(self, symbol: str, window_s: int = 60, sub_s: int = 5, history_obs: int = 1440, min_obs: int = 60) -> None:
        self.symbol = symbol
        self.window_ms = window_s * 1000
        self.sub_ms = sub_s * 1000
        self.min_obs = min_obs
        self._trades: deque[Tick] = deque()
        self._books: deque[BookEvent] = deque()
        self._mids: deque[tuple[int, float]] = deque()
        self.vol_pct: float = 0.0  # 1-minute volatility (range as % of price), set from bars
        self.last_mid: float = 0.0
        self.last_book: BookEvent | None = None
        keys = list(INDICATORS) + [k + "_s" for k in DIRECTIONAL]
        self._hist = {k: _Ranked(history_obs) for k in keys}
        self.observations = 0

    # -- inputs ---------------------------------------------------------------

    def add_tick(self, t: Tick) -> None:
        self._trades.append(t)
        if self.last_book is None:
            self.last_mid = t.price
            self._mids.append((t.ts_ms, t.price))
        self._trim(t.ts_ms)

    def add_book(self, b: BookEvent) -> None:
        if not b.synced:
            return  # an unsynced book carries no usable depth
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
            if t.ts_ms > now_ms:
                continue
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
        vals = [m for t, m in self._mids if lo <= t <= now_ms]
        return max(vals) if vals else self.last_mid

    def recent_low_mid(self, now_ms: int, seconds: int) -> float:
        lo = now_ms - seconds * 1000
        vals = [m for t, m in self._mids if lo <= t <= now_ms]
        return min(vals) if vals else self.last_mid

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
        vals = [(b.ask_depth_near if side == "ask" else b.bid_depth_near) for b in self._books if lo <= b.ts_ms <= now_ms]
        return sum(vals) / len(vals) if vals else 0.0

    def _spread_median(self, now_ms: int, window_ms: int) -> float:
        lo = now_ms - window_ms
        vals = sorted(b.spread_pct for b in self._books if lo <= b.ts_ms <= now_ms)
        return vals[len(vals) // 2] if vals else 0.0

    def realized_vol_pct(self, now_ms: int, window_ms: int, step_ms: int = 5000) -> float:
        """Mean absolute mid change per ``step_ms``, as % of price, over the window."""
        moves = []
        t = now_ms - window_ms
        prev = self.mid_at(t)
        while t + step_ms <= now_ms:
            t += step_ms
            cur = self.mid_at(t)
            if prev and cur:
                moves.append(abs(cur / prev - 1.0) * 100.0)
            prev = cur or prev
        return sum(moves) / len(moves) if moves else 0.0

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
        signed_move = (self.last_mid - m0) / m0 * 100.0 if m0 else 0.0
        move_pct = abs(signed_move)
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
        # 6. order-flow acceleration: imbalance of the last quarter window vs the three before it
        q = max(self.sub_ms, self.window_ms // 4)
        acc_raw = self.imbalance(now_ms, q) - self.imbalance(now_ms - q, self.window_ms - q)
        # 7. volume-adjusted price response: signed move per unit of (log) volume
        vpr_raw = signed_move / math.log1p(vol60) if vol60 > 0 else 0.0
        # 8. spread expansion: current spread vs its window median
        spread_now = book_now.spread_pct if book_now else 0.0
        med = self._spread_median(now_ms, self.window_ms)
        sx_raw = (spread_now / med - 1.0) if med > 0 else 0.0
        # 9. short-term volatility change: last minute vs last ten minutes
        v_short = self.realized_vol_pct(now_ms, self.window_ms)
        v_long = self.realized_vol_pct(now_ms, 10 * self.window_ms)
        vx_raw = (v_short / v_long - 1.0) if v_long > 0 else 0.0
        raw_long = {"f": f_raw, "a": a_raw, "w": w_raw, "r": r_raw, "c": c_raw, "acc": acc_raw, "vpr": vpr_raw, "sx": sx_raw, "vx": vx_raw}
        raw_short = {"f": -f_raw, "a": a_raw, "w": ws_raw, "r": rs_raw, "c": -c_raw, "acc": -acc_raw, "vpr": -vpr_raw, "sx": sx_raw, "vx": vx_raw}
        obs = Observation(
            now_ms, self.last_mid, book_now.best_bid if book_now else self.last_mid, book_now.best_ask if book_now else self.last_mid,
            book_now.bid_depth_near if book_now else 0.0, book_now.ask_depth_near if book_now else 0.0,
            self.imbalance(now_ms), spread_now, self.vol_pct, vol60, raw_long, raw_short,
        )
        for k in INDICATORS:
            self._hist[k].add(raw_long[k])
        for k in DIRECTIONAL:
            self._hist[k + "_s"].add(raw_short[k])
        self.observations += 1
        return obs

    @property
    def warmed_up(self) -> bool:
        return self.observations >= self.min_obs

    def scores(self, obs: Observation, weights: dict[str, float], related_available: bool) -> tuple[Scores, Scores] | None:
        """(long, short) scores, or None while the coin has too little history. Never a placeholder."""
        if not self.warmed_up:
            return None
        long_d: dict[str, float] = {}
        short_d: dict[str, float] = {}
        for k in INDICATORS:
            long_d[k] = self._hist[k].rank(obs.raw_long[k])
            short_d[k] = self._hist[k + "_s"].rank(obs.raw_short[k]) if k in DIRECTIONAL else long_d[k]
        if not related_available:
            long_d["c"] = short_d["c"] = 50.0
        long_ = Scores(**long_d, prs=combine(long_d, weights))
        short = Scores(**short_d, prs=combine(short_d, weights))
        return long_, short
