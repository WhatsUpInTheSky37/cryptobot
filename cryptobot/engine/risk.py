"""The single choke point for money.

Every order, simulated or real, must carry an ``Approval`` issued here, and an
executor consumes the approval exactly once. There is no other way to build an
order. Spending limits are enforced identically in TEST and BUY modes.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from pydantic import BaseModel

from cryptobot.strategy.config import RiskRules


class RiskLimits(BaseModel):
    """User-facing spending limits (Risk & Budget screen)."""

    budget: float = 100.0  # hard cap on money at risk, fees included
    per_position_pct: float = 25.0  # max % of budget in any single coin (exchange minimums make 10% unusable at $100)
    daily_loss_pct: float = 5.0  # stop for the day when today's loss reaches this % of budget
    max_drawdown_pct: float = 20.0  # stop entirely when equity falls this % from its peak
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


def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step


class RiskManager:
    def __init__(self, limits: RiskLimits, rules: RiskRules, slippage_pct: float = 0.05) -> None:
        self.limits = limits
        self.rules = rules
        self.slippage_pct = slippage_pct  # sizing assumes fills this much worse than the quoted price
        self._issued: dict[str, Approval] = {}
        self.approved = 0
        self.rejected = 0

    def approve(self, intent: OrderIntent, ctx: RiskContext) -> Approval | Rejection:
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
        if ctx.position_units <= 0:
            return self._reject(intent, "nothing to sell")
        if intent.amount >= ctx.position_units * (1 - 1e-9):
            amount = ctx.position_units  # closing: sell everything, dust included
        else:
            amount = floor_to_step(min(intent.amount, ctx.position_units), ctx.info.amount_step)
        if amount <= 0:
            return self._reject(intent, "sell amount rounds to zero")
        return self._issue(intent, amount, ctx.info.taker_fee, ctx.now_ms)

    def _issue(self, intent: OrderIntent, amount: float, fee: float, now_ms: int) -> Approval:
        a = Approval(intent, amount, fee, uuid.uuid4().hex, now_ms)
        self._issued[a.token] = a
        self.approved += 1
        return a

    def _reject(self, intent: OrderIntent, reason: str) -> Rejection:
        self.rejected += 1
        return Rejection(intent, reason)

    def consume(self, token: str) -> Approval:
        """Executors call this; a token works exactly once."""
        try:
            return self._issued.pop(token)
        except KeyError:
            raise PermissionError("order has no valid risk approval") from None
