# Start here

CryptoBot is an automated trading app with a hard spending cap. It watches live prices, runs one strategy, sizes positions inside your budget, attaches a stop-loss to every position, and either **simulates** orders (TEST mode) or **sends** them to your exchange (BUY mode).

**What to expect, honestly.** Over several years most automated strategies make *less* than simply holding Bitcoin. Their real benefit is losing less in crashes. The app never promises profit; it shows you exactly what the strategy would have done (Strategy screen) and what it is doing now (Dashboard and History), and it enforces limits that no strategy can bypass.

**The one rule that matters:** run TEST mode for at least a few weeks and read the numbers before you ever switch to BUY. If TEST is not making money, BUY will not either.

**Safety:** the app only ever asks for an exchange API key with withdrawals disabled. Never enter a wallet private key or seed phrase anywhere, ever.

# Dashboard

- **Mode badge** (top-left of every screen): grey STOPPED, blue TEST, red BUY. If it is red, real money is in use.
- **TEST | BUY toggle**: BUY is locked until 14 days of TEST have passed and an API key that can trade is saved. Switching to BUY shows your TEST results and asks you to type BUY. Switching back to TEST is one click and asks whether to keep or close real positions.
- **START / STOP**: start or stop the bot with the saved strategy and limits. STOP leaves positions as they are and stops managing them.
- **EMERGENCY STOP**: cancels open orders and sells every position back to cash, then stops. Asks for confirmation. In BUY mode these are real market orders.
- **Budget tiles**: allocated (your cap), in use (cost of open positions, fees included), available, today's P&L (since local midnight), equity, and drawdown from the best equity so far.
- **Open positions**: coin, mode, amount, entry and current price, P&L, stop-loss and take-profit levels.
- **Equity curve** and the **trades and signals feed**: every fill and every decision the bot makes, with the reason.
- **Alert bar**: appears when a limit halts the bot, with a Resume button where applicable.

# Market

Live candles for one pair: 1-second bars built from every trade, plus 1-minute and 5-minute bars with recent history. Hollow triangles mark TEST trades, solid triangles mark BUY trades. The exchange and pair chosen here are what the bot trades, together with any extra coins selected on the Coins screen.

The 1-second chart is for watching, not trading: the strategy decides on daily (or hourly) bars. Trading on seconds at retail fees loses money with near certainty.

# Coins

Every pair the exchange offers, with price, 24-hour volume, 24-hour change, spread and 24-hour range. **Suggested only** hides thin markets (under $1M daily volume or spread wider than 0.3%). Thin coins mean bad fills and stop-hunts; stick to the suggested list. Tick coins and press Apply. If the bot is running, STOP and START it to trade the new list.

# Strategy

Pick a strategy and tune it with sliders. Hover any control for a plain-English explanation.

- **EMA crossover** (trend): invested while the fast average is above the slow one. The best-evidenced family. Expect to lag buy-and-hold in strong bull runs; the payoff is smaller drawdowns.
- **MACD** (trend): a variant of EMA crossover with more lag.
- **RSI** and **Bollinger** mean reversion: buy dips, sell rebounds. Work in sideways markets, catastrophic in crashes. Keep the regime gate on and the stop-loss set.
- **Donchian breakout**: buy new highs on strong volume. Most breakouts fail; a few big trends pay for them.
- **Grid**: holds more as price falls through a ladder and less as it rises. Steady small gains in a range, large loss in a crash. Highest tail risk here.
- **DCA baseline**: buys on a schedule and holds. Not a timing strategy, the honest benchmark.

**Modifiers (recommended):** the ADX gate lets trend strategies trade only when the market is trending and mean-reversion only when it is not. Volatility targeting holds less when the market is wild.

**Timeframe:** daily is recommended. Faster bars mean more trades and more fees, and there is no evidence they work better.

# Reading a backtest

Press **Run Backtest**. The app downloads history, applies fees and slippage, and shows:

- **Equity curve** against **buy & hold** and **weekly DCA**. If the blue line is below the grey one, the strategy made less than doing nothing.
- **Worst drawdown**: the biggest peak-to-trough loss. Lower is the usual benefit of a strategy.
- **Sharpe ratio**: return per unit of risk. Below 0.5 is poor, above 1 is good.
- **Trades**: fewer than 20 means the result is mostly luck.
- **Walk-forward**: parameters chosen on earlier data, tested on later data they never saw. If out-of-sample is much worse than in-sample, the strategy is overfit and the full-period result should not be trusted.
- **Verdicts**: the plain-English bullets at the top. Red means the evidence is against the strategy.

Never tune parameters until the backtest looks good. That is fitting the past, and it reliably fails in the future.

# Crypto Pulse

A separate, paper-only research engine for the "Liquidity Pressure Divergence" idea: find moments when aggressive buying is strong but price is not moving (absorption), then act when price breaks out of that absorption with buying still strong. Version 0.3 rebuilt it around one rule: **no data, no score; no evidence, no trade.**

- **Data health first.** Each coin shows DATA OK or INSUFFICIENT DATA with the reason (connection down, no trades yet, trades stale, order book missing / out of sync / stale, market inactive, not enough history). Nothing is scored, recorded or traded from a coin that is not OK. Binance.US order books are rebuilt locally from the exchange's update stream with sequence checks; a gap triggers an automatic resynchronisation.
- **Nine indicators**, each ranked 0–100 against the coin's own last two hours, separately for the bullish and bearish side: Pressure, Absorption, Withdrawal, Replenishment weakening, Cross-market confirmation, Order-flow acceleration, Volume-adjusted price response, Spread expansion (counts against), Volatility change. Their weighted combination is the **Pressure Release Score (PRS)**. The market regime (trend up/down, range, high volatility, quiet) is shown next to the coin.
- **Costs are real.** A round trip pays the exchange fee twice, the spread once and slippage twice (including the impact of your order size on the visible depth). The card shows the expected net profit after all of that.
- **Targets come from the expected move**, not from the fees: the stop is one minute of volatility (0.15–0.6%), the target is a share of how far price can plausibly travel. A quiet market is rejected as "expected profit below fees", never given a bigger target.
- **Market Memory.** Every setup, taken or not, is recorded with its indicator values, costs and regime, then followed for 20 minutes: returns after 30 s, 1, 2, 5, 10 and 20 minutes, the best and worst excursion and when they happened, whether the stop or the target came first, and the net return after costs. Signal values are never changed afterwards. Open the **Market Memory** tab and select any signal to read the complete reasoning.
- **Evidence.** Before a trade the engine finds similar past setups (same direction, same regime when possible). Setups on the same coin within ten minutes count as one observation. It needs a minimum number of independent ones and shows a confidence interval; otherwise it says INSUFFICIENT HISTORICAL EVIDENCE and does not trade. With a fresh database the three paper books stay idle for a while: that is correct, the memory fills from the scanner.
- **Three paper books.** Conservative (A), Balanced (B) and Experimental (C) see the same signals and pay the same costs. A and B demand historical evidence and a positive expected value before they buy; C is the research book: it trades every signal above its strength threshold without waiting for evidence, still at real costs and with the same stops and risk limits, so its equity curve answers "do these signals pay?" from the first day. Only the cost gate is never bypassed: a trade that cannot pay its own fees and spread is refused by every book. Each has its own budget, equity curve and risk limits: 0.25% of equity per trade, 1% daily loss including open positions, two positions, 0.5% total open risk, a pause after four losses in a row, never adding to a position. The Performance tab compares them; the app never picks one for you.
- **Why didn't we trade?** lists every rejection reason per mode with what those signals did afterwards, counting a miss as "profitable" only when it would have been profitable after costs.
- **Machine learning** (Experimental mode) trains only once there are hundreds of resolved signals, is validated on later data it never saw, and is kept only if it beats the plain rules there. The Live tab shows whether a model is in use.
- Spot accounts cannot short, so bearish setups are recorded (as "research") but never traded.

# Early Warning

A separate, paper-only research module that looks for unusual accumulation before a price surge, rather than chasing coins that already moved.

- **Research first.** The Research tab downloads Binance.US candles and aggregated trades for the chosen coins (MXC, POND, FLUX, COMP by default; a symbol that is not listed is reported, never swapped for a look-alike), finds historical surges (+5% in 5 min, +10% in 15, +15% in 30, +20% in 60; configurable), reconstructs the buying in the hours and minutes before each one, and compares it with random ordinary periods. The report says which patterns separated the two, how often the same pattern showed up with no surge afterwards, and how early a warning would have been visible. Run it on a machine with internet access; it is resumable.
- **Early Accumulation Score (EAS).** Six components (relative volume, aggressive buying imbalance, trade-frequency acceleration, price compression, liquidity absorption, persistence) ranked against the coin's own history. INSUFFICIENT DATA until there is enough valid history; never a neutral placeholder.
- **States** are model labels: QUIET, POSSIBLE ACCUMULATION, ACCUMULATION CONFIRMED, BREAKOUT DEVELOPING, SURGE ACTIVE, POSSIBLE DISTRIBUTION, REVERSAL WARNING, NO DATA.
- **Targets** come from validated history only: a low quantile of how far past setups actually moved, minus costs, checked on later data. Until that passes you see TARGET NOT YET RELIABLE and the paper book does not trade.
- **Scanner.** Every eligible Binance.US spot market is screened from 24-hour tickers; the strongest few get live streams. Thin, wide-spread, stale or restricted markets are excluded.
- **Paper book.** Entries only with valid data, a positive validated net target, enough liquidity and spread, and within the risk limits (0.5% per trade, 2% daily loss, 3 positions, pause after 4 losses). Exits default to 50% at the first target, 25% at the second, 25% trailing; stops are never assumed to fill at their level.
- **Event explorer** shows each historical surge, the buying before it, the earliest warning, whether the strategy would have entered and a realistic hypothetical outcome, with a timeline.

# Risk & Budget

These limits apply identically in TEST and BUY and cannot be bypassed by any strategy.

- **Budget**: the hard cap on money at risk, fees included. Raising it shows your results and asks you to type the new amount twice; increases are limited to +50% per step. For the first 30 days of BUY mode it cannot exceed $250.
- **Per-position limit**: the most the bot may hold in one coin, as a percentage of the budget. Exchanges reject orders under about $5–10, so keep this at 25% or more at a $100 budget.
- **Daily loss limit**: if today's loss reaches this, every position is closed and the bot waits until you press Resume tomorrow.
- **Total drawdown limit**: if equity falls this far below its best level, the bot closes everything and waits until you press Restart.
- **Stop-loss**: every position gets one, this many ATRs (a volatility unit) below entry. Trailing stop and take-profit are optional.

# History & Reports

Every trade in both modes, with the expected price, the actual fill, slippage, fees, reason and P&L. Filter TEST, BUY or both. **TEST vs BUY** compares both modes over the same period so you can see whether real fills match the simulation. Daily, weekly and monthly summaries with a P&L chart. **Export CSV** saves the table; **Export BUY trades for taxes** writes FIFO lots (date acquired, date sold, cost basis, proceeds, gain, short/long term) for tax software.

# Setting up an API key

TEST mode needs no key. For BUY mode:

1. On the exchange, create an API key with **Read** and **Spot Trading** enabled and **Withdrawals disabled**. Restrict it to your IP (the app shows your IP).
2. In the app, Settings → Add / replace API key, or step 3 of the setup wizard. Paste the key and secret, press **Test connection**. The app checks the key works and refuses to save one that can withdraw.
3. Press **Save to keychain**. The key goes into your operating system's keychain and is never shown again.

Binance.com does not serve US residents; use Binance.US, Coinbase or Kraken.

# Settings

Exchange key, notifications (desktop pop-ups, Discord webhook, Telegram bot), keep running in the tray when the window is closed, start with the computer, run the setup wizard again, and the engine log.

# What to do if…

- **"Blocked" next to the exchange**: the exchange is not reachable from your country. Pick another on the Market screen.
- **"Buy skipped: below the exchange minimum"**: the order would be under about $5–10. Raise the budget or the per-position limit.
- **"Order was not filled in time"** (BUY): the market moved past the price cap. The bot retries at the next decision. Nothing was bought.
- **"Position adjusted to match the exchange"** (BUY): the exchange holds less of a coin than the app recorded, usually because you sold some by hand. The app trusts the exchange.
- **Daily loss limit reached**: wait until tomorrow and press Resume, or review the strategy first.
- **Drawdown limit reached**: the strategy has lost more than you allowed. Review it before pressing Restart.
- **The app closed but the bot kept running**: that is the tray setting. Use the tray icon to show the window or quit.
- **Nothing happens for a day**: on daily bars the bot decides once a day at 8 PM Eastern (midnight UTC). The Dashboard shows the next decision time.
