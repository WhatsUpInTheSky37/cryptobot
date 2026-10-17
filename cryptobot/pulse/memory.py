"""Market Memory: similar past setups and how they resolved.

Similarity is a standardised nearest-neighbour search over the signal-time
indicator vector, restricted to the same direction and (when there is enough
of it) the same regime. Events on the same coin within ``overlap_ms`` of each
other are one observation, not several: they saw the same move. The estimate
reports the number of *independent* observations, a Wilson confidence
interval, and refuses to produce a probability below ``min_n``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from cryptobot.db.store import Store

KEYS = ("f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx", "vol_pct", "spread_pct", "imbalance")
RESOLVED = ("target", "stop", "expired")


@dataclass(slots=True)
class MemoryRow:
    symbol: str
    ts_ms: int
    direction: str
    regime: str
    vec: list[float]
    hit: bool  # target before stop
    stopped: bool
    net_pct: float
    hold_ms: int
    mfe_pct: float


@dataclass(slots=True)
class Estimate:
    sufficient: bool
    p_target: float | None
    p_stop: float | None
    expected_net_pct: float | None
    expected_hold_ms: int | None
    expected_mfe_pct: float | None
    n: int  # neighbours considered
    n_effective: int  # independent observations among them
    ci_low: float | None
    ci_high: float | None
    same_regime: bool
    note: str
    neighbours: list[tuple[str, int]] = field(default_factory=list)  # (symbol, ts_ms) for display

    def as_dict(self) -> dict[str, object]:
        return {
            "sufficient": self.sufficient, "p_target": self.p_target, "p_stop": self.p_stop, "expected_net_pct": self.expected_net_pct,
            "expected_hold_ms": self.expected_hold_ms, "expected_mfe_pct": self.expected_mfe_pct, "n": self.n, "n_effective": self.n_effective,
            "ci_low": self.ci_low, "ci_high": self.ci_high, "same_regime": self.same_regime, "note": self.note,
        }


INSUFFICIENT = "INSUFFICIENT HISTORICAL EVIDENCE"


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def row_from_event(e: dict) -> MemoryRow | None:
    if e.get("outcome") not in RESOLVED:
        return None
    vec = [float(e.get(k) or 0.0) for k in KEYS]
    first = e.get("first_hit") or ("target" if e["outcome"] == "target" else "stop" if e["outcome"] == "stop" else None)
    hold = int((e.get("first_hit_ms") or e.get("resolved_ts_ms") or e["ts_ms"]) - e["ts_ms"])
    net = e.get("net_pct")
    if net is None:  # legacy rows: reconstruct from the recorded target/stop/cost
        net = (e["target_pct"] if first == "target" else -e["stop_pct"] if first == "stop" else 0.0) - float(e.get("cost_pct") or 0.0)
    return MemoryRow(e["symbol"], int(e["ts_ms"]), e["direction"], e.get("regime") or "unknown", vec, first == "target", first == "stop", float(net), max(0, hold), float(e.get("mfe_pct") or 0.0))


class MarketMemory:
    def __init__(self, store: Store, k: int = 50, min_n: int = 30, prior: float = 0.45, overlap_ms: int = 600_000) -> None:
        self.store = store
        self.k = k
        self.min_n = min_n
        self.prior = prior  # reference only: shown as "what we assumed", never used to accept a trade
        self.overlap_ms = overlap_ms
        self.rows: list[MemoryRow] = []
        self.load()

    def load(self) -> None:
        self.rows = []
        for e in self.store.load_pulse_events(limit=100_000, resolved_only=True):
            r = row_from_event(e)
            if r is not None:
                self.rows.append(r)

    def add(self, row: MemoryRow) -> None:
        self.rows.append(row)

    # -- summaries ------------------------------------------------------------

    def resolved(self, direction: str | None = None) -> int:
        return sum(1 for r in self.rows if direction is None or r.direction == direction)

    def effective(self, direction: str | None = None) -> int:
        rows = sorted((r for r in self.rows if direction is None or r.direction == direction), key=lambda r: (r.symbol, r.ts_ms))
        return len(self._clusters(rows))

    def hit_rate(self, direction: str = "long") -> float:
        rows = [r for r in self.rows if r.direction == direction]
        return sum(r.hit for r in rows) / len(rows) if rows else 0.0

    # -- similarity -----------------------------------------------------------

    def _clusters(self, rows: list[MemoryRow]) -> list[list[MemoryRow]]:
        """Group rows on the same coin that overlap in time (sorted input not required)."""
        out: list[list[MemoryRow]] = []
        last: dict[str, list[MemoryRow]] = {}
        for r in sorted(rows, key=lambda r: (r.symbol, r.ts_ms)):
            c = last.get(r.symbol)
            if c is not None and r.ts_ms - c[-1].ts_ms < self.overlap_ms:
                c.append(r)
            else:
                c = [r]
                out.append(c)
                last[r.symbol] = c
        return out

    def estimate(self, direction: str, vec: dict[str, float], regime: str = "unknown", *, k: int | None = None, min_n: int | None = None) -> Estimate:
        k = k or self.k
        min_n = self.min_n if min_n is None else min_n
        pool = [r for r in self.rows if r.direction == direction]
        same = [r for r in pool if r.regime == regime]
        use_same = regime != "unknown" and len(self._clusters(same)) >= min_n
        rows = same if use_same else pool
        if not rows:
            return Estimate(False, None, None, None, None, None, 0, 0, None, None, use_same, f"{INSUFFICIENT}: no resolved {direction} setups yet")
        dims = len(KEYS)
        means = [sum(r.vec[i] for r in rows) / len(rows) for i in range(dims)]
        stds = [max(1e-9, math.sqrt(sum((r.vec[i] - means[i]) ** 2 for r in rows) / len(rows))) for i in range(dims)]
        q = [float(vec.get(key, 0.0) or 0.0) for key in KEYS]
        ranked = sorted(rows, key=lambda r: sum(((r.vec[i] - q[i]) / stds[i]) ** 2 for i in range(dims)))
        # walk outward collecting independent observations (clusters) until k of them
        picked: list[MemoryRow] = []
        for r in ranked:
            picked.append(r)
            if len(self._clusters(picked)) >= k:
                break
        clusters = self._clusters(picked)
        n_eff = len(clusters)
        if n_eff < min_n:
            return Estimate(False, None, None, None, None, None, len(picked), n_eff, None, None, use_same,
                            f"{INSUFFICIENT}: {n_eff} independent similar setups, need {min_n}", [(r.symbol, r.ts_ms) for r in picked[:10]])
        # each cluster contributes one observation: its mean outcome
        hits = sum(sum(r.hit for r in c) / len(c) for c in clusters)
        stops = sum(sum(r.stopped for r in c) / len(c) for c in clusters)
        net = sum(sum(r.net_pct for r in c) / len(c) for c in clusters) / n_eff
        hold = sum(sum(r.hold_ms for r in c) / len(c) for c in clusters) / n_eff
        mfe = sorted(sum(r.mfe_pct for r in c) / len(c) for c in clusters)[n_eff // 2]
        p = hits / n_eff
        lo, hi = wilson(int(round(hits)), n_eff)
        note = f"p={p:.0%} [{lo:.0%}–{hi:.0%}] from {n_eff} independent setups ({len(picked)} raw){' in the same regime' if use_same else ''}"
        return Estimate(True, p, stops / n_eff, net, int(hold), mfe, len(picked), n_eff, lo, hi, use_same, note, [(r.symbol, r.ts_ms) for r in picked[:10]])
