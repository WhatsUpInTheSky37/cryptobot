"""Mode switching rules (TEST ⇄ BUY) and the TEST statistics shown before switching."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cryptobot.backtest.metrics import max_drawdown
from cryptobot.db.store import Store

MIN_TEST_DAYS = 14
FIRST_MONTH_DAYS = 30
FIRST_MONTH_CAP = 250.0
DAY_MS = 86_400_000


@dataclass
class TestStats:
    days_in_test: float
    trades: int  # round trips (sells)
    win_rate: float
    total_pnl: float
    fees: float
    max_drawdown: float
    equity: float
    start_equity: float


def simulation_stats(store: Store, now_ms: int) -> TestStats:
    started = store.get_setting("test.started_ms")
    days = (now_ms - started) / DAY_MS if started else 0.0
    trades = store.load_trades("TEST", limit=100_000)
    from cryptobot.reports import completed_trades
    sells = [t for t in trades if t["side"] == "sell" and t["pnl"] is not None]
    completed = completed_trades(trades)
    wins = sum(1 for _, pnl in completed if pnl > 0)
    eq = [e for _t, e in store.load_equity("TEST", limit=100_000)]
    return TestStats(
        max(0.0, days),
        len(completed),
        wins / len(completed) if completed else 0.0,
        float(sum(t["pnl"] for t in sells)),
        float(sum(t["fee"] for t in trades)),
        max_drawdown(np.array(eq)) if eq else 0.0,
        eq[-1] if eq else 0.0,
        eq[0] if eq else 0.0,
    )


def buy_gate(store: Store, now_ms: int, budget: float, has_key: bool, key_can_trade: bool | None) -> tuple[bool, list[str], TestStats]:
    """Return (allowed, blocking reasons, stats). Every reason is written for a human."""
    stats = simulation_stats(store, now_ms)
    reasons: list[str] = []
    if stats.days_in_test < MIN_TEST_DAYS:
        left = MIN_TEST_DAYS - stats.days_in_test
        reasons.append(f"BUY mode unlocks after {MIN_TEST_DAYS} days of TEST mode. {left:.1f} more day(s) to go.")
    if not has_key:
        reasons.append("No API key is saved for this exchange. Add one on the Settings screen.")
    elif key_can_trade is False:
        reasons.append("The saved API key cannot trade. Create one with spot trading enabled (and withdrawals disabled).")
    first_buy = store.get_setting("buy.first_started_ms")
    in_first_month = first_buy is None or (now_ms - first_buy) < FIRST_MONTH_DAYS * DAY_MS
    if in_first_month and budget > FIRST_MONTH_CAP:
        reasons.append(f"For the first {FIRST_MONTH_DAYS} days of BUY mode the budget may not exceed ${FIRST_MONTH_CAP:,.0f}. Lower it on the Risk screen.")
    return not reasons, reasons, stats

