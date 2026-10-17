"""Phase 6: lightweight market-wide screening from 24h tickers, before any detailed stream."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptobot.earlywarning.config import ScannerConfig


@dataclass
class Candidate:
    symbol: str
    quote_volume_24h: float
    trades_24h: int
    change_24h_pct: float
    spread_pct: float
    score: float
    reasons: list[str]
    excluded: str = ""


def _ticker_fields(t: dict[str, Any]) -> tuple[float, int, float, float]:
    last = float(t.get("last") or t.get("close") or 0.0)
    qv = t.get("quoteVolume")
    if qv is None and t.get("baseVolume") is not None and last:
        qv = float(t["baseVolume"]) * last
    info = t.get("info") or {}
    n = int(info.get("count") or 0)
    bid, ask = t.get("bid"), t.get("ask")
    spread = ((float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2) * 100) if bid and ask and float(bid) > 0 else -1.0
    pct = t.get("percentage")
    if pct is None and t.get("open") and last:
        pct = (last / float(t["open"]) - 1) * 100
    return float(qv or 0.0), n, float(pct or 0.0), spread


def screen(tickers: dict[str, dict[str, Any]], eligible: list[dict[str, Any]], cfg: ScannerConfig, previous: dict[str, dict[str, Any]] | None = None) -> list[Candidate]:
    """Rank eligible markets. With a previous ticker snapshot, "unusual" means versus that snapshot:
    volume accruing faster than the 24h average, more trades, buying (price up on volume) with a
    still-modest 24h move. Without one, the ranking falls back to liquidity so streams can start."""
    out: list[Candidate] = []
    elig = {m["symbol"] for m in eligible}
    for sym in sorted(elig):
        t = tickers.get(sym)
        if not t:
            continue
        qv, n, pct, spread = _ticker_fields(t)
        reasons: list[str] = []
        excluded = ""
        if qv < cfg.min_quote_volume_24h:
            excluded = f"24h volume {qv:,.0f} below {cfg.min_quote_volume_24h:,.0f}"
        elif spread < 0:
            excluded = "no bid/ask"
        elif spread > cfg.max_spread_pct:
            excluded = f"spread {spread:.2f}% above {cfg.max_spread_pct:g}%"
        score = 0.0
        if previous and sym in previous and not excluded:
            p = previous[sym]
            dt_h = max(0.05, (float(t.get("timestamp") or 0) - float(p.get("ts") or 0)) / 3_600_000) if t.get("timestamp") and p.get("ts") else cfg.rescan_s / 3600
            dq = max(0.0, qv - float(p.get("qv", qv)))
            dn = max(0, n - int(p.get("n", n)))
            rate_ratio = (dq / dt_h) / (qv / 24.0) if qv > 0 else 0.0  # recent volume rate vs the 24h average rate
            trade_ratio = (dn / dt_h) / (n / 24.0) if n > 0 else 0.0
            last_prev = float(p.get("last") or 0.0)
            last_now = float(t.get("last") or 0.0)
            dpx = (last_now / last_prev - 1) * 100 if last_prev > 0 else 0.0
            if rate_ratio >= 1.5:
                reasons.append(f"volume {rate_ratio:.1f}x its 24h rate")
                score += min(rate_ratio, 6.0)
            if trade_ratio >= 1.5:
                reasons.append(f"trades {trade_ratio:.1f}x their 24h rate")
                score += min(trade_ratio, 6.0) * 0.5
            if 0 < dpx < 1.5 and rate_ratio >= 1.3:
                reasons.append("price firm on rising volume")
                score += 1.0
            if abs(pct) > 15:
                reasons.append(f"already moved {pct:+.0f}% today")
                score *= 0.5
            if spread <= cfg.max_spread_pct / 2:
                score += 0.25
        elif not excluded:
            score = min(3.0, qv / cfg.min_quote_volume_24h / 10.0) + (0.5 if spread <= cfg.max_spread_pct / 2 else 0.0)
            reasons.append("liquidity baseline (no previous snapshot yet)")
        out.append(Candidate(sym, qv, n, pct, spread, score, reasons, excluded))
    out.sort(key=lambda c: (c.excluded != "", -c.score, -c.quote_volume_24h))
    return out


def snapshot(tickers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for sym, t in tickers.items():
        qv, n, _pct, _sp = _ticker_fields(t)
        out[sym] = {"qv": qv, "n": n, "last": float(t.get("last") or 0.0), "ts": float(t.get("timestamp") or 0.0)}
    return out
