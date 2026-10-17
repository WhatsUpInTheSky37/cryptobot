from __future__ import annotations

from pydantic import BaseModel


class PulseConfig(BaseModel):
    # observation
    window_s: int = 60  # pressure window
    obs_interval_s: int = 5  # one observation every 5 s
    history_obs: int = 1440  # 2 hours of observations for "how unusual is this" ranking
    min_obs: int = 60  # 5 minutes of history before scores mean anything
    # scoring thresholds (Pressure Release Score, 0..100)
    monitor_prs: float = 50.0
    prepare_prs: float = 70.0
    enter_prs: float = 85.0
    prepared_ttl_s: int = 120
    event_cooldown_s: int = 300
    # exits
    target_pct: float = 0.40
    stop_pct: float = 0.20
    adaptive: bool = True  # stop = stop_vol_mult × 1-minute volatility, clamped; target = 2 × stop
    stop_vol_mult: float = 1.0
    min_stop_pct: float = 0.15
    max_stop_pct: float = 0.60
    max_hold_s: int = 1200
    max_spread_pct: float = 0.10
    # expected value gate
    ev_margin_pct: float = 0.02
    prior_p: float = 0.45
    min_similar: int = 30
    k_similar: int = 50
    research_mode: bool = True  # paper-trade every confirmed setup so the memory fills with real after-cost results
    min_target_cost_mult: float = 1.2  # never trade when the target cannot cover round-trip costs with 20% to spare
    # risk (section 6 of the proposal)
    budget: float = 100.0  # paper budget for Pulse
    risk_per_trade_pct: float = 0.25
    max_positions: int = 2
    daily_loss_pct: float = 1.0
    max_consecutive_losses: int = 4
    pause_minutes: int = 60
    per_position_pct: float = 25.0

    def weights(self) -> dict[str, float]:
        return {"f": 0.25, "a": 0.25, "w": 0.20, "r": 0.15, "c": 0.15}


def break_even_win_rate(target_pct: float, stop_pct: float, cost_pct: float) -> float:
    """p at which EV = p·G − (1−p)·L − C is zero."""
    return (stop_pct + cost_pct) / (target_pct + stop_pct) if (target_pct + stop_pct) > 0 else 1.0


def expected_value_pct(p: float, target_pct: float, stop_pct: float, cost_pct: float) -> float:
    return p * target_pct - (1 - p) * stop_pct - cost_pct
