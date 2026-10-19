"""User-facing strategy configuration (saved in settings, reused live in Milestone 3)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

    model_config = ConfigDict(allow_inf_nan=False)
    stop_atr_mult: float = Field(default=2.0, ge=0, le=100)
    atr_n: int = Field(default=14, ge=2, le=500)
    trailing_pct: float = Field(default=0.0, ge=0, lt=100)
    take_profit_pct: float = Field(default=0.0, ge=0)
    rebalance_threshold_pct: float = Field(default=5.0, ge=0, le=100)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    strategies: list[StrategySpec] = Field(default_factory=lambda: [StrategySpec()])
    timeframe_s: int = 86400
    regime_gate: bool = True
    adx_threshold: float = 25.0
    adx_n: int = 14
    vol_target: bool = True
    target_annual_vol: float = 0.40
    vol_window: int = 20
    risk: RiskRules = Field(default_factory=RiskRules)

    @model_validator(mode="after")
    def valid_parameters(self):
        if not self.strategies or self.timeframe_s not in (60, 300, 900, 3600, 14400, 86400):
            raise ValueError("choose a strategy and supported timeframe")
        if len(self.strategies) > 1 and any(spec.id == "dca" for spec in self.strategies):
            raise ValueError("DCA requires its own scheduled book")
        if self.vol_window < 2 or self.adx_n < 2 or self.target_annual_vol <= 0:
            raise ValueError("invalid volatility or ADX settings")
        for spec in self.strategies:
            params = spec.full_params()
            for p in STRATEGIES[spec.id].params:
                if not p.min <= params[p.name] <= p.max:
                    raise ValueError(f"{spec.id}.{p.name} outside supported range")
            if "fast" in params and params["fast"] >= params["slow"]:
                raise ValueError("fast length must be below slow length")
        return self

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

