"""Funded chronological replay of one Early Warning entry/exit policy."""
from __future__ import annotations

import heapq
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from cryptobot.earlywarning.paper import simulate_trade, summarize


def replay(setups, data, cfg, entry_method, exit_method, target_pct, cfg_for=None):
    cfg_for = cfg_for or {}
    budget, cash = cfg.risk.budget, cfg.risk.budget
    eligible = []
    for setup in setups:
        if setup["symbol"].partition("/")[2] != cfg.scanner.quotes[0]:
            continue
        k, tr = data[setup["symbol"]]
        probe = simulate_trade(k, tr, setup["symbol"], setup["t0_ms"], entry_method, exit_method, cfg_for.get(setup["symbol"], cfg), target_pct=target_pct, budget=budget)
        if probe:
            eligible.append((probe.entry_ms, setup))
    eligible.sort(key=lambda pair: (pair[0], pair[1]["symbol"]))
    pending, active, taken, completed = [], {}, [], []
    equity_points = [(eligible[0][0], budget)] if eligible else []
    day, baseline, losses, paused_until = "", budget, 0, 0
    counter = 0

    def equity(at):
        value = cash
        for symbol, position in active.items():
            k = data[symbol][0]
            idx = np.searchsorted(k["ts"], at, side="left") - 1
            mark = float(k["close"][idx]) if idx >= 0 else position["trade"].entry_price
            value += position["units"] * mark * (1 - cfg.costs.spread_estimate_pct / 200)
        return value

    def settle(at):
        nonlocal cash, losses, paused_until
        while pending and pending[0][0] <= at:
            ts, _, symbol, fill, trade = heapq.heappop(pending)
            position = active[symbol]
            cash += fill.price * fill.amount - fill.fee
            position["units"] -= fill.amount
            if position["units"] <= max(1e-12, trade.amount * 1e-9):
                active.pop(symbol)
                completed.append(trade)
                losses = losses + 1 if trade.pnl <= 0 else 0
                if losses >= cfg.risk.max_consecutive_losses:
                    paused_until = ts + cfg.risk.pause_minutes * 60_000
            equity_points.append((ts, equity(ts)))

    for at, setup in eligible:
        settle(at)
        eq = equity(at)
        key = datetime.fromtimestamp(at / 1000, ZoneInfo("America/New_York")).date().isoformat()
        if key != day:
            day, baseline = key, eq
        equity_points.append((at, eq))
        if setup["symbol"] in active or len(active) >= cfg.risk.max_positions or at < paused_until or eq - baseline <= -baseline * cfg.risk.daily_loss_pct / 100:
            continue
        k, tr = data[setup["symbol"]]
        trade = simulate_trade(k, tr, setup["symbol"], setup["t0_ms"], entry_method, exit_method, cfg_for.get(setup["symbol"], cfg),
                               target_pct=target_pct, budget=budget, equity=eq, max_notional=max(0, cash / (1 + cfg.costs.fee_rate)))
        if not trade:
            continue
        entry_cost = trade.entry_price * trade.amount + trade.fills[0].fee
        if entry_cost > cash + 1e-9:
            raise AssertionError("portfolio replay attempted to reuse reserved capital")
        cash -= entry_cost
        active[trade.symbol] = {"units": trade.amount, "trade": trade}
        taken.append(trade)
        for fill in trade.fills[1:]:
            counter += 1
            heapq.heappush(pending, (fill.ts_ms, counter, trade.symbol, fill, trade))
        settle(at)
    if pending:
        settle(max(row[0] for row in pending))
    summary = summarize(completed, budget)
    values = np.array([e for _, e in equity_points])
    if len(values):
        summary["max_drawdown_pct"] = float(np.max(1 - values / np.maximum.accumulate(values))) * 100
    summary.update(final_cash=cash, max_positions=cfg.risk.max_positions, funded_portfolio=True,
                   valuation_note="drawdown sampled at decisions and fills; intrabar portfolio drawdown requires finer replay")
    return summary, completed
