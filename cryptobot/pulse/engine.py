"""The Pulse engine: observe → score → prepare → confirm → (paper) trade → learn.

Long-only: spot accounts cannot short, so bearish setups are recorded for
research but never traded. All trades go through the same RiskManager and
SimulatedExecutor as the main bot, with mode "PULSE".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from cryptobot.core.events import (
    BarsEvent,
    BookEvent,
    HeartbeatEvent,
    MarketInfoEvent,
    PulseEventRecord,
    PulseSnapshot,
    PulseStatusEvent,
    TickEvent,
)
from cryptobot.core.models import Bar
from cryptobot.db.store import Store
from cryptobot.engine.executors import SimulatedExecutor
from cryptobot.engine.risk import MarketInfo, OrderIntent, Rejection, RiskContext, RiskLimits, RiskManager
from cryptobot.pulse.config import PulseConfig, break_even_win_rate, expected_value_pct
from cryptobot.pulse.features import Observation, Scores, SymbolFeatures
from cryptobot.pulse.memory import KEYS, MarketMemory
from cryptobot.strategy.config import RiskRules

log = logging.getLogger("pulse")
MODE = "PULSE"


@dataclass
class _Pending:
    event_id: int
    symbol: str
    direction: str
    entry_mid: float
    target_pct: float
    stop_pct: float
    start_ms: int
    horizon_ms: int
    vec: dict[str, float]
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class _Position:
    symbol: str
    units: float
    entry_px: float
    cost: float
    stop: float
    target: float
    entry_ms: int
    event_id: int


@dataclass
class _SymState:
    feats: SymbolFeatures
    state: str = "warming"
    prepared_ms: int = 0
    absorb_high: float = 0.0
    last_event_ms: dict[str, int] = field(default_factory=lambda: {"long": 0, "short": 0})
    last_obs: Observation | None = None
    last_scores: tuple[Scores, Scores] | None = None
    bars_1m: list[Bar] = field(default_factory=list)
    note: str = ""


class PulseEngine:
    def __init__(
        self,
        store: Store,
        emit: Callable[[Any], None],
        exchange_id: str,
        symbols: list[str],
        config: PulseConfig,
        *,
        slippage_pct: float = 0.05,
        clock: Callable[[], float] = time.time,
        timezone: str = "America/New_York",
        fallback_fee: float = 0.001,
    ) -> None:
        self.store = store
        self.emit = emit
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.cfg = config
        self.clock = clock
        self.slippage_pct = slippage_pct
        self.fallback_fee = fallback_fee
        self._tz = ZoneInfo(timezone)
        self.limits = RiskLimits(budget=config.budget, per_position_pct=config.per_position_pct, daily_loss_pct=config.daily_loss_pct, max_drawdown_pct=100)
        self.risk = RiskManager(self.limits, RiskRules(stop_atr_mult=0), slippage_pct)
        self.executor = SimulatedExecutor(self.risk, slippage_pct)
        self.memory = MarketMemory(store, config.k_similar, config.min_similar, config.prior_p)
        self._infos: dict[str, MarketInfo] = {}
        self.sym: dict[str, _SymState] = {
            s: _SymState(SymbolFeatures(s, config.window_s, config.obs_interval_s, config.history_obs, config.min_obs)) for s in self.symbols
        }
        self.running = False
        self.cash = config.budget
        self.positions: dict[str, _Position] = {}
        self.pending: list[_Pending] = []
        self.trades = 0
        self.trade_pnl = 0.0
        self.consecutive_losses = 0
        self.paused_until_ms = 0
        self.day_key = ""
        self.day_realized = 0.0
        self._last_obs_ms = 0
        self._last_status_ms = 0
        self.events_recorded = self.store.get_setting("pulse.events_recorded", 0) or 0
        self.message = ""

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.running = True
        self.message = "Warming up: scores need a few minutes of history per coin."
        self.store.audit("pulse", "started", symbols=self.symbols, config=self.cfg.model_dump())
        self._publish(force=True)

    def stop(self) -> None:
        for s in list(self.positions):
            self._exit(s, self.sym[s].feats.last_mid, "stopped")
        self.running = False
        self.store.audit("pulse", "stopped")
        self._publish(force=True)

    # --------------------------------------------------------------- events

    def on_event(self, ev: Any) -> None:
        if not self.running:
            return
        if isinstance(ev, MarketInfoEvent) and ev.symbol in self.sym:
            self._infos[ev.symbol] = MarketInfo(ev.symbol, ev.taker_fee or self.fallback_fee, ev.maker_fee, ev.min_amount, ev.min_cost, ev.amount_step, ev.price_step)
        elif isinstance(ev, TickEvent) and ev.tick.symbol in self.sym:
            st = self.sym[ev.tick.symbol]
            st.feats.add_tick(ev.tick)
            self._on_price(ev.tick.symbol, ev.tick.price, ev.tick.ts_ms)
        elif isinstance(ev, BookEvent) and ev.symbol in self.sym:
            st = self.sym[ev.symbol]
            st.feats.add_book(ev)
            self._on_price(ev.symbol, ev.mid, ev.ts_ms)
        elif isinstance(ev, BarsEvent) and ev.timeframe_s == 60 and ev.symbol in self.sym:
            st = self.sym[ev.symbol]
            bars = {b.ts_ms: b for b in st.bars_1m}
            for b in ev.bars:
                bars[b.ts_ms] = b
            st.bars_1m = [bars[k] for k in sorted(bars)][-60:]
            closed = [b for b in st.bars_1m if b.closed][-14:]
            if closed:
                st.feats.vol_pct = sum((b.high - b.low) / b.close * 100.0 for b in closed if b.close) / len(closed)
        elif isinstance(ev, HeartbeatEvent):
            self._tick_clock(ev.ts_ms)

    def _tick_clock(self, now_ms: int) -> None:
        if now_ms - self._last_obs_ms >= self.cfg.obs_interval_s * 1000:
            self._last_obs_ms = now_ms
            self._observe_all(now_ms)
        self._publish()

    # ---------------------------------------------------------- observation

    def _related_imb(self, symbol: str, now_ms: int) -> float | None:
        base = symbol.partition("/")[0]
        pref = ["BTC", "ETH"] if base not in ("BTC", "ETH") else (["ETH"] if base == "BTC" else ["BTC"])
        for s, st in self.sym.items():
            if s != symbol and s.partition("/")[0] in pref and st.feats.last_mid:
                return st.feats.imbalance(now_ms)
        for s, st in self.sym.items():
            if s != symbol and st.feats.last_mid:
                return st.feats.imbalance(now_ms)
        return None

    def _observe_all(self, now_ms: int) -> None:
        for symbol, st in self.sym.items():
            rel = self._related_imb(symbol, now_ms)
            obs = st.feats.observe(now_ms, rel)
            if obs is None:
                continue
            st.last_obs = obs
            long_, short = st.feats.scores(obs, self.cfg.weights(), rel is not None)
            st.last_scores = (long_, short)
            if not st.feats.warmed_up:
                st.state = "warming"
                st.note = f"{st.feats.observations}/{st.feats.min_obs} observations"
                continue
            self._record_research(symbol, st, obs, short, "short", now_ms)
            self._step_long(symbol, st, obs, long_, now_ms)

    # ------------------------------------------------------------ research

    def _vec(self, sc: Scores, obs: Observation) -> dict[str, float]:
        return {"f": sc.f, "a": sc.a, "w": sc.w, "r": sc.r, "c": sc.c, "vol_pct": obs.vol_pct}

    def _exit_levels(self, obs: Observation) -> tuple[float, float]:
        if self.cfg.adaptive and obs.vol_pct > 0:
            stop = min(self.cfg.max_stop_pct, max(self.cfg.min_stop_pct, self.cfg.stop_vol_mult * obs.vol_pct))
            return 2.0 * stop, stop
        return self.cfg.target_pct, self.cfg.stop_pct

    def _cost_pct(self, symbol: str) -> float:
        fee = (self._infos.get(symbol) or MarketInfo(symbol, taker_fee=self.fallback_fee)).taker_fee
        return 2.0 * (fee * 100.0 + self.slippage_pct)

    def _record(self, symbol: str, st: _SymState, obs: Observation, sc: Scores, direction: str, now_ms: int, status: str, reason: str, p: float, n: int, ev: float, target: float, stop: float) -> int:
        vec = self._vec(sc, obs)
        eid = self.store.insert_pulse_event(
            ts_ms=now_ms, exchange=self.exchange_id, symbol=symbol, direction=direction, prs=sc.prs, f=sc.f, a=sc.a, w=sc.w, r=sc.r, c=sc.c,
            imbalance=obs.imb60, vol_pct=obs.vol_pct, spread_pct=obs.spread_pct, mid=obs.mid, target_pct=target, stop_pct=stop,
            cost_pct=self._cost_pct(symbol), similar_n=n, similar_p=p, ev_pct=ev, status=status, reason=reason,
        )
        self.pending.append(_Pending(eid, symbol, direction, obs.mid, target, stop, now_ms, self.cfg.max_hold_s * 1000, vec))
        self.events_recorded += 1
        self.store.set_setting("pulse.events_recorded", self.events_recorded)
        st.last_event_ms[direction] = now_ms
        self.emit(PulseEventRecord(eid, now_ms, symbol, direction, sc.prs, status, reason, "pending", None, None, n, p, ev))
        return eid

    def _record_research(self, symbol: str, st: _SymState, obs: Observation, sc: Scores, direction: str, now_ms: int) -> None:
        if sc.prs < self.cfg.prepare_prs or now_ms - st.last_event_ms[direction] < self.cfg.event_cooldown_s * 1000:
            return
        target, stop = self._exit_levels(obs)
        p, n = self.memory.estimate(direction, self._vec(sc, obs))
        ev = expected_value_pct(p, target, stop, self._cost_pct(symbol))
        self._record(symbol, st, obs, sc, direction, now_ms, "research", "short setups are recorded, not traded (spot)", p, n, ev, target, stop)

    # ---------------------------------------------------------- long logic

    def _step_long(self, symbol: str, st: _SymState, obs: Observation, sc: Scores, now_ms: int) -> None:
        if symbol in self.positions:
            st.state = "entered"
            return
        cfg = self.cfg
        if st.state in ("warming", "idle", "monitor"):
            if sc.prs >= cfg.prepare_prs:
                st.state = "prepared"
                st.prepared_ms = now_ms
                st.absorb_high = st.feats.recent_high_mid(now_ms, 10)
                st.note = f"prepared: waiting for price above {st.absorb_high:,.2f} with PRS ≥ {cfg.enter_prs:g}"
            else:
                st.state = "monitor" if sc.prs >= cfg.monitor_prs else "idle"
                st.note = ""
            return
        if st.state == "prepared":
            if sc.prs < cfg.prepare_prs and now_ms - st.prepared_ms > cfg.prepared_ttl_s * 1000:
                st.state = "monitor"
                st.note = "pressure faded without confirmation"
                return
            if sc.prs >= cfg.enter_prs and obs.mid > st.absorb_high and obs.imb60 > 0:
                if now_ms - st.last_event_ms["long"] < cfg.event_cooldown_s * 1000:
                    return
                self._try_enter(symbol, st, obs, sc, now_ms)

    def _try_enter(self, symbol: str, st: _SymState, obs: Observation, sc: Scores, now_ms: int) -> None:
        cfg = self.cfg
        target, stop = self._exit_levels(obs)
        cost = self._cost_pct(symbol)
        p, n = self.memory.estimate("long", self._vec(sc, obs))
        ev = expected_value_pct(p, target, stop, cost)
        reasons = []
        if obs.spread_pct > cfg.max_spread_pct:
            reasons.append(f"spread {obs.spread_pct:.3f}% above {cfg.max_spread_pct:g}%")
        if now_ms < self.paused_until_ms:
            reasons.append("paused after consecutive losses")
        if self._daily_loss_hit():
            reasons.append("daily loss limit reached")
        if len(self.positions) >= cfg.max_positions:
            reasons.append(f"already {len(self.positions)} open position(s)")
        if target < cfg.min_target_cost_mult * cost:
            reasons.append(f"expected move {target:.2f}% cannot cover costs {cost:.2f}% (market too quiet)")
        if ev < cfg.ev_margin_pct and not cfg.research_mode:
            reasons.append(f"EV {ev:+.3f}% below margin (p={p:.0%} from {n} similar; break-even {break_even_win_rate(target, stop, cost):.0%})")
        if reasons:
            self._record(symbol, st, obs, sc, "long", now_ms, "rejected", "; ".join(reasons), p, n, ev, target, stop)
            st.state = "monitor"
            st.note = "rejected: " + reasons[0]
            return
        # size: risk a fixed fraction of the budget per trade
        risk_usd = cfg.budget * cfg.risk_per_trade_pct / 100.0
        notional = risk_usd / (stop / 100.0)
        price = obs.mid
        info = self._infos.get(symbol) or MarketInfo(symbol, taker_fee=self.fallback_fee)
        in_use = sum(p_.cost for p_ in self.positions.values())
        ctx = RiskContext(self.cash, in_use, 0.0, 0.0, False, info, now_ms)
        res = self.risk.approve(OrderIntent(symbol, "buy", notional / price, price, "pulse entry", price * (1 - stop / 100)), ctx)
        if isinstance(res, Rejection):
            self._record(symbol, st, obs, sc, "long", now_ms, "rejected", res.reason, p, n, ev, target, stop)
            st.state = "monitor"
            st.note = "rejected: " + res.reason
            return
        fill = self.executor.execute(res, price, now_ms)
        assert fill is not None
        cost_usd = fill.notional + fill.fee
        self.cash -= cost_usd
        eid = self._record(symbol, st, obs, sc, "long", now_ms, "entered", f"confirmed above {st.absorb_high:,.2f}; EV {ev:+.3f}% (p={p:.0%}, n={n})" + ("" if ev >= cfg.ev_margin_pct else " [research mode]"), p, n, ev, target, stop)
        self.positions[symbol] = _Position(symbol, fill.amount, fill.price, cost_usd, fill.price * (1 - stop / 100), fill.price * (1 + target / 100), now_ms, eid)
        self.store.insert_trade(mode=MODE, exchange=self.exchange_id, symbol=symbol, side="buy", ts_ms=now_ms, price=price, fill_price=fill.price, amount=fill.amount, fee=fill.fee, fee_currency=symbol.partition("/")[2], slippage=fill.slippage_pct, reason=f"pulse entry PRS {sc.prs:.0f}", status="filled")
        st.state = "entered"
        st.note = f"in position: stop {self.positions[symbol].stop:,.2f} target {self.positions[symbol].target:,.2f}"
        log.info("PULSE BUY %s %.6f @ %.2f (PRS %.0f, EV %+.3f%%)", symbol, fill.amount, fill.price, sc.prs, ev)
        self._publish(force=True)

    def _on_price(self, symbol: str, price: float, now_ms: int) -> None:
        pos = self.positions.get(symbol)
        if pos is not None:
            if price <= pos.stop:
                self._exit(symbol, price, "stop")
            elif price >= pos.target:
                self._exit(symbol, price, "target")
            elif now_ms - pos.entry_ms >= self.cfg.max_hold_s * 1000:
                self._exit(symbol, price, "expired")
        self._track_pending(symbol, price, now_ms)

    def _exit(self, symbol: str, price: float, reason: str) -> None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return
        now_ms = self._now_ms()
        info = self._infos.get(symbol) or MarketInfo(symbol, taker_fee=self.fallback_fee)
        ctx = RiskContext(self.cash, pos.cost, pos.cost, pos.units, False, info, now_ms)
        res = self.risk.approve(OrderIntent(symbol, "sell", pos.units, price, f"pulse {reason}"), ctx)
        if isinstance(res, Rejection):
            log.error("pulse exit rejected: %s", res.reason)
            return
        fill = self.executor.execute(res, price, now_ms)
        assert fill is not None
        proceeds = fill.notional - fill.fee
        pnl = proceeds - pos.cost
        self.cash += proceeds
        self.trades += 1
        self.trade_pnl += pnl
        self._roll_day(now_ms)
        self.day_realized += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.paused_until_ms = now_ms + self.cfg.pause_minutes * 60_000
            self.message = f"Paused {self.cfg.pause_minutes} min after {self.consecutive_losses} consecutive losses."
            self.store.audit("pulse", self.message, level="WARNING")
        self.store.insert_trade(mode=MODE, exchange=self.exchange_id, symbol=symbol, side="sell", ts_ms=now_ms, price=price, fill_price=fill.price, amount=fill.amount, fee=fill.fee, fee_currency=symbol.partition("/")[2], slippage=fill.slippage_pct, reason=f"pulse {reason}", pnl=pnl, status="filled")
        self.store.update_pulse_event(pos.event_id, trade_pnl=pnl)
        self.sym[symbol].state = "monitor"
        self.sym[symbol].note = f"exited ({reason}) P&L {pnl:+.2f}"
        log.info("PULSE SELL %s %.6f @ %.2f (%s) pnl %+.2f", symbol, fill.amount, fill.price, reason, pnl)
        self._publish(force=True)

    def _track_pending(self, symbol: str, price: float, now_ms: int) -> None:
        done = []
        for pe in self.pending:
            if pe.symbol != symbol:
                continue
            sign = 1.0 if pe.direction == "long" else -1.0
            move = sign * (price / pe.entry_mid - 1.0) * 100.0
            pe.mfe = max(pe.mfe, move)
            pe.mae = min(pe.mae, move)
            outcome = None
            if move >= pe.target_pct:
                outcome = "target"
            elif move <= -pe.stop_pct:
                outcome = "stop"
            elif now_ms - pe.start_ms >= pe.horizon_ms:
                outcome = "expired"
            if outcome:
                self.store.update_pulse_event(pe.event_id, outcome=outcome, mfe_pct=pe.mfe, mae_pct=pe.mae, resolved_ts_ms=now_ms)
                self.memory.add(pe.direction, pe.vec, outcome == "target")
                self.emit(PulseEventRecord(pe.event_id, pe.start_ms, symbol, pe.direction, 0.0, "", "", outcome, pe.mfe, pe.mae, 0, 0.0, 0.0))
                done.append(pe)
        for pe in done:
            self.pending.remove(pe)

    # ----------------------------------------------------------------- misc

    def _roll_day(self, now_ms: int) -> None:
        key = datetime.fromtimestamp(now_ms / 1000, self._tz).date().isoformat()
        if key != self.day_key:
            self.day_key = key
            self.day_realized = 0.0

    def _daily_loss_hit(self) -> bool:
        self._roll_day(self._now_ms())
        return self.day_realized <= -self.cfg.budget * self.cfg.daily_loss_pct / 100.0

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def equity(self) -> float:
        return self.cash + sum(p.units * (self.sym[s].feats.last_mid or p.entry_px) for s, p in self.positions.items())

    def status(self) -> PulseStatusEvent:
        snaps = []
        for s, st in self.sym.items():
            o, sc = st.last_obs, st.last_scores
            if o is None or sc is None:
                snaps.append(PulseSnapshot(s, self._now_ms(), "warming" if self.running else "off", 50, 50, 50, 50, 50, 50, 50, 0.0, st.feats.last_mid, 0.0, st.feats.vol_pct, st.note))
            else:
                L, S = sc
                snaps.append(PulseSnapshot(s, o.ts_ms, st.state if self.running else "off", L.prs, S.prs, L.f, L.a, L.w, L.r, L.c, o.imb60, o.mid, o.spread_pct, o.vol_pct, st.note))
        # overall EV at current costs using the memory's long hit rate (or the prior)
        sym0 = self.symbols[0] if self.symbols else ""
        cost = self._cost_pct(sym0) if sym0 else 0.3
        obs0 = self.sym[sym0].last_obs if sym0 and self.sym[sym0].last_obs else None
        target, stop = self._exit_levels(obs0) if obs0 else (self.cfg.target_pct, self.cfg.stop_pct)
        n_res = self.memory.resolved("long")
        p = self.memory.hit_rate("long") if n_res >= self.cfg.min_similar else self.cfg.prior_p
        return PulseStatusEvent(
            self.running, self.cfg.budget, self.equity(), len(self.positions), self.events_recorded, self.memory.resolved(),
            self.memory.hit_rate("long"), break_even_win_rate(target, stop, cost), expected_value_pct(p, target, stop, cost),
            self.trades, self.trade_pnl, self.consecutive_losses, self.paused_until_ms, self.message, snaps,
            stop, target, cost,
        )

    def _publish(self, force: bool = False) -> None:
        now = self._now_ms()
        if not force and now - self._last_status_ms < 2000:
            return
        self._last_status_ms = now
        self.emit(self.status())
