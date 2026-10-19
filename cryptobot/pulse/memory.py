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
    target_pct: float = 0.0
    stop_pct: float = 0.0
    horizon_ms: int = 1_200_000
    policy_version: str = "legacy"
    exchange: str = ""
    expiry_gross_pct: float = 0.0


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
    expiry_gross_pct: float | None = None

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
    return MemoryRow(e["symbol"], int(e["ts_ms"]), e["direction"], e.get("regime") or "unknown", vec, first == "target", first == "stop", float(net), max(0, hold), float(e.get("mfe_pct") or 0.0),
                     float(e["target_pct"]), float(e["stop_pct"]), int(e.get("horizon_ms") or 1_200_000), e.get("policy_version") or "legacy", e.get("exchange") or "", float(e.get("last_move") or 0))


class MarketMemory:
    def __init__(self, store: Store, k: int = 50, min_n: int = 30, prior: float = 0.45, overlap_ms: int = 600_000, min_span_ms: int = 0) -> None:
        self.store = store
        self.k = k
        self.min_n = min_n
        self.min_span_ms = min_span_ms  # the picked evidence must cover at least this much market time
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
        """Use nonoverlapping market-time episodes, including correlated coins.

        The first setup represents the episode; later overlapping signals are
        retained for diagnostics and cannot become extra trials in the interval.
        """
        out: list[list[MemoryRow]] = []
        for r in sorted(rows, key=lambda r: (r.ts_ms, r.symbol)):
            if out and r.ts_ms < out[-1][0].ts_ms + max(self.overlap_ms, out[-1][0].horizon_ms):
                out[-1].append(r)
            else:
                out.append([r])
        return out

    def estimate(self, direction: str, vec: dict[str, float], regime: str = "unknown", *, k: int | None = None, min_n: int | None = None,
                 target_pct: float | None = None, stop_pct: float | None = None, horizon_ms: int | None = None, policy_version: str | None = None, exchange: str | None = None) -> Estimate:
        k = k or self.k
        min_n = self.min_n if min_n is None else min_n
        pool = [r for r in self.rows if r.direction == direction]
        pool = [r for r in pool if (target_pct is None or math.isclose(r.target_pct, target_pct, abs_tol=1e-8))
                and (stop_pct is None or math.isclose(r.stop_pct, stop_pct, abs_tol=1e-8))
                and (horizon_ms is None or r.horizon_ms == horizon_ms)
                and (policy_version is None or r.policy_version == policy_version)
                and (exchange is None or r.exchange == exchange)]
        same = [r for r in pool if r.regime == regime]
        use_same = regime != "unknown" and len(self._clusters(same)) >= min_n
        rows = same if use_same else pool
        if not rows:
            return Estimate(False, None, None, None, None, None, 0, 0, None, None, use_same, f"{INSUFFICIENT}: no resolved {direction} setups yet")
        dims = len(KEYS)
        means = [sum(r.vec[i] for r in rows) / len(rows) for i in range(dims)]
        stds = [max(1e-9, math.sqrt(sum((r.vec[i] - means[i]) ** 2 for r in rows) / len(rows))) for i in range(dims)]
        q = [float(vec.get(key, 0.0) or 0.0) for key in KEYS]
        # Form episodes before nearest-neighbour selection; omitted bridging rows
        # cannot split one observed move into several independent observations.
        distance = lambda r: sum(((r.vec[i] - q[i]) / stds[i]) ** 2 for i in range(dims))
        episodes = self._clusters(rows)
        clusters = sorted(episodes, key=lambda c: distance(c[0]))[:k]
        picked = [r for c in clusters for r in c]
        n_eff = len(clusters)
        if n_eff < min_n:
            return Estimate(False, None, None, None, None, None, len(picked), n_eff, None, None, use_same,
                            f"{INSUFFICIENT}: {n_eff} independent similar setups, need {min_n}", [(r.symbol, r.ts_ms) for r in picked[:10]])
        span = max(r.ts_ms for r in picked) - min(r.ts_ms for r in picked)
        if span < self.min_span_ms:
            return Estimate(False, None, None, None, None, None, len(picked), n_eff, None, None, use_same,
                            f"{INSUFFICIENT}: the {n_eff} similar setups span only {span / 3_600_000:.1f} h of market history, need {self.min_span_ms / 3_600_000:g} h", [(r.symbol, r.ts_ms) for r in picked[:10]])
        # each cluster contributes one observation: its mean outcome
        hits = sum(c[0].hit for c in clusters)
        stops = sum(c[0].stopped for c in clusters)
        net = sum(c[0].net_pct for c in clusters) / n_eff
        hold = sum(c[0].hold_ms for c in clusters) / n_eff
        mfe = sorted(c[0].mfe_pct for c in clusters)[n_eff // 2]
        p = hits / n_eff
        lo, hi = wilson(int(round(hits)), n_eff)
        note = f"p={p:.0%} [{lo:.0%}–{hi:.0%}] from {n_eff} independent setups ({len(picked)} raw){' in the same regime' if use_same else ''}"
        expired = [c[0].expiry_gross_pct for c in clusters if not c[0].hit and not c[0].stopped]
        return Estimate(True, p, stops / n_eff, net, int(hold), mfe, len(picked), n_eff, lo, hi, use_same, note, [(r.symbol, r.ts_ms) for r in picked[:10]], sum(expired) / len(expired) if expired else 0.0)

