"""User-facing strategy configuration (saved in settings, reused live in Milestone 3)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from cryptobot.strategy.library import STRATEGIES


class StrategySpec(BaseModel):
    id: str = "ema_cross"
    params: dict[str, float] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in STRATEGIES:
            raise ValueError(f"unknown strategy {v}")
        return v

    def full_params(self) -> dict[str, float]:
        return STRATEGIES[self.id].merge(self.params)


class RiskRules(BaseModel):
    """Per-position protection applied identically in backtests, TEST and BUY."""

    stop_atr_mult: float = 2.0  # 0 disables
    atr_n: int = 14
    trailing_pct: float = 0.0  # 0 disables; e.g. 10 = trail 10% below the highest high since entry
    take_profit_pct: float = 0.0  # 0 disables
    rebalance_threshold_pct: float = 5.0  # ignore exposure changes smaller than this % of budget


class StrategyConfig(BaseModel):
    strategies: list[StrategySpec] = Field(default_factory=lambda: [StrategySpec()])
    timeframe_s: int = 86400
    regime_gate: bool = True
    adx_threshold: float = 25.0
    adx_n: int = 14
    vol_target: bool = True
    target_annual_vol: float = 0.40
    vol_window: int = 20
    risk: RiskRules = Field(default_factory=RiskRules)

    def effective_risk(self) -> RiskRules:
        """A pure schedule (DCA) has no exits by definition, so stops do not apply to it."""
        if all(STRATEGIES[s.id].kind == "schedule" for s in self.strategies):
            return self.risk.model_copy(update={"stop_atr_mult": 0.0, "trailing_pct": 0.0, "take_profit_pct": 0.0})
        return self.risk

    def describe(self) -> str:
        parts = [STRATEGIES[s.id].name for s in self.strategies]
        mods = []
        if self.regime_gate:
            mods.append(f"ADX gate {self.adx_threshold:g}")
        if self.vol_target:
            mods.append(f"vol target {self.target_annual_vol:.0%}")
        return " + ".join(parts) + (f" [{', '.join(mods)}]" if mods else "")

    def to_json(self) -> dict[str, Any]:
        return self.model_dump()
