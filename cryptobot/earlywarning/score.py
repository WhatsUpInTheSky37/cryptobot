"""Phase 5: the Early Accumulation Score (EAS), 0-100.

Each component is the percentile rank of a feature against the same coin's own
history of that feature (mid-rank for ties). Without ``min_obs`` observations
there is no score: ``score`` returns None and the status INSUFFICIENT DATA.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from typing import Any

from cryptobot.earlywarning.config import COMPONENTS

INSUFFICIENT = "INSUFFICIENT DATA"

# feature -> component (the direction is "more is more accumulation-like")
FEATURE_FOR = {"rel_volume": "rel_volume", "imbalance": "imbalance", "freq_change": "freq_accel", "compression": "compression", "absorption": "absorption", "persistence": "persistence"}


def components_from_features(f: dict[str, Any] | None) -> dict[str, float] | None:
    if not f:
        return None
    out: dict[str, float] = {}
    for feat, comp in FEATURE_FOR.items():
        v = f.get(feat)
        if v is None:
            return None  # a missing input means no score, never a neutral stand-in
        out[comp] = float(v)
    return out


def percentile(sorted_vals: list[float], x: float) -> float:
    if not sorted_vals:
        return 50.0
    lo, hi = bisect_left(sorted_vals, x), bisect_right(sorted_vals, x)
    return 100.0 * (lo + 0.5 * (hi - lo)) / len(sorted_vals)


class _Hist:
    def __init__(self, maxlen: int) -> None:
        self.q: deque[float] = deque(maxlen=maxlen)
        self.s: list[float] = []

    def add(self, x: float) -> None:
        if len(self.q) == self.q.maxlen:
            old = self.q[0]
            i = bisect_left(self.s, old)
            if i < len(self.s) and self.s[i] == old:
                self.s.pop(i)
        self.q.append(x)
        insort(self.s, x)

    def __len__(self) -> int:
        return len(self.q)


class EASModel:
    """Per-symbol rolling distributions and the weighted score."""

    def __init__(self, weights: dict[str, float], min_obs: int = 120, maxlen: int = 20_000) -> None:
        tot = sum(abs(v) for v in weights.values()) or 1.0
        self.weights = {k: weights.get(k, 0.0) / tot for k in COMPONENTS}
        self.min_obs = min_obs
        self.maxlen = maxlen
        self._hist: dict[str, dict[str, _Hist]] = {}

    def _h(self, symbol: str) -> dict[str, _Hist]:
        if symbol not in self._hist:
            self._hist[symbol] = {c: _Hist(self.maxlen) for c in COMPONENTS}
        return self._hist[symbol]

    def observations(self, symbol: str) -> int:
        h = self._hist.get(symbol)
        return min(len(x) for x in h.values()) if h else 0

    def add(self, symbol: str, comps: dict[str, float]) -> None:
        h = self._h(symbol)
        for c in COMPONENTS:
            h[c].add(float(comps[c]))

    def score(self, symbol: str, comps: dict[str, float] | None) -> tuple[float | None, dict[str, Any]]:
        """(EAS or None, {component: rank} or {'status': INSUFFICIENT DATA, ...})."""
        if comps is None:
            return None, {"status": INSUFFICIENT, "detail": "a required feature is missing"}
        n = self.observations(symbol)
        if n < self.min_obs:
            return None, {"status": INSUFFICIENT, "detail": f"{n}/{self.min_obs} observations for {symbol}"}
        h = self._h(symbol)
        ranks = {c: percentile(h[c].s, float(comps[c])) for c in COMPONENTS}
        eas = sum(self.weights[c] * ranks[c] for c in COMPONENTS)
        return float(eas), {**ranks, "status": "ok"}

    def seed(self, symbol: str, rows: list[dict[str, float]]) -> None:
        for r in rows:
            if all(k in r and r[k] is not None for k in COMPONENTS):
                self.add(symbol, r)
