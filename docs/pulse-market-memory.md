# Crypto Pulse 0.3 — design notes, database, and what live deployment would require

## What changed and why

The 0.2 engine traded a fixed 0.15% stop / 0.30% target against ~0.30% of round-trip cost. That needs roughly an 85% win rate just to break even, and "research mode" let it paper-trade without evidence. 0.3 replaces that with:

1. **Data first.** A coin gets a score only when the exchange is connected, trades are fresh, the order book is synchronised and fresh, the market is active, and the rolling history is long enough. Anything else shows INSUFFICIENT DATA.
2. **Honest costs.** Fee ×2 + spread + slippage ×2 with depth impact, computed per trade for the intended size.
3. **Adaptive exits.** Stop = one 1-minute volatility unit (clamped 0.15–0.60%). Target = 60% of the expected move, which is the 1-minute range scaled by √(holding minutes) or, with evidence, the median favourable excursion of similar past setups. A target is never inflated to cover fees; if the expected move cannot cover costs with 20% to spare the setup is rejected as "target below costs".
4. **Evidence before money.** Every signal is recorded and followed for 20 minutes with the same cost assumptions. Trades need enough *independent* similar past signals (overlapping ones on the same coin count once) and a positive expected value and reward/risk after costs. With no history the three paper books sit idle and the memory fills from the scanner alone.

## Database

All tables are SQLite (`cryptobot.db`, WAL). Existing files are migrated in place: `Store._migrate` adds any column the models have and the file lacks.

### `pulse_events` (one row per recorded signal, immutable except for outcome columns)

| Group | Columns |
|---|---|
| Identity | `id`, `ts_ms`, `exchange`, `symbol`, `direction` (long/short), `status` (`signal` = long, handed to the modes; `research` = short, never traded; legacy rows: `entered`/`rejected`), `reason` |
| Scores | `prs`, `f`, `a`, `w`, `r`, `c`, `acc`, `vpr`, `sx`, `vx` (0–100 ranks), `features_json` (raw values, both sides, for ML) |
| Market at signal time | `mid`, `bid`, `ask`, `bid_depth`, `ask_depth`, `imbalance`, `vol_pct`, `spread_pct`, `regime`, `health`, `confirmed` (price cleared the absorption level) |
| Plan and costs | `expected_move_pct`, `target_pct`, `stop_pct`, `fee_pct`, `spread_cost_pct`, `slip_pct`, `cost_pct` (total), `rr_after_costs` |
| Evidence | `similar_n` (raw neighbours), `similar_eff` (independent), `similar_p`, `p_stop`, `ci_low`, `ci_high`, `ev_pct`, `evidence` (`sufficient`/`insufficient`) |
| Outcome (the only updatable columns) | `outcome` (`pending`, `target`, `stop`, `expired`, `abandoned`), `first_hit`, `first_hit_ms`, `mfe_pct`, `mae_pct`, `time_to_mfe_ms`, `time_to_mae_ms`, `ret_30s`, `ret_1m`, `ret_2m`, `ret_5m`, `ret_10m`, `ret_20m`, `net_pct` (hypothetical net return after costs), `resolved_ts_ms`, `trade_pnl` (legacy) |

`net_pct` = +target − cost if the target was hit first, −stop − cost if the stop was hit first, otherwise the 20-minute move − cost. It uses the same fee, spread and slippage as a paper fill would, so a rejected signal's "what if" is comparable with a taken one's.

### `pulse_decisions`

`id`, `event_id`, `ts_ms`, `mode` (A/B/C), `symbol`, `direction`, `accepted`, `code` (machine reason such as `ev_below_margin`, `insufficient_evidence`, `daily_loss_limit`), `reason` (text), `trade_pnl` (filled when the paper position closes).

### Shared tables

- `trades` with `mode` = `PULSE-A`, `PULSE-B`, `PULSE-C`: paper fills with price, fill price, fee, slippage and P&L. The old `PULSE` mode rows from 0.2 remain readable on the History page.
- `equity` with the same modes: one point per minute and on every trade.
- `settings`: `pulse.config`, `pulse.mode.<key>.state` (cash, open positions, daily counters, streaks — restored on restart), `pulse.ml.model` (validated model as JSON), `pulse.ml.report`, `pulse.ml.note`, `pulse.events_recorded`.

## Modes

| | A Conservative | B Balanced | C Experimental |
|---|---|---|---|
| Minimum PRS | 85 | 75 | 65 |
| Breakout confirmation | required | optional | optional |
| Order-flow acceleration score | ≥ 60 | ≥ 70 | any |
| Minimum EV after costs | +0.05% | +0.02% | ≥ 0 |
| Minimum reward/risk after costs | 1.5 | 1.0 | 0.8 |
| Independent similar setups needed | 50 | 30 | 15 |
| Probability source | memory | memory | validated ML model, else memory |
| Max spread / 1-minute range | 0.08% / 1.0% | 0.10% / 1.5% | 0.12% / 2.0% |
| Regimes | trend_up, range, quiet | all | all |

Risk defaults shared by all three: 0.25% of equity per trade, 1% daily loss (realized + unrealized), 2 positions, 0.5% aggregate open risk, pause 60 minutes after 4 consecutive losses, one position per coin, no adding.

## What the numbers say so far

- At Binance.US standard fees (0.10% per side) plus a realistic spread and slippage, a round trip costs about 0.26–0.35% on BTC/ETH and more on thinner pairs. A 0.2% stop and 0.4% target cannot pass a 1:1 reward/risk-after-costs gate at any win rate; the engine tests show the gate rejecting exactly that. The adaptive target is what makes a trade possible, and only in markets that move enough.
- A stop-out costs about twice the nominal risk once costs are paid (the tests pin this: a 0.2% stop loses ~0.39% of equity). The daily loss limit counts the real number.

## Evidence required before considering live deployment

None of the following is met today; all of it is measurable on the Performance and Market Memory tabs.

1. At least **500 resolved long signals** across **at least 30 trading days** and at least two different market regimes, with no data-health exclusions during the sessions that produced them.
2. For the candidate mode: at least **100 paper trades**, net profit factor **> 1.3 after costs**, maximum drawdown **< 5%** of the paper budget, and the Wilson lower bound of the hit rate **above the break-even rate** shown on screen.
3. The rejected-trade analysis must not show a reason whose rejected signals would have been clearly more profitable than the taken ones (that would mean the gate is wrong, not that it should be loosened).
4. Results split by coin and by regime must each be non-negative; one coin or one regime carrying everything is not evidence.
5. If Mode C is the candidate, the ML report must show out-of-sample improvement on every fold, not on average.
6. A second paper period of **at least 14 days** after any parameter change, with the parameters frozen.
7. A written review of the paper fills against the exchange's real fills in BUY mode for the main bot (History → TEST vs BUY), to confirm the slippage model is not optimistic.

Even then, live trading is a separate decision with its own gate (the main bot's 14-day TEST, key permission check and typed BUY confirmation). Pulse itself has no live path and this release adds none.

## Not implemented / limitations

- The native depth stream is implemented for Binance and Binance.US against the documented protocol and tested with recorded message sequences, but it could not be exercised against the live exchange from the build environment (no exchange connectivity there). The first live run should be watched on the Live tab: book sequence numbers should climb and the health badge should stay DATA OK.
- LightGBM is optional and not installed by default (`pip install lightgbm` to use it); the logistic fallback is used otherwise and is reported on screen.
- Expected holding time and the adverse-excursion regressor are reported from the memory, not predicted by the model (the classifier predicts target-before-stop only).
- The screener/coin list is still the Coins page; Pulse does not choose coins itself.

## Tuning after the first real day (0.5.0)

The first day on Binance.US produced eight paper trades, all losers: stops hit seconds after entry on thin alts (the stop sat inside the spread), expiries at a flat price, and targets never reached. Four rules changed:

1. **Stops outside the spread.** The stop distance is at least `stop_spread_mult` (2) times the quoted spread. If that exceeds `max_stop_pct` the trade is refused (`stop_inside_spread`).
2. **Evidence must span time.** The similar setups behind a probability must cover at least `min_evidence_span_h` (24) hours; thirty setups from one afternoon describe one afternoon.
3. **Conservative probability.** Expected value and reward/risk use the lower bound of the Wilson interval (`conservative_p`), so a 90% hit rate from 30 setups is treated as roughly 75%.
4. **Slippage never zero.** The Market page's slippage setting can only raise the assumption.

The status line now also states when round-trip costs exceed the expected move on every coin (Coinbase at a 1.2% taker fee, for example): Pulse will not trade there, and that is the cost gate working. The Market Memory tab shows an indicator-edge line: the AUC of each indicator over resolved long signals, which is how weights should eventually be chosen, once there are dozens of resolved signals across several days.
