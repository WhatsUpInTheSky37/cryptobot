# Trading logic and reliability changes (0.6.0)

This change addresses the October 18 source review of 0.5.3. It does not enable
BUY mode, change an exchange account, or establish that a strategy is profitable.

## Order ownership and protection

The trader is a single-owner worker. Market subscriptions, heartbeats, and command
receipt continue while synchronous exchange requests wait. Pulse has a separate
worker so its similarity search and model fitting cannot block the trading feed.
An independent GUI timer checks whether the engine process died. A local app
lock prevents two desktop engines from opening the same trading book.

Each real order writes its approval and client identifier to `order_journal`
before submission. Ambiguous requests are not resubmitted. Unknown, acknowledged,
and unbooked terminal orders are recovered before entries can proceed. A terminal
order's aggregate fill, position, equity, execution key, and saved ledger commit
in one transaction. Replaying the same execution key is a no-op.

STOP pauses entries while continuing position supervision. Emergency stop and
loss-limit liquidation retain failed or partially sold positions, retry the
remaining quantity, and report confirmed closure rather than counting a partial
fill as a closed position. A pending liquidation also applies to a buy recovered
after its response was lost.

Protected BUY entries require advertised native-stop capability. Native stops
are journaled and reconciled. They are canceled and checked before a manual exit
or a position addition, so a stop fill racing with cancellation is accounted for
before another sell is attempted. Known bought units receive a protective order
even if commission conversion temporarily prevents final accounting.

Venue order types are explicit: Kraken uses `stop-loss`; Coinbase uses a native
stop-limit; Binance-family venues use a native stop market only where the market
metadata lists `STOP_LOSS`, otherwise a stop-limit. A stop-limit can remain
unfilled after a gap. The running worker can escalate a protective exit to a
market sell; that escalation needs a working process and exchange connection.
The interval between an entry and acknowledgment of its protective order is not
an atomic exchange bracket. Venue-specific sandbox/fault testing remains needed.

## Accounting and data

The ledger distinguishes gross fills, base-asset commissions, quote-currency
fees, and externally paid commissions. Third-asset commissions require a quote
valuation and retain their original currency and amount. An unavailable
valuation leaves the order in reconciliation rather than recording a zero fee.
Reports store commission detail and distinguish partial reductions from completed
positions. Position P&L includes every partial sale, and entry prices are weighted
when adding to a position.

A trading book has one quote currency and is scoped to venue, API-key identity
hash, and mode. The hash does not store the API key. Unknown/missing quote cash or
inventory discrepancies block entries; the program no longer deletes holdings or
invents sale proceeds to make balances match. Deposits/budget changes adjust
daily and peak baselines separately from returns. Reducing a budget below deployed
cost is rejected. The first-month BUY cap and the existing BUY gate also run in
the engine, beyond the GUI.

The main trader validates venue identity, quote freshness, tick order, book
synchronization, and market metadata. Held assets stay subscribed when settings
change. Active books pin their venue/watchlist; incompatible mode changes pause
entries and preserve the old book. Early Warning subscribes to held assets and
pending signals even when their scanner rank falls.

Risk inputs reject nonfinite numbers and invalid limits. Approval tokens reserve
cash/inventory while pending and expire. Decimal step flooring and precision
validation prevent increasing approved quantities. Below-minimum residuals remain
visible as dust requiring resolution. Configurable loss-at-stop caps default to
1% of budget per trade and 3% across positions; commissions and slippage are
included in the planned loss. These are planned losses, not guaranteed fills
during a market gap.

## Strategy and research behavior

`OnlineSignals` checkpoints bounded indicator and strategy state. EMA, MACD, RSI,
Bollinger, Donchian, grid, regime gates and volatility targeting are compared
against the batch calculation through a checkpoint at bar 600. Appending data or
restoring a checkpoint does not reseed these indicators at a moving window.

DCA uses a persisted clock and installment count rather than the number of
historical bars in a request. DCA runs in its own scheduled book; timing-strategy
blends and volatility targeting do not change that purchase schedule. Rebalance
thresholds are expressed against the position allocation. Exposure continues to
mean deployed cost as a fraction of the allowed allocation; it is not a current
market-value portfolio weight.

Main-strategy simulation uses shared risk approval, fees, precision/minimum
metadata when supplied, position allocation, and portfolio loss limits. The GUI
passes its saved risk limits into backtests and walk-forward runs. Folds pay for
closing their remaining inventory before the next fold begins. OHLC simulation
still has limited knowledge of intrabar sequencing, execution latency, and
liquidity; it cannot establish exact live fills or a cross-asset live-track record.

Pulse events version their entry-price convention, exact target, stop, horizon,
and venue. The reference plan and mode use the same barriers, including the
spread floor. Evidence for another stop or holding period cannot accept a trade.
Memory forms market-time episodes before selecting neighbors and counts an
episode once, including correlated coins. Its confidence calculation uses the
episode's representative outcome. Cost overrides apply to both assessment and
paper fills. EV can represent target, stop and expiry payoffs rather than forcing
every expiry to be a full stop. Midnight P&L uses start-of-day marked equity.

Outcome restoration preserves first hits, times, last movement, and horizons.
Unobserved gaps before a known first hit are censored; later prices cannot create
a hit after expiry. Executable bid/ask observations replace optimistic mid-price
labels. Old Pulse models/evidence are separated from this policy version.

ML has corrected tied-prediction AUC, payoff-weighted fold aggregation, direction
and barrier/horizon features, and policy/venue filtering. It still produces
experimental probability estimates. The validation report compares signal-level
returns and a rule proxy; it is not an independently verified deployment-policy
portfolio or evidence of guaranteed daily profits. Forward paper validation and
an untouched final period remain necessary before relying on the model.

Early Warning ranks historical observations against earlier data, calculates
entry volatility from prior candles, checks stops on entry candles, and requires
complete forward horizons. Probability/target datasets use the chronological
scanner grid instead of mixing outcome-selected event/control samples. Targets
use favorable movement before a stop and require positive observed first-passage
net outcomes; old pre-fix reports cannot enable paper entries.

Early Warning method comparisons reserve shared cash and replay overlapping
trades in entry/fill order, enforce position/daily-entry limits and pauses, and
embargo training outcomes across the split. The comparison reports its drawdown
sampling convention. Streaming breakout/retest entries require closed-bar
confirmation. A historical next-open fill is still an execution approximation;
partial/trailing intrabar paths, liquidity, and streaming-versus-replay method
parity need finer event replay and forward verification.

## Review finding map

| Review item | Implemented change / explicit validation boundary |
| --- | --- |
| 01 Failed liquidation | Persistent liquidation intent, residual units and retries; terminal closure checks. |
| 02 Uncertain cancellation | Explicit unknown state; incomplete cancellation responses cannot release an order. |
| 03 Duplicate submission | Durable client ID/intent, no blind resubmission, native venue identifiers, deduplicated accounting. |
| 04 Blocking execution | Trader/Pulse workers and prioritized commands keep feed processing independent. |
| 05 Protection while stopped | Native stops, pause versus liquidation, residual supervision; venue/gap testing remains. |
| 06 Reconciliation | Recover journaled executions and compare cash/inventory; discrepancies pause rather than delete holdings. |
| 07 Atomic persistence | Composable SQL transactions and execution-key deduplication. |
| 08 Fee assets | Net received units, quote/external fees, preserved commission detail and valuation gating. |
| 09 Held assets/venue changes | Scoped books, pinned active market, held-asset subscriptions and venue checks. |
| 10 Data health | Fresh synchronized quotes, tick ordering and invalidation on gaps/disconnects. |
| 11 Wallets/funding | One funded quote per book; budget/free-cash checks and cash-flow baselines. |
| 12 Invalid inputs/filters | Bounded models, finite checks, reservations, expiry, Decimal flooring and dust reporting. |
| 13 Simulation consistency | Shared risk approval and configured limits; exact execution/portfolio parity remains a validation boundary. |
| 14 DCA clock | Persisted elapsed-time schedule with a separate scheduled book. |
| 15 Rolling-window seeds | Bounded, checkpointed indicators with batch-equivalence regressions. |
| 16 Allocation/risk | Allocation-based thresholds and cost-inclusive per-trade/aggregate loss caps. |
| 17 Settings | Canonical nested risk handling; UI accurately states which changes need a new book. |
| 18 Walk-forward curves | Explicit charged liquidation at fold boundaries; same configured risk limits. |
| 19 Exact probabilities | Policy/venue/barrier/horizon-matched evidence and feature vectors. |
| 20 Label/payoff semantics | Executable quotes, exact expiry horizon and explicit expiry payoff. |
| 21 Restored outcomes | Preserved first-hit/timing/last-move state and gap censoring. |
| 22 Daily P&L | Equity change since midnight instead of lifetime unrealized P&L. |
| 23 Cost assumptions | Fee override reaches fills; depth impact participates in approval sizing. |
| 24 Dependence/drift | Episodes formed before neighbor selection, horizon-sized grouping and policy isolation; long-term drift needs data. |
| 25 ML policy | Ties, direction/barrier features and weighted folds corrected; deployed-policy forward validation remains experimental. |
| 26 Future-data leakage | Chronological ranking and pre-entry volatility. |
| 27 Entry-candle stop | Immediate entry-candle protection and conservative OHLC stop ordering. |
| 28 Censoring/sampling | Complete horizons and scanner-grid opportunity samples. |
| 29 Achievable targets | MFE before stop plus positive first-passage net gate; policy viability still needs forward evidence. |
| 30 Rotating scanner | Held/pending assets remain in subscriptions and initialized state. |
| 31 Missing books | Explicit synchronization requirement and stale-quote execution refusal. |
| 32 Partial P&L | Whole-position realized result drives wins, losses and pause counters. |
| 33 Streaming/research methods | Correct entry boundary and closed breakout/retest checks; precise event replay remains needed. |
| 34 Funded research | Shared-cash overlapping-trade replay and commission-inclusive cash caps. |
| 35 Round trips | Partial reductions separated from completed positions; cumulative position results and net units. |
| 36 Policy/watchdog | Engine BUY/budget gates, per-command error containment and independent crash detection. Calendar TEST age is still not evidence of active uptime. |
| 37 Regression coverage | Failure, restart, transaction, fee, scheduling, causal-data and indicator checkpoint tests. |
| 38 Releases/performance | Frozen dependency lock, PR CI, separate computational workers and bounded indicators. |

## Migration and operational verification

1. Keep an independent copy of the current database and trade exports before
   installing. Schema additions are additive. Existing live state is reconciled
   under a scoped book; missing historical fills or external transfers are not
   automatically classified as trading gains/losses. A changed API key creates a
   distinct account scope and does not prove ownership of a legacy ledger.
2. Regenerate Early Warning research. Pre-`causal-v2` target reports are blocked.
   Pulse starts new venue/policy-scoped paper evidence/model state. Preserve old
   paper reports for comparison; their labels used different assumptions.
3. Exercise TEST mode and injected connection failures. Confirm the visible
   wallet, limits, native stop type, actual stop status, partial quantities,
   unknown-order alerts, and recovery after killing the process between exchange
   acknowledgment and database commit.
4. Verify native order support and client-ID recovery in each venue's supported
   test environment, including cancel-after-fill races, missing fill details,
   base/third-asset fees, price gaps and minimum-order dust. No authenticated
   exchange calls were made as part of developing this branch.
5. Collect independent forward paper observations and an untouched final test
   period. The 14-day TEST gate is an elapsed-calendar policy and must not be
   interpreted as 14 days of healthy observation, statistical significance, or
   approval of a particular strategy. Full native protection, exchange-fill
   behavior, calibrated probabilities and profitability require operational data.

## Reproducing checks

```sh
uv sync --frozen --extra dev --python 3.12
QT_QPA_PLATFORM=offscreen uv run --frozen --extra dev pytest -q
```

The regression suite uses fake exchanges and synthetic market data. It does not
place orders. Packaging CI builds macOS and Windows installers from the same
locked dependency resolution after the tests pass.
