"""Trade reports: period summaries, TEST-vs-BUY comparison, CSV and tax exports."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

Trade = dict[str, Any]

TRADE_COLUMNS = ["id", "mode", "exchange", "symbol", "side", "ts_ms", "price", "fill_price", "amount", "fee", "fee_currency", "slippage", "reason", "pnl", "position_id", "order_id", "status"]


def _period_key(ts_ms: int, period: str, tz: ZoneInfo) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000, tz)
    if period == "daily":
        return d.strftime("%Y-%m-%d")
    if period == "weekly":
        start = d - timedelta(days=d.weekday())
        return "week of " + start.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m")


def period_summary(trades: list[Trade], period: str = "daily", tz: str = "America/New_York") -> list[dict[str, Any]]:
    z = ZoneInfo(tz)
    buckets: dict[str, dict[str, Any]] = {}
    for t in trades:
        k = _period_key(int(t["ts_ms"]), period, z)
        b = buckets.setdefault(k, {"period": k, "fills": 0, "round_trips": 0, "wins": 0, "pnl": 0.0, "fees": 0.0, "volume": 0.0})
        b["fills"] += 1
        b["fees"] += float(t.get("fee") or 0.0)
        b["volume"] += float(t.get("fill_price") or t.get("price") or 0.0) * float(t.get("amount") or 0.0)
        if t["side"] == "sell" and t.get("pnl") is not None:
            b["round_trips"] += 1
            b["pnl"] += float(t["pnl"])
            if float(t["pnl"]) > 0:
                b["wins"] += 1
    out = sorted(buckets.values(), key=lambda b: b["period"])
    for b in out:
        b["win_rate"] = b["wins"] / b["round_trips"] if b["round_trips"] else 0.0
    return out


def mode_stats(trades: list[Trade]) -> dict[str, Any]:
    sells = [t for t in trades if t["side"] == "sell" and t.get("pnl") is not None]
    slips = [float(t["slippage"]) for t in trades if t.get("slippage") is not None]
    fills = [t for t in trades if t.get("status") in (None, "filled", "partial")]
    return {
        "fills": len(trades),
        "round_trips": len(sells),
        "win_rate": (sum(1 for t in sells if t["pnl"] > 0) / len(sells)) if sells else 0.0,
        "pnl": float(sum(t["pnl"] for t in sells)),
        "fees": float(sum(float(t.get("fee") or 0.0) for t in trades)),
        "avg_slippage_pct": (sum(slips) / len(slips)) if slips else 0.0,
        "partial_fills": sum(1 for t in fills if t.get("status") == "partial"),
        "volume": float(sum(float(t.get("fill_price") or t.get("price") or 0.0) * float(t.get("amount") or 0.0) for t in trades)),
    }


def comparison(test_trades: list[Trade], buy_trades: list[Trade], since_ms: int | None = None) -> dict[str, dict[str, Any]]:
    """TEST vs BUY over the same period (from the first BUY trade unless given)."""
    if since_ms is None:
        since_ms = min((int(t["ts_ms"]) for t in buy_trades), default=0)
    tt = [t for t in test_trades if int(t["ts_ms"]) >= since_ms]
    bt = [t for t in buy_trades if int(t["ts_ms"]) >= since_ms]
    return {"TEST": mode_stats(tt), "BUY": mode_stats(bt), "since_ms": since_ms}


def export_csv(trades: list[Trade], path: Path | str, tz: str = "America/New_York") -> int:
    z = ZoneInfo(tz)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_local", *TRADE_COLUMNS])
        for t in sorted(trades, key=lambda t: t["ts_ms"]):
            w.writerow([datetime.fromtimestamp(int(t["ts_ms"]) / 1000, z).isoformat(), *[t.get(c) for c in TRADE_COLUMNS]])
    return len(trades)


def tax_lots(trades: list[Trade]) -> list[dict[str, Any]]:
    """FIFO lot matching per asset: one row per (buy lot, sell) pair, the way tax software wants it."""
    lots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    for t in sorted(trades, key=lambda t: (int(t["ts_ms"]), int(t.get("id") or 0))):
        sym = t["symbol"]
        px = float(t.get("fill_price") or t.get("price") or 0.0)
        amt = float(t["amount"])
        fee = float(t.get("fee") or 0.0)
        if t["side"] == "buy":
            lots[sym].append({"ts": int(t["ts_ms"]), "amount": amt, "cost": px * amt + fee})
            continue
        remaining = amt
        proceeds_total = px * amt - fee
        while remaining > 1e-12 and lots[sym]:
            lot = lots[sym][0]
            take = min(remaining, lot["amount"])
            frac_lot = take / lot["amount"]
            frac_sell = take / amt
            cost = lot["cost"] * frac_lot
            proceeds = proceeds_total * frac_sell
            held_days = (int(t["ts_ms"]) - lot["ts"]) / 86_400_000
            rows.append({
                "asset": sym.partition("/")[0],
                "amount": take,
                "date_acquired": datetime.fromtimestamp(lot["ts"] / 1000, UTC).date().isoformat(),
                "date_sold": datetime.fromtimestamp(int(t["ts_ms"]) / 1000, UTC).date().isoformat(),
                "proceeds": round(proceeds, 8),
                "cost_basis": round(cost, 8),
                "gain": round(proceeds - cost, 8),
                "term": "long" if held_days > 365 else "short",
                "exchange": t.get("exchange", ""),
            })
            lot["amount"] -= take
            lot["cost"] -= cost
            remaining -= take
            if lot["amount"] <= 1e-12:
                lots[sym].pop(0)
    return rows


def export_tax_csv(trades: list[Trade], path: Path | str) -> int:
    rows = tax_lots(trades)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["asset", "amount", "date_acquired", "date_sold", "proceeds", "cost_basis", "gain", "term", "exchange"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)
