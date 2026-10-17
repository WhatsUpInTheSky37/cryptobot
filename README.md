# CryptoBot

Budget-capped automated crypto trading desktop app with TEST and BUY modes.
See [PLAN.md](PLAN.md) for the architecture, strategy analysis, wireframes and milestones.

**Status: Milestone 6 (help, reports, installers) done.** BUY mode is locked until 14 days of TEST mode have passed. The bot trades in TEST mode only: live prices, simulated orders, no real money. BUY mode is locked until Milestone 5.

## What works now
- Dark-themed desktop app with navigation, always-visible mode badge (STOPPED / TEST / BUY), clock and connection status.
- Market screen: pick an exchange (Kraken, Coinbase Advanced, Binance.US; Binance.com is listed but blocked in the US) and a pair; live candles at 1s (built from every trade), 1m and 5m (recent history loaded from the exchange, then live).
- Trading engine runs in its own process, reconnects automatically after a network drop, and backfills the candles it missed.
- Local SQLite database (settings, 1m/5m candles, audit log; trade/position tables ready for later milestones).
- Settings page shows the live engine log.
- BUY mode: real orders via marketable limit orders capped at 0.25% slippage, partial fills handled, retries never duplicate an order (client order ids), actual fees and fill prices recorded, restart reconciles positions against the exchange. Locked until 14 days of TEST, an API key that can trade, and typing BUY in a dialog that shows the TEST results. Budget capped at $250 for the first 30 days of BUY. BUY → TEST is one click (keep or close real positions). Every switch is logged.
- History & Reports: all trades with expected vs actual price and slippage, TEST / BUY / Both filter, TEST-vs-BUY comparison over the same period, daily / weekly / monthly summaries, CSV export, and a FIFO tax-lot export for BUY trades.
- First-run setup wizard: exchange → API key (tested; keys that can withdraw are refused; stored in the OS keychain) → budget and limits → coins → strategy → start in TEST mode. Re-runnable from Settings.
- Coins screen: every pair on the exchange with price, 24h volume, change, spread and range; "Suggested only" hides thin markets; the bot trades the selected list.
- Settings: API key add/remove, desktop pop-ups, Discord webhook and Telegram alerts (secrets in keychain), keep running in the system tray, start with the computer, engine log.
- Closing the window keeps the bot trading in the tray; Quit from the tray icon stops it.
- Crypto Pulse (paper only): the Liquidity Pressure Divergence research engine. Computes five order-flow signals (pressure, absorption, liquidity withdrawal, replenishment weakening, cross-market confirmation) from live trades and the order book, ranks each against the coin's own recent history, scores the Pressure Release Score, waits for price confirmation, paper-trades with a volatility-scaled stop and 2:1 target through the same risk manager, applies the expected-value gate with real costs, and records every setup with its outcome (Market Memory) to estimate win rates from similar past events. Long-only on spot. Cannot be backtested from candles; evidence accumulates live.
- Built-in Help: a plain-English guide for every screen, every strategy, reading a backtest, limits, API keys and troubleshooting (Help menu or the Help page).
- Reports: daily / weekly / monthly summaries with a P&L chart.
- Dashboard: START / STOP / EMERGENCY STOP, the TEST | BUY toggle (BUY locked), budget gauge (allocated / in use / available / today's P&L / equity / drawdown), open positions with stops, equity curve, trade and signal feed. Halts itself at the daily-loss or drawdown limit and shows an alert with Resume.
- Risk & Budget screen: budget (change dialog: type twice, +50% max step), per-position limit, daily loss limit, drawdown limit, stop-loss / trailing / take-profit, TEST slippage.
- Trading engine: the same signal function as the backtester runs on closed bars of the strategy timeframe; every order needs a risk-manager approval (hard budget cap incl. fees and slippage, per-position cap, exchange minimums, mandatory stop) and is consumed once by the executor. TEST mode uses a simulated executor; the real one cannot even be constructed yet. State is saved so a restart reloads open positions.
- Strategy screen: seven strategies (EMA, MACD, RSI, Bollinger, Donchian, grid, DCA) plus the ADX regime gate and volatility targeting; sliders with plain-English tooltips; one-click backtest that downloads history, applies fees and slippage, compares with buy-and-hold and weekly DCA, runs walk-forward validation, and explains the result in plain English. The configuration you run is saved as what TEST / BUY mode will trade.

## Installing
- **macOS**: run `./scripts/build_mac.sh` (or download the CI artifact) and open `dist/CryptoBot.dmg`; drag CryptoBot to Applications. The app is ad-hoc signed, so the first launch needs right-click → Open. Notarising with an Apple Developer ID removes that step (set `CODESIGN_IDENTITY`).
- **Windows**: run `.\scripts\build_windows.ps1` (needs Python 3.12 and Inno Setup 6) or download the CI artifact `CryptoBot-Setup-<version>.exe`. SmartScreen warns about unsigned installers; click "More info → Run anyway", or sign the installer.
- GitHub Actions (`.github/workflows/build.yml`) runs the tests and builds both installers on every push to main.

## Running during development

```bash
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -e ".[dev]"
.venv/bin/python -m cryptobot
.venv/bin/python -m pytest
```

Data and logs live in the OS app-data folder (`~/Library/Application Support/CryptoBot` and `~/Library/Logs/CryptoBot` on macOS).

Set `CRYPTOBOT_SCREENSHOT=/path/out.png` (optionally `CRYPTOBOT_SCREENSHOT_AFTER=seconds`, `CRYPTOBOT_SCREENSHOT_TF=1|60|300`) to run the app, capture the window, and exit. This is how the GUI is verified without a human.

## Layout
```
cryptobot/
  app.py            GUI entry point
  config.py         paths, exchange list, defaults
  core/             models (Tick, Bar, Mode), bar aggregator, engine->GUI events
  strategy/         indicators, strategy library, regime modifiers, config, THE shared signal path
  backtest/         simulator (budget cap, stops, fees), benchmarks, metrics, walk-forward, verdicts
  data/             ccxt.pro live feed with reconnect, REST backfill, history download, account check, market ranking
  secrets.py        OS keychain access (keyring); autostart.py login item; notify.py Discord/Telegram
  db/               SQLAlchemy schema + Store (SQLite, WAL)
  engine/           engine process entry, risk manager (the choke point), ledger, executors, trader loop
  pulse/            Crypto Pulse: features, scoring config, market memory, paper-trading engine
  gui/              theme, controller (process bridge), main window, pages, widgets
tests/              pytest + hypothesis
```
