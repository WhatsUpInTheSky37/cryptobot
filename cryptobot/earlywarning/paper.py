"""Phase 9-10: paper trading. Entries A/B/C, seven exit methods, bid-side execution, fees,
spread, slippage scaled by available volume, no guaranteed stops, no martingale."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cryptobot.earlywarning.config import EWConfig, ExitConfig

ENTRY_METHODS = {"A": "early accumulation", "B": "confirmed breakout", "C": "breakout + retest"}
EXIT_METHODS = ("fixed", "volatility", "percentile", "partial", "trailing", "time", "distribution")


@dataclass
class Fill:
    ts_ms: int
    side: str
    price: float
    amount: float
    fee: float
    reason: str


@dataclass
class TradeResult:
    symbol: str
    setup_ms: int
    entry_method: str
    exit_method: str
    entry_ms: int
    entry_price: float
    amount: float
    fills: list[Fill] = field(default_factory=list)
    exit_ms: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    reason: str = ""
    mfe_pct: float = 0.0
    mae_pct: float = 0.0


def slip_pct(notional: float, minute_quote_volume: float, cfg: EWConfig) -> float:
    """Slippage per side: base plus impact once we are more than ``impact_share`` of the minute's volume."""
    base = cfg.costs.base_slippage_pct
    if minute_quote_volume <= 0:
        return base + cfg.costs.spread_estimate_pct
    share = notional / minute_quote_volume
    return base + max(0.0, share - cfg.costs.impact_share) * 10.0  # 1% of extra share costs 0.1%


def bid_price(k_bar, trades: np.ndarray | None, ts_ms: int, cfg: EWConfig, ref: float | None = None) -> float:
    """Executable bid near ts: the last sell-taker print within a minute, else the price less the assumed spread."""
    ref = ref if ref is not None else float(k_bar["open"])
    if trades is not None and len(trades):
        m = (trades["ts"] >= ts_ms - 60_000) & (trades["ts"] <= ts_ms) & (trades["buyer_maker"] == 1)
        if m.any():
            return min(ref, float(trades["price"][m][-1]))
    return ref * (1 - cfg.costs.spread_estimate_pct / 200.0)


def _entry_index(k: np.ndarray, setup_ms: int, method: str) -> int | None:
    """Index of the bar at whose OPEN we would enter, or None if the method never triggers."""
    idx = np.searchsorted(k["ts"], setup_ms, side="right")  # the first bar after the setup time
    if idx >= len(k) - 1:
        return None
    if method == "A":
        return int(idx)
    lookback = 30
    level = float(k["high"][max(0, idx - lookback):idx].max()) if idx > 0 else float(k["high"][idx])
    end = min(len(k) - 1, idx + 60)
    brk = None
    for j in range(idx, end):
        if float(k["close"][j]) > level:
            brk = j
            break
    if brk is None:
        return None
    if method == "B":
        return brk + 1 if brk + 1 < len(k) else None
    # C: wait for a pullback to within 0.5% of the breakout level, then a close back above it
    touched = False
    for j in range(brk + 1, min(len(k) - 1, brk + 120)):
        if not touched and float(k["low"][j]) <= level * 1.005:
            touched = True
            continue
        if touched and float(k["close"][j]) > level:
            return j + 1
    return None


def simulate_trade(k: np.ndarray, trades: np.ndarray | None, symbol: str, setup_ms: int, entry_method: str, exit_method: str, cfg: EWConfig, *, target_pct: float | None = None, vol_pct: float | None = None, budget: float = 100.0, equity: float | None = None) -> TradeResult | None:
    ex: ExitConfig = cfg.exits
    i0 = _entry_index(k, setup_ms, entry_method)
    if i0 is None:
        return None
    bar = k[i0]
    equity = equity if equity is not None else budget
    stop_pct = ex.stop_pct if exit_method != "volatility" else max(ex.stop_pct, (vol_pct or ex.stop_pct) * 1.5)
    risk_usd = equity * cfg.risk.risk_per_trade_pct / 100.0
    notional = min(risk_usd / (stop_pct / 100.0), budget * 0.5)
    recent_vol = float(k["quote_volume"][max(0, i0 - 5):i0].mean()) if i0 > 0 else float(bar["quote_volume"])
    notional = min(notional, recent_vol * cfg.risk.max_position_share_of_minute_volume) if recent_vol > 0 else 0.0
    if notional < 5.0:
        return None  # liquidity does not support even a minimum position
    ask = float(bar["open"]) * (1 + cfg.costs.spread_estimate_pct / 200.0 + slip_pct(notional, recent_vol, cfg) / 100.0)
    amount = notional / ask
    fee_in = amount * ask * cfg.costs.fee_rate
    tr = TradeResult(symbol, setup_ms, entry_method, exit_method, int(bar["ts"]), ask, amount, [Fill(int(bar["ts"]), "buy", ask, amount, fee_in, f"entry {entry_method}")], fees=fee_in)
    cost_basis = amount * ask + fee_in
    remaining = amount
    proceeds = 0.0
    stop = ask * (1 - stop_pct / 100.0)
    if exit_method == "fixed":
        t1 = ask * (1 + ex.fixed_target_pct / 100.0)
    elif exit_method == "volatility":
        t1 = ask * (1 + max(ex.fixed_target_pct, (vol_pct or ex.fixed_target_pct) * 2.0) / 100.0)
    elif exit_method == "percentile":
        t1 = ask * (1 + (target_pct if target_pct is not None else ex.fixed_target_pct) / 100.0)
    else:
        t1 = ask * (1 + ex.fixed_target_pct / 100.0)
    t2 = ask * (1 + ex.fixed_target_pct * ex.t2_multiple / 100.0)
    rt_cost = 2 * cfg.costs.fee_rate * 100 + cfg.costs.spread_estimate_pct + 2 * slip_pct(notional, recent_vol, cfg)
    if exit_method not in ("trailing", "time", "distribution") and (t1 / ask - 1) * 100 <= rt_cost:
        return None  # the target cannot pay for the round trip: no trade, never a bigger target
    highest = ask
    took = [False, False]
    base_vol = recent_vol or 1.0
    gain_seen = 0.0

    def sell(j: int, qty: float, price: float, reason: str) -> None:
        nonlocal remaining, proceeds
        minute_vol = float(k["quote_volume"][j]) or base_vol
        px = price * (1 - slip_pct(qty * price, minute_vol, cfg) / 100.0)
        fee = qty * px * cfg.costs.fee_rate
        tr.fills.append(Fill(int(k["ts"][j]), "sell", px, qty, fee, reason))
        tr.fees += fee
        proceeds += qty * px - fee
        remaining -= qty
        tr.exit_ms = int(k["ts"][j])
        tr.reason = reason

    last = min(len(k) - 1, i0 + ex.max_hold_min)
    for j in range(i0, last + 1):
        b = k[j]
        hi, lo, op = float(b["high"]), float(b["low"]), float(b["open"])
        tr.mfe_pct = max(tr.mfe_pct, (hi / ask - 1) * 100)
        tr.mae_pct = min(tr.mae_pct, (lo / ask - 1) * 100)
        if j > i0 and lo <= stop:
            # a stop is a market order after the level trades: filled at the worse of the stop and the open of the bar, bid side
            fill = bid_price(b, trades, int(b["ts"]), cfg, ref=min(stop, op))
            sell(j, remaining, fill, "stop")
            break
        if exit_method == "partial":
            if not took[0] and hi >= t1:
                sell(j, amount * ex.partial_shares[0], bid_price(b, trades, int(b["ts"]), cfg, ref=t1), "target 1 (partial)")
                took[0] = True
                stop = max(stop, ask)  # protect the rest at break-even
            if took[0] and not took[1] and hi >= t2:
                sell(j, amount * ex.partial_shares[1], bid_price(b, trades, int(b["ts"]), cfg, ref=t2), "target 2 (partial)")
                took[1] = True
            if took[1]:
                highest = max(highest, hi)
                if lo <= highest * (1 - ex.trailing_pct / 100.0) and j > i0:
                    sell(j, remaining, bid_price(b, trades, int(b["ts"]), cfg, ref=min(op, highest * (1 - ex.trailing_pct / 100.0))), "trailing")
                    break
        elif exit_method == "trailing":
            highest = max(highest, hi)
            if j > i0 and lo <= highest * (1 - ex.trailing_pct / 100.0) and highest > ask:
                sell(j, remaining, bid_price(b, trades, int(b["ts"]), cfg, ref=min(op, highest * (1 - ex.trailing_pct / 100.0))), "trailing")
                break
        elif exit_method == "distribution":
            q = float(b["quote_volume"])
            sell_share = 1 - (float(b["taker_buy_quote"]) / q if q > 0 else 0.5)
            gain_seen = max(gain_seen, (hi / ask - 1) * 100)
            if hi >= t1:
                sell(j, remaining, bid_price(b, trades, int(b["ts"]), cfg, ref=t1), "target")
                break
            if gain_seen >= 1.0 and q >= 2 * base_vol and sell_share >= 0.65 and j + 1 <= last:
                nb = k[j + 1]
                sell(j + 1, remaining, bid_price(nb, trades, int(nb["ts"]), cfg), "distribution warning")
                break
        elif exit_method == "time":
            pass
        else:  # fixed / volatility / percentile
            if hi >= t1:
                sell(j, remaining, bid_price(b, trades, int(b["ts"]), cfg, ref=t1), "target")
                break
        if remaining <= 1e-12:
            break
    if remaining > 1e-12:
        j = last
        sell(j, remaining, bid_price(k[j], trades, int(k["ts"][j]), cfg, ref=float(k["close"][j])), tr.reason or "time exit")
    tr.pnl = proceeds - cost_basis
    tr.pnl_pct = tr.pnl / cost_basis * 100.0 if cost_basis else 0.0
    return tr


def summarize(trades: list[TradeResult], budget: float) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "net_pnl": 0.0, "net_pct": 0.0, "win_rate": None, "profit_factor": None, "expectancy_pct": None, "max_drawdown_pct": 0.0, "fees": 0.0}
    pnls = np.array([t.pnl for t in trades])
    eq = budget + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[budget], eq]))
    dd = float(np.max(1 - np.concatenate([[budget], eq]) / peak)) * 100
    gp, gl = float(pnls[pnls > 0].sum()), float(-pnls[pnls <= 0].sum())
    return {"trades": len(trades), "net_pnl": float(pnls.sum()), "net_pct": float(pnls.sum() / budget * 100), "win_rate": float(np.mean(pnls > 0)),
            "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0), "expectancy_pct": float(np.mean([t.pnl_pct for t in trades])),
            "max_drawdown_pct": dd, "fees": float(sum(t.fees for t in trades)), "avg_mfe_pct": float(np.mean([t.mfe_pct for t in trades])), "avg_mae_pct": float(np.mean([t.mae_pct for t in trades]))}
