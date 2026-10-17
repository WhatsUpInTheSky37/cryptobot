"""Phase 14: the written report, built only from what the store actually contains."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cryptobot.earlywarning.config import EWConfig
from cryptobot.earlywarning.features import FEATURE_NAMES
from cryptobot.earlywarning.store import EWStore


def _f(v: Any, fmt: str = "{:.2f}", none: str = "—") -> str:
    return none if v is None else fmt.format(v)


def build_report(store: EWStore, cfg: EWConfig, *, demo: bool = False) -> str:
    markets = store.markets()
    downloads = store.downloads()
    per_coin = store.get_study("compare", {}) or {}
    pooled = store.get_study("pooled", {}) or {}
    targets = store.get_study("targets", {}) or {}
    methods = store.get_study("methods", {}) or {}
    run = store.get_study("run", {}) or {}
    lines = ["# Early Warning — research report", ""]
    if demo or run.get("demo"):
        lines += ["> **SYNTHETIC DATA.** This report was produced from generated candles to exercise the pipeline. It says nothing about real markets.", ""]
    lines += [f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · exchange {run.get('exchange', 'binanceus')} · requested {run.get('days', cfg.days)} days · paper only.", ""]
    lines += ["## 1. Markets and data", "", "| Symbol | Binance.US id | Status | Available | 1m candles | Aggregated trades | Note |", "|---|---|---|---|---|---|---|"]
    dl = {(d["symbol"], d["kind"]): d for d in downloads}
    for m in markets:
        k1 = dl.get((m["symbol"], "klines_1m"))
        tr = dl.get((m["symbol"], "aggtrades"))
        span = f"{k1['rows']} ({datetime.fromtimestamp(k1['first_ms'] / 1000, timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(k1['last_ms'] / 1000, timezone.utc):%Y-%m-%d})" if k1 else "none"
        lines.append(f"| {m['symbol']} | {m['market_id']} | {m['status']} | {'yes' if m['available'] else 'NO'} | {span} | {tr['rows'] if tr else 'none'} | {m['note'] or ''} |")
    lines += ["", "Individual historical trades require an API key on Binance.US and were not used; aggregated trades carry the taker side. Historical order-book snapshots do not exist on the exchange, so bid prices are approximated by the last sell-taker print near each moment (or an assumed spread where no trade is available).", ""]
    lines += ["## 2. Surge events", ""]
    for sym, r in per_coin.items():
        evs = store.events(sym)
        lines.append(f"### {sym}: {len(evs)} events")
        if evs:
            lines += ["", "| Start (UTC) | Pre price | Peak | Gain | Min to peak | Min to reversal | Max decline after peak | Quote volume | Thresholds |", "|---|---|---|---|---|---|---|---|---|"]
            for e in evs[:40]:
                lines.append(f"| {datetime.fromtimestamp(e['start_ms'] / 1000, timezone.utc):%m-%d %H:%M} | {e['pre_price']:.6g} | {e['peak_price']:.6g} (bid ≈ {_f(e.get('peak_bid'), '{:.6g}')}) | {e['gain_pct']:+.1f}% | {e['minutes_to_peak']:.0f} | {_f(e.get('minutes_to_reversal'), '{:.0f}')} | {_f(e.get('max_decline_pct'), '{:+.1f}%')} | {_f(e.get('quote_volume'), '{:,.0f}')} | {', '.join(e['thresholds'])} |")
            if len(evs) > 40:
                lines.append(f"| … {len(evs) - 40} more | | | | | | | | |")
        lines.append("")
    lines += ["## 3-4. Pre-surge patterns versus ordinary periods", ""]
    if pooled:
        lines += [f"Pooled over {pooled.get('events', 0)} events and {pooled.get('controls', 0)} control windows. AUC = probability that an event window scores higher than a control window (0.5 = no information).", ""]
        feats = sorted(((k, v) for k, v in (pooled.get("features") or {}).items() if v is not None), key=lambda kv: -abs(kv[1] - 0.5))
        lines += ["| Feature @ lookback | AUC |", "|---|---|"] + [f"| {FEATURE_NAMES.get(k.split('@')[0], k.split('@')[0])} @ {k.split('@')[1]} min | {v:.2f} |" for k, v in feats[:16]]
        lines.append("")
        lines += ["EAS thresholds, pooled (event windows vs control windows):", "", "| Threshold @ lookback | Event windows above | Control windows above (false-positive rate) |", "|---|---|---|"]
        for k, v in (pooled.get("thresholds") or {}).items():
            lines.append(f"| {k} | {_f(v['true_positive_rate'], '{:.0%}')} | {_f(v['false_positive_rate'], '{:.0%}')} |")
        lines.append("")
    for sym, r in per_coin.items():
        g = r.get("grid") or {}
        lt = r.get("lead_time") or {}
        lines.append(f"### {sym}")
        lines.append(f"- Lead time: {lt.get('detected', 0)}/{lt.get('events', 0)} events had an unbroken warning (EAS ≥ {cfg.score.possible_eas:g}) reaching the start; median lead {_f(lt.get('median_lead_min'), '{:.0f} min')}. Median EAS by minutes before the start: " + ", ".join(f"{o}m: {v:.0f}" for o, v in (lt.get("median_eas_by_offset") or {}).items()))
        for name in ("possible", "confirmed"):
            gs = g.get(name) or {}
            if gs:
                lines.append(f"- Scanner view at EAS ≥ {gs['threshold']:g}: flagged {gs['flagged']} of {g.get('windows', 0)} 15-minute grid points ({_f(gs.get('flag_rate'), '{:.1%}')}); {gs['true_positives']} were followed by a surge within {g.get('surge_window_min')} min, {gs['false_positives']} were not (false-positive rate {_f(gs.get('false_positive_rate'), '{:.0%}')}, precision {_f(gs.get('precision'), '{:.1%}')}, recall {_f(gs.get('recall'), '{:.0%}')}, missed surges {gs.get('missed_surges')}).")
        st = r.get("strongest") or []
        if st:
            lines.append("- Strongest features at 60 min: " + ", ".join(f"{FEATURE_NAMES.get(s['feature'], s['feature'])} (AUC {s['auc']:.2f})" for s in st))
        lines.append("")
    lines += ["## 8. Targets (walk-forward)", ""]
    if targets:
        lines += [f"{targets.get('n_setups', 0)} independent setups (EAS ≥ {cfg.targets.setup_eas:g}, overlapping ones merged). Model A: probability of +{cfg.targets.up_pct:g}% before −{cfg.targets.down_pct:g}% from the k nearest past setups. Model B: p{int(cfg.targets.target_quantile * 100)} of the maximum favourable excursion over ALL setups, minus costs.", "",
                  "| Horizon | OOS setups | OOS hit rate | Model Brier vs base | Probability validated | Gross p-target | Net target | OOS coverage | Verdict |", "|---|---|---|---|---|---|---|---|---|"]
        for h, v in targets.get("horizons", {}).items():
            lines.append(f"| {h} min | {v.get('n_oos', 0)} | {_f(v.get('oos_hit_rate'), '{:.0%}')} | {_f(v.get('brier_model'), '{:.3f}')} vs {_f(v.get('brier_base'), '{:.3f}')} | {'yes' if v.get('probability_validated') else 'no'} | {_f(v.get('target_gross_pct'), '{:+.2f}%')} | {_f(v.get('target_net_pct'), '{:+.2f}%')} | {_f(v.get('coverage'), '{:.0%}')} | {v.get('label')} |")
        lines.append("")
    lines += ["## 9-10. Entry and exit methods, out of sample", ""]
    if methods and methods.get("results"):
        lines += [f"{methods['n_setups']} setups: first {methods['n_train']} (chronological) for reference, last {methods['n_test']} held out. Fees {cfg.costs.fee_rate * 100:.2f}%/side, spread {cfg.costs.spread_estimate_pct:g}%, slippage {cfg.costs.base_slippage_pct:g}%/side plus volume impact, bid-side fills, stops filled at the worse of the level and the next open.", "",
                  "| Entry / exit | Test trades | Test net | Win rate | Profit factor | Expectancy / trade | Max DD | Train net (reference) |", "|---|---|---|---|---|---|---|---|"]
        for key, r in sorted(methods["results"].items(), key=lambda kv: -(kv[1]["test"]["net_pct"] or 0)):
            t, tr = r["test"], r["train"]
            pf = "∞" if t["profit_factor"] == float("inf") else _f(t["profit_factor"])
            lines.append(f"| {key} | {t['trades']} | {t['net_pct']:+.2f}% | {_f(t['win_rate'], '{:.0%}')} | {pf} | {_f(t['expectancy_pct'], '{:+.2f}%')} | {t['max_drawdown_pct']:.1f}% | {tr['net_pct']:+.2f}% |")
        best = max(methods["results"].items(), key=lambda kv: kv[1]["test"]["net_pct"] or -1e9)
        by_sym = best[1]["test"].get("by_symbol") or {}
        if by_sym:
            lines += ["", f"Best test combination {best[0]} by pair: " + ", ".join(f"{s} {v:+.2f}%" for s, v in sorted(by_sym.items(), key=lambda kv: -kv[1]))]
        lines.append("")
    lines += ["## Limitations", "",
              "- No historical order book: executable bids are approximated from sell-taker prints or an assumed spread; thin pairs can be worse.",
              "- Individual trades are not available without an API key; aggregated trades were used.",
              "- Surge thresholds, EAS weights and risk limits are starting values, not optimised; nothing here is a profit claim.",
              "- Chronological validation with embargo was used throughout; overlapping setups were merged. Small event counts make every statistic wide.",
              "- All trading is paper. Nothing in this module can place an order.", ""]
    return "\n".join(lines)
