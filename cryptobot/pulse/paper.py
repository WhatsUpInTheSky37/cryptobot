"""Paper-trading configurations (Conservative / Balanced / Experimental).

Each ``PaperTrader`` sees the same signals, keeps its own virtual portfolio,
applies its own thresholds, and runs every order through the shared
``RiskManager`` choke point with the shared cost model. Nothing in here can
place a real order: the executor is the simulated one and there is no
exchange object.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from cryptobot.db.store import Store
from cryptobot.engine.executors import SimulatedExecutor
from cryptobot.engine.risk import MarketInfo, OrderIntent, Rejection, RiskContext, RiskLimits, RiskManager
from cryptobot.pulse.config import CostConfig, ModeConfig, PulseConfig, RiskConfig
from cryptobot.pulse.costs import Assessment, assess, expected_move_pct, plan_exits, round_trip_costs, slippage_per_side_pct
from cryptobot.pulse.features import Observation, Scores
from cryptobot.pulse.health import HealthStatus
from cryptobot.pulse.memory import Estimate
from cryptobot.strategy.config import RiskRules

log = logging.getLogger("pulse.paper")


@dataclass
class Setup:
    """A scored opportunity handed to every mode."""

    symbol: str
    direction: str
    ts_ms: int
    obs: Observation
    scores: Scores
    regime: str
    confirmed: bool  # price cleared the absorption range with flow still positive
    health: HealthStatus
    vec: dict[str, float]
    absorb_level: float = 0.0
    event_id: int | None = None


@dataclass
class Decision:
    accepted: bool
    code: str
    reason: str
    assessment: Assessment | None = None
    estimate: Estimate | None = None
    notional: float = 0.0
    p_source: str = ""  # memory | ml | none


@dataclass
class PaperPosition:
    symbol: str
    units: float
    entry_px: float
    cost: float  # notional + entry fee
    stop: float
    target: float
    entry_ms: int
    event_id: int
    decision_id: int
    risk_usd: float
    target_pct: float
    stop_pct: float
    max_hold_ms: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperTrader:
    def __init__(
        self,
        mode: ModeConfig,
        cfg: PulseConfig,
        store: Store,
        exchange_id: str,
        *,
        clock_ms: Callable[[], int],
        timezone: str = "America/New_York",
        fallback_fee: float = 0.001,
        emit: Callable[[Any], None] | None = None,
    ) -> None:
        self.mode = mode
        self.cfg = cfg
        self.risk_cfg: RiskConfig = cfg.risk
        self.cost_cfg: CostConfig = cfg.costs
        self.store = store
        self.exchange_id = exchange_id
        self.clock_ms = clock_ms
        self._tz = ZoneInfo(timezone)
        self.fallback_fee = fallback_fee
        self.emit = emit or (lambda ev: None)
        self.trade_mode = f"PULSE-{exchange_id}-{mode.key}"
        self.risk = RiskManager(RiskLimits(budget=mode.budget, per_position_pct=self.risk_cfg.per_position_pct, daily_loss_pct=self.risk_cfg.daily_loss_pct, max_drawdown_pct=100, timezone=timezone), RiskRules(stop_atr_mult=0), self.cost_cfg.base_slippage_pct)
        # state
        self.cash = mode.budget
        self.positions: dict[str, PaperPosition] = {}
        self.day_key = ""
        self.day_start_equity = mode.budget
        self.realized_today = 0.0
        self.consecutive_losses = 0
        self.paused_until_ms = 0
        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        self.fees_paid = 0.0
        self.equity_peak = mode.budget
        self.accepted = 0
        self.rejected = 0
        self.message = ""
        self._marks: dict[str, tuple[float, float, float]] = {}  # symbol -> (bid, ask, bid_depth)

    # ------------------------------------------------------------- state

    def to_state(self) -> dict[str, Any]:
        return {
            "cash": self.cash, "positions": {s: p.as_dict() for s, p in self.positions.items()}, "day_key": self.day_key, "day_start_equity": self.day_start_equity,
            "realized_today": self.realized_today, "consecutive_losses": self.consecutive_losses, "paused_until_ms": self.paused_until_ms, "trades": self.trades,
            "wins": self.wins, "losses": self.losses, "gross_profit": self.gross_profit, "gross_loss": self.gross_loss, "fees_paid": self.fees_paid,
            "equity_peak": self.equity_peak, "accepted": self.accepted, "rejected": self.rejected, "budget": self.mode.budget,
            "exchange": self.exchange_id,
        }

    def restore(self, st: dict[str, Any] | None) -> None:
        if st and st.get("exchange") not in (None, self.exchange_id):
            raise ValueError("paper book belongs to another exchange")
        if st and st.get("budget") != self.mode.budget and st.get("positions"):
            raise ValueError("close the existing paper book before changing its budget")
        if not st or st.get("budget") != self.mode.budget:
            return  # a budget change starts a fresh book
        self.cash = float(st.get("cash", self.cash))
        self.positions = {s: PaperPosition(**p) for s, p in (st.get("positions") or {}).items()}
        for k in ("day_key",):
            setattr(self, k, st.get(k, getattr(self, k)))
        for k in ("day_start_equity", "realized_today", "gross_profit", "gross_loss", "fees_paid", "equity_peak"):
            setattr(self, k, float(st.get(k, getattr(self, k))))
        for k in ("consecutive_losses", "paused_until_ms", "trades", "wins", "losses", "accepted", "rejected"):
            setattr(self, k, int(st.get(k, getattr(self, k))))

    def save(self) -> None:
        self.store.set_setting(f"pulse.mode.{self.exchange_id}.{self.mode.key}.state", self.to_state())

    # ----------------------------------------------------------- marking

    def mark(self, symbol: str, bid: float, ask: float, bid_depth: float) -> None:
        if bid > 0 and ask > 0:
            self._marks[symbol] = (bid, ask, bid_depth)

    def _mark_px(self, symbol: str, fallback: float) -> float:
        m = self._marks.get(symbol)
        return m[0] if m else fallback

    def equity(self) -> float:
        return self.cash + sum(p.units * self._mark_px(s, p.entry_px) for s, p in self.positions.items())

    def unrealized(self) -> float:
        return sum(p.units * self._mark_px(s, p.entry_px) - p.cost for s, p in self.positions.items())

    def in_use(self) -> float:
        return sum(p.cost for p in self.positions.values())

    def open_risk_usd(self) -> float:
        return sum(max(0.0, (p.entry_px - p.stop) * p.units) for p in self.positions.values())

    def open_risk_pct(self) -> float:
        eq = self.equity()
        return self.open_risk_usd() / eq * 100.0 if eq > 0 else 0.0

    def daily_pnl_incl_open(self) -> float:
        return self.equity() - self.day_start_equity

    def daily_loss_hit(self, now_ms: int) -> bool:
        self._roll_day(now_ms)
        return self.daily_pnl_incl_open() <= -self.day_start_equity * self.risk_cfg.daily_loss_pct / 100.0

    def _roll_day(self, now_ms: int) -> None:
        key = datetime.fromtimestamp(now_ms / 1000, self._tz).date().isoformat()
        if key != self.day_key:
            self.day_key = key
            self.realized_today = 0.0
            self.day_start_equity = self.equity()

    # --------------------------------------------------------- evaluation

    def evaluate(self, setup: Setup, info: MarketInfo, estimate_fn: Callable[[int], Estimate], ml_p: float | None, now_ms: int) -> Decision:
        """Decide on a setup. ``estimate_fn(min_n)`` returns the memory estimate for this mode's
        evidence requirement; ``ml_p`` is a validated model probability (None if none)."""
        m = self.mode
        if self.cfg.wallet_quote and setup.symbol.partition("/")[2] != self.cfg.wallet_quote:
            return self._rej("wallet_quote_mismatch", f"this paper book is funded in {self.cfg.wallet_quote}")
        sc = setup.scores
        if not setup.health.ok:
            return self._rej("stale_data" if setup.health.status in ("stale_trades", "stale_book") else "insufficient_data", setup.health.detail or setup.health.label)
        if setup.direction != "long":
            return self._rej("spot_no_short", "spot accounts cannot short; bearish setups are research only")
        if setup.symbol in self.positions:
            return self._rej("already_in_position", "already holding this coin (no averaging down, no adding)")
        if setup.regime not in m.allowed_regimes:
            return self._rej("regime_excluded", f"regime {setup.regime} not traded by this mode")
        if sc.prs < m.enter_prs:
            return self._rej("weak_signal", f"PRS {sc.prs:.0f} below {m.enter_prs:g}")
        if m.require_breakout and not setup.confirmed:
            return self._rej("no_confirmation", f"waiting for price above {setup.absorb_level:,.2f} with flow still positive")
        if sc.acc < m.min_acc:
            return self._rej("weak_momentum", f"order-flow acceleration {sc.acc:.0f} below {m.min_acc:g}")
        if setup.obs.spread_pct > m.max_spread_pct:
            return self._rej("spread_too_high", f"spread {setup.obs.spread_pct:.3f}% above {m.max_spread_pct:g}%")
        if setup.obs.vol_pct > m.max_vol_pct:
            return self._rej("volatility_too_high", f"1-minute range {setup.obs.vol_pct:.2f}% above {m.max_vol_pct:g}%")
        if now_ms < self.paused_until_ms:
            return self._rej("paused_losses", f"paused after {self.consecutive_losses} consecutive losses")
        if self.daily_loss_hit(now_ms):
            return self._rej("daily_loss_limit", f"today's loss incl. open positions {self.daily_pnl_incl_open():+.2f} reached {self.risk_cfg.daily_loss_pct:g}%")
        if len(self.positions) >= self.risk_cfg.max_positions:
            return self._rej("max_positions", f"already {len(self.positions)} open position(s)")
        # sizing: risk a fixed fraction of *equity* per trade; never scaled by streaks
        eq = self.equity()
        risk_usd = eq * self.risk_cfg.risk_per_trade_pct / 100.0
        if (self.open_risk_usd() + risk_usd) / eq * 100.0 > self.risk_cfg.max_open_risk_pct + 1e-9:
            return self._rej("open_risk_limit", f"open risk {self.open_risk_pct():.2f}% + {self.risk_cfg.risk_per_trade_pct:g}% exceeds {self.risk_cfg.max_open_risk_pct:g}%")
        est = estimate_fn(m.min_similar)
        move = expected_move_pct(setup.obs.vol_pct, self.cost_cfg.hold_minutes_for_move)
        target, stop = plan_exits(setup.obs.vol_pct, move, stop_vol_mult=self.cfg.stop_vol_mult, min_stop_pct=self.cfg.min_stop_pct, max_stop_pct=self.cfg.max_stop_pct,
                                  target_capture=self.cost_cfg.target_capture, max_target_pct=self.cost_cfg.max_target_pct, adaptive=self.cfg.adaptive,
                                  fixed_target=self.cfg.target_pct, fixed_stop=self.cfg.stop_pct)
        floor = self.cfg.stop_spread_mult * setup.obs.spread_pct
        if floor > self.cfg.max_stop_pct:
            return self._rej("stop_inside_spread", f"a stop {self.cfg.stop_spread_mult:g} spreads away ({floor:.3f}%) would exceed the maximum stop {self.cfg.max_stop_pct:g}%: the spread is too wide to place a stop that the next print would not hit")
        stop = max(stop, floor)
        notional = risk_usd / (stop / 100.0) if stop > 0 else 0.0
        notional = min(notional, self.mode.budget * self.risk_cfg.per_position_pct / 100.0, self.cash)
        fee_rate = self.cost_cfg.fee_rate_override if self.cost_cfg.fee_rate_override is not None else (info.taker_fee or self.fallback_fee)
        costs = round_trip_costs(fee_rate, setup.obs.spread_pct, notional, setup.obs.bid_depth, setup.obs.ask_depth, band_pct=self.cost_cfg.impact_band_pct, base_slip_pct=self.cost_cfg.base_slippage_pct)
        p_src = "none"
        p: float | None = None
        if m.use_ml and ml_p is not None and est.sufficient:
            p, p_src = ml_p, "ml"
        elif est.sufficient:
            # conservative: the lower confidence bound, so thin evidence cannot look like a sure thing
            p, p_src = (est.ci_low if self.cfg.conservative_p and est.ci_low is not None else est.p_target), "memory"
        a = assess(target, stop, costs, p, min_ev_pct=m.min_ev_pct, min_rr=m.min_rr, min_target_cost_mult=self.cfg.min_target_cost_mult,
                   p_stop=est.p_stop if est.sufficient else None, expiry_pct=est.expiry_gross_pct if est.sufficient else None)
        if a.verdict in ("insufficient_evidence", "ev_below_margin", "rr_below_min") and not m.require_evidence:
            # the research book: it trades the signal at real costs so the equity curve shows whether the signal pays,
            # instead of waiting for history to say so. The cost gate still applies: a trade that cannot pay its costs is not research.
            self.accepted += 1
            return Decision(True, "ok", f"research book: trading without evidence ({a.reason if a.verdict != 'insufficient_evidence' else est.note}); costs {costs.total_pct:.3f}%", a, est, notional, p_src)
        if a.verdict != "ok":
            d = self._rej(a.verdict, a.reason if a.verdict != "insufficient_evidence" else est.note)
            d.assessment, d.estimate, d.notional, d.p_source = a, est, notional, p_src
            return d
        self.accepted += 1
        return Decision(True, "ok", a.reason + f"; {est.note if p_src == 'memory' else 'model p=%.0f%%' % (100 * (p or 0))}", a, est, notional, p_src)

    def _rej(self, code: str, reason: str) -> Decision:
        self.rejected += 1
        return Decision(False, code, reason)

    # ---------------------------------------------------------- execution

    def enter(self, setup: Setup, d: Decision, info: MarketInfo, decision_id: int, now_ms: int) -> PaperPosition | Rejection:
        assert d.accepted and d.assessment is not None
        a = d.assessment
        if self.cost_cfg.fee_rate_override is not None:
            from dataclasses import replace
            info = replace(info, taker_fee=self.cost_cfg.fee_rate_override)
        ask = setup.obs.ask or setup.obs.mid
        units = d.notional / ask if ask > 0 else 0.0
        ctx = RiskContext(self.cash, self.in_use(), 0.0, 0.0, False, info, now_ms)
        self.risk.slippage_pct = slippage_per_side_pct(d.notional, setup.obs.ask_depth, self.cost_cfg.impact_band_pct, self.cost_cfg.base_slippage_pct)
        res = self.risk.approve(OrderIntent(setup.symbol, "buy", units, ask, f"pulse {self.mode.key} entry", ask * (1 - a.stop_pct / 100)), ctx)
        if isinstance(res, Rejection):
            self.rejected += 1
            self.accepted -= 1
            return res
        slip = slippage_per_side_pct(res.notional, setup.obs.ask_depth, self.cost_cfg.impact_band_pct, self.cost_cfg.base_slippage_pct)
        fill = SimulatedExecutor(self.risk, slip).execute(res, ask, now_ms)
        assert fill is not None
        cost = fill.notional + fill.fee
        self.cash -= cost
        self.fees_paid += fill.fee
        pos = PaperPosition(setup.symbol, fill.amount, fill.price, cost, fill.price * (1 - a.stop_pct / 100), fill.price * (1 + a.target_pct / 100), now_ms,
                            setup.event_id or 0, decision_id, fill.price * fill.amount * a.stop_pct / 100, a.target_pct, a.stop_pct, self.cfg.max_hold_s * 1000)
        self.positions[setup.symbol] = pos
        self.store.insert_trade(mode=self.trade_mode, exchange=self.exchange_id, symbol=setup.symbol, side="buy", ts_ms=now_ms, price=ask, fill_price=fill.price, amount=fill.amount,
                                fee=fill.fee, fee_currency=setup.symbol.partition("/")[2], slippage=fill.slippage_pct, reason=f"pulse {self.mode.key} entry PRS {setup.scores.prs:.0f} (event {setup.event_id})", status="filled")
        self.message = f"{setup.symbol}: bought {fill.amount:.6f} @ {fill.price:,.2f}, stop {pos.stop:,.2f}, target {pos.target:,.2f}"
        log.info("PULSE-%s BUY %s %.6f @ %.2f (%s)", self.mode.key, setup.symbol, fill.amount, fill.price, d.reason)
        self.save()
        return pos

    def on_price(self, symbol: str, price: float, bid: float, ask: float, bid_depth: float, info: MarketInfo, now_ms: int) -> tuple[str, float] | None:
        """Check exits for one symbol. Returns (reason, pnl) when a position closed."""
        self.mark(symbol, bid, ask, bid_depth)
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        ref = min(price, bid) if bid > 0 else price
        if ref <= pos.stop:
            return self._exit(symbol, bid or price, bid_depth, info, "stop", now_ms)
        if max(price, bid) >= pos.target:
            return self._exit(symbol, bid or price, bid_depth, info, "target", now_ms)
        if now_ms - pos.entry_ms >= pos.max_hold_ms:
            return self._exit(symbol, bid or price, bid_depth, info, "expired", now_ms)
        return None

    def close_all(self, reason: str, info_for: Callable[[str], MarketInfo], now_ms: int) -> None:
        for s in list(self.positions):
            bid, _ask, depth = self._marks.get(s, (self.positions[s].entry_px, self.positions[s].entry_px, 0.0))
            self._exit(s, bid, depth, info_for(s), reason, now_ms)

    def _exit(self, symbol: str, bid: float, bid_depth: float, info: MarketInfo, reason: str, now_ms: int) -> tuple[str, float] | None:
        if self.cost_cfg.fee_rate_override is not None:
            from dataclasses import replace
            info = replace(info, taker_fee=self.cost_cfg.fee_rate_override)
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        ctx = RiskContext(self.cash, pos.cost, pos.cost, pos.units, False, info, now_ms)
        res = self.risk.approve(OrderIntent(symbol, "sell", pos.units, bid, f"pulse {self.mode.key} {reason}"), ctx)
        if isinstance(res, Rejection):
            log.error("pulse %s exit rejected: %s", self.mode.key, res.reason)
            self.positions[symbol] = pos
            return None
        slip = slippage_per_side_pct(pos.units * bid, bid_depth, self.cost_cfg.impact_band_pct, self.cost_cfg.base_slippage_pct)
        fill = SimulatedExecutor(self.risk, slip).execute(res, bid, now_ms)
        assert fill is not None
        proceeds = fill.notional - fill.fee
        pnl = proceeds - pos.cost
        self.cash += proceeds
        self.fees_paid += fill.fee
        self._roll_day(now_ms)
        self.realized_today += pnl
        self.trades += 1
        if pnl > 0:
            self.wins += 1
            self.gross_profit += pnl
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.gross_loss += -pnl
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.risk_cfg.max_consecutive_losses:
                self.paused_until_ms = now_ms + self.risk_cfg.pause_minutes * 60_000
                self.message = f"paused {self.risk_cfg.pause_minutes} min after {self.consecutive_losses} consecutive losses"
                self.store.audit("pulse", f"mode {self.mode.key}: {self.message}", level="WARNING")
        self.equity_peak = max(self.equity_peak, self.equity())
        self.store.insert_trade(mode=self.trade_mode, exchange=self.exchange_id, symbol=symbol, side="sell", ts_ms=now_ms, price=bid, fill_price=fill.price, amount=fill.amount, fee=fill.fee,
                                fee_currency=symbol.partition("/")[2], slippage=fill.slippage_pct, reason=f"pulse {self.mode.key} {reason} (event {pos.event_id})", pnl=pnl, status="filled")
        if pos.decision_id:
            self.store.update_decision(pos.decision_id, trade_pnl=pnl)
        log.info("PULSE-%s SELL %s %.6f @ %.2f (%s) pnl %+.2f", self.mode.key, symbol, fill.amount, fill.price, reason, pnl)
        self.save()
        return reason, pnl

    def record_equity(self, now_ms: int) -> None:
        self.store.add_equity(self.trade_mode, now_ms, self.equity(), self.cash, self.in_use())

    # ------------------------------------------------------------ status

    def status(self, now_ms: int) -> dict[str, Any]:
        self._roll_day(now_ms)
        return {
            "key": self.mode.key, "label": self.mode.label, "budget": self.mode.budget, "equity": self.equity(), "cash": self.cash, "open_positions": len(self.positions),
            "realized_today": self.realized_today, "unrealized": self.unrealized(), "open_risk_pct": self.open_risk_pct(), "trades": self.trades, "wins": self.wins,
            "losses": self.losses, "net_pnl": self.gross_profit - self.gross_loss, "gross_profit": self.gross_profit, "gross_loss": self.gross_loss,
            "consecutive_losses": self.consecutive_losses, "paused_until_ms": self.paused_until_ms, "halted": self.daily_loss_hit(now_ms), "message": self.message,
            "positions": [dict(p.as_dict(), mark=self._mark_px(s, p.entry_px)) for s, p in self.positions.items()], "accepted": self.accepted, "rejected": self.rejected,
        }

