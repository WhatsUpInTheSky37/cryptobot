# Changelog

## 0.5.0 — tuned from the first day of real data (paper only)

What the first day showed: every Crypto Pulse trade on Binance.US lost (stops hit within seconds on thin alts whose spread was wider than the stop; expiries at a flat price; targets never reached because thirty similar setups from the same two hours looked like evidence), and the Early Warning research on the four seed coins was mostly noise on pairs too thin to trade. Changes:

Crypto Pulse
- The stop must sit at least `stop_spread_mult` (2) spreads below the fill; when that distance exceeds the maximum stop the trade is refused with `stop_inside_spread` instead of being stopped by the next print.
- Similar setups must span at least `min_evidence_span_h` (24) hours of market time before they count as evidence.
- Expected value uses the lower confidence bound of the probability (`conservative_p`), in every mode and in the recorded signal.
- The Market page's slippage setting can raise Pulse's slippage assumption but no longer zero it.
- The status line says when round-trip costs exceed the expected move on every coin (the case on a 1.2% fee tier), so "no trades" reads as the cost gate, not a fault.
- Market Memory tab: an indicator-edge line (AUC per indicator over resolved long signals).

Early Warning
- Surges must close above a threshold (`confirm_on_close`); an event's start is the last low before the move; `peak_truncated` marks peaks at the end of the search window (shown as "60+").
- Events carry the trades and quote volume of the surge itself and a `tradeable` flag (`tradeable_quote_volume` 2,000, `tradeable_min_trades` 20); the pattern study, lead times, targets and methods use tradeable events only (`study_tradeable_only`), while controls stay clear of every surge.
- Optional volatility-relative thresholds (`vol_multiple`): each threshold is at least that multiple of the pair's trailing 60-minute range.
- `universe_top` / `--top N` researches the N most traded Binance.US pairs instead of the seed coins.
- Observed spread per pair from taker prints replaces the assumed spread in the method backtest and is shown in the report.
- EAS threshold sweep by expected value (in-sample, labelled as such) in the report.
- `python -m cryptobot.earlywarning export` and an Export CSV button write candles, events, feature windows, signals and paper trades.

## 0.4.1 — Early Warning research: demo data kept apart, thin pairs named

- Demo runs now use their own database (`earlywarning-demo.db`, report `earlywarning-demo-report.md`). A demo run analyses only its synthetic markets, and a real run removes any synthetic rows an earlier demo left in `earlywarning.db`, so real and generated data can no longer be pooled in one report or feed the live scanner's targets.
- Aggregated-trade download seeds its first time window from the first candle that actually traded and walks forward through idle hours. Pairs younger than the requested history (or idle at its start) previously came back with no trades at all, which silenced every trade-based feature.
- The report's market table gains a median daily quote volume column; pairs below the scanner's minimum are flagged THIN, with a note that their surge statistics describe fills a position could not have had.

## 0.4.0 — Early Warning: accumulation & surge detection (paper only)

A new, independent module and navigation item. See `docs/early-warning.md`.

- `earlywarning/data.py`: Binance.US exchangeInfo verification (exact symbols only), resumable 1m/5m candle and aggregated-trade downloads with rate-limit backoff, cached in a separate `earlywarning.db`.
- `earlywarning/events.py`: configurable surge thresholds, merged windows, start/peak/reversal/decline/volume and executable-bid estimates.
- `earlywarning/features.py`, `study.py`: pre-surge windows (24h … 1m), control windows, a 15-minute grid, per-feature AUC, threshold true/false-positive rates, grid precision/recall/missed surges, lead-time profiles.
- `earlywarning/score.py`, `states.py`: Early Accumulation Score with INSUFFICIENT DATA semantics; eight market states with recorded transitions.
- `earlywarning/targets.py`: Model A (probability) and Model B (MFE quantile) with chronological walk-forward, embargo, overlap merging; TARGET NOT YET RELIABLE and "validated but not tradeable" labels.
- `earlywarning/paper.py`, `backtest.py`: entries A/B/C, seven exit methods including 50/25/25 partials, bid-side fills, no guaranteed stops, target-must-cover-costs gate, out-of-sample method comparison.
- `earlywarning/scanner.py`, `engine.py`: market-wide screening, detailed streams for the top candidates, live EAS/state, signal bookkeeping (true/false positives, missed surges), independent paper book with circuit breakers.
- `earlywarning/cli.py`: `python -m cryptobot.earlywarning research [--days N] [--demo] [--no-trades]` writes the report.
- Screen: Live candidates, Event explorer with timeline, Paper trading, Research.
- Nothing else changed: Crypto Pulse, the TEST/BUY trader, credentials and limits are untouched.

## 0.3.0 — Crypto Pulse: adaptive, evidence-driven paper engine

Crypto Pulse stays **paper only**. Nothing in this release touches API credentials, the TEST/BUY trader, leverage, margin or futures. Binance.US remains the default exchange for Pulse; Kraken and Coinbase keep working.

### Phase 1 — data validation and order-book accuracy
- `pulse/health.py`: per-coin data health (`disconnected`, `no_data`, `stale_trades`, `no_book`, `book_unsynced`, `stale_book`, `inactive`, `insufficient_data`, `ok`). No score, signal or trade is produced unless the status is `ok`. The screen shows **INSUFFICIENT DATA** with the reason instead of placeholder values; the old neutral "50" scores are gone (`SymbolFeatures.scores` now returns `None` until warmed up).
- Inactive markets (fewer than 2 trades/min over 5 minutes by default) are excluded automatically for a cooldown period.
- `data/orderbook.py`: a local order book that follows the Binance/Binance.US sequencing rules (REST snapshot `lastUpdateId`, buffer, drop `u <= lastUpdateId`, first event `U <= lastUpdateId+1 <= u`, then `U == previous u + 1`). Gaps and crossed books mark the book unsynced, publish an unsynced `BookEvent`, and trigger an automatic resync; a periodic snapshot verification catches silent drift. Too many resyncs per minute fail the feed so it reconnects with backoff. Other exchanges keep ccxt's book but it is validated (nonce going backwards, crossed, empty side) and resubscribed after repeated problems. `BookEvent` carries `seq` and `synced`; a new `BookSyncEvent` reports state changes.
- `pulse/costs.py`: a full cost model. Round trip = taker fee ×2 (the exchange's actual fee, or a configured override) + the quoted spread + slippage ×2 (base + order-book depth impact for the intended size). Targets come from the **expected movement** (1-minute range scaled by √time, replaced by the median favourable excursion of similar past setups when there is evidence) and are never raised to cover fees. Every trade must show a positive expected value after costs and a minimum reward-to-risk after costs; otherwise it is rejected with the reason.

### Phase 2 — pressure detection
- Four new indicators join the original five: order-flow acceleration, volume-adjusted price response, spread expansion (negative weight) and short-term volatility change. Market regime (`trend_up`, `trend_down`, `range`, `high_vol`, `quiet`, `unknown`) is classified from 1-minute bars.
- All indicators are ranked against each coin's own rolling distribution (mid-rank for ties). Bullish and bearish sides are ranked separately; a bearish score is never 100 minus the bullish one.
- Weights are configurable (`PulseConfig.weights`) and normalised; they are set by hand, never optimised against the history.

### Phase 3 — Market Memory
- `pulse_events` gained signal-time columns (new indicators, regime, bid/ask/depth, cost breakdown, expected move, R/R, evidence, confidence interval, data health, breakout confirmation, a JSON blob of raw and ranked features) and outcome columns (first hit, time to MFE/MAE, hypothetical net return after costs, returns after 30 s, 1, 2, 5, 10 and 20 minutes). `Store.update_pulse_event` only accepts outcome columns, so later prices cannot leak into signal-time values.
- `pulse/outcomes.py` follows every recorded signal, taken or not, using only prices stamped after the signal; progress is flushed every minute, pending signals are reloaded after a restart, and signals that were not watched for the full horizon are marked `abandoned`.
- New `pulse_decisions` table: what each mode decided for each signal, with a reason code and text, plus the paper P&L when taken.
- Existing databases are migrated in place (`Store._migrate` adds missing columns).

### Phase 4 — historical similarity
- `pulse/memory.py`: standardised nearest neighbours over the signal vector, same direction, same regime when there is enough of it. Signals on the same coin within 10 minutes count as one observation. The estimate gives p(target first), p(stop), expected net return, expected holding time, the number of independent observations and a Wilson 95% interval. Below the minimum it reports **INSUFFICIENT HISTORICAL EVIDENCE** and no probability. The old prior (45%) is displayed for reference only and can no longer accept a trade.

### Phase 5 — three paper modes
- `pulse/paper.py`: Conservative (A), Balanced (B) and Experimental (C) each keep their own cash, positions, equity curve (`equity` table, modes `PULSE-A/B/C`) and trades (`trades` table, same modes). All three see the same signals and use the same cost model and simulated executor; fills are at the ask/bid plus depth impact and the actual fee, not at the mid. The screen shows all three side by side; nothing selects "the best one".

### Phase 6 — risk
- Per mode: 0.25% of **equity** risked per trade, 1% daily loss limit counting realized **and unrealized** P&L, 2 open positions, 0.5% aggregate open risk, pause after 4 consecutive losses. No adding to a position, no averaging down, no size scaling by streak, no trading on unhealthy data. Every order still goes through the shared `RiskManager`.

### Phase 7 — why didn't we trade
- `pulse/analysis.py`: rejected decisions grouped by mode and reason with how many later hit the target and how many would have been profitable **after costs**; outcomes by coin and by regime; per-mode statistics (profit factor, max drawdown, share of profitable days, expectancy).

### Phase 8 — machine learning
- `pulse/ml.py`: LightGBM when installed, otherwise a dependency-free logistic regression. Chronological walk-forward with an embargo of one outcome horizon, out-of-sample comparison against the rule-based gate on the same folds, minimum 300 resolved signals with at least 50 of each outcome. The model is kept (and used by Mode C) only when it improves out-of-sample net return per trade. Retrains every 4 hours when there is enough data, or on demand from the screen.

### Phase 9 — screen
- Four tabs: Live (data health per coin, bullish and bearish scores for every indicator, expected net profit after costs, the three mode cards), Performance (statistics, equity curves, results by coin and regime), Why didn't we trade?, and Market Memory (filterable signal history; selecting a signal shows the complete reasoning).

### Phase 10 — tests
- 34 new tests (order book, health, costs, regime, outcomes, memory, store migration and immutability, paper modes, risk limits, ML validation, GUI rendering). The whole suite: 152 passed, 1 skipped.
