"""The live trading loop, shared by TEST and BUY modes.

It consumes the same events the GUI sees (market info, bars, ticks, heartbeats),
runs the SAME signal function as the backtester on closed bars, asks the risk
manager for approval, and hands approvals to the executor. Only the executor
knows whether money is real.
"""
from __future__ import annotations

import logging
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
from cryptobot.engine.executors import ExecutionError, Fill, LiveExecutor, SimulatedExecutor
from cryptobot.engine.modes import FIRST_MONTH_CAP, FIRST_MONTH_DAYS
from cryptobot.engine.ledger import Ledger
from cryptobot.engine.risk import MarketInfo, OrderIntent, Rejection, RiskContext, RiskLimits, RiskManager
from cryptobot.strategy import indicators as ind
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import StrategyConfig
from cryptobot.strategy.signals import compute_target_exposure

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
            first = store.get_setting("buy.first_started_ms")
            now_ms = int(clock() * 1000)
            if (first is None or now_ms - int(first) < FIRST_MONTH_DAYS * 86_400_000) and limits.budget > FIRST_MONTH_CAP:
                raise ValueError(f"budget may not exceed ${FIRST_MONTH_CAP:,.0f} during the first {FIRST_MONTH_DAYS} days of BUY mode")
            self.executor = LiveExecutor(self.risk, exchange, exchange_id=exchange_id, max_slippage_pct=max_slippage_pct, fill_timeout_s=fill_timeout_s)
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
        self.ledger = Ledger(cash=limits.budget, positions={})
        self._load_state()

    # ------------------------------------------------------------------ state

    @property
    def _state_key(self) -> str:
        return f"trader.state.{self.mode.value}"

    def _load_state(self) -> None:
        saved = self.store.get_setting(self._state_key)
        if saved and saved.get("exchange") == self.exchange_id:
            self.ledger = Ledger.from_dict(saved["ledger"])
            allocated = float(saved.get("allocated", self.limits.budget))
            if abs(allocated - self.limits.budget) > 1e-9:
                self.ledger.cash += self.limits.budget - allocated  # budget changed while stopped
            if saved.get("status") in HALTED and not (self._reset_peak and saved["status"] == "halted_drawdown"):
                self.status = saved["status"]
                self.halted_until_ms = int(saved.get("halted_until_ms", 0))
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
                "ledger": self.ledger.to_dict(),
            },
        )

    # --------------------------------------------------------------- commands

    def start(self) -> None:
        self.started_ms = self._now_ms()
        if self.status not in HALTED:
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
        self.status = "stopped"
        self.store.audit("trader", reason, mode=self.mode.value)
        self._persist()
        self._publish_state(reason)

    def emergency_stop(self) -> int:
        if isinstance(self.executor, LiveExecutor):
            for s in self.symbols:
                c = self.executor.cancel_ours(s)
                if c:
                    log.warning("cancelled %d open order(s) on %s", c, s)
        n = self._close_all("emergency stop", urgent=True)
        self.status = "stopped"
        self.store.audit("trader", f"EMERGENCY STOP: closed {n} position(s)", mode=self.mode.value, level="WARNING")
        self._persist()
        self._publish_state(f"Emergency stop: closed {n} position(s).")
        self.emit(AlertEvent("warn", "Emergency stop", f"Closed {n} position(s) and stopped.", self._now_ms()))
        return n

    def resume(self) -> bool:
        now = self._now_ms()
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
        delta = limits.budget - self.limits.budget
        self.ledger.cash += delta
        self.limits = limits
        self.risk.limits = limits
        self._tz = ZoneInfo(limits.timezone)
        self.store.audit("risk", "limits changed", **limits.model_dump())
        self._persist()
        self._publish_budget(force=True)

    # ----------------------------------------------------------------- events

    def on_event(self, ev: Any) -> None:
        if self.status == "stopped":
            return
        if isinstance(ev, MarketInfoEvent):
            if ev.symbol in self._bars:
                self._infos[ev.symbol] = MarketInfo(ev.symbol, ev.taker_fee or self.fallback_fee, ev.maker_fee, ev.min_amount, ev.min_cost, ev.amount_step, ev.price_step)
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
            for k in sorted(series)[: len(series) - 1200]:
                del series[k]
        closed = [series[k] for k in sorted(series) if series[k].closed]
        if not closed:
            return
        p = self.ledger.position(ev.symbol)
        if p.last_decided_ts_ms is None or closed[-1].ts_ms > p.last_decided_ts_ms:
            self._decide(ev.symbol, closed)

    def _on_tick(self, symbol: str, price: float, ts_ms: int) -> None:
        p = self.ledger.position(symbol)
        p.last_price = price
        if p.open and self.status == "running":
            if self.rules.trailing_pct > 0:
                p.high_water = max(p.high_water, price)
                trail = p.high_water * (1 - self.rules.trailing_pct / 100.0)
                p.stop = trail if p.stop is None else max(p.stop, trail)
            if p.stop is not None and price <= p.stop:
                self._sell(symbol, p.units, price, "stop-loss")
                p.blocked = True
            elif p.take_profit is not None and price >= p.take_profit:
                self._sell(symbol, p.units, price, "take-profit")
                p.blocked = True
        self._mark()

    def _on_heartbeat(self) -> None:
        now = self._now_ms()
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
        bars = closed[-600:]
        p.last_decided_ts_ms = bars[-1].ts_ms
        if len(bars) < 30:
            self._note(symbol, f"Waiting for more history ({len(bars)} bars).", 0.0)
            return
        ba = BarArrays.from_bars(bars)
        expo, _warm = compute_target_exposure(self.strategy, ba)
        e = float(expo[-1])
        a = ind.atr(ba.high, ba.low, ba.close, self.rules.atr_n)[-1]
        p.atr = None if np.isnan(a) else float(a)
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
        thresh = self.limits.budget * self.rules.rebalance_threshold_pct / 100.0
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
        return RiskContext(self.ledger.cash, self.ledger.in_use(), p.basis, p.units, self.status != "running", info, self._now_ms())

    def _buy(self, symbol: str, spend: float, price: float, reason: str) -> bool:
        p = self.ledger.position(symbol)
        ctx = self._ctx(symbol)
        fee = ctx.info.taker_fee
        amount = spend / (1 + fee) / price
        stop = price - self.rules.stop_atr_mult * p.atr if self.rules.stop_atr_mult > 0 and p.atr else None
        tp = price * (1 + self.rules.take_profit_pct / 100.0) if self.rules.take_profit_pct > 0 else None
        intent = OrderIntent(symbol, "buy", amount, price, reason, stop, tp)
        res = self.risk.approve(intent, ctx)
        if isinstance(res, Rejection):
            self._note(symbol, f"Buy skipped: {res.reason}.", p.held_e)
            self.store.audit("risk", f"buy rejected: {res.reason}", symbol=symbol, spend=spend, level="WARNING")
            return False
        fill = self._execute(res, price)
        if fill is None:
            return False
        self._apply_fill(fill, stop, tp)
        return True

    def _sell(self, symbol: str, amount: float, price: float, reason: str, urgent: bool = False) -> bool:
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
        p = self.ledger.position(fill.symbol)
        was_open = p.open
        pid_before = p.position_id
        pnl = self.ledger.apply_fill(fill)
        quote = fill.symbol.partition("/")[2]
        if fill.side == "buy":
            if not was_open:
                p.stop, p.take_profit, p.high_water = stop, tp, fill.price
            if p.position_id is None:
                p.position_id = self.store.open_position(
                    mode=self.mode.value, exchange=self.exchange_id, symbol=fill.symbol, side="long",
                    entry_ts_ms=p.entry_ts_ms, entry_price=p.entry_price, amount=p.units, stop_loss=p.stop,
                    take_profit=p.take_profit, trailing_pct=self.rules.trailing_pct or None, status="open",
                )
            else:
                self.store.update_position(p.position_id, amount=p.units, stop_loss=p.stop, take_profit=p.take_profit)
            pid = p.position_id
        else:
            pid = pid_before
            if pid is not None:
                if p.open:
                    self.store.update_position(pid, amount=p.units)
                else:
                    self.store.update_position(pid, status="closed", exit_ts_ms=fill.ts_ms, exit_price=fill.price, pnl=pnl, amount=0.0)
        alloc = self.limits.budget * self.limits.per_position_pct / 100.0
        p.held_e = min(1.0, p.basis / alloc) if alloc > 0 and p.open else 0.0
        self.store.insert_trade(
            mode=self.mode.value, exchange=self.exchange_id, symbol=fill.symbol, side=fill.side, ts_ms=fill.ts_ms,
            price=fill.expected_price or fill.price, amount=fill.amount, fee=fill.fee, fee_currency=quote, slippage=fill.slippage_pct,
            reason=fill.reason, pnl=pnl, position_id=pid, order_id=fill.order_id, fill_price=fill.price, status=fill.status,
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
                price = p.last_price or p.entry_price or 0.0
                if price > 0 and self._sell(s, p.units, price, reason, urgent=urgent):
                    n += 1
                p.held_e = 0.0
                p.blocked = False
        return n

    def _reconcile(self) -> None:
        """BUY mode start: make our records match what the exchange actually holds."""
        ex = self.executor
        if not isinstance(ex, LiveExecutor):
            return
        for s in self.symbols:
            fee = ex.taker_fee(s)
            if fee is not None:
                self._fee_override[s] = fee
            stray = ex.cancel_ours(s)
            if stray:
                self.store.audit("reconcile", f"cancelled {stray} leftover order(s)", symbol=s, level="WARNING")
        adjusted = False
        for s, p in self.ledger.positions.items():
            if not p.open:
                continue
            base = s.partition("/")[0]
            try:
                held = ex.balance(base)
            except Exception as e:  # noqa: BLE001
                log.warning("balance check failed for %s: %s", base, e)
                continue
            if held >= p.units * 0.995:
                continue
            msg = f"The exchange holds {held:.6f} {base} but the app recorded {p.units:.6f}. "
            if held <= 0 or (p.last_price or p.entry_price or 0) * held < 1.0:
                pid = p.position_id
                p.units, p.basis, p.held_e, p.position_id = 0.0, 0.0, 0.0, None
                p.entry_price = p.entry_ts_ms = p.stop = p.take_profit = None
                if pid is not None:
                    self.store.update_position(pid, status="closed", exit_ts_ms=self._now_ms(), amount=0.0)
                msg += "The position was removed from the app's records (sold or moved outside the app?)."
            else:
                factor = held / p.units
                p.units, p.basis = held, p.basis * factor
                if p.position_id is not None:
                    self.store.update_position(p.position_id, amount=held)
                msg += "The app's records were reduced to match."
            log.warning(msg)
            self.store.audit("reconcile", msg, symbol=s, level="WARNING")
            self.emit(AlertEvent("warn", "Position adjusted to match the exchange", msg, self._now_ms()))
            adjusted = True
        if adjusted:
            # An external change is not a trading loss: re-base the daily baseline and the peak.
            eq = self.ledger.equity()
            self.ledger.day_start_equity = eq
            self.ledger.peak = eq
        self._persist()

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
        if self.status == "running":
            if today <= -self.limits.daily_loss_pct / 100.0 * self.limits.budget:
                self._halt("halted_daily", f"Today's loss reached ${-today:.2f} ({-today / self.limits.budget:.1%} of budget).")
            elif dd >= self.limits.max_drawdown_pct / 100.0:
                self._halt("halted_drawdown", f"Equity fell {dd:.1%} from its peak of ${led.peak:.2f}.")
        self._publish_budget(force=True)

    def _halt(self, kind: str, why: str) -> None:
        n = self._close_all("daily loss limit" if kind == "halted_daily" else "drawdown limit")
        self.status = kind
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
