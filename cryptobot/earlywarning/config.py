"""Early Warning configuration. Every number is a starting point, not a validated optimum."""
from __future__ import annotations

from pydantic import BaseModel, Field

SEED_SYMBOLS = ["MXC/USD", "POND/USD", "FLUX/USD", "COMP/USD"]
LOOKBACKS_MIN: tuple[int, ...] = (1440, 720, 360, 180, 60, 30, 15, 5, 1)
HORIZONS_MIN: tuple[int, ...] = (5, 15, 30, 60)
STATES = ("NO DATA", "QUIET", "POSSIBLE ACCUMULATION", "ACCUMULATION CONFIRMED", "BREAKOUT DEVELOPING", "SURGE ACTIVE", "POSSIBLE DISTRIBUTION", "REVERSAL WARNING")
COMPONENTS = ("rel_volume", "imbalance", "freq_accel", "compression", "absorption", "persistence")
COMPONENT_NAMES = {
    "rel_volume": "Relative volume anomaly",
    "imbalance": "Aggressive buying imbalance",
    "freq_accel": "Trade frequency acceleration",
    "compression": "Price compression",
    "absorption": "Liquidity absorption",
    "persistence": "Accumulation persistence",
}
DEFAULT_WEIGHTS = {"rel_volume": 0.20, "imbalance": 0.25, "freq_accel": 0.15, "compression": 0.10, "absorption": 0.15, "persistence": 0.15}


class SurgeThreshold(BaseModel):
    pct: float
    minutes: int


class SurgeConfig(BaseModel):
    thresholds: list[SurgeThreshold] = Field(default_factory=lambda: [SurgeThreshold(pct=5, minutes=5), SurgeThreshold(pct=10, minutes=15), SurgeThreshold(pct=15, minutes=30), SurgeThreshold(pct=20, minutes=60)])
    reversal_retrace: float = 0.5  # the surge is over when price gives back this share of the gain
    post_peak_minutes: int = 120  # how far after the peak to look for the decline
    max_peak_search_minutes: int = 240
    confirm_on_close: bool = True  # a candle must CLOSE above the threshold, not merely wick through it
    vol_multiple: float = 0.0  # >0: each threshold becomes max(pct, vol_multiple x the trailing 60-minute range %), so a 4-cent coin and BTC are judged against their own noise
    tradeable_quote_volume: float = 2_000.0  # an event is "tradeable" only when this much quote volume traded between its start and the threshold crossing ...
    tradeable_min_trades: int = 20  # ... across at least this many trades; otherwise it was a few prints crossing a wide spread
    study_tradeable_only: bool = True  # pattern study, lead times, targets and methods use tradeable events only


class CostConfig(BaseModel):
    fee_rate: float = 0.001  # per side (Binance.US standard taker)
    base_slippage_pct: float = 0.05  # per side
    spread_estimate_pct: float = 0.20  # used when no bid is observable (thin alt pairs)
    impact_share: float = 0.05  # our notional as a share of the minute's quote volume before impact is assumed


class ScoreConfig(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    min_history_obs: int = 120  # observations per symbol before any score is shown
    baseline_minutes: int = 7 * 1440
    possible_eas: float = 60.0
    confirmed_eas: float = 75.0
    large_trade_pct: float = 99.0  # percentile of baseline trade size that counts as "large"


class TargetConfig(BaseModel):
    up_pct: float = 3.0  # Model A: reach +up_pct before -down_pct
    down_pct: float = 2.0
    target_quantile: float = 0.25  # Model B: conservative target = this quantile of MFE
    min_setups: int = 40  # out-of-sample setups per horizon before a target is "reliable"
    min_coverage: float = 0.70  # share of OOS setups whose MFE reached the predicted target
    setup_eas: float = 60.0  # EAS at which a window counts as a setup


class RiskConfig(BaseModel):
    budget: float = 100.0
    risk_per_trade_pct: float = 0.5
    daily_loss_pct: float = 2.0
    max_positions: int = 3
    max_consecutive_losses: int = 4
    pause_minutes: int = 120
    max_position_share_of_minute_volume: float = 0.10  # liquidity: never more than this share of recent 1-minute quote volume
    max_spread_pct: float = 0.60
    min_quote_volume_24h: float = 100_000.0


class ScannerConfig(BaseModel):
    quotes: list[str] = Field(default_factory=lambda: ["USD", "USDT"])
    min_quote_volume_24h: float = 100_000.0
    max_spread_pct: float = 0.60
    top_n: int = 8
    rescan_s: int = 300
    stale_s: int = 120


class ExitConfig(BaseModel):
    method: str = "partial"  # fixed | volatility | percentile | partial | trailing | time
    fixed_target_pct: float = 4.0
    stop_pct: float = 2.5
    trailing_pct: float = 2.0
    max_hold_min: int = 240
    partial_shares: list[float] = Field(default_factory=lambda: [0.5, 0.25, 0.25])  # experimental: 50% at T1, 25% at T2, 25% trailing
    t2_multiple: float = 2.0  # second target = this × first target


class EWConfig(BaseModel):
    seed_symbols: list[str] = Field(default_factory=lambda: list(SEED_SYMBOLS))
    days: int = 90
    lookbacks_min: list[int] = Field(default_factory=lambda: list(LOOKBACKS_MIN))
    horizons_min: list[int] = Field(default_factory=lambda: list(HORIZONS_MIN))
    surge: SurgeConfig = Field(default_factory=SurgeConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    targets: TargetConfig = Field(default_factory=TargetConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    entry_method: str = "A"  # A early accumulation | B confirmed breakout | C breakout + retest
    control_windows_per_event: int = 5
    train_share: float = 0.6  # chronological split for out-of-sample comparisons
    universe_top: int = 0  # >0: research the N most traded Binance.US spot pairs (by 24h quote volume) instead of the seed list

    def weights_normalized(self) -> dict[str, float]:
        tot = sum(abs(v) for v in self.score.weights.values()) or 1.0
        return {k: self.score.weights.get(k, 0.0) / tot for k in COMPONENTS}
