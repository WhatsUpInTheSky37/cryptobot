"""Market Memory: similar past setups and how they resolved."""
from __future__ import annotations

import math

from cryptobot.db.store import Store

KEYS = ("f", "a", "w", "r", "c", "vol_pct")


class MarketMemory:
    def __init__(self, store: Store, k: int = 50, min_n: int = 30, prior: float = 0.45) -> None:
        self.store = store
        self.k = k
        self.min_n = min_n
        self.prior = prior
        self._rows: list[tuple[str, list[float], bool]] = []  # direction, vector, hit_target
        self.load()

    def load(self) -> None:
        self._rows = []
        for e in self.store.load_pulse_events(limit=50_000, resolved_only=True):
            if e["outcome"] in ("target", "stop", "expired"):
                self._rows.append((e["direction"], [float(e[k]) for k in KEYS], e["outcome"] == "target"))

    def add(self, direction: str, vec: dict[str, float], hit_target: bool) -> None:
        self._rows.append((direction, [float(vec[k]) for k in KEYS], hit_target))

    def resolved(self, direction: str | None = None) -> int:
        return sum(1 for d, _v, _h in self._rows if direction is None or d == direction)

    def hit_rate(self, direction: str = "long") -> float:
        rows = [h for d, _v, h in self._rows if d == direction]
        return sum(rows) / len(rows) if rows else 0.0

    def estimate(self, direction: str, vec: dict[str, float]) -> tuple[float, int]:
        """(estimated p of reaching the target first, number of similar resolved events)."""
        rows = [(v, h) for d, v, h in self._rows if d == direction]
        if len(rows) < self.min_n:
            return self.prior, len(rows)
        dims = len(KEYS)
        means = [sum(v[i] for v, _ in rows) / len(rows) for i in range(dims)]
        stds = [max(1e-9, math.sqrt(sum((v[i] - means[i]) ** 2 for v, _ in rows) / len(rows))) for i in range(dims)]
        q = [float(vec[k]) for k in KEYS]
        scored = sorted(rows, key=lambda vh: sum(((vh[0][i] - q[i]) / stds[i]) ** 2 for i in range(dims)))[: self.k]
        hits = sum(1 for _v, h in scored if h)
        return (hits + 1) / (len(scored) + 2), len(scored)
