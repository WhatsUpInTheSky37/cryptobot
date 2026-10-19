"""The Pulse engine: validate data → observe → score → detect → record → (paper) trade → learn.

One scanner sees every coin. For each coin it checks data health first; a coin
with stale, unsynced or insufficient data produces no scores and no signals.
Healthy coins are observed every few seconds, scored long and short, and when a
pressure setup triggers, one Market Memory event is recorded (immutable
signal-time values) and handed to the three paper-trading modes, which decide
independently. Every event, taken or not, is followed forward for 20 minutes
with the same cost assumptions so the memory learns from everything.

Paper only. Spot cannot short, so bearish setups are research only. All
orders go through the shared RiskManager and the SimulatedExecutor.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from cryptobot.core.events import (
    BarsEvent,
    BookEvent,
    BookSyncEvent,
    ConnectionEvent,
    SymbolDroppedEvent,
    HeartbeatEvent,
    MarketInfoEvent,
    PulseEventRecord,
    PulseModeStatus,
    PulseSnapshot,
    PulseStatusEvent,
    TickEvent,
)
from cryptobot.core.models import Bar
from cryptobot.db.store import Store
from cryptobot.engine.risk import MarketInfo, Rejection
from cryptobot.pulse import ml as mlmod
from cryptobot.pulse.config import PulseConfig, break_even_win_rate, expected_value_pct
from cryptobot.pulse.costs import assess, expected_move_pct, plan_exits, round_trip_costs, slippage_per_side_pct
from cryptobot.pulse.features import Observation, Scores, SymbolFeatures
from cryptobot.pulse.health import HealthStatus, SymbolHealth
from cryptobot.pulse.memory import KEYS, Estimate, MarketMemory, row_from_event
from cryptobot.pulse.outcomes import OutcomeTracker, Pending
from cryptobot.pulse.paper import PaperTrader, Setup
from cryptobot.pulse.regime import Regime, classify

log = logging.getLogger("pulse")
MODE = "PULSE"
MOMENTUM_TRIGGER = 70.0  # order-flow acceleration score that triggers a decision without a breakout


@dataclass
class _DirState:
    state: str = "idle"  # idle | monitor | prepared
    prepared_ms: int = 0
    absorb_level: float = 0.0
    last_event_ms: dict[str, int] = field(default_factory=lambda: {"confirmed": 0, "momentum": 0})
    sent: set[str] = field(default_factory=set)  # trigger kinds already signalled in this prepared episode


@dataclass
class _SymState:
    feats: SymbolFeatures
    health: SymbolHealth
    dirs: dict[str, _DirState] = field(default_factory=lambda: {"long": _DirState(), "short": _DirState()})
    status: HealthStatus = field(default_factory=lambda: HealthStatus("no_data", "no public trades received yet"))
    last_obs: Observation | None = None
    last_scores: tuple[Scores, Scores] | None = None
    bars_1m: list[Bar] = field(default_factory=list)
    regime: Regime = field(default_factory=lambda: Regime("unknown", 0.0, 1.0, 0))
    note: str = ""
    assessment: dict[str, dict[str, Any]] = field(default_factory=dict)  # direction -> display assessment
    last_bid: float = 0.0
    last_ask: float = 0.0
    last_bid_depth: float = 0.0
    book_seq: int = 0

    @property
    def state(self) -> str:
        if not self.status.ok:
            return "warming" if self.status.status == "insufficient_data" else "excluded"
        order = {"prepared": 2, "monitor": 1, "idle": 0}
        return max((d.state for d in self.dirs.values()), key=lambda s: order.get(s, 0))


class PulseEngine:
    def __init__(
        self,
        store: Store,
        emit: Callable[[Any], None],
        exchange_id: str,
        symbols: list[str],
        config: PulseConfig,
        *,
        slippage_pct: float | None = None,
        clock: Callable[[], float] = time.time,
        timezone: str = "America/New_York",
        fallback_fee: float = 0.001,
    ) -> None:
        self.store = store
        self.emit = emit
        self.exchange_id = exchange_id
        self.symbols = list(symbols)
        self.cfg = config
        if not self.cfg.wallet_quote and self.symbols:
            self.cfg.wallet_quote = self.symbols[0].partition("/")[2]
        if slippage_pct is not None:
            # the Market page's slippage setting can raise the assumption, never zero it: a fill always costs something
            self.cfg.costs.base_slippage_pct = max(float(slippage_pct), self.cfg.costs.base_slippage_pct)
        self.clock = clock
        self.timezone = timezone
        self.fallback_fee = fallback_fee
        self.weights = config.weights_normalized()
        self._infos: dict[str, MarketInfo] = {}
        self.sym: dict[str, _SymState] = {}
        for s in self.symbols:
            h = SymbolHealth(config.health)
            h.min_obs = config.min_obs
            h.obs_interval_s = config.obs_interval_s
            self.sym[s] = _SymState(SymbolFeatures(s, config.window_s, config.obs_interval_s, config.history_obs, config.min_obs), h)
        self.memory = MarketMemory(store, config.k_similar, config.min_similar, config.prior_p, max(config.overlap_ms, config.max_hold_s * 1000), int(config.min_evidence_span_h * 3_600_000))
        self.tracker = OutcomeTracker(store, self._on_resolved)
        self.modes: dict[str, PaperTrader] = {
            m.key: PaperTrader(m, config, store, exchange_id, clock_ms=self._now_ms, timezone=timezone, fallback_fee=fallback_fee) for m in config.modes
        }
        self.model: mlmod.PulseModel | None = None
        raw = store.get_setting("pulse.ml.model.v2")
        if raw:
            self.model = mlmod.PulseModel.from_json(raw)
        self.ml_note = store.get_setting("pulse.ml.note", "") or ""
        self.running = False
        self.connected = False
        self.events_recorded = store.get_setting("pulse.events_recorded", 0) or 0
        self.message = ""
        self._last_obs_ms = 0
        self._last_status_ms = 0
        self._last_minute_ms = 0
        self._last_train_ms = 0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.running = True
        now = self._now_ms()
        restored, abandoned = self.tracker.restore(now)
        for key, m in self.modes.items():
            m.restore(self.store.get_setting(f"pulse.mode.{self.exchange_id}.{key}.state"))
            for s in m.positions:
                if s not in self.sym:
                    h = SymbolHealth(self.cfg.health)
                    h.min_obs, h.obs_interval_s = self.cfg.min_obs, self.cfg.obs_interval_s
                    self.sym[s] = _SymState(SymbolFeatures(s, self.cfg.window_s, self.cfg.obs_interval_s, self.cfg.history_obs, self.cfg.min_obs), h)
                    self.symbols.append(s)
        self.message = "Validating market data: scores need a few minutes of healthy history per coin."
        if restored or abandoned:
            self.message += f" Resumed {restored} pending setup(s); {abandoned} abandoned (not watched)."
        self.store.audit("pulse", "started", symbols=self.symbols, config=self.cfg.model_dump(), restored=restored, abandoned=abandoned)
        self._publish(force=True)

    def stop(self) -> None:
        now = self._now_ms()
        for m in self.modes.values():
            m.close_all("stopped", self._info, now)
            m.save()
        self.tracker.flush(now)
        self.running = False
        self.store.audit("pulse", "stopped")
        self._publish(force=True)

    # --------------------------------------------------------------- events

    def on_event(self, ev: Any) -> None:
        if getattr(ev, "exchange", self.exchange_id) != self.exchange_id:
            return
        if isinstance(ev, ConnectionEvent):
            self.connected = ev.status == "connected"
            for st in self.sym.values():
                st.health.set_connected(self.connected)
            if not self.running:
                return
            if self.connected:
                self.message = f"Connected to {self.exchange_id}. Warming up: each coin needs {self.cfg.min_obs} observations (about {self.cfg.min_obs * self.cfg.obs_interval_s // 60} min) of healthy data before it is scored."
            else:
                self.message = f"Exchange connection {ev.status}: no signals until it is back."
                for st in self.sym.values():
                    st.status = HealthStatus("disconnected", "exchange connection is down")
                    st.last_scores, st.assessment = None, {}
                    for d in st.dirs.values():
                        d.state, d.sent = "idle", set()
            self._publish(force=True)
            return
        if not self.running:
            return
        if isinstance(ev, MarketInfoEvent) and ev.symbol in self.sym:
            self._infos[ev.symbol] = MarketInfo(ev.symbol, ev.taker_fee if ev.taker_fee is not None else self.fallback_fee, ev.maker_fee, ev.min_amount, ev.min_cost, ev.amount_step, ev.price_step)
        elif isinstance(ev, TickEvent) and ev.tick.symbol in self.sym:
            st = self.sym[ev.tick.symbol]
            st.feats.add_tick(ev.tick)
            st.health.on_tick(ev.tick.ts_ms)
            if st.last_bid == 0:
                st.last_bid = st.last_ask = ev.tick.price
            self._on_price(ev.tick.symbol, ev.tick.price, ev.tick.ts_ms)
        elif isinstance(ev, BookEvent) and ev.symbol in self.sym:
            st = self.sym[ev.symbol]
            st.health.on_book(ev.ts_ms, ev.synced, ev.seq)
            st.book_seq = ev.seq
            if ev.synced:
                st.feats.add_book(ev)
                st.last_bid, st.last_ask, st.last_bid_depth = ev.best_bid, ev.best_ask, ev.bid_depth_near
                self._on_price(ev.symbol, ev.mid, ev.ts_ms)
        elif isinstance(ev, SymbolDroppedEvent) and ev.symbol in self.sym:
            st = self.sym[ev.symbol]
            st.health.unlisted = ev.reason + " (check the spelling on the Pulse settings tab)"
            st.status = st.health.evaluate(self._now_ms())
            st.last_scores, st.assessment = None, {}
            self._publish(force=True)
        elif isinstance(ev, BookSyncEvent) and ev.symbol in self.sym:
            self.sym[ev.symbol].health.on_sync(ev.status, ev.detail)
        elif isinstance(ev, BarsEvent) and ev.timeframe_s == 60 and ev.symbol in self.sym:
            st = self.sym[ev.symbol]
            bars = {b.ts_ms: b for b in st.bars_1m}
            for b in ev.bars:
                bars[b.ts_ms] = b
            st.bars_1m = [bars[k] for k in sorted(bars)][-120:]
            closed = [b for b in st.bars_1m if b.closed][-14:]
            if closed:
                st.feats.vol_pct = sum((b.high - b.low) / b.close * 100.0 for b in closed if b.close) / len(closed)
            st.regime = classify(st.bars_1m)
        elif isinstance(ev, HeartbeatEvent):
            self._tick_clock(ev.ts_ms)

    def _tick_clock(self, now_ms: int) -> None:
        if now_ms - self._last_obs_ms >= self.cfg.obs_interval_s * 1000:
            self._last_obs_ms = now_ms
            self._observe_all(now_ms)
        if now_ms - self._last_minute_ms >= 60_000:
            self._last_minute_ms = now_ms
            for m in self.modes.values():
                m.record_equity(now_ms)
                m.save()
            self.tracker.flush(now_ms)
            self._maybe_train(now_ms)
        self._publish()

    # ---------------------------------------------------------- observation

    def _related_imb(self, symbol: str, now_ms: int) -> float | None:
        base = symbol.partition("/")[0]
        pref = ["BTC", "ETH"] if base not in ("BTC", "ETH") else (["ETH"] if base == "BTC" else ["BTC"])
        healthy = [(s, st) for s, st in self.sym.items() if s != symbol and st.feats.last_mid and st.status.status not in ("disconnected", "no_data", "stale_trades", "inactive")]
        for s, st in healthy:
            if s.partition("/")[0] in pref:
                return st.feats.imbalance(now_ms)
        for _s, st in healthy:
            return st.feats.imbalance(now_ms)
        return None

    def _observe_all(self, now_ms: int) -> None:
        for symbol, st in self.sym.items():
            st.health.observations = st.feats.observations
            st.health.min_obs = self.cfg.min_obs
            hs = st.health.evaluate(now_ms)
            fresh = hs.status not in ("disconnected", "no_data", "stale_trades", "stale_book", "no_book", "book_unsynced", "inactive")
            if not fresh:
                # do not pollute the rolling distributions with stale readings
                st.status, st.last_obs, st.last_scores, st.assessment = hs, None, None, {}
                st.note = hs.detail
                for d in st.dirs.values():
                    d.state = "idle"
                continue
            rel = self._related_imb(symbol, now_ms)
            obs = st.feats.observe(now_ms, rel)
            if obs is None:
                st.status = HealthStatus("no_data", "no price yet")
                continue
            st.health.observations = st.feats.observations
            hs = st.health.evaluate(now_ms)
            st.status = hs
            if not hs.ok:
                st.last_obs, st.last_scores, st.assessment = obs, None, {}
                st.note = hs.detail
                continue
            sc = st.feats.scores(obs, self.weights, rel is not None)
            if sc is None:  # cannot happen once health says ok, but never fall through to placeholders
                st.last_scores = None
                st.note = "INSUFFICIENT DATA"
                continue
            long_, short = sc
            st.last_obs, st.last_scores = obs, (long_, short)
            st.assessment = {"long": self._display_assessment(symbol, obs, long_, "long", st.regime.name), "short": self._display_assessment(symbol, obs, short, "short", st.regime.name)}
            self._step(symbol, st, obs, short, "short", now_ms)
            self._step(symbol, st, obs, long_, "long", now_ms)

    # ---------------------------------------------------------- detection

    def _step(self, symbol: str, st: _SymState, obs: Observation, sc: Scores, direction: str, now_ms: int) -> None:
        """Per-direction state machine: idle → monitor → prepared → (signal) → monitor.

        A prepared episode may produce at most one *momentum* signal (flow accelerating
        or PRS at the entry level before any breakout) and one *confirmed* signal (price
        clears the absorption level with flow still in the direction). Each kind has its
        own cooldown so an early momentum signal never hides the breakout behind it."""
        cfg = self.cfg
        d = st.dirs[direction]
        if d.state in ("idle", "monitor"):
            if sc.prs >= cfg.prepare_prs:
                d.state = "prepared"
                d.prepared_ms = now_ms
                d.sent = set()
                d.absorb_level = st.feats.recent_high_mid(now_ms, 10) if direction == "long" else st.feats.recent_low_mid(now_ms, 10)
                if direction == "long":
                    st.note = f"prepared: waiting for price above {d.absorb_level:,.2f} with buying still positive"
            else:
                d.state = "monitor" if sc.prs >= cfg.monitor_prs else "idle"
            return
        # prepared
        if sc.prs < cfg.prepare_prs and now_ms - d.prepared_ms > cfg.prepared_ttl_s * 1000:
            d.state = "monitor"
            d.sent = set()
            if direction == "long":
                st.note = "pressure faded without confirmation"
            return
        if direction == "long":
            confirmed = obs.mid > d.absorb_level and obs.imb60 > 0
        else:
            confirmed = obs.mid < d.absorb_level and obs.imb60 < 0
        kind = "confirmed" if confirmed else "momentum" if (sc.acc >= MOMENTUM_TRIGGER or sc.prs >= cfg.enter_prs) else None
        if kind is None or kind in d.sent or now_ms - d.last_event_ms[kind] < cfg.event_cooldown_s * 1000:
            return
        d.sent.add(kind)
        d.last_event_ms[kind] = now_ms
        self._signal(symbol, st, obs, sc, direction, now_ms, confirmed, d.absorb_level)
        if confirmed:  # the release happened; a new episode needs new pressure
            d.state = "monitor"
            d.sent = set()

    # ---------------------------------------------------------- recording

    def _vec(self, sc: Scores, obs: Observation) -> dict[str, float]:
        v = sc.as_dict()
        v.update({"vol_pct": obs.vol_pct, "spread_pct": obs.spread_pct, "imbalance": obs.imb60})
        return {k: float(v[k]) for k in KEYS}

    def _info(self, symbol: str) -> MarketInfo:
        return self._infos.get(symbol) or MarketInfo(symbol, taker_fee=self.fallback_fee)

    def _fee_rate(self, symbol: str) -> float:
        o = self.cfg.costs.fee_rate_override
        return o if o is not None else (self._info(symbol).taker_fee or self.fallback_fee)

    def _reference_plan(self, symbol: str, obs: Observation, est: Estimate) -> tuple[float, float, float, Any]:
        """(target, stop, expected move, costs) for a reference-size trade, used for the record and the display."""
        c = self.cfg.costs
        move = expected_move_pct(obs.vol_pct, c.hold_minutes_for_move)
        target, stop = plan_exits(obs.vol_pct, move, stop_vol_mult=self.cfg.stop_vol_mult, min_stop_pct=self.cfg.min_stop_pct, max_stop_pct=self.cfg.max_stop_pct,
                                  target_capture=c.target_capture, max_target_pct=c.max_target_pct, adaptive=self.cfg.adaptive, fixed_target=self.cfg.target_pct, fixed_stop=self.cfg.stop_pct)
        stop = max(stop, self.cfg.stop_spread_mult * obs.spread_pct)
        notional = min(self.cfg.budget * self.cfg.risk.risk_per_trade_pct / 100.0 / (stop / 100.0) if stop > 0 else 0.0, self.cfg.budget * self.cfg.risk.per_position_pct / 100.0)
        costs = round_trip_costs(self._fee_rate(symbol), obs.spread_pct, notional, obs.bid_depth, obs.ask_depth, band_pct=c.impact_band_pct, base_slip_pct=c.base_slippage_pct)
        return target, stop, move, costs

    def _estimate(self, direction, vec, regime, symbol, obs, min_n=None):
        target, stop, _, _ = self._reference_plan(symbol, obs, None)
        return self.memory.estimate(direction, vec, regime, min_n=min_n, target_pct=target, stop_pct=stop,
                                    horizon_ms=self.cfg.max_hold_s * 1000, policy_version="bidask-v2", exchange=self.exchange_id)

    def _p_for_ev(self, est: Estimate) -> float | None:
        """The probability the expected value is computed from: the lower confidence bound when conservative."""
        if not est.sufficient:
            return None
        return est.ci_low if self.cfg.conservative_p and est.ci_low is not None else est.p_target

    def _display_assessment(self, symbol: str, obs: Observation, sc: Scores, direction: str, regime: str) -> dict[str, Any]:
        est = self._estimate(direction, self._vec(sc, obs), regime, symbol, obs)
        target, stop, move, costs = self._reference_plan(symbol, obs, est)
        a = assess(target, stop, costs, self._p_for_ev(est), min_ev_pct=self.cfg.ev_margin_pct, min_rr=1.0, min_target_cost_mult=self.cfg.min_target_cost_mult)
        return {"prs": sc.prs, "target_pct": target, "stop_pct": stop, "expected_move_pct": move, "cost_pct": costs.total_pct, "fee_pct": costs.fee_pct, "spread_pct": costs.spread_pct,
                "slippage_pct": costs.slippage_pct, "p_target": est.p_target, "ev_pct": a.ev_pct, "rr": a.rr_after_costs, "break_even_p": a.break_even_p, "verdict": a.verdict,
                "reason": a.reason, "evidence": est.note, "n_effective": est.n_effective, "ci": (est.ci_low, est.ci_high)}

    def _signal(self, symbol: str, st: _SymState, obs: Observation, sc: Scores, direction: str, now_ms: int, confirmed: bool, absorb: float) -> int:
        vec = self._vec(sc, obs)
        est = self._estimate(direction, vec, st.regime.name, symbol, obs)
        target, stop, move, costs = self._reference_plan(symbol, obs, est)
        a = assess(target, stop, costs, self._p_for_ev(est), min_ev_pct=self.cfg.ev_margin_pct, min_rr=1.0, min_target_cost_mult=self.cfg.min_target_cost_mult)
        status = "signal" if direction == "long" else "research"
        features = {"raw_long": obs.raw_long, "raw_short": obs.raw_short, "scores": sc.as_dict(), "vol60_quote": obs.vol60_quote, "bid": obs.bid, "ask": obs.ask}
        entry_depth = obs.ask_depth if direction == "long" else obs.bid_depth
        entry_slip = slippage_per_side_pct(costs.notional, entry_depth, self.cfg.costs.impact_band_pct, self.cfg.costs.base_slippage_pct)
        label_entry = obs.ask * (1 + entry_slip / 100) if direction == "long" else obs.bid * (1 - entry_slip / 100)
        exit_depth = obs.bid_depth if direction == "long" else obs.ask_depth
        label_cost = costs.fee_pct + slippage_per_side_pct(costs.notional, exit_depth, self.cfg.costs.impact_band_pct, self.cfg.costs.base_slippage_pct)
        eid = self.store.insert_pulse_event(
            ts_ms=now_ms, exchange=self.exchange_id, symbol=symbol, direction=direction, prs=sc.prs, f=sc.f, a=sc.a, w=sc.w, r=sc.r, c=sc.c, acc=sc.acc, vpr=sc.vpr, sx=sc.sx, vx=sc.vx,
            imbalance=obs.imb60, vol_pct=obs.vol_pct, spread_pct=obs.spread_pct, mid=obs.mid, bid=obs.bid, ask=obs.ask, bid_depth=obs.bid_depth, ask_depth=obs.ask_depth,
            target_pct=target, stop_pct=stop, cost_pct=costs.total_pct, fee_pct=costs.fee_pct, spread_cost_pct=costs.spread_pct, slip_pct=costs.slippage_pct, expected_move_pct=move,
            rr_after_costs=a.rr_after_costs or 0.0, similar_n=est.n, similar_eff=est.n_effective, similar_p=est.p_target or 0.0, p_stop=est.p_stop or 0.0, ci_low=est.ci_low or 0.0,
            ci_high=est.ci_high or 0.0, ev_pct=a.ev_pct if a.ev_pct is not None else 0.0, evidence="sufficient" if est.sufficient else "insufficient", regime=st.regime.name,
            health=st.status.status, confirmed=confirmed, status=status, reason=a.reason if direction == "long" else "bearish setup recorded for research (spot cannot short)",
            features_json=json.dumps(features, default=float), horizon_ms=self.cfg.max_hold_s * 1000, policy_version="bidask-v2", label_entry=label_entry,
        )
        self.tracker.add(Pending(eid, symbol, direction, now_ms, label_entry, target, stop, label_cost, self.cfg.max_hold_s * 1000))
        self.events_recorded += 1
        self.store.set_setting("pulse.events_recorded", self.events_recorded)
        decisions: dict[str, tuple[bool, str]] = {}
        if direction == "long":
            setup = Setup(symbol, direction, now_ms, obs, sc, st.regime.name, confirmed, st.status, vec, absorb, eid)
            decisions = self._run_modes(setup, now_ms)
            taken = [k for k, (ok, _r) in decisions.items() if ok]
            st.note = ("entered by " + ", ".join(taken)) if taken else "signal recorded; " + next(iter(decisions.values()))[1][:80] if decisions else "signal recorded"
        self.emit(PulseEventRecord(eid, now_ms, symbol, direction, sc.prs, status, a.reason, "pending", None, None, est.n_effective, est.p_target or 0.0, a.ev_pct or 0.0, st.regime.name, None, None, decisions))
        return eid

    def _run_modes(self, setup: Setup, now_ms: int) -> dict[str, tuple[bool, str]]:
        info = self._info(setup.symbol)
        cache: dict[int, Estimate] = {}

        def est_fn(min_n: int) -> Estimate:
            if min_n not in cache:
                cache[min_n] = self._estimate(setup.direction, setup.vec, setup.regime, setup.symbol, setup.obs, min_n=min_n)
            return cache[min_n]

        ml_p = None
        if self.model is not None and self.model.usable:
            like = dict(setup.vec)
            like["regime"] = setup.regime
            target, stop, _, _ = self._reference_plan(setup.symbol, setup.obs, None)
            like.update(target_pct=target, stop_pct=stop, horizon_ms=self.cfg.max_hold_s * 1000, direction=setup.direction)
            ml_p = self.model.p(like)
        out: dict[str, tuple[bool, str]] = {}
        for key, m in self.modes.items():
            d = m.evaluate(setup, info, est_fn, ml_p, now_ms)
            did = self.store.insert_decision(event_id=setup.event_id or 0, ts_ms=now_ms, mode=key, symbol=setup.symbol, direction=setup.direction, accepted=d.accepted, code=d.code, reason=d.reason)
            if d.accepted:
                res = m.enter(setup, d, info, did, now_ms)
                if isinstance(res, Rejection):
                    self.store.update_decision(did, accepted=False, code="risk_rejected", reason=res.reason)
                    out[key] = (False, res.reason)
                    continue
            out[key] = (d.accepted, d.reason)
        if any(ok for ok, _ in out.values()):
            self._publish(force=True)
        return out

    # --------------------------------------------------------- price path

    def _on_price(self, symbol: str, price: float, now_ms: int) -> None:
        st = self.sym[symbol]
        # Labels and paper exits use executable quotes, never an optimistic mid.
        long_price = st.last_bid or price
        short_price = st.last_ask or price
        for direction, executable in (("long", long_price), ("short", short_price)):
            pending = self.tracker.pending
            subset = [p for p in pending if p.direction == direction]
            other = [p for p in pending if p.direction != direction]
            self.tracker.pending = subset
            self.tracker.on_price(symbol, executable, now_ms)
            self.tracker.pending.extend(other)
        info = self._info(symbol)
        for m in self.modes.values():
            closed = m.on_price(symbol, price, st.last_bid or price, st.last_ask or price, st.last_bid_depth, info, now_ms)
            if closed:
                self._publish(force=True)

    def _on_resolved(self, p: Pending) -> None:
        e = self.store.pulse_event(p.event_id)
        if e:
            row = row_from_event(e)
            if row is not None:
                self.memory.add(row)
        self.emit(PulseEventRecord(p.event_id, p.start_ms, p.symbol, p.direction, 0.0, "", "", p.outcome(), p.mfe, p.mae, 0, 0.0, 0.0, "", p.net_pct(), p.first_hit))

    # ------------------------------------------------------------------ ML

    def _maybe_train(self, now_ms: int) -> None:
        if self.store.count_pulse_events(resolved_only=True) < self.cfg.ml_min_events:
            return
        last = self.model.report.trained_at_ms if self.model else self._last_train_ms
        if now_ms - last < self.cfg.ml_retrain_minutes * 60_000:
            return
        self.train_now(now_ms)

    def train_now(self, now_ms: int | None = None) -> str:
        now_ms = now_ms or self._now_ms()
        self._last_train_ms = now_ms
        events = [e for e in self.store.load_pulse_events(limit=100_000, resolved_only=True, ascending=True)
                  if e.get("policy_version") == "bidask-v2" and e.get("exchange") == self.exchange_id and e.get("horizon_ms") == self.cfg.max_hold_s * 1000]
        try:
            model = mlmod.train(events)
        except Exception as e:  # noqa: BLE001
            self.ml_note = f"training failed: {e}"
            self.store.set_setting("pulse.ml.note", self.ml_note)
            return self.ml_note
        rep = model.report
        self.ml_note = rep.reason + (" — model retained for Mode C" if rep.improved else " — model NOT retained (no out-of-sample improvement)" if rep.ok else "")
        if rep.ok and rep.improved:
            self.model = model
            self.store.set_setting("pulse.ml.model.v2", model.to_json())
        else:
            self.model = None
            self.store.set_setting("pulse.ml.model.v2", "")
        self.store.set_setting("pulse.ml.note", self.ml_note)
        self.store.set_setting("pulse.ml.report", rep.as_dict())
        self.store.audit("pulse", "ml trained", level="INFO", report=rep.as_dict())
        self._publish(force=True)
        return self.ml_note

    # ----------------------------------------------------------------- misc

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def equity(self) -> float:
        return sum(m.equity() for m in self.modes.values())

    def status(self) -> PulseStatusEvent:
        now = self._now_ms()
        snaps = []
        for s, st in self.sym.items():
            o, sc = st.last_obs, st.last_scores
            valid = st.status.ok and sc is not None and o is not None
            if not valid:
                snaps.append(PulseSnapshot(s, now, st.state if self.running else "off", 0.0, 0.0, 0, 0, 0, 0, 0, 0.0, st.feats.last_mid, 0.0, st.feats.vol_pct, st.note, False,
                                           st.status.status if self.running else "off", st.status.detail if self.running else "Pulse is not running", st.regime.name, {}, {}, {}, {},
                                           st.last_bid, st.last_ask, st.health.trades_per_min(now), st.book_seq))
                continue
            L, S = sc
            snaps.append(PulseSnapshot(s, o.ts_ms, st.state if self.running else "off", L.prs, S.prs, L.f, L.a, L.w, L.r, L.c, o.imb60, o.mid, o.spread_pct, o.vol_pct, st.note, True,
                                       "ok", "", st.regime.name, L.as_dict(), S.as_dict(), st.assessment.get("long", {}), st.assessment.get("short", {}),
                                       o.bid, o.ask, st.health.trades_per_min(now), st.book_seq))
        modes = [PulseModeStatus(**m.status(now)) for m in self.modes.values()]
        budget = sum(m.mode.budget for m in self.modes.values())
        equity = sum(ms.equity for ms in modes)
        trades = sum(ms.trades for ms in modes)
        pnl = sum(ms.net_pnl for ms in modes)
        # legacy summary line: reference costs for the first healthy coin
        ref = next((st.assessment.get("long") for st in self.sym.values() if st.assessment.get("long")), None)
        if ref:
            stop, target, cost = ref["stop_pct"], ref["target_pct"], ref["cost_pct"]
            p = ref["p_target"]
        else:
            stop, target, cost, p = self.cfg.stop_pct, self.cfg.target_pct, 0.0, None
        ok = sum(1 for st in self.sym.values() if st.status.ok)
        bad = [f"{s}: INSUFFICIENT DATA ({st.status.detail})" for s, st in self.sym.items() if not st.status.ok]
        health_summary = f"{ok}/{len(self.sym)} coins with valid data" + ("; " + "; ".join(bad[:3]) if bad else "")
        rates = [st.health.trades_per_min(now) for st in self.sym.values() if st.health.last_trade_ms]
        if self.connected and rates and max(rates) < 2.0 and self.running:
            health_summary += " · this exchange is thin (under 2 trades/min on every coin): order-flow scores need activity; Pulse is paper-only, so pick Coinbase or Kraken on the Market page for liquid data"
        priced = [st.assessment.get("long") for st in self.sym.values() if st.status.ok and st.assessment.get("long")]
        if self.running and priced and all(a["cost_pct"] * self.cfg.min_target_cost_mult > a["target_pct"] for a in priced):
            worst = max(a["cost_pct"] for a in priced)
            health_summary += (f" · round-trip costs (up to {worst:.2f}%: fees twice, spread, slippage) exceed the expected {self.cfg.costs.hold_minutes_for_move:g}-minute move on every coin, "
                               f"so no entry can pass the cost gate on {self.exchange_id} at this fee tier; that is the gate working, not a fault")
        mem = {"events": self.events_recorded, "pending": len(self.tracker.pending), "resolved": self.memory.resolved(), "resolved_long": self.memory.resolved("long"),
               "effective_long": self.memory.effective("long"), "hit_rate_long": self.memory.hit_rate("long"), "min_similar": self.cfg.min_similar}
        mlinfo = {"available": mlmod.HAVE_LGBM, "backend": "lightgbm" if mlmod.HAVE_LGBM else "logistic (numpy)", "usable": bool(self.model and self.model.usable), "note": self.ml_note,
                  "min_events": self.cfg.ml_min_events}
        return PulseStatusEvent(
            self.running, budget, equity, sum(ms.open_positions for ms in modes), self.events_recorded, self.memory.resolved(), self.memory.hit_rate("long"),
            break_even_win_rate(target, stop, cost), expected_value_pct(p, target, stop, cost) if p is not None else 0.0, trades, pnl,
            max((ms.consecutive_losses for ms in modes), default=0), max((ms.paused_until_ms for ms in modes), default=0), self.message, snaps, stop, target, cost,
            self.connected, modes, mem, mlinfo, health_summary,
        )

    def _publish(self, force: bool = False) -> None:
        now = self._now_ms()
        if not force and now - self._last_status_ms < 2000:
            return
        self._last_status_ms = now
        self.emit(self.status())

