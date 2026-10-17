"""Market-data health per symbol. No signal is ever computed from unhealthy data."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from cryptobot.pulse.config import HealthConfig

INSUFFICIENT = "INSUFFICIENT DATA"


@dataclass(frozen=True)
class HealthStatus:
    status: str  # ok | disconnected | no_data | stale_trades | no_book | book_unsynced | stale_book | inactive | insufficient_data
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def label(self) -> str:
        return "OK" if self.ok else INSUFFICIENT


class SymbolHealth:
    def __init__(self, cfg: HealthConfig) -> None:
        self.cfg = cfg
        self.connected = False
        self.last_trade_ms = 0
        self.last_book_ms = 0
        self.book_synced = False
        self.book_seq = 0
        self.observations = 0
        self.min_obs = 0
        self._trade_times: deque[int] = deque()
        self.inactive_until_ms = 0
        self.sync_detail = ""

    # -- inputs -------------------------------------------------------------

    def on_tick(self, ts_ms: int) -> None:
        self.last_trade_ms = max(self.last_trade_ms, ts_ms)
        self._trade_times.append(ts_ms)
        keep = ts_ms - self.cfg.activity_window_s * 1000
        while self._trade_times and self._trade_times[0] < keep:
            self._trade_times.popleft()

    def on_book(self, ts_ms: int, synced: bool, seq: int) -> None:
        self.last_book_ms = max(self.last_book_ms, ts_ms)
        self.book_synced = synced
        self.book_seq = seq

    def on_sync(self, status: str, detail: str) -> None:
        self.sync_detail = detail
        if status in ("synced", "verified"):
            self.book_synced = True
        elif status in ("gap", "resync", "unsynced", "mismatch", "stopped", "snapshot"):
            self.book_synced = False

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.book_synced = False

    def trades_per_min(self, now_ms: int) -> float:
        """Trades per minute over the activity window, measured from the first trade that is inside it."""
        keep = now_ms - self.cfg.activity_window_s * 1000
        inside = [t for t in self._trade_times if t >= keep]
        if not inside:
            return 0.0
        span_min = min(self.cfg.activity_window_s, max(60, (now_ms - inside[0]) / 1000)) / 60.0
        return len(inside) / span_min

    # -- verdict ------------------------------------------------------------

    def evaluate(self, now_ms: int) -> HealthStatus:
        c = self.cfg
        if not self.connected:
            return HealthStatus("disconnected", "exchange connection is down")
        if self.last_trade_ms == 0:
            return HealthStatus("no_data", "no public trades received yet")
        age = (now_ms - self.last_trade_ms) / 1000
        if age > c.stale_trade_s:
            return HealthStatus("stale_trades", f"last trade {age:.0f}s ago (limit {c.stale_trade_s}s)")
        if c.require_book:
            if self.last_book_ms == 0:
                return HealthStatus("no_book", "order book not received yet")
            if not self.book_synced:
                return HealthStatus("book_unsynced", f"order book out of sync{': ' + self.sync_detail if self.sync_detail else ''}")
            bage = (now_ms - self.last_book_ms) / 1000
            if bage > c.stale_book_s:
                return HealthStatus("stale_book", f"order book {bage:.0f}s old (limit {c.stale_book_s}s)")
        if now_ms < self.inactive_until_ms:
            return HealthStatus("inactive", f"market excluded until activity recovers ({self.trades_per_min(now_ms):.1f} trades/min)")
        tpm = self.trades_per_min(now_ms)
        # only judge activity once the window has had time to fill
        if len(self._trade_times) and now_ms - self._trade_times[0] >= min(c.activity_window_s, 60) * 1000 and tpm < c.min_trades_per_min:
            self.inactive_until_ms = now_ms + c.inactive_cooldown_s * 1000
            return HealthStatus("inactive", f"{tpm:.1f} trades/min below {c.min_trades_per_min:g}; excluded for {c.inactive_cooldown_s // 60} min")
        if self.observations < self.min_obs:
            return HealthStatus("insufficient_data", f"{self.observations}/{self.min_obs} observations")
        return HealthStatus("ok", "")
