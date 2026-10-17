"""Strategy interface shared by the backtester and the live engine.

A strategy maps a bar series to a *target exposure* in [0, 1]: the fraction of
its allowed allocation it wants invested after each bar closes. 0 = all cash,
1 = fully invested. NaN marks the warm-up period. The risk manager, not the
strategy, decides how many dollars that is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import numpy as np

from cryptobot.core.models import Bar

Kind = Literal["trend", "reversion", "schedule"]


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    default: float
    min: float
    max: float
    step: float
    tooltip: str
    integer: bool = True
    suffix: str = ""


@dataclass
class BarArrays:
    ts_ms: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    timeframe_s: int
    symbol: str = ""

    @classmethod
    def from_bars(cls, bars: list[Bar]) -> "BarArrays":
        if not bars:
            raise ValueError("no bars")
        return cls(
            np.array([b.ts_ms for b in bars], dtype=np.int64),
            np.array([b.open for b in bars], dtype=float),
            np.array([b.high for b in bars], dtype=float),
            np.array([b.low for b in bars], dtype=float),
            np.array([b.close for b in bars], dtype=float),
            np.array([b.volume for b in bars], dtype=float),
            bars[0].timeframe_s,
            bars[0].symbol,
        )

    def __len__(self) -> int:
        return len(self.close)

    def head(self, n: int) -> "BarArrays":
        return BarArrays(
            self.ts_ms[:n], self.open[:n], self.high[:n], self.low[:n], self.close[:n], self.volume[:n],
            self.timeframe_s, self.symbol,
        )

    @property
    def periods_per_year(self) -> float:
        return 365.0 * 86400.0 / self.timeframe_s


class Strategy:
    """Subclass and implement ``compute``. Keep it causal."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    kind: ClassVar[Kind] = "trend"
    description: ClassVar[str] = ""
    params: ClassVar[tuple[Param, ...]] = ()
    # Small, explicit grid used ONLY by walk-forward validation to measure how
    # fragile the parameters are. It is never used to pick live parameters.
    grid: ClassVar[tuple[dict[str, float], ...]] = ()

    @classmethod
    def defaults(cls) -> dict[str, float]:
        return {p.name: p.default for p in cls.params}

    @classmethod
    def merge(cls, params: dict[str, Any] | None) -> dict[str, float]:
        out = cls.defaults()
        for k, v in (params or {}).items():
            if k in out:
                out[k] = v
        for p in cls.params:
            if p.integer:
                out[p.name] = int(round(out[p.name]))
        return out

    def compute(self, bars: BarArrays, params: dict[str, float]) -> np.ndarray:  # pragma: no cover - abstract
        raise NotImplementedError


def warmup_index(exposure: np.ndarray) -> int:
    """First bar at which the strategy has an opinion."""
    idx = np.flatnonzero(~np.isnan(exposure))
    return int(idx[0]) if len(idx) else len(exposure)
