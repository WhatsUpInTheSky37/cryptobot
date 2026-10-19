"""The single choke point for money.

Every order, simulated or real, must carry an ``Approval`` issued here, and an
executor consumes the approval exactly once. There is no other way to build an
order. Spending limits are enforced identically in TEST and BUY modes.
"""
from __future__ import annotations

import math
import uuid
from decimal import Decimal, ROUND_FLOOR
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from cryptobot.strategy.config import RiskRules


class RiskLimits(BaseModel):
    """User-facing spending limits (Risk & Budget screen)."""

    model_config = ConfigDict(allow_inf_nan=False)
    budget: float = Field(default=100.0, gt=0)
    per_position_pct: float = Field(default=25.0, gt=0, le=100)
    daily_loss_pct: float = Field(default=5.0, gt=0, le=100)
    max_drawdown_pct: float = Field(default=20.0, gt=0, le=100)
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=100)
    max_open_risk_pct: float = Field(default=3.0, gt=0, le=100)
    timezone: str = "America/New_York"  # the "day" for the daily limit


@dataclass(frozen=True)
class MarketInfo:
    symbol: str
    taker_fee: float = 0.006
    maker_fee: float = 0.004
    min_amount: float = 0.0
    min_cost: float = 0.0
    amount_step: float = 0.0
    price_step: float = 0.0


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # buy | sell
    amount: float
    price: float  # current market price used for sizing
    reason: str
    stop_price: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class Approval:
    intent: OrderIntent
    amount: float  # possibly reduced
    fee_rate: float
    token: str
    issued_ms: int

    @property
    def notional(self) -> float:
        return self.amount * self.intent.price


@dataclass(frozen=True)
class Rejection:
    intent: OrderIntent
    reason: str


@dataclass
class RiskContext:
    cash: float
    in_use_total: float  # cost basis of all open positions, fees included
    position_basis: float  # cost basis of the position in this symbol
    position_units: float
    halted: bool
    info: MarketInfo
    now_ms: int
    open_risk: float = 0.0


def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return float((Decimal(str(x)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(step)))


class RiskManager:
    def __init__(self, limits: RiskLimits, rules: RiskRules, slippage_pct: float = 0.05) -> None:
        self.limits = limits
        self.rules = rules
        self.slippage_pct = slippage_pct  # sizing assumes fills this much worse than the quoted price
        self._issued: dict[str, Approval] = {}
        self.approved = 0
        self.rejected = 0

    def approve(self, intent: OrderIntent, ctx: RiskContext) -> Approval | Rejection:
        self._issued = {k: a for k, a in self._issued.items() if 0 <= ctx.now_ms - a.issued_ms <= 30_000}
        values = (intent.amount, intent.price, ctx.cash, ctx.in_use_total, ctx.position_basis,
                  ctx.position_units, ctx.open_risk, self.slippage_pct,
                  ctx.info.taker_fee, ctx.info.amount_step, ctx.info.min_amount, ctx.info.min_cost)
        if not all(math.isfinite(v) for v in values) or intent.amount <= 0 or intent.price <= 0:
            return self._reject(intent, "amount, price, balances and market filters must be finite and valid")
        if min(ctx.info.taker_fee, ctx.info.amount_step, ctx.info.min_amount, ctx.info.min_cost) < 0:
            return self._reject(intent, "negative market filter or fee")
        if ctx.open_risk < 0 or self.slippage_pct < 0:
            return self._reject(intent, "negative open risk or slippage")
        if intent.stop_price is not None and (not math.isfinite(intent.stop_price) or intent.stop_price <= 0):
            return self._reject(intent, "invalid stop-loss")
        if intent.side == "sell":
            return self._approve_sell(intent, ctx)
        if intent.side != "buy":
            return self._reject(intent, f"unknown side {intent.side!r}")
        if ctx.halted:
            return self._reject(intent, "trading is halted")
        if intent.amount <= 0 or intent.price <= 0:
            return self._reject(intent, "invalid amount or price")
        if self.rules.stop_atr_mult > 0:
            if intent.stop_price is None:
                return self._reject(intent, "no stop-loss attached")
            if intent.stop_price >= intent.price:
                return self._reject(intent, "stop-loss must be below the entry price")
        fee = ctx.info.taker_fee
        eff_price = intent.price * (1 + self.slippage_pct / 100.0)
        requested = intent.amount * eff_price * (1 + fee)
        caps = {
            "budget": self.limits.budget - ctx.in_use_total,
            "per-position limit": self.limits.budget * self.limits.per_position_pct / 100.0 - ctx.position_basis,
            "cash": ctx.cash,
        }
        if intent.stop_price is not None:
            loss_per_unit = eff_price * (1 + fee) - intent.stop_price * (1 - self.slippage_pct / 100) * (1 - fee)
            pending_risk = sum(
                a.amount * max(0, a.intent.price * (1 + self.slippage_pct / 100) * (1 + a.fee_rate)
                               - a.intent.stop_price * (1 - self.slippage_pct / 100) * (1 - a.fee_rate))
                for a in self._issued.values() if a.intent.side == "buy" and a.intent.stop_price is not None
            )
            risk_left = min(self.limits.budget * self.limits.risk_per_trade_pct / 100,
                            self.limits.budget * self.limits.max_open_risk_pct / 100 - ctx.open_risk - pending_risk)
            if loss_per_unit > 0:
                caps["loss-at-stop limit"] = max(0, risk_left) / loss_per_unit * eff_price * (1 + fee)
        for a in self._issued.values():
            if a.intent.side == "buy":
                reserve = a.notional * (1 + self.slippage_pct / 100) * (1 + a.fee_rate)
                caps["cash"] -= reserve
                caps["budget"] -= reserve
                if a.intent.symbol == intent.symbol:
                    caps["per-position limit"] -= reserve
        allowed = min(requested, *caps.values())
        if allowed <= 1e-9:
            tight = min(caps, key=caps.get)  # type: ignore[arg-type]
            return self._reject(intent, f"{tight} fully used (${max(0.0, caps[tight]):.2f} left)")
        amount = floor_to_step(allowed / (1 + fee) / eff_price, ctx.info.amount_step)
        notional = amount * intent.price
        if amount <= 0 or amount < ctx.info.min_amount or notional < ctx.info.min_cost:
            return self._reject(
                intent,
                f"order of ${notional:.2f} is below the exchange minimum "
                f"(min ${ctx.info.min_cost:g} / {ctx.info.min_amount:g} units); raise the budget or per-position limit",
            )
        return self._issue(intent, amount, fee, ctx.now_ms)

    def _approve_sell(self, intent: OrderIntent, ctx: RiskContext) -> Approval | Rejection:
        reserved = sum(a.amount for a in self._issued.values() if a.intent.side == "sell" and a.intent.symbol == intent.symbol)
        available = max(0.0, ctx.position_units - reserved)
        if ctx.position_units <= 0:
            return self._reject(intent, "nothing to sell")
        amount = floor_to_step(min(intent.amount, available), ctx.info.amount_step)
        if amount <= 0:
            return self._reject(intent, "sell amount rounds to zero")
        if amount < ctx.info.min_amount or amount * intent.price < ctx.info.min_cost:
            return self._reject(intent, "remaining position is below the exchange minimum; manual dust conversion may be required")
        return self._issue(intent, amount, ctx.info.taker_fee, ctx.now_ms)

    def _issue(self, intent: OrderIntent, amount: float, fee: float, now_ms: int) -> Approval:
        a = Approval(intent, amount, fee, uuid.uuid4().hex, now_ms)
        self._issued[a.token] = a
        self.approved += 1
        return a

    def _reject(self, intent: OrderIntent, reason: str) -> Rejection:
        self.rejected += 1
        return Rejection(intent, reason)

    def consume(self, token: str, now_ms: int | None = None) -> Approval:
        """Executors call this; a token works exactly once."""
        try:
            approval = self._issued.pop(token)
            if now_ms is not None and not 0 <= now_ms - approval.issued_ms <= 30_000:
                raise PermissionError("risk approval expired")
            return approval
        except KeyError:
            raise PermissionError("order has no valid risk approval") from None

