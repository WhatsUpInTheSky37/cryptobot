"""Trading costs and the after-cost profitability test.

A round trip on a spot exchange pays: the taker fee twice, the bid/ask spread
once (buy at the ask, sell at the bid), and slippage twice (walking the book
plus latency). All numbers are in percent of notional.

The target is derived from the expected price movement, never from the fees.
If the market is too quiet for the expected move to cover costs, the trade is
rejected rather than given a bigger target.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CostBreakdown:
    fee_pct: float  # round trip
    spread_pct: float  # round trip (the full quoted spread)
    slippage_pct: float  # round trip, base + depth impact
    total_pct: float
    notional: float
    fee_rate: float  # per side, fraction

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def slippage_per_side_pct(notional: float, depth_near_quote: float, band_pct: float = 0.10, base_pct: float = 0.02) -> float:
    """Expected slippage for a market-style fill of ``notional`` against ``depth_near_quote``
    (quote value resting within ``band_pct`` of the mid). Filling the whole band moves the
    average price half a band; anything beyond the band is assumed to cost a full band more."""
    if notional <= 0:
        return base_pct
    if depth_near_quote <= 0:
        return base_pct + band_pct  # no visible liquidity: assume we walk through the band
    frac = notional / depth_near_quote
    if frac <= 1.0:
        return base_pct + band_pct * frac / 2.0
    return base_pct + band_pct / 2.0 + band_pct * min(frac - 1.0, 4.0)


def round_trip_costs(fee_rate: float, spread_pct: float, notional: float, bid_depth: float, ask_depth: float, *, band_pct: float = 0.10, base_slip_pct: float = 0.02) -> CostBreakdown:
    fee = 2.0 * fee_rate * 100.0
    slip = slippage_per_side_pct(notional, ask_depth, band_pct, base_slip_pct) + slippage_per_side_pct(notional, bid_depth, band_pct, base_slip_pct)
    spread = max(0.0, spread_pct)
    return CostBreakdown(fee, spread, slip, fee + spread + slip, notional, fee_rate)


def expected_move_pct(vol_1m_pct: float, hold_minutes: float, evidence_move_pct: float | None = None) -> float:
    """How far price can plausibly travel over the holding horizon.

    Square-root-of-time scaling of the 1-minute range, replaced by the median
    favourable excursion of similar past events when there is evidence."""
    if evidence_move_pct is not None and evidence_move_pct > 0:
        return evidence_move_pct
    if vol_1m_pct <= 0 or hold_minutes <= 0:
        return 0.0
    return vol_1m_pct * math.sqrt(hold_minutes)


def plan_exits(vol_1m_pct: float, expected_move: float, *, stop_vol_mult: float, min_stop_pct: float, max_stop_pct: float, target_capture: float, max_target_pct: float, adaptive: bool = True, fixed_target: float = 0.4, fixed_stop: float = 0.2) -> tuple[float, float]:
    """(target_pct, stop_pct). The stop is one volatility unit (clamped); the target is a
    share of the expected move. The target is never raised to cover costs."""
    if not adaptive:
        return fixed_target, fixed_stop
    stop = min(max_stop_pct, max(min_stop_pct, stop_vol_mult * vol_1m_pct)) if vol_1m_pct > 0 else min_stop_pct
    target = min(max_target_pct, max(0.0, target_capture * expected_move))
    return target, stop


@dataclass(frozen=True)
class Assessment:
    target_pct: float
    stop_pct: float
    costs: CostBreakdown
    p_target: float | None
    ev_pct: float | None  # net expected value per trade after costs (None without evidence)
    rr_after_costs: float | None
    net_target_pct: float  # what a winner nets
    net_stop_pct: float  # what a loser costs
    break_even_p: float
    verdict: str  # ok | target_below_costs | ev_below_margin | rr_below_min | insufficient_evidence
    reason: str

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["costs"] = self.costs.as_dict()
        return d


def assess(target_pct: float, stop_pct: float, costs: CostBreakdown, p_target: float | None, *, min_ev_pct: float, min_rr: float, min_target_cost_mult: float = 1.2) -> Assessment:
    total = costs.total_pct
    net_target = target_pct - total
    net_stop = stop_pct + total
    be = (stop_pct + total) / (target_pct + stop_pct) if (target_pct + stop_pct) > 0 else 1.0
    rr = net_target / net_stop if net_stop > 0 and net_target > 0 else 0.0
    if target_pct < min_target_cost_mult * total:
        return Assessment(target_pct, stop_pct, costs, p_target, None if p_target is None else p_target * target_pct - (1 - p_target) * stop_pct - total, rr, net_target, net_stop, be,
                          "target_below_costs", f"expected move {target_pct:.3f}% cannot cover costs {total:.3f}% (needs {min_target_cost_mult:g}x)")
    if p_target is None:
        return Assessment(target_pct, stop_pct, costs, None, None, rr, net_target, net_stop, be, "insufficient_evidence", f"INSUFFICIENT HISTORICAL EVIDENCE (break-even win rate {be:.0%})")
    ev = p_target * target_pct - (1 - p_target) * stop_pct - total
    if ev < min_ev_pct:
        return Assessment(target_pct, stop_pct, costs, p_target, ev, rr, net_target, net_stop, be, "ev_below_margin", f"EV {ev:+.3f}% below {min_ev_pct:+.3f}% (p={p_target:.0%}, break-even {be:.0%})")
    if rr < min_rr:
        return Assessment(target_pct, stop_pct, costs, p_target, ev, rr, net_target, net_stop, be, "rr_below_min", f"reward/risk after costs {rr:.2f} below {min_rr:g}")
    return Assessment(target_pct, stop_pct, costs, p_target, ev, rr, net_target, net_stop, be, "ok", f"EV {ev:+.3f}% (p={p_target:.0%}, R/R {rr:.2f}, costs {total:.3f}%)")


def net_return_pct(gross_move_pct: float, costs: CostBreakdown) -> float:
    """Hypothetical net return of a round trip that moved ``gross_move_pct`` in our favour (mid to mid)."""
    return gross_move_pct - costs.total_pct
