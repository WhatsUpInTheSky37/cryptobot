# Early Warning — accumulation & surge detection (paper only)

Early Warning is an independent research and paper-trading module. It shares nothing with
Crypto Pulse or the TEST/BUY trader except the app shell and the market-data feed code. It
has its own SQLite database (`earlywarning.db` next to `cryptobot.db`), its own navigation
item, and no path to a real order.

## What it does, phase by phase

| Phase | Module | What |
|---|---|---|
| 1 Historical investigation | `earlywarning/data.py` | Verifies exact symbols against Binance.US `exchangeInfo` (never substitutes a similarly named coin; unavailable symbols are recorded and skipped). Downloads 1m and 5m candles (with taker-buy volume and trade counts) and aggregated trades (taker side) for the last N days, resumable, cached locally, rate-limit aware (`X-MBX-USED-WEIGHT`). Individual trades need an API key on Binance.US and are not used; historical order books do not exist. |
| 2 Price explosions | `events.py` | Configurable thresholds (+5%/5m, +10%/15m, +15%/30m, +20%/60m), overlapping windows merged, start = lowest low before the trigger, peak, minutes to peak, reversal (50% give-back), decline after peak, volume, and an executable bid at the peak (last sell-taker print, else an assumed spread). |
| 3 Pre-surge buying | `features.py` | For windows of 24h … 1 min before each start: relative volume, aggressive buy/sell imbalance, trade-frequency change, median and p90 trade-size change, large-trade clusters, buying per % of price move (absorption), volatility compression, activity change, persistence. Every metric is relative to the 7-day baseline before the window. |
| 4 Common patterns | `study.py` | The same features for random non-surge control windows and for a 15-minute grid over the whole history. Per feature: medians and AUC (event vs control). Per threshold: true-positive and false-positive rates, and on the grid: flag rate, precision, recall, missed surges. Lead time from fixed 60-minute windows ending N minutes before the start (the unbroken run that reaches the start). |
| 5 EAS | `score.py` | Six components ranked against the coin's own distribution (the grid), weighted (configurable, not optimised), 0–100. No score without `min_history_obs` observations or with a missing input: INSUFFICIENT DATA, never a neutral stand-in. |
| 6 Scanner | `scanner.py`, `engine.py` | All eligible Binance.US spot markets from `exchangeInfo` + 24h tickers; ranked by volume and trade-count accrual versus the 24h rate, price firm on volume, spread; thin or wide markets excluded; the top N get trade and order-book streams; rescan every 5 minutes. |
| 7 States | `states.py` | QUIET, POSSIBLE ACCUMULATION, ACCUMULATION CONFIRMED, BREAKOUT DEVELOPING, SURGE ACTIVE, POSSIBLE DISTRIBUTION, REVERSAL WARNING, NO DATA. Transitions are recorded. |
| 8 Targets | `targets.py` | Model A: p(+3% before −2%) per horizon from the k nearest past setups; Model B: p25 of MFE over all setups (winners and losers) minus costs. Chronological expanding-window validation with an embargo of one horizon; overlapping setups merged. Validated only with ≥40 OOS setups, Brier better than base rate (A) and ≥70% OOS coverage (B); otherwise TARGET NOT YET RELIABLE. A validated target that is negative after costs is shown as such and never traded. |
| 9–10 Paper | `paper.py`, `backtest.py`, `engine.py` | Entries A (early accumulation), B (confirmed breakout), C (breakout + retest). Exits: fixed, volatility, percentile, partial (50/25/25), trailing, time, distribution-warning. Bid-side fills, fees 0.10%/side, spread, slippage scaled by share of minute volume, stops filled at the worse of the level and the next open. Compared out of sample on the chronologically later 40% of setups. Live: one virtual book, entries only with valid data, a validated positive net target, liquidity, spread and risk limits. |
| 11 Screen | `gui/pages/earlywarning.py` | Live candidates (all 13 items), event explorer with timeline, paper book, research runner and report. |
| 12 Learning | `store.py`, `engine.py` | Every live signal is recorded and resolved after 60 minutes (true/false positive), missed surges logged, precision/recall shown; research statistics by coin. |
| 13 Safety | everywhere | Paper only, no credentials, no leverage, no martingale, no social-media inputs; data validity, spread, liquidity, daily loss, consecutive-loss pause, position cap. |

## Running the research

```bash
# on a machine that can reach api.binance.us (not the build container)
.venv/bin/python -m cryptobot.earlywarning research --days 90
# faster first pass without aggregated trades (trade-size features fall back to candles):
.venv/bin/python -m cryptobot.earlywarning research --days 90 --no-trades
# or from the app: Early Warning → Research → Run research
```

The report lands next to the database (`earlywarning-report.md`, `earlywarning-report.json`) and in the Research tab. `--demo` runs the same pipeline on synthetic data in a separate database (`earlywarning-demo.db`, report `earlywarning-demo-report.md`) and labels the result as such; a real run removes any synthetic rows an older build left in `earlywarning.db`. The market table shows each pair's median daily quote volume and marks pairs under the scanner's minimum as THIN: their surges are mostly a few trades crossing a wide spread, and the live scanner would not select them.

## Database (`earlywarning.db`)

| Table | Contents |
|---|---|
| `ew_markets` | symbol, Binance.US id, status, availability, note (e.g. "not listed; similarly named: …") |
| `ew_klines` | 1m/5m candles: open/high/low/close, base and quote volume, trade count, taker-buy base and quote volume |
| `ew_aggtrades` | aggregated trades: id, time, price, quantity, buyer-is-maker (taker side) |
| `ew_downloads` | what has been fetched (resume points) |
| `ew_events` | surge events with all Phase-2 fields |
| `ew_windows` | features, EAS and component ranks for `event` (per lookback), `lead` (60-min windows ending N min before the start; `lookback_min` holds the offset), `control`, `grid` windows |
| `ew_study` | JSON results: `compare` (per coin), `pooled`, `targets`, `setups`, `methods`, `run` |
| `ew_signals` | live signals with EAS, components, state, outcome (`surge` / `no_surge` after 60 min), MFE/MAE |
| `ew_transitions` | state transitions, including `MISSED → SURGE ACTIVE` |
| `ew_paper_trades`, `ew_equity` | the live paper book |
| `ew_settings` | config and the paper book's persisted state |

## Findings

The build environment has no route to Binance.US, so the four-coin findings (MXC/USD, POND/USD, FLUX/USD, COMP/USD) could not be produced here. The pipeline was validated end to end on synthetic data with planted accumulation episodes; the honest numbers that run produced are instructive about what to expect from the real one:

- On a time grid, a percentile score fires often by construction: EAS ≥ 60 flagged about a quarter of all 15-minute points with ~96% false positives; EAS ≥ 75 flagged ~3% with ~75% false positives and ~85% recall. Precision, not recall, is the problem.
- Entering every EAS ≥ 60 setup lost money after costs with every exit; only the breakout entries with trailing or partial exits were positive out of sample, and win rates were under 20%. Expect real markets to be noisier, not cleaner.
- Lead time on planted episodes was 60–120 minutes; on real data it must be measured, not assumed.

Run the research on your Mac and read the generated report for the real answers to the Phase-14 questions.

## Limitations

- No historical order book on Binance.US; executable bids are approximations.
- Individual trades require an API key; aggregated trades are used.
- Thresholds, weights and limits are starting values.
- Live scanning depends on ccxt.pro streams for Binance.US; the feed code is shared with the rest of the app and reconnects with backoff.
