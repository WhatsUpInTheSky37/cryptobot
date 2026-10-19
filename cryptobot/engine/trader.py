"""The live trading loop, shared by TEST and BUY modes.

It consumes the same events the GUI sees (market info, bars, ticks, heartbeats),
runs the SAME signal function as the backtester on closed bars, asks the risk
manager for approval, and hands approvals to the executor. Only the executor
knows whether money is real.
"""
from __future__ import annotations

import logging
import hashlib
import math
import uuid
import json
from dataclasses import asdict
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np

from cryptobot.core.bars import bucket_ms
from cryptobot.core.events import (
    AlertEvent,
    BarsEvent,
    BookEvent,
    BookSyncEvent,
    ConnectionEvent,
    BudgetEvent,
    EquityEvent,
    FillEvent,
    HeartbeatEvent,
    MarketInfoEvent,
    PositionSnapshot,
    PositionsEvent,
    SignalEvent,
    TickEvent,
    TraderStateEvent,
)
from cryptobot.core.models import Bar, Mode
from cryptobot.db.store import Store
from cryptobot.engine.executors import ExecutionError, Fill, LiveExecutor, OrderUncertain, SimulatedExecutor
from cryptobot.engine.modes import FIRST_MONTH_CAP, FIRST_MONTH_DAYS, buy_gate
from cryptobot.engine.ledger import Ledger
from cryptobot.engine.risk import Approval, MarketInfo, OrderIntent, Rejection, RiskContext, RiskLimits, RiskManager
from cryptobot.strategy import indicators as ind
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import StrategyConfig
from cryptobot.strategy.signals import compute_target_exposure
from cryptobot.strategy.online import OnlineSignals

log = logging.getLogger("trader")

HALTED = ("halted_daily", "halted_drawdown")


class Trader:
    def __init__(
        self,
        store: Store,
        emit: Callable[[Any], None],
        exchange_id: str,
        symbols: list[str],
        strategy: StrategyConfig,
        limits: RiskLimits,
        *,
        mode: Mode = Mode.TEST,
        slippage_pct: float = 0.05,
        clock: Callable[[], float] = time.time,
        reset_peak: bool = False,
        fallback_fee: float = 0.006,
        exchange: Any = None,
        max_slippage_pct: float = 0.25,
        fill_timeout_s: float = 15.0,
    ) -> None:
        self.store = store
        self.emit = emit
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        quotes = {s.partition("/")[2] for s in symbols}
        if len(quotes) != 1 or "" in quotes:
            raise ValueError("one quote-currency wallet is required per trading book")
        self.quote = next(iter(quotes))
        self.account_id = hashlib.sha256(str(getattr(exchange, "apiKey", "paper")).encode()).hexdigest()[:12]
        self.scope = f"{mode.value}:{exchange_id}:{self.account_id}:{self.quote}"
        self.strategy = strategy
        self.limits = limits
        self.mode = mode
        self.slippage_pct = slippage_pct
        self.clock = clock
        self.fallback_fee = fallback_fee
        self.rules = strategy.effective_risk()
        # Size orders assuming the WORST fill the executor can accept, so the budget cap holds even at the cap.
        sizing_slip = max_slippage_pct if mode == Mode.BUY else slippage_pct
        self.risk = RiskManager(limits, self.rules, sizing_slip)
        if mode == Mode.TEST:
            self.executor: SimulatedExecutor | LiveExecutor = SimulatedExecutor(self.risk, slippage_pct)
        elif mode == Mode.BUY:
            if exchange is None:
                raise ValueError("BUY mode needs an authenticated exchange connection")
            allowed, reasons, _ = buy_gate(store, int(clock() * 1000), limits.budget, True, store.get_setting(f"apikey.can_trade.{exchange_id}"))
            if not allowed:
                raise ValueError("; ".join(reasons))
            first = store.get_setting("buy.first_started_ms")
            now_ms = int(clock() * 1000)
            if (first is None or now_ms - int(first) < FIRST_MONTH_DAYS * 86_400_000) and limits.budget > FIRST_MONTH_CAP:
                raise ValueError(f"budget may not exceed ${FIRST_MONTH_CAP:,.0f} during the first {FIRST_MONTH_DAYS} days of BUY mode")
            self.executor = LiveExecutor(self.risk, exchange, exchange_id=exchange_id, max_slippage_pct=max_slippage_pct, fill_timeout_s=fill_timeout_s, store=store, scope=self.scope)
        else:
            raise ValueError("mode must be TEST or BUY")
        self._fee_override: dict[str, float] = {}
        self._tz = ZoneInfo(limits.timezone)
        self._infos: dict[str, MarketInfo] = {}
        self._bars: dict[str, dict[int, Bar]] = {s: {} for s in self.symbols}
        self.status = "stopped"
        self.halted_until_ms = 0
        self.started_ms = 0
        self._last_mark_ms = 0
        self._last_equity_save_ms = 0
        self._reset_peak = reset_peak
        self.connected = mode == Mode.TEST
        self._quotes: dict[str, tuple[float, float, int]] = {}
        self._decision_pending: set[str] = set()
        self._liquidation_status = "stopped"
        self._liquidation_requested = False
        self._last_exit_attempt: dict[str, int] = {}
        self._last_recovery_ms = 0
        self._signal_state: dict[str, Any] = {}
        self.ledger = Ledger(cash=limits.budget, positions={})
        self._load_state()
        self._runtimes = {s: OnlineSignals(strategy, state=self._signal_state.get(s)) for s in self.symbols}
        for symbol in self.symbols:
            prior = self._signal_state.get(symbol)
            if prior and prior.get("config") != strategy.model_dump():
                p = self.ledger.position(symbol)
                p.last_decided_ts_ms = None
                if not p.open:
                    p.schedule_started_ms = None
        for s, p in self.ledger.positions.items():
            if p.open and s not in self.symbols:
                self.symbols.append(s)
                self._bars[s] = {}
                self._runtimes[s] = OnlineSignals(strategy, state=self._signal_state.get(s))

    # ------------------------------------------------------------------ state

    @property
    def _state_key(self) -> str:
        return f"trader.state.{self.scope}"

    def _load_state(self) -> None:
        saved = self.store.get_setting(self._state_key) or self.store.get_setting(f"trader.state.{self.mode.value}")
        if saved and saved.get("exchange") == self.exchange_id:
            self._signal_state = saved.get("signal_state", {})
            self.ledger = Ledger.from_dict(saved["ledger"])
            allocated = float(saved.get("allocated", self.limits.budget))
            if abs(allocated - self.limits.budget) > 1e-9:
                if self.limits.budget < self.ledger.in_use() - 1e-9:
                    raise ValueError("close or reduce positions before lowering the budget below deployed cost")
                delta = self.limits.budget - allocated
                self.ledger.cash += delta
                self.ledger.day_start_equity += delta
                self.ledger.peak += delta
            if saved.get("status") in (*HALTED, "liquidating", "reconcile_required") and not (self._reset_peak and saved["status"] == "halted_drawdown"):
                self.status = saved["status"]
                self.halted_until_ms = int(saved.get("halted_until_ms", 0))
                self._liquidation_status = saved.get("liquidation_status", "stopped")
                self._liquidation_requested = saved.get("liquidation_requested", saved.get("status") == "liquidating")
        for s in self.symbols:
            self.ledger.position(s)
        if self._reset_peak:
            self.ledger.peak = 0.0

    def _persist(self) -> None:
        self.store.set_setting(
            self._state_key,
            {
                "exchange": self.exchange_id,
                "symbols": self.symbols,
                "allocated": self.limits.budget,
                "status": self.status,
                "halted_until_ms": self.halted_until_ms,
                "liquidation_status": self._liquidation_status,
                "liquidation_requested": self._liquidation_requested,
                "ledger": self.ledger.to_dict(),
                "signal_state": {s: runtime.to_dict() for s, runtime in getattr(self, "_runtimes", {}).items()},
            },
        )

    # --------------------------------------------------------------- commands

    def start(self) -> None:
        self.started_ms = self._now_ms()
        if self.status == "reconcile_required":
            self.status = "running"  # explicit restart retries full reconciliation before entries
        if self.status not in (*HALTED, "liquidating", "reconcile_required"):
            self.status = "running"
        key = "test.started_ms" if self.mode == Mode.TEST else "buy.first_started_ms"
        if self.store.get_setting(key) is None:
            self.store.set_setting(key, self.started_ms)
        self.store.audit("trader", f"started in {self.mode.value} mode", mode=self.mode.value, strategy=self.strategy.describe(), limits=self.limits.model_dump())
        if self.mode == Mode.BUY:
            self._reconcile()
        self._mark(force=True)  # establishes today's baseline and the peak before any trade
        self._persist()
        self._publish_state("Waiting for market data…")
        self._publish_positions()
        eq = self.ledger.equity()
        self.store.add_equity(self.mode.value, self.started_ms, eq, self.ledger.cash, self.ledger.in_use())
        self.emit(EquityEvent(self.mode.value, self.started_ms, eq))

    def stop(self, reason: str = "stopped by user") -> None:
        self.status = "paused" if any(p.open for p in self.ledger.positions.values()) or self._pending_orders(include_protection=True) else "stopped"
        self.store.audit("trader", reason, mode=self.mode.value)
        self._persist()
        self._publish_state(reason)

    def emergency_stop(self) -> int:
        self.status = "liquidating"
        self._liquidation_requested = True
        self._liquidation_status = "stopped"
        self._recover_orders()
        n = self._close_all("emergency stop", urgent=True)
        if not any(p.open for p in self.ledger.positions.values()) and not self._pending_orders():
            self.status = "stopped"
            self._liquidation_requested = False
        self.store.audit("trader", f"EMERGENCY STOP: closed {n} position(s)", mode=self.mode.value, level="WARNING")
        self._persist()
        self._publish_state(f"Emergency stop: confirmed {n} closed; residual holdings remain supervised until flat.")
        remaining = sum(p.open for p in self.ledger.positions.values())
        self.emit(AlertEvent("warn", "Emergency stop", f"Confirmed {n} closed position(s); {remaining} still held. Entry decisions are disabled and residual exposure remains supervised.", self._now_ms()))
        return n

    def resume(self) -> bool:
        now = self._now_ms()
        if self.status == "paused":
            self.status = "running"
            self._persist()
            self._publish_state("Entry decisions resumed; protection remains active.")
            return True
        if self.status != "halted_daily" or now < self.halted_until_ms:
            return False
        self.status = "running"
        self.halted_until_ms = 0
        self.ledger.day_key = self._day_key(now)
        self.ledger.day_start_equity = self.ledger.equity()
        self.store.audit("trader", "resumed after daily loss limit", mode=self.mode.value)
        self._persist()
        self._publish_state("Resumed.")
        self._publish_budget(force=True)
        return True

    def set_limits(self, limits: RiskLimits) -> None:
        if limits.budget < self.ledger.in_use() - 1e-9:
            raise ValueError("budget cannot be reduced below deployed cost; reduce positions first")
        if self.mode == Mode.BUY:
            first = int(self.store.get_setting("buy.first_started_ms", self._now_ms()))
            if self._now_ms() - first < FIRST_MONTH_DAYS * 86_400_000 and limits.budget > FIRST_MONTH_CAP:
                raise ValueError("first-month BUY budget cap still applies")
            free = self.executor.exchange.fetch_balance().get("free", {}).get(self.quote)
            if free is None or float(free) < self.ledger.cash + limits.budget - self.limits.budget:
                raise ValueError("budget exceeds verified free quote balance")
        delta = limits.budget - self.limits.budget
        self.ledger.cash += delta
        self.ledger.day_start_equity += delta
        self.ledger.peak += delta
        self.limits = limits
        self.risk.limits = limits
        self._tz = ZoneInfo(limits.timezone)
        self.store.audit("risk", "limits changed", **limits.model_dump())
        self._persist()
        self._publish_budget(force=True)

    # ----------------------------------------------------------------- events

    def on_event(self, ev: Any) -> None:
        if getattr(ev, "exchange", self.exchange_id) != self.exchange_id:
            return
        if self.status == "stopped":
            return
        if isinstance(ev, ConnectionEvent):
            self.connected = ev.status == "connected"
            if not self.connected:
                self._quotes.clear()
        elif isinstance(ev, BookSyncEvent) and ev.status not in ("synced", "verified"):
            self._quotes.pop(ev.symbol, None)
        elif isinstance(ev, BookEvent) and ev.symbol in self._bars:
            if ev.synced and 0 < ev.best_bid <= ev.best_ask and self._now_ms() - ev.ts_ms <= 30_000:
                self._quotes[ev.symbol] = (ev.best_bid, ev.best_ask, ev.ts_ms)
                self._on_tick(ev.symbol, ev.best_bid, ev.ts_ms)
                if ev.symbol in self._decision_pending and self.status == "running":
                    self._decision_pending.discard(ev.symbol)
                    bars = [b for _, b in sorted(self._bars[ev.symbol].items()) if b.closed]
                    if bars:
                        self._decide(ev.symbol, bars)
            else:
                self._quotes.pop(ev.symbol, None)
        elif isinstance(ev, MarketInfoEvent):
            if ev.symbol in self._bars:
                self._infos[ev.symbol] = MarketInfo(ev.symbol, ev.taker_fee if ev.taker_fee is not None else self.fallback_fee, ev.maker_fee, ev.min_amount, ev.min_cost, ev.amount_step, ev.price_step)
        elif isinstance(ev, BarsEvent):
            if ev.timeframe_s == self.strategy.timeframe_s and ev.symbol in self._bars:
                self._on_bars(ev)
        elif isinstance(ev, TickEvent):
            if ev.tick.symbol in self._bars:
                self._on_tick(ev.tick.symbol, ev.tick.price, ev.tick.ts_ms)
        elif isinstance(ev, HeartbeatEvent):
            self._on_heartbeat()

    def _on_bars(self, ev: BarsEvent) -> None:
        series = self._bars[ev.symbol]
        if ev.replace:
            series.clear()
        for b in ev.bars:
            series[b.ts_ms] = b
        if len(series) > 1200:
            if not (len(self.strategy.strategies) == 1 and self.strategy.strategies[0].id == "dca"):
                self._runtimes.setdefault(ev.symbol, OnlineSignals(self.strategy)).evaluate([b for _, b in sorted(series.items()) if b.closed])
            for key in sorted(series)[:-1200]:
                del series[key]
        closed = [series[k] for k in sorted(series) if series[k].closed]
        if not closed:
            return
        p = self.ledger.position(ev.symbol)
        if p.last_decided_ts_ms is None or closed[-1].ts_ms > p.last_decided_ts_ms:
            self._decide(ev.symbol, closed)

    def _on_tick(self, symbol: str, price: float, ts_ms: int) -> None:
        p = self.ledger.position(symbol)
        if not math.isfinite(price) or price <= 0 or ts_ms < p.last_tick_ms or ts_ms > self._now_ms() + 5_000:
            return
        if self.mode == Mode.BUY and self._now_ms() - ts_ms > 30_000:
            return
        p.last_tick_ms = ts_ms
        p.last_price = price
        if p.open and p.exit_pending:
            if self._now_ms() - self._last_exit_attempt.get(symbol, 0) >= 5_000:
                self._last_exit_attempt[symbol] = self._now_ms()
                self._sell(symbol, p.units, price, p.exit_pending, urgent=True)
        elif p.open:
            if self.rules.trailing_pct > 0:
                p.high_water = max(p.high_water, price)
                trail = p.high_water * (1 - self.rules.trailing_pct / 100.0)
                p.stop = trail if p.stop is None else max(p.stop, trail)
            if p.stop is not None and price <= p.stop:
                p.exit_pending = "stop-loss"
                self._sell(symbol, p.units, price, "stop-loss", urgent=True)
                p.blocked = True
            elif p.take_profit is not None and price >= p.take_profit:
                p.exit_pending = "take-profit"
                self._sell(symbol, p.units, price, "take-profit")
                p.blocked = True
        self._mark()

    def _on_heartbeat(self) -> None:
        now = self._now_ms()
        if now - self._last_recovery_ms >= 5_000:
            self._last_recovery_ms = now
            self._recover_orders()
            for s, p in list(self.ledger.positions.items()):
                if p.open and p.exit_pending and p.last_price and now - p.last_tick_ms <= 30_000:
                    self._on_tick(s, p.last_price, p.last_tick_ms)
                elif p.open and p.stop is not None:
                    if p.protection_client_id and p.protected_stop is not None and p.stop > p.protected_stop:
                        self._cancel_protection(s)
                    self._ensure_protection(s)
            if self._liquidation_requested and not any(p.open for p in self.ledger.positions.values()) and not self._pending_orders():
                self.status = self._liquidation_status
                self._liquidation_requested = False
                self._persist()
        self._mark()
        if now - self._last_equity_save_ms >= 60_000:
            self._last_equity_save_ms = now
            eq = self.ledger.equity()
            self.store.add_equity(self.mode.value, now, eq, self.ledger.cash, self.ledger.in_use())
            self.emit(EquityEvent(self.mode.value, now, eq))
            self._persist()
            self._publish_state()

    # --------------------------------------------------------------- decision

    def _decide(self, symbol: str, closed: list[Bar]) -> None:
        p = self.ledger.position(symbol)
        bars = closed
        p.last_decided_ts_ms = bars[-1].ts_ms
        if len(bars) < 1:
            self._note(symbol, f"Waiting for more history ({len(bars)} bars).", 0.0)
            return
        ba = BarArrays.from_bars(bars)
        pure_schedule = len(self.strategy.strategies) == 1 and self.strategy.strategies[0].id == "dca"
        if pure_schedule:
            params = self.strategy.strategies[0].full_params()
            if p.schedule_started_ms is None:
                p.schedule_started_ms = self._now_ms()
            due = min(int(params["purchases"]), (self._now_ms() - p.schedule_started_ms) // (int(params["interval"]) * self.strategy.timeframe_s * 1000) + 1)
            expo = np.array([due / params["purchases"]])
        else:
            runtime = self._runtimes.setdefault(symbol, OnlineSignals(self.strategy))
            exposure, atr = runtime.evaluate(bars)
            expo = np.array([np.nan if exposure is None else exposure])
            p.atr = atr
        e = float(expo[-1])
        if pure_schedule:
            p.atr = None
        if np.isnan(e):
            self._note(symbol, "Strategy is still warming up (not enough bars for its indicators).", 0.0)
            return
        price = p.last_price or bars[-1].close
        self._note(symbol, f"{self.strategy.describe()} → target exposure {e:.0%} after bar close {self._fmt_ts(bars[-1].end_ms)}", e)
        if self.status == "running":
            self._rebalance(symbol, e, price)
        self._persist()

    def _rebalance(self, symbol: str, e: float, price: float) -> None:
        p = self.ledger.position(symbol)
        if e <= 0:
            p.blocked = False
        if p.blocked:
            self._note(symbol, "Stopped out earlier; waiting for the signal to reset before re-entering.", e)
            return
        alloc = self.limits.budget * self.limits.per_position_pct / 100.0
        thresh = alloc * self.rules.rebalance_threshold_pct / 100.0
        if e <= 0:
            if p.open:
                self._sell(symbol, p.units, price, "signal exit")
            p.held_e = 0.0
            return
        held = min(1.0, p.basis / alloc) if alloc > 0 and p.open else 0.0  # what is really deployed (partial fills count)
        p.held_e = held
        d = e - held
        if d * alloc > thresh:
            self._buy(symbol, d * alloc, price, "signal entry" if not p.open else "signal add")
        elif -d * alloc > thresh and p.open and held > 0:
            self._sell(symbol, p.units * min(1.0, -d / held), price, "signal reduce")

    # ----------------------------------------------------------------- orders

    def _ctx(self, symbol: str) -> RiskContext:
        p = self.ledger.position(symbol)
        info = self._infos.get(symbol) or MarketInfo(symbol, taker_fee=self.fallback_fee)
        if symbol in self._fee_override:
            info = MarketInfo(symbol, self._fee_override[symbol], info.maker_fee, info.min_amount, info.min_cost, info.amount_step, info.price_step)
        open_risk = sum(max(0, q.basis - q.units * (q.stop or 0) * (1 - info.taker_fee - self.risk.slippage_pct / 100)) for q in self.ledger.positions.values() if q.open)
        return RiskContext(self.ledger.cash, self.ledger.in_use(), p.basis, p.units, self.status != "running", info, self._now_ms(), open_risk)

    def _buy(self, symbol: str, spend: float, price: float, reason: str) -> bool:
        if self._pending_orders():
            return False
        if self.mode == Mode.BUY:
            quote = self._quotes.get(symbol)
            if not self.connected or symbol not in self._infos or not quote or self._now_ms() - quote[2] > 30_000:
                self._decision_pending.add(symbol)
                self._note(symbol, "Waiting for fresh synchronized bid/ask and market filters.", 0.0)
                return False
            price = quote[1]
        p = self.ledger.position(symbol)
        ctx = self._ctx(symbol)
        fee = ctx.info.taker_fee
        amount = spend / (1 + fee) / price
        stop = price - self.rules.stop_atr_mult * p.atr if self.rules.stop_atr_mult > 0 and p.atr else None
        if self.mode == Mode.BUY and stop is not None:
            capability = getattr(self.executor.exchange, "feature_value", lambda *args: False)
            if not capability(symbol, "createOrder", "stopLossPrice"):
                self._note(symbol, "Entry refused: this market has no verified native stop capability.", p.held_e)
                return False
        tp = price * (1 + self.rules.take_profit_pct / 100.0) if self.rules.take_profit_pct > 0 else None
        intent = OrderIntent(symbol, "buy", amount, price, reason, stop, tp)
        res = self.risk.approve(intent, ctx)
        if isinstance(res, Rejection):
            self._note(symbol, f"Buy skipped: {res.reason}.", p.held_e)
            self.store.audit("risk", f"buy rejected: {res.reason}", symbol=symbol, spend=spend, level="WARNING")
            return False
        if p.open and not self._cancel_protection(symbol):
            self.risk.consume(res.token)
            return False
        fill = self._execute(res, price)
        if fill is None:
            self._ensure_protection(symbol)
            return False
        self._apply_fill(fill, stop, tp)
        return True

    def _sell(self, symbol: str, amount: float, price: float, reason: str, urgent: bool = False) -> bool:
        if not self._cancel_protection(symbol):
            return False
        if any(o["symbol"] == symbol for o in self._pending_orders()):
            return False
        ctx = self._ctx(symbol)
        intent = OrderIntent(symbol, "sell", amount, price, reason)
        res = self.risk.approve(intent, ctx)
        if isinstance(res, Rejection):
            self.store.audit("risk", f"sell rejected: {res.reason}", symbol=symbol, level="WARNING")
            return False
        fill = self._execute(res, price, urgent)
        if fill is None:
            return False
        self._apply_fill(fill)
        return True

    def _execute(self, approval, price: float, urgent: bool = False) -> Fill | None:
        sym, side = approval.intent.symbol, approval.intent.side
        try:
            fill = self.executor.execute(approval, price, self._now_ms(), urgent=urgent)
        except ExecutionError as e:
            if isinstance(e, OrderUncertain):
                self.status = "reconcile_required"
                self._persist()
            msg = f"{side} order failed: {e}"
            log.error("%s %s", sym, msg)
            self.store.audit("order", msg, symbol=sym, level="ERROR")
            self.emit(AlertEvent("bad", "Order failed", f"{sym}: {e}", self._now_ms()))
            return None
        if fill is None:
            self._note(sym, f"{side} order was not filled in time and was cancelled; will retry at the next decision.", self.ledger.position(sym).held_e)
            self.store.audit("order", f"{side} not filled, cancelled", symbol=sym, level="WARNING")
            return None
        if fill.status == "partial":
            self._note(sym, f"{side} order partially filled ({fill.amount:.6f}); the rest was cancelled.", self.ledger.position(sym).held_e)
        return fill

    def _apply_fill(self, fill: Fill, stop: float | None = None, tp: float | None = None) -> None:
        if fill.execution_key and self.store.execution_applied(fill.execution_key):
            return
        previous = self.ledger.to_dict()
        events: list[Any] = []
        emit = self.emit
        self.emit = events.append
        try:
            with self.store.transaction():
                self._apply_fill_transaction(fill, stop, tp)
                if fill.execution_key:
                    self.store.mark_execution(fill.execution_key, fill.ts_ms)
                if fill.client_id:
                    self.store.update_order(fill.client_id, "applied", applied=True)
        except BaseException:
            self.ledger = Ledger.from_dict(previous)
            self.status = "reconcile_required"
            raise
        finally:
            self.emit = emit
        for event in events:
            emit(event)
        if fill.side == "buy":
            self._ensure_protection(fill.symbol)
            if self._liquidation_requested and self.ledger.position(fill.symbol).open:
                self.ledger.position(fill.symbol).exit_pending = "liquidation requested"
                self._persist()

    def _ensure_protection(self, symbol: str) -> None:
        if not isinstance(self.executor, LiveExecutor):
            return
        p = self.ledger.position(symbol)
        if not p.open or p.stop is None or p.protection_client_id:
            return
        existing = [r for r in self._pending_orders(include_protection=True) if r["symbol"] == symbol and r["request"].get("role") == "protection"]
        if existing:
            p.protection_client_id = existing[0]["client_id"]
            p.protected_stop = existing[0]["request"]["market_price"]
            return
        a = Approval(OrderIntent(symbol, "sell", p.units, p.last_price or p.entry_price, "exchange stop-loss"), p.units, self._ctx(symbol).info.taker_fee, uuid.uuid4().hex, self._now_ms())
        try:
            cid, _ = self.executor.protect(a, p.stop, self._now_ms())
            p.protection_client_id, p.protected_stop = cid, p.stop
            self._persist()
        except ExecutionError as error:
            self.status = "reconcile_required"
            p.exit_pending = "native protection unavailable"
            self._persist()
            self.emit(AlertEvent("bad", "Native protection unavailable", f"{symbol}: {error}. Local liquidation remains active.", self._now_ms()))

    def _cancel_protection(self, symbol: str) -> bool:
        if not isinstance(self.executor, LiveExecutor):
            return True
        p = self.ledger.position(symbol)
        rows = [r for r in self._pending_orders(include_protection=True) if r["symbol"] == symbol and r["request"].get("role") == "protection"]
        for row in rows:
            try:
                order = row["response"]
                if not order.get("id"):
                    order = self.executor._find_by_client_id(symbol, row["client_id"])
                if not order:
                    raise OrderUncertain("native stop submission is unresolved")
                final = self.executor._wait_for_fill(order, symbol, cancel_immediately=True)
                self._recover_order(row, final)
            except Exception as error:
                self.status = "reconcile_required"
                self.emit(AlertEvent("bad", "Native stop status unknown", str(error), self._now_ms()))
                return False
        p.protection_client_id, p.protected_stop = None, None
        self._persist()
        return p.open

    def _apply_fill_transaction(self, fill: Fill, stop: float | None = None, tp: float | None = None) -> None:
        p = self.ledger.position(fill.symbol)
        was_open = p.open
        pid_before = p.position_id
        pnl = self.ledger.apply_fill(fill)
        quote = fill.symbol.partition("/")[2]
        if fill.side == "buy":
            if not was_open:
                # Use actual execution for the same protective distance as the decision.
                shift = fill.price - (fill.expected_price or fill.price)
                p.stop, p.take_profit, p.high_water = (stop + shift if stop is not None else None), (tp + shift if tp is not None else None), fill.price
            if p.position_id is None:
                p.position_id = self.store.open_position(
                    mode=self.mode.value, exchange=self.exchange_id, symbol=fill.symbol, side="long",
                    entry_ts_ms=p.entry_ts_ms, entry_price=p.entry_price, amount=p.units, stop_loss=p.stop,
                    take_profit=p.take_profit, trailing_pct=self.rules.trailing_pct or None, status="open",
                )
            else:
                self.store.update_position(p.position_id, amount=p.units, entry_price=p.entry_price, stop_loss=p.stop, take_profit=p.take_profit)
            pid = p.position_id
        else:
            pid = pid_before
            if pid is not None:
                if p.open:
                    self.store.update_position(pid, amount=p.units)
                else:
                    self.store.update_position(pid, status="closed", exit_ts_ms=fill.ts_ms, exit_price=fill.price, pnl=p.realized_pnl, amount=0.0)
            if not p.open:
                p.exit_pending = ""
        alloc = self.limits.budget * self.limits.per_position_pct / 100.0
        p.held_e = min(1.0, p.basis / alloc) if alloc > 0 and p.open else 0.0
        if fill.side == "buy" and len(self.strategy.strategies) == 1 and self.strategy.strategies[0].id == "dca":
            total = int(self.strategy.strategies[0].full_params()["purchases"])
            p.schedule_purchases = min(total, int(math.floor(p.held_e * total + .01)))
        self.store.insert_trade(
            mode=self.mode.value, exchange=self.exchange_id, symbol=fill.symbol, side=fill.side, ts_ms=fill.ts_ms,
            price=fill.expected_price or fill.price, amount=fill.amount, fee=fill.fee, fee_currency=quote, slippage=fill.slippage_pct,
            reason=fill.reason, pnl=pnl, position_id=pid, order_id=fill.order_id, fill_price=fill.price, status=fill.status,
            position_closed=(not p.open) if fill.side == "sell" else False, fees_json=json.dumps(fill.fees), fee_estimated=fill.fee_estimated,
            base_fee=fill.base_fee, quote_fee=fill.quote_fee, external_fee_value=fill.external_fee_value,
        )
        log.info("%s %s %s %.8f @ %.2f fee %.4f (%s)%s", self.mode.value, fill.side.upper(), fill.symbol, fill.amount, fill.price, fill.fee, fill.reason, f" pnl {pnl:+.2f}" if pnl is not None else "")
        self.emit(FillEvent(self.mode.value, fill.ts_ms, fill.symbol, fill.side, fill.price, fill.amount, fill.fee, fill.reason, pnl, fill.order_id, fill.status))
        eq = self.ledger.equity()
        self.store.add_equity(self.mode.value, fill.ts_ms, eq, self.ledger.cash, self.ledger.in_use())
        self.emit(EquityEvent(self.mode.value, fill.ts_ms, eq))
        self._publish_positions()
        self._publish_budget(force=True)
        self._persist()

    def _close_all(self, reason: str, urgent: bool = False) -> int:
        n = 0
        for s, p in list(self.ledger.positions.items()):
            if p.open:
                p.exit_pending = reason
                price = p.last_price or p.entry_price or 0.0
                if price > 0 and self._sell(s, p.units, price, reason, urgent=urgent) and not p.open:
                    n += 1
                p.held_e = 0.0
                p.blocked = False
        return n

    def _reconcile(self) -> None:
        """BUY mode start: make our records match what the exchange actually holds."""
        ex = self.executor
        if not isinstance(ex, LiveExecutor):
            return
        self._recover_orders()
        owned_ids = {o["response"].get("id") for o in self._pending_orders(include_protection=True)}
        for s in self.symbols:
            fee = ex.taker_fee(s)
            if fee is not None:
                self._fee_override[s] = fee
            try:
                for order in ex.exchange.fetch_open_orders(s):
                    if str(order.get("clientOrderId") or "").startswith("cb") and order.get("id") not in owned_ids:
                        raise OrderUncertain("untracked app order requires import before trading")
            except OrderUncertain as error:
                self.status = "reconcile_required"
                self.emit(AlertEvent("bad", "Order reconciliation required", str(error), self._now_ms()))
        try:
            balances = ex.exchange.fetch_balance()
            free = (balances.get("free") or {}).get(self.quote)
            if free is None or float(free) + 1e-8 < self.ledger.cash:
                raise ValueError("local cash exceeds verified free quote balance")
            total = balances.get("total") or {}
            for symbol, p in self.ledger.positions.items():
                if p.open and float(total.get(symbol.partition("/")[0]) or 0) + max(1e-12, p.units * 1e-8) < p.units:
                    raise ValueError(f"{symbol}: exchange inventory differs; import executions or resolve external transfers")
        except Exception as error:
            self.status = "reconcile_required"
            self.store.audit("reconcile", str(error), level="ERROR")
            self.emit(AlertEvent("bad", "Account reconciliation required", str(error), self._now_ms()))
        self._persist()

    def _pending_orders(self, include_protection: bool = False) -> list[dict[str, Any]]:
        rows = self.store.pending_orders(self.scope) if self.mode == Mode.BUY else []
        return rows if include_protection else [r for r in rows if r["request"].get("role") != "protection"]

    def _recover_orders(self) -> None:
        if not isinstance(self.executor, LiveExecutor):
            return
        for row in self._pending_orders(include_protection=True):
            try:
                order = row["response"]
                if not row["fill"]:
                    order = self.executor.exchange.fetch_order(order["id"], row["symbol"]) if order.get("id") else self.executor._find_by_client_id(row["symbol"], row["client_id"])
                if row["request"].get("role") == "protection" and order and order.get("status") == "open":
                    p = self.ledger.position(row["symbol"])
                    p.protection_client_id = row["client_id"]
                    continue
                if not row["fill"]:
                    if not order:
                        raise OrderUncertain("submission still unresolved")
                    order = self.executor._wait_for_fill(order, row["symbol"], cancel_immediately=True)
                self._recover_order(row, order)
            except Exception as error:
                self.status = "reconcile_required"
                self.store.audit("reconcile", f"unresolved order {row['client_id']}: {type(error).__name__}", level="ERROR")
        # Recovery never silently re-enables entries. An explicit restart performs
        # full cash/inventory checks and re-establishes the data-health gate.

    def _recover_order(self, row: dict[str, Any], order: dict[str, Any]) -> None:
        request = row["request"]
        raw = request["approval"]
        approval = Approval(OrderIntent(**raw["intent"]), raw["amount"], raw["fee_rate"], raw["token"], raw["issued_ms"])
        fill = Fill(**row["fill"]) if row["fill"] else self.executor.fill_from_order(order, approval, request["market_price"], request["now_ms"], row["client_id"])
        self.store.update_order(row["client_id"], "filled" if fill else "canceled_empty", order, asdict(fill) if fill else None, applied=False if fill else True)
        if request.get("role") == "protection":
            p = self.ledger.position(row["symbol"])
            p.protection_client_id, p.protected_stop = None, None
        if fill:
            self._apply_fill(fill, raw["intent"].get("stop_price"), raw["intent"].get("take_profit"))

    # ----------------------------------------------------------------- limits

    def _mark(self, force: bool = False) -> None:
        now = self._now_ms()
        if not force and now - self._last_mark_ms < 1000:
            return
        self._last_mark_ms = now
        led = self.ledger
        eq = led.equity()
        key = self._day_key(now)
        if led.day_key != key:
            led.day_key = key
            led.day_start_equity = eq
        if led.peak <= 0:
            led.peak = eq
        led.peak = max(led.peak, eq)
        today = eq - led.day_start_equity
        dd = 1 - eq / led.peak if led.peak > 0 else 0.0
        if self.status in ("running", "paused"):
            if today <= -self.limits.daily_loss_pct / 100.0 * self.limits.budget:
                self._halt("halted_daily", f"Today's loss reached ${-today:.2f} ({-today / self.limits.budget:.1%} of budget).")
            elif dd >= self.limits.max_drawdown_pct / 100.0:
                self._halt("halted_drawdown", f"Equity fell {dd:.1%} from its peak of ${led.peak:.2f}.")
        self._publish_budget(force=True)

    def _halt(self, kind: str, why: str) -> None:
        self.status = "liquidating"
        self._liquidation_requested = True
        self._liquidation_status = kind
        n = self._close_all("daily loss limit" if kind == "halted_daily" else "drawdown limit", urgent=True)
        if not any(p.open for p in self.ledger.positions.values()) and not self._pending_orders():
            self.status = kind
            self._liquidation_requested = False
        now = self._now_ms()
        if kind == "halted_daily":
            self.halted_until_ms = self._next_midnight_ms(now)
            title = "Daily loss limit reached"
            msg = f"{why} Closed {n} position(s). The bot will not trade again today; click Resume tomorrow."
        else:
            self.halted_until_ms = 0
            title = "Drawdown limit reached"
            msg = f"{why} Closed {n} position(s) and stopped. Review the strategy, then press START to continue."
        log.warning("%s: %s", title, msg)
        self.store.audit("risk", title, level="WARNING", detail=msg)
        self.emit(AlertEvent("bad", title, msg, now))
        self._persist()
        self._publish_state(msg)

    # ---------------------------------------------------------------- publish

    def _publish_state(self, message: str = "") -> None:
        tf = self.strategy.timeframe_s
        now = self._now_ms()
        nxt = bucket_ms(now, tf) + tf * 1000 if self.status == "running" else 0
        self.emit(
            TraderStateEvent(
                self.mode.value if self.status != "stopped" else Mode.STOPPED.value,
                self.status, message, self.exchange_id, self.symbols, self.strategy.describe(),
                self.started_ms, self.halted_until_ms, nxt,
            )
        )

    def _publish_budget(self, force: bool = False) -> None:
        led = self.ledger
        eq = led.equity()
        peak = max(led.peak, eq) if led.peak > 0 else eq
        in_use = led.in_use()
        self.emit(
            BudgetEvent(
                self.mode.value, self.limits.budget, in_use, max(0.0, min(led.cash, self.limits.budget - in_use)),
                led.cash, eq, eq - led.day_start_equity if led.day_key else 0.0, peak,
                (1 - eq / peak) if peak > 0 else 0.0, self._now_ms(),
            )
        )

    def _publish_positions(self) -> None:
        snaps = []
        for s, p in self.ledger.positions.items():
            if not p.open:
                continue
            cur = p.last_price or p.entry_price or 0.0
            val = p.units * cur
            snaps.append(PositionSnapshot(s, self.mode.value, p.units, p.entry_price or 0.0, cur, val - p.basis, (val / p.basis - 1) if p.basis else 0.0, p.stop, p.take_profit, p.entry_ts_ms or 0, p.basis))
        self.emit(PositionsEvent(self.mode.value, snaps))

    def _note(self, symbol: str, text: str, e: float) -> None:
        log.info("%s: %s", symbol, text)
        self.emit(SignalEvent(self.mode.value, symbol, self._now_ms(), e, text))

    # ------------------------------------------------------------------- time

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _day_key(self, now_ms: int) -> str:
        return datetime.fromtimestamp(now_ms / 1000, self._tz).date().isoformat()

    def _next_midnight_ms(self, now_ms: int) -> int:
        d = datetime.fromtimestamp(now_ms / 1000, self._tz).date() + timedelta(days=1)
        return int(datetime.combine(d, dtime(0, 0), tzinfo=self._tz).timestamp() * 1000)

    def _fmt_ts(self, ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, self._tz).strftime("%b %d %H:%M")

    # ------------------------------------------------------------------ debug

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status, "cash": self.ledger.cash, "equity": self.ledger.equity(), "in_use": self.ledger.in_use(), "positions": {s: (p.units, p.basis, p.stop, p.blocked) for s, p in self.ledger.positions.items()}}

