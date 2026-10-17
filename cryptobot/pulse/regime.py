"""Market regime from 1-minute bars: trend_up | trend_down | range | high_vol | quiet | unknown."""
from __future__ import annotations

from dataclasses import dataclass

from cryptobot.core.models import Bar

REGIMES = ("trend_up", "trend_down", "range", "high_vol", "quiet", "unknown")


@dataclass(frozen=True)
class Regime:
    name: str
    trend_pct: float  # fast EMA vs slow EMA, in %
    vol_ratio: float  # recent range vs longer-run range
    bars: int


def _ema(vals: list[float], n: int) -> float:
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def classify(bars: list[Bar], *, min_bars: int = 20, trend_pct: float = 0.15, high_vol_ratio: float = 1.6, quiet_ratio: float = 0.5) -> Regime:
    closed = [b for b in bars if b.closed and b.close > 0]
    if len(closed) < min_bars:
        return Regime("unknown", 0.0, 1.0, len(closed))
    closes = [b.close for b in closed]
    ranges = [(b.high - b.low) / b.close * 100.0 for b in closed]
    fast, slow = _ema(closes[-30:], 10), _ema(closes[-60:], 30)
    trend = (fast / slow - 1.0) * 100.0 if slow else 0.0
    recent = sum(ranges[-10:]) / len(ranges[-10:])
    longer = sorted(ranges[-60:])
    med = longer[len(longer) // 2] if longer else 0.0
    ratio = recent / med if med > 0 else 1.0
    if ratio >= high_vol_ratio:
        return Regime("high_vol", trend, ratio, len(closed))
    if ratio <= quiet_ratio:
        return Regime("quiet", trend, ratio, len(closed))
    if trend >= trend_pct:
        return Regime("trend_up", trend, ratio, len(closed))
    if trend <= -trend_pct:
        return Regime("trend_down", trend, ratio, len(closed))
    return Regime("range", trend, ratio, len(closed))
