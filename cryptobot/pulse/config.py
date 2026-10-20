"""Crypto Pulse configuration: scanner, cost model, three paper modes, risk.

Everything here is a plain number the user can change. Weights are deliberately
hand-set and are never optimised against the whole history (that would be
fitting the past). The ML module (phase 8) is the only place a model is
fitted, and it is validated walk-forward before it may influence a mode.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

# Indicator keys. Long and short sides are ranked separately for the
# directional ones (f, w, r, c, acc, vpr); a, sx and vx are symmetric.
INDICATORS: tuple[str, ...] = ("f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx")
INDICATOR_NAMES: dict[str, str] = {
    "f": "Pressure",
    "a": "Absorption",
    "w": "Withdrawal",
    "r": "Replenishment weakening",
    "c": "Cross-market confirmation",
    "acc": "Order-flow acceleration",
    "vpr": "Volume-adjusted price response",
    "sx": "Spread expansion",
    "vx": "Volatility change",
}

# Default weights. A negative weight means "more of this is worse": the score
# used is (100 - rank). PRS = sum(|w| * s) / sum(|w|), so it stays in 0..100.
DEFAULT_WEIGHTS: dict[str, float] = {
    "f": 0.20, "a": 0.20, "w": 0.15, "r": 0.10, "c": 0.10,
    "acc": 0.10, "vpr": 0.05, "sx": -0.05, "vx": 0.05,
}

OUTCOME_HORIZONS_MS: dict[str, int] = {
    "30s": 30_000, "1m": 60_000, "2m": 120_000, "5m": 300_000, "10m": 600_000, "20m": 1_200_000,
}


class HealthConfig(BaseModel):
    stale_trade_s: int = 240  # no public trade for this long -> stale (Binance.US prints well under one trade a minute on quiet pairs)
    stale_book_s: int = 30  # no order-book update for this long -> stale
    activity_window_s: int = 300
    min_trades_per_min: float = 0.5  # below this the market is "inactive" and excluded
    inactive_cooldown_s: int = 300  # stays excluded at least this long
    require_book: bool = True


class CostConfig(BaseModel):
    fee_rate_override: float | None = None  # per side, as a fraction (0.001 = 0.1%); None = exchange's taker fee
    base_slippage_pct: float = 0.02  # per side, on top of the spread and depth impact
    impact_band_pct: float = 0.10  # depth within this band of the mid is "near"
    hold_minutes_for_move: float = 10.0  # horizon the expected move is scaled to
    target_capture: float = 0.6  # target = this share of the expected move (never inflated to cover fees)
    max_target_pct: float = 1.5


class ModeConfig(BaseModel):
    key: str
    label: str
    budget: float = 100.0
    prepare_prs: float = 70.0
    enter_prs: float = 85.0
    min_acc: float = 0.0  # order-flow acceleration score required (momentum)
    require_breakout: bool = True  # price must clear the absorption high
    min_ev_pct: float = 0.02  # net expected value per trade after all costs
    min_rr: float = 1.0  # reward-to-risk after costs
    min_similar: int = 30  # independent similar past events required
    use_ml: bool = False
    require_evidence: bool = True  # False: a research book that trades every signal above its thresholds at real costs, with no history or expected-value gate
    max_spread_pct: float = 0.10
    max_vol_pct: float = 1.5  # 1-minute range above this is "excessive volatility"
    allowed_regimes: list[str] = Field(default_factory=lambda: ["trend_up", "range", "quiet", "high_vol", "trend_down", "unknown"])


def default_modes() -> list[ModeConfig]:
    return [
        ModeConfig(key="A", label="Conservative", enter_prs=85, min_acc=60, require_breakout=True, min_ev_pct=0.05, min_rr=1.5, min_similar=50,
                   max_spread_pct=0.08, max_vol_pct=1.0, allowed_regimes=["trend_up", "range", "quiet"]),
        ModeConfig(key="B", label="Balanced", enter_prs=75, min_acc=70, require_breakout=False, min_ev_pct=0.02, min_rr=1.0, min_similar=30,
                   max_spread_pct=0.10, max_vol_pct=1.5),
        ModeConfig(key="C", label="Experimental", enter_prs=65, min_acc=0, require_breakout=False, min_ev_pct=0.0, min_rr=0.8, min_similar=15, use_ml=True,
                   require_evidence=False, max_spread_pct=0.12, max_vol_pct=2.0),
    ]


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 0.25  # of current equity, distance to the stop
    daily_loss_pct: float = 1.0  # realized + unrealized today, of start-of-day equity
    max_positions: int = 2
    max_open_risk_pct: float = 0.5  # sum of (entry - stop) exposure across open positions
    max_consecutive_losses: int = 4
    pause_minutes: int = 60
    per_position_pct: float = 50.0  # of budget, exchange minimums make smaller caps unusable at $100


class PulseConfig(BaseModel):
    wallet_quote: str = ""
    # observation
    window_s: int = 60
    obs_interval_s: int = 5
    history_obs: int = 1440  # 2 hours of observations for the rolling distributions
    min_obs: int = 60  # 5 minutes before any score is shown
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # scanner thresholds
    monitor_prs: float = 50.0
    prepare_prs: float = 70.0
    enter_prs: float = 85.0  # kept for the legacy status line; modes have their own
    prepared_ttl_s: int = 120
    event_cooldown_s: int = 300
    # exits
    adaptive: bool = True
    stop_vol_mult: float = 1.0
    min_stop_pct: float = 0.15
    max_stop_pct: float = 0.60
    target_pct: float = 0.40  # only when adaptive is off
    stop_pct: float = 0.20
    max_hold_s: int = 1200
    max_spread_pct: float = 0.10
    stop_spread_mult: float = 2.0  # the stop must sit at least this many spreads below the fill: a stop inside the spread is hit by the next print
    # evidence
    ev_margin_pct: float = 0.02
    prior_p: float = 0.45  # shown for reference only; never used to accept a trade
    min_similar: int = 30
    k_similar: int = 50
    min_evidence_span_h: float = 24.0  # the similar setups must span at least this much market time: 30 setups from one afternoon are one afternoon
    conservative_p: bool = True  # expected value uses the LOWER bound of the probability's confidence interval, not the point estimate
    overlap_ms: int = 600_000  # events on the same coin closer than this are one observation
    min_target_cost_mult: float = 1.2
    research_mode: bool = True  # record and track every setup (always on; kept for config compatibility)
    # legacy single-budget fields (the Pulse screen's summary line)
    budget: float = 100.0
    risk_per_trade_pct: float = 0.25
    max_positions: int = 2
    daily_loss_pct: float = 1.0
    max_consecutive_losses: int = 4
    pause_minutes: int = 60
    per_position_pct: float = 50.0
    # new sections
    health: HealthConfig = Field(default_factory=HealthConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    modes: list[ModeConfig] = Field(default_factory=default_modes)
    ml_min_events: int = 300
    ml_retrain_minutes: int = 240

    @model_validator(mode="before")
    @classmethod
    def migrate_risk_fields(cls, values):
        values = dict(values or {})
        risk = values.get("risk")
        if isinstance(risk, RiskConfig):
            risk = risk.model_dump()
        if isinstance(risk, dict):
            for key in ("risk_per_trade_pct", "daily_loss_pct", "max_positions", "max_consecutive_losses", "pause_minutes", "per_position_pct"):
                if key in risk:
                    values[key] = risk[key]
        return values

    def model_post_init(self, _ctx) -> None:  # noqa: D401
        # keep the legacy risk fields and the risk section in step
        self.risk = self.risk.model_copy(update={
            "risk_per_trade_pct": self.risk_per_trade_pct, "daily_loss_pct": self.daily_loss_pct, "max_positions": self.max_positions,
            "max_consecutive_losses": self.max_consecutive_losses, "pause_minutes": self.pause_minutes, "per_position_pct": self.per_position_pct,
            "max_open_risk_pct": max(self.risk.max_open_risk_pct, self.risk_per_trade_pct),  # the aggregate cap can never be below one trade's risk
        })
        for m in self.modes:
            if "budget" not in m.model_fields_set:
                m.budget = self.budget
            if m.key == "C" and "require_evidence" not in m.model_fields_set:
                m.require_evidence = False  # configs saved before 0.6.1: Experimental becomes the evidence-free research book

    def weights_normalized(self) -> dict[str, float]:
        w = {k: float(self.weights.get(k, 0.0)) for k in INDICATORS}
        tot = sum(abs(v) for v in w.values()) or 1.0
        return {k: v / tot for k, v in w.items()}


def break_even_win_rate(target_pct: float, stop_pct: float, cost_pct: float) -> float:
    """p at which EV = p·G − (1−p)·L − C is zero."""
    return (stop_pct + cost_pct) / (target_pct + stop_pct) if (target_pct + stop_pct) > 0 else 1.0


def expected_value_pct(p: float, target_pct: float, stop_pct: float, cost_pct: float) -> float:
    return p * target_pct - (1 - p) * stop_pct - cost_pct

