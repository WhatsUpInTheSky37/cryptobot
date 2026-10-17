"""Command line: ``python -m cryptobot.earlywarning research [--symbols ...] [--days 90] [--demo]``.

``research`` runs phases 1-4, 8 and 9-10 against Binance.US (or synthetic data with --demo),
stores everything in the Early Warning database and writes a markdown report next to it.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

from cryptobot.config import AppPaths
from cryptobot.earlywarning import backtest, study, targets
from cryptobot.earlywarning.config import EWConfig
from cryptobot.earlywarning.data import BinanceUSData, download_all, make_client
from cryptobot.earlywarning.events import detect_events, observed_spread_pct, tradeable
from cryptobot.earlywarning.report import build_report
from cryptobot.earlywarning.store import EWStore
from cryptobot.earlywarning.synth import synthetic_aggtrades, synthetic_klines

log = logging.getLogger("earlywarning")


SYNTHETIC_NOTE = "synthetic"


def ew_db_path(paths: AppPaths | None = None, *, demo: bool = False) -> Path:
    """The Early Warning database. Demo runs use their own file so generated data never mixes with
    real Binance.US data, and the live scanner never picks up targets validated on synthetic candles."""
    return (paths or AppPaths.default()).data_dir / ("earlywarning-demo.db" if demo else "earlywarning.db")


def report_path(db: Path, *, demo: bool = False) -> Path:
    return db.with_name("earlywarning-demo-report.md" if demo else "earlywarning-report.md")


def purge_synthetic(store: EWStore, progress: Callable[[str], None] = print) -> list[str]:
    """Remove markets written by an earlier demo run (and all their data) from a real database."""
    gone = [m["symbol"] for m in store.markets() if m.get("note") == SYNTHETIC_NOTE]
    for sym in gone:
        store.delete_symbol(sym)
        progress(f"{sym}: removed synthetic demo data left by an earlier demo run")
    return gone


def research(store: EWStore, cfg: EWConfig, *, data: BinanceUSData | None = None, demo: bool = False, progress: Callable[[str], None] = print, trades: bool = True, max_trade_rows: int | None = None) -> dict[str, Any]:
    """Phases 1-4, 8, 9-10. Returns the summary dict that is also stored under ew_study['run']."""
    t0 = time.time()
    symbols = list(cfg.seed_symbols)
    if not demo and cfg.universe_top > 0:
        data = data or BinanceUSData(make_client("binanceus"), store)
        top = data.top_markets(cfg.universe_top, tuple(cfg.scanner.quotes))
        if top:
            symbols = top
            progress(f"universe: the {len(top)} most traded Binance.US pairs by 24h quote volume: {', '.join(top)}")
        else:
            progress("universe: the exchange returned no tickers; falling back to the seed symbols")
    if demo:
        progress("DEMO: generating synthetic candles and trades (not market data)")
        recs = []
        for i, sym in enumerate(symbols):
            k, _starts = synthetic_klines(days=min(cfg.days, 45), seed=10 + i, surges=6 + i)
            tr, rows = synthetic_aggtrades(k, seed=20 + i, per_bar=3)
            store.put_market(sym, sym.replace("/", ""), "TRADING", True, True, *sym.split("/"), note=SYNTHETIC_NOTE)
            store.upsert_klines(sym, "1m", [tuple(r) for r in k.tolist()])
            store.upsert_aggtrades(sym, rows)
            rng = store.kline_range(sym, "1m")
            store.put_download(sym, "klines_1m", rng[0], rng[1], rng[2], SYNTHETIC_NOTE)
            store.put_download(sym, "aggtrades", int(tr["ts"][0]), int(tr["ts"][-1]), len(rows), SYNTHETIC_NOTE)
            recs.append({"symbol": sym, "available": True})
        # only this run's synthetic markets are analysed, never real rows that may share the database
    else:
        purge_synthetic(store, progress)
        data = data or BinanceUSData(make_client("binanceus"), store)
        recs = download_all(data, symbols, cfg.days, trades=trades, progress=progress, max_trade_rows=max_trade_rows)
    available = [r["symbol"] for r in recs if r["available"]]
    compare_all: dict[str, Any] = {}
    spreads: dict[str, float | None] = {}
    for sym in available:
        k = store.klines(sym, "1m")
        if len(k) < 2 * 1440:
            progress(f"{sym}: only {len(k)} 1-minute candles; skipping analysis")
            continue
        rng = store.aggtrade_range(sym)
        tr = store.aggtrades(sym, rng[0], rng[1] + 1) if rng else None
        ev = detect_events(k, cfg.surge, tr)
        store.replace_events(sym, ev)
        ev_study = tradeable(ev, cfg.surge)
        progress(f"{sym}: {len(ev)} surge events in {len(k)} candles, {len(ev_study)} tradeable (>= {cfg.surge.tradeable_quote_volume:,.0f} quote volume and {cfg.surge.tradeable_min_trades} trades during the surge)")
        counts = study.build_windows(store, sym, k, tr, ev_study, cfg, exclude_events=ev)
        study.score_windows(store, sym, cfg)
        compare_all[sym] = study.compare(store, sym, cfg)
        sp = observed_spread_pct(tr) if tr is not None else None
        spreads[sym] = sp
        if sp is not None:
            progress(f"{sym}: observed spread (median of taker prints) {sp:.3f}% vs assumed {cfg.costs.spread_estimate_pct:g}%")
        progress(f"{sym}: windows {counts}; lead time median {compare_all[sym]['lead_time'].get('median_lead_min')} min; grid precision@confirmed {compare_all[sym]['grid']['confirmed'].get('precision')}")
    store.put_study("compare", compare_all)
    store.put_study("spread", spreads)
    pooled = study.pooled_compare(list(compare_all.values())) if compare_all else {}
    store.put_study("pooled", pooled)
    setups = []
    for sym in compare_all:
        setups += targets.collect_setups(store, sym, store.klines(sym, "1m"), cfg)
    tr_report = targets.walk_forward_targets(setups, cfg) if setups else {"n_setups": 0, "horizons": {}}
    store.put_study("targets", tr_report)
    sweep = targets.threshold_sweep(setups, cfg, spreads) if setups else {}
    store.put_study("sweep", sweep)
    if sweep.get("best"):
        progress(f"EAS threshold sweep ({sweep['horizon_min']} min): best net per setup {sweep['best']['mean_net_pct']:+.3f}% at EAS >= {sweep['best']['threshold']:g} ({sweep['best']['n']} setups)")
    store.put_study("setups", [{k: v for k, v in s.items() if k != "outcomes"} | {"outcomes": {str(h): o for h, o in s["outcomes"].items()}} for s in setups][:5000])
    progress(f"targets: {tr_report.get('n_setups', 0)} independent setups; " + "; ".join(f"{h}m {v.get('label')}" for h, v in tr_report.get("horizons", {}).items()))
    methods = backtest.run_methods(store, list(compare_all), cfg, spreads=spreads) if compare_all else {}
    store.put_study("methods", methods)
    summary = {"demo": demo, "exchange": "synthetic" if demo else "binanceus", "days": cfg.days, "symbols": symbols, "available": available, "events": {s: len(store.events(s)) for s in compare_all},
               "finished_ms": int(time.time() * 1000), "seconds": round(time.time() - t0, 1)}
    store.put_study("run", summary)
    return summary


def export_csv(store: EWStore, out_dir: Path) -> list[Path]:
    """Phase-14 data access: everything the research stored, as plain CSV for a spreadsheet or notebook."""
    import csv

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        path = out_dir / name
        keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        written.append(path)

    for m in store.markets():
        sym = m["symbol"]
        tag = sym.replace("/", "-")
        k = store.klines(sym, "1m")
        if len(k):
            write(f"{tag}-candles-1m.csv", [dict(zip(k.dtype.names, (v.item() for v in row))) for row in k])
        write(f"{tag}-events.csv", [{k2: v for k2, v in e.items()} for e in store.events(sym)])
        wins = []
        for w in store.windows(sym):
            row = {"kind": w["kind"], "event_id": w["event_id"], "t0_ms": w["t0_ms"], "lookback_min": w["lookback_min"], "eas": w["eas"]}
            row.update({f"f_{k2}": v for k2, v in (w.get("features") or {}).items() if not isinstance(v, (dict, list))})
            row.update({f"c_{k2}": v for k2, v in (w.get("components") or {}).items() if not isinstance(v, (dict, list))})
            wins.append(row)
        write(f"{tag}-windows.csv", wins)
    write("paper-trades.csv", store.paper_trades("EW", limit=100_000))
    write("signals.csv", store.signals(limit=100_000) if hasattr(store, "signals") else [])
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cryptobot.earlywarning", description="Early Warning research (paper only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("research", help="download data, find surges, study pre-surge patterns, validate targets and methods, write the report")
    r.add_argument("--symbols", nargs="*", help="e.g. MXC/USD POND/USD FLUX/USD COMP/USD (default: the four seed markets)")
    r.add_argument("--days", type=int, default=90)
    r.add_argument("--top", type=int, default=None, help="research the N most traded Binance.US pairs instead of the seed symbols")
    r.add_argument("--db", help="path to the Early Warning database (default: the app data folder)")
    r.add_argument("--demo", action="store_true", help="use synthetic data (no network) to exercise the pipeline")
    r.add_argument("--no-trades", action="store_true", help="skip aggregated trades (faster; trade-size features fall back to candles)")
    r.add_argument("--max-trade-rows", type=int, default=None, help="cap aggregated trades per symbol per run (resumable)")
    rp = sub.add_parser("report", help="print the last report")
    rp.add_argument("--db")
    ex = sub.add_parser("export", help="write candles, events, windows and paper trades as CSV files next to the database")
    ex.add_argument("--db")
    ex.add_argument("--out", help="output folder (default: earlywarning-export next to the database)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db = Path(args.db) if args.db else ew_db_path(demo=getattr(args, "demo", False))
    store = EWStore(db)
    cfg = EWConfig.model_validate(store.get("config") or {})
    if args.cmd == "report":
        print(build_report(store, cfg))
        return 0
    if args.cmd == "export":
        for f in export_csv(store, Path(args.out) if args.out else db.with_name("earlywarning-export")):
            print(f)
        return 0
    if args.symbols:
        cfg.seed_symbols = args.symbols
    if args.top is not None:
        cfg.universe_top = args.top
    cfg.days = args.days
    summary = research(store, cfg, demo=args.demo, trades=not args.no_trades, max_trade_rows=args.max_trade_rows)
    text = build_report(store, cfg, demo=args.demo)
    out = report_path(db, demo=args.demo)
    out.write_text(text)
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2, default=float))
    print(text)
    print(f"\nreport written to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
