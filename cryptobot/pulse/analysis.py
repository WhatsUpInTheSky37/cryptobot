"""Performance and rejected-opportunity analysis for the Pulse screen."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from cryptobot.backtest.metrics import max_drawdown
from cryptobot.pulse.config import INDICATOR_NAMES, INDICATORS

REASON_TEXT: dict[str, str] = {
    "insufficient_data": "Insufficient market data",
    "stale_data": "Stale market data",
    "spot_no_short": "Bearish setup (spot cannot short)",
    "already_in_position": "Already in a position",
    "regime_excluded": "Market regime excluded",
    "weak_signal": "Weak pressure signal",
    "no_confirmation": "No price confirmation",
    "weak_momentum": "Weak order-flow momentum",
    "spread_too_high": "High spread",
    "volatility_too_high": "Excessive volatility",
    "paused_losses": "Paused after consecutive losses",
    "daily_loss_limit": "Daily risk limit reached",
    "max_positions": "Maximum positions open",
    "open_risk_limit": "Aggregate open risk limit",
    "target_below_costs": "Expected profit below fees",
    "ev_below_margin": "Expected value below margin",
    "rr_below_min": "Reward/risk after costs too low",
    "insufficient_evidence": "Insufficient historical evidence",
    "stop_inside_spread": "Spread too wide for a stop",
    "ml_low_probability": "Model probability too low",
    "risk_rejected": "Risk manager rejected the order",
    "ok": "Accepted",
}


def mode_stats(trades: list[dict[str, Any]], equity: list[tuple[int, float]], budget: float, tz: str = "America/New_York") -> dict[str, Any]:
    sells = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
    pnls = [float(t["pnl"]) for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    fees = sum(float(t.get("fee") or 0.0) for t in trades)
    z = ZoneInfo(tz)
    by_day: dict[str, float] = defaultdict(float)
    for t in sells:
        by_day[datetime.fromtimestamp(t["ts_ms"] / 1000, z).date().isoformat()] += float(t["pnl"])
    days = len(by_day)
    eq = np.array([e for _t, e in equity], dtype=float) if equity else np.array([budget])
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "net_pnl": sum(pnls), "gross_profit": gp, "gross_loss": gl, "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "avg_win": gp / len(wins) if wins else 0.0, "avg_loss": gl / len(losses) if losses else 0.0, "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "fees": fees, "max_drawdown_pct": max_drawdown(eq) * 100.0, "trading_days": days,
        "profitable_days_pct": (sum(1 for v in by_day.values() if v > 0) / days * 100.0) if days else 0.0,
        "return_pct": (eq[-1] / budget - 1.0) * 100.0 if budget > 0 and len(eq) else 0.0,
    }


def group_outcomes(events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Resolved signal outcomes grouped by ``key`` (symbol, regime, direction...)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        groups[str(e.get(key) or "unknown")].append(e)
    out = []
    for g, rows in sorted(groups.items()):
        res = [r for r in rows if r.get("outcome") in ("target", "stop", "expired")]
        hits = sum(1 for r in res if (r.get("first_hit") or r.get("outcome")) == "target")
        nets = [float(r["net_pct"]) for r in res if r.get("net_pct") is not None]
        out.append({
            key: g, "signals": len(rows), "resolved": len(res), "hit_rate": hits / len(res) if res else None,
            "avg_net_pct": sum(nets) / len(nets) if nets else None, "sum_net_pct": sum(nets) if nets else 0.0,
            "avg_mfe_pct": float(np.mean([r["mfe_pct"] for r in res if r.get("mfe_pct") is not None])) if res else None,
            "avg_mae_pct": float(np.mean([r["mae_pct"] for r in res if r.get("mae_pct") is not None])) if res else None,
        })
    return out


def rejected_analysis(decisions: list[dict[str, Any]], events_by_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Why didn't we trade, and what would have happened with the same execution assumptions.

    A rejected signal "would have been profitable" only when its hypothetical net return,
    after fees, spread and slippage, is positive; favourable movement that could not have
    been captured does not count."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in decisions:
        if d.get("accepted"):
            continue
        groups[(d["mode"], d.get("code") or "")].append(d)
    out = []
    for (mode, code), rows in sorted(groups.items()):
        res = []
        for d in rows:
            e = events_by_id.get(d["event_id"])
            if e and e.get("outcome") in ("target", "stop", "expired") and e.get("net_pct") is not None:
                res.append(e)
        hits = sum(1 for e in res if (e.get("first_hit") or e.get("outcome")) == "target")
        nets = [float(e["net_pct"]) for e in res]
        out.append({
            "mode": mode, "code": code, "reason": REASON_TEXT.get(code, code), "count": len(rows), "resolved": len(res),
            "hit_rate": hits / len(res) if res else None, "profitable": sum(1 for n in nets if n > 0), "avg_net_pct": sum(nets) / len(nets) if nets else None,
            "sum_net_pct": sum(nets) if nets else 0.0,
        })
    return out


def _auc(pos: list[float], neg: list[float]) -> float | None:
    """Probability that a random positive outranks a random negative (ties count half)."""
    if not pos or not neg:
        return None
    p, n = np.asarray(pos, dtype=float), np.asarray(neg, dtype=float)
    wins = (p[:, None] > n[None, :]).sum() + 0.5 * (p[:, None] == n[None, :]).sum()
    return float(wins / (len(p) * len(n)))


def indicator_edge(events: list[dict[str, Any]], keys: tuple[str, ...] = INDICATORS, *, direction: str = "long") -> dict[str, Any]:
    """Which indicators separated winners from losers in the resolved signals of one direction.

    AUC per indicator: 0.5 = no information, above 0.5 = higher values went with a hit,
    below 0.5 = higher values went with a stop or expiry. Wide intervals with few signals."""
    res = [e for e in events if e.get("direction") == direction and e.get("outcome") in ("target", "stop", "expired")]
    hit = [e for e in res if (e.get("first_hit") or e.get("outcome")) == "target"]
    miss = [e for e in res if (e.get("first_hit") or e.get("outcome")) != "target"]
    rows = []
    for k in keys:
        a = _auc([float(e.get(k) or 0.0) for e in hit], [float(e.get(k) or 0.0) for e in miss])
        if a is not None:
            rows.append({"indicator": k, "name": INDICATOR_NAMES.get(k, k), "auc": a, "n": len(res)})
    rows.sort(key=lambda r: -abs(r["auc"] - 0.5))
    return {"direction": direction, "resolved": len(res), "hits": len(hit), "rows": rows,
            "note": "needs dozens of resolved signals across several days before any ranking means something" if len(res) < 50 else ""}


def decision_summary(decisions: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"accepted": 0, "rejected": 0})
    for d in decisions:
        out[d["mode"]]["accepted" if d.get("accepted") else "rejected"] += 1
    return dict(out)
