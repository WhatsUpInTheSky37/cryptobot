"""Phase 7: market state classification. Model labels, not facts about anyone's intentions."""
from __future__ import annotations

from typing import Any

from cryptobot.earlywarning.config import ScoreConfig


def classify(eas: float | None, comps: dict[str, Any] | None, changes: dict[int, float | None], *, imbalance: float | None, rel_volume: float | None, data_ok: bool, cfg: ScoreConfig) -> str:
    if not data_ok or eas is None or comps is None or comps.get("status") != "ok":
        return "NO DATA"
    c5, c15, c30, c60 = (changes.get(m) for m in (5, 15, 30, 60))
    c5, c15, c30, c60 = (0.0 if v is None else v for v in (c5, c15, c30, c60))
    rv = rel_volume or 0.0
    imb = imbalance or 0.0
    if c60 >= 5.0 and c5 <= -2.0:
        return "REVERSAL WARNING"
    if c60 >= 3.0 and rv >= 1.5 and imb < -0.2:
        return "POSSIBLE DISTRIBUTION"
    if c15 >= 5.0 or c5 >= 3.0:
        return "SURGE ACTIVE"
    if c5 >= 1.0 and rv >= 1.5 and imb > 0:
        return "BREAKOUT DEVELOPING"
    if eas >= cfg.confirmed_eas and abs(c15) < 1.5:
        return "ACCUMULATION CONFIRMED"
    if eas >= cfg.possible_eas:
        return "POSSIBLE ACCUMULATION"
    return "QUIET"
