"""Forward outcome tracking for every recorded signal, taken or not.

Each pending signal is followed with the prices that arrive *after* it was
recorded. Only the outcome columns of the event row are ever updated; the
signal-time values are immutable (the store enforces that), so later prices
cannot leak into the features. Pending signals are reloaded on restart so a
session boundary does not lose them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from cryptobot.db.store import Store
from cryptobot.pulse.config import OUTCOME_HORIZONS_MS


@dataclass
class Pending:
    event_id: int
    symbol: str
    direction: str
    start_ms: int
    entry_mid: float
    target_pct: float
    stop_pct: float
    cost_pct: float  # round trip, same assumptions as a paper trade
    horizon_ms: int
    mfe: float = 0.0
    mae: float = 0.0
    t_mfe: int = 0
    t_mae: int = 0
    first_hit: str | None = None
    first_hit_ms: int | None = None
    rets: dict[str, float] = field(default_factory=dict)
    last_move: float = 0.0

    def move(self, price: float) -> float:
        sign = 1.0 if self.direction == "long" else -1.0
        return sign * (price / self.entry_mid - 1.0) * 100.0

    def net_pct(self) -> float:
        if self.first_hit == "target":
            gross = self.target_pct
        elif self.first_hit == "stop":
            gross = -self.stop_pct
        else:
            gross = self.last_move
        return gross - self.cost_pct

    def outcome(self) -> str:
        return self.first_hit or "expired"


class OutcomeTracker:
    def __init__(self, store: Store, on_resolved: Callable[[Pending], None] | None = None, horizon_ms: int | None = None) -> None:
        self.store = store
        self.on_resolved = on_resolved
        self.horizon_ms = horizon_ms or max(OUTCOME_HORIZONS_MS.values())
        self.pending: list[Pending] = []

    def add(self, p: Pending) -> None:
        p.horizon_ms = max(p.horizon_ms, self.horizon_ms)
        self.pending.append(p)

    def restore(self, now_ms: int) -> tuple[int, int]:
        """Reload pending rows from the database. Rows older than the horizon are
        marked abandoned (we were not watching). Returns (restored, abandoned)."""
        restored = abandoned = 0
        for e in self.store.load_pulse_events(limit=10_000, pending_only=True, ascending=True):
            if now_ms - e["ts_ms"] >= self.horizon_ms:
                self.store.update_pulse_event(e["id"], outcome="abandoned", resolved_ts_ms=now_ms)
                abandoned += 1
                continue
            p = Pending(e["id"], e["symbol"], e["direction"], int(e["ts_ms"]), float(e["mid"]), float(e["target_pct"]), float(e["stop_pct"]), float(e.get("cost_pct") or 0.0), self.horizon_ms,
                        float(e.get("mfe_pct") or 0.0), float(e.get("mae_pct") or 0.0))
            for name in OUTCOME_HORIZONS_MS:
                v = e.get(f"ret_{name}")
                if v is not None:
                    p.rets[name] = float(v)
            self.pending.append(p)
            restored += 1
        return restored, abandoned

    def on_price(self, symbol: str, price: float, now_ms: int) -> list[Pending]:
        done: list[Pending] = []
        for p in self.pending:
            if p.symbol != symbol or now_ms < p.start_ms or price <= 0:
                continue
            elapsed = now_ms - p.start_ms
            mv = p.move(price)
            p.last_move = mv
            if mv > p.mfe:
                p.mfe, p.t_mfe = mv, elapsed
            if mv < p.mae:
                p.mae, p.t_mae = mv, elapsed
            if p.first_hit is None:
                if mv >= p.target_pct:
                    p.first_hit, p.first_hit_ms = "target", now_ms
                elif mv <= -p.stop_pct:
                    p.first_hit, p.first_hit_ms = "stop", now_ms
            for name, h in OUTCOME_HORIZONS_MS.items():
                if name not in p.rets and elapsed >= h:
                    p.rets[name] = mv
            if elapsed >= p.horizon_ms:
                done.append(p)
        for p in done:
            self.pending.remove(p)
            self._finalize(p, now_ms)
        return done

    def _finalize(self, p: Pending, now_ms: int) -> None:
        fields = dict(outcome=p.outcome(), mfe_pct=p.mfe, mae_pct=p.mae, resolved_ts_ms=now_ms, first_hit=p.first_hit, first_hit_ms=p.first_hit_ms,
                      time_to_mfe_ms=p.t_mfe, time_to_mae_ms=p.t_mae, net_pct=p.net_pct())
        for name in OUTCOME_HORIZONS_MS:
            fields[f"ret_{name}"] = p.rets.get(name)
        self.store.update_pulse_event(p.event_id, **fields)
        if self.on_resolved:
            self.on_resolved(p)

    def flush(self, now_ms: int) -> None:
        """Persist partial progress (called periodically so a crash keeps what we saw)."""
        for p in self.pending:
            fields = dict(mfe_pct=p.mfe, mae_pct=p.mae, first_hit=p.first_hit, first_hit_ms=p.first_hit_ms, time_to_mfe_ms=p.t_mfe, time_to_mae_ms=p.t_mae)
            for name in OUTCOME_HORIZONS_MS:
                if name in p.rets:
                    fields[f"ret_{name}"] = p.rets[name]
            self.store.update_pulse_event(p.event_id, **fields)
