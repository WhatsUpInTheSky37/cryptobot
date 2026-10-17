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

A separate, paper-only research engine for the "Liquidity Pressure Divergence" idea: find moments when aggressive buying is strong but price is not moving (absorption), then enter only when the price breaks out of that absorption with buying still strong.

- **Five signals**, each ranked 0–100 against the coin's own last two hours: Pressure (buy vs sell aggression), Absorption (volume per % of price move), Withdrawal (sell-side depth vanishing), Replenishment weakening (sell-side depth below its norm), Confirmation (BTC and ETH agree).
- **Pressure Release Score (PRS)** = 0.25·F + 0.25·A + 0.20·W + 0.15·R + 0.15·C. Below 50 nothing; 70 prepares; 85 plus a price breakout plus positive imbalance may enter.
- **Exits**: stop one unit of 1-minute volatility below entry (clamped 0.15–0.6%), target twice that, maximum hold 20 minutes.
- **The fee problem**: a round trip costs about 0.3% on Binance.US. With a 0.2% stop and 0.4% target the strategy must win over 80% of trades to break even. The screen shows the break-even win rate and the expected value per trade at all times.
- **Market Memory**: every setup, taken or not, is recorded with what price did next (target first, stop first, or neither within 20 minutes). The hit rate it builds is the only evidence. In research mode the engine paper-trades every confirmed setup so the memory fills with real after-cost results; with research mode off it trades only when similar past setups give a positive expected value, which with no history means never.
- Spot accounts cannot short, so bearish setups are recorded but never traded.

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
