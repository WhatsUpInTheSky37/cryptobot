"""First-run setup wizard."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cryptobot.config import DEFAULT_EXCHANGE, EXCHANGES
from cryptobot.db.store import Store
from cryptobot.engine.risk import RiskLimits
from cryptobot.gui.theme import BLUE, GREEN, MUTED
from cryptobot.gui.widgets.api_key_panel import ApiKeyPanel
from cryptobot.strategy.config import StrategyConfig, StrategySpec
from cryptobot.strategy.library import STRATEGIES

STEPS = ["Welcome", "Exchange", "API key", "Budget & limits", "Coins", "Strategy", "Review"]
FIRST_MONTH_CAP = 250.0


def _page(title: str, blurb: str) -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    h = QLabel(title)
    h.setObjectName("h1")
    lay.addWidget(h)
    b = QLabel(blurb)
    b.setWordWrap(True)
    b.setObjectName("muted")
    lay.addWidget(b)
    return w, lay


class SetupWizard(QDialog):
    def __init__(self, store: Store, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("CryptoBot setup")
        self.setModal(True)
        self.resize(760, 620)
        self.result_exchange = DEFAULT_EXCHANGE
        self.result_symbols: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        self.progress = QLabel("")
        self.progress.setObjectName("muted")
        root.addWidget(self.progress)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self._back)
        self.next_btn = QPushButton("Next")
        self.next_btn.setStyleSheet(f"background: {BLUE}; color: white; font-weight: 600; padding: 8px 20px;")
        self.next_btn.clicked.connect(self._next)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        nav.addWidget(self.next_btn)
        root.addLayout(nav)

        self._build_welcome()
        self._build_exchange()
        self._build_key()
        self._build_budget()
        self._build_coins()
        self._build_strategy()
        self._build_review()
        self._go(0)

    # ---------------------------------------------------------------- pages

    def _build_welcome(self) -> None:
        w, lay = _page(
            "Welcome to CryptoBot",
            "This wizard takes about three minutes. The bot will start in TEST mode: live prices, simulated orders, "
            "no real money. BUY mode stays locked until you have at least 14 days of TEST results to look at.",
        )
        txt = QLabel(
            "<b>What to expect, honestly.</b><br>"
            "Most automated strategies do not beat simply holding Bitcoin over several years; their realistic benefit is "
            "smaller losses in crashes, not bigger profits. The app will show you exactly what the strategy would have "
            "done and what it is doing now, and it enforces a hard budget cap, stop-losses, and daily and total loss "
            "limits that no strategy can bypass.<br><br>"
            "<b>Never</b> enter a wallet private key or seed phrase anywhere. The app only ever asks for an exchange API "
            "key with withdrawals disabled."
        )
        txt.setWordWrap(True)
        lay.addWidget(txt)
        lay.addStretch(1)
        self.stack.addWidget(w)

    def _build_exchange(self) -> None:
        w, lay = _page("Choose your exchange", "You are responsible for confirming the exchange serves your country. From the United States, Binance.com does not; Binance.US, Coinbase and Kraken do.")
        self.ex_group = QButtonGroup(self)
        for i, (ex_id, meta) in enumerate(EXCHANGES.items()):
            r = QRadioButton(meta["name"])
            r.setEnabled(meta["available_us"])
            self.ex_group.addButton(r, i)
            lay.addWidget(r)
            if ex_id == self.store.get_setting("market.exchange", DEFAULT_EXCHANGE):
                r.setChecked(True)
        if self.ex_group.checkedId() < 0:
            self.ex_group.button(0).setChecked(True)
        lay.addStretch(1)
        self.stack.addWidget(w)

    def _build_key(self) -> None:
        w, lay = _page("Connect your exchange (optional for TEST mode)", "TEST mode only needs public price data, so you can skip this and add a key later on the Settings screen. BUY mode will require it.")
        self.key_panel = ApiKeyPanel(self._exchange_id(), self.store)
        lay.addWidget(self.key_panel)
        self.skip_key = QCheckBox("Skip for now")
        self.skip_key.setChecked(True)
        lay.addWidget(self.skip_key)
        lay.addStretch(1)
        self.stack.addWidget(w)

    def _build_budget(self) -> None:
        w, lay = _page("Trading budget and limits", f"The budget is a hard cap on money at risk, even if your exchange account holds more. It starts small on purpose; for the first 30 days of BUY mode it cannot exceed ${FIRST_MONTH_CAP:,.0f}.")
        form = QFormLayout()
        saved = RiskLimits.model_validate(self.store.get_setting("risk.limits") or {})
        self.budget = QDoubleSpinBox()
        self.budget.setRange(10, FIRST_MONTH_CAP)
        self.budget.setPrefix("$")
        self.budget.setValue(min(saved.budget, FIRST_MONTH_CAP) if saved.budget != 100 else 50)
        form.addRow("Budget", self.budget)
        self.per_pos = QDoubleSpinBox()
        self.per_pos.setRange(1, 100)
        self.per_pos.setSuffix("% of budget per coin")
        self.per_pos.setValue(saved.per_position_pct)
        self.per_pos.setToolTip("Exchanges reject orders under about $5–10, so at a $50 budget keep this at 25% or more.")
        form.addRow("Per-position limit", self.per_pos)
        self.daily = QDoubleSpinBox()
        self.daily.setRange(0.5, 50)
        self.daily.setSuffix("% of budget")
        self.daily.setValue(saved.daily_loss_pct)
        form.addRow("Daily loss limit", self.daily)
        self.dd = QDoubleSpinBox()
        self.dd.setRange(1, 90)
        self.dd.setSuffix("% from peak")
        self.dd.setValue(saved.max_drawdown_pct)
        form.addRow("Total drawdown limit", self.dd)
        lay.addLayout(form)
        exp = QLabel("Daily loss limit: the bot closes everything and waits for tomorrow. Drawdown limit: it closes everything and waits for you. Every position also gets an automatic stop-loss.")
        exp.setWordWrap(True)
        exp.setObjectName("muted")
        lay.addWidget(exp)
        lay.addStretch(1)
        self.stack.addWidget(w)

    def _build_coins(self) -> None:
        w, lay = _page("Choose coins", "Start with the most liquid pairs. The Coins screen lets you search every pair on the exchange with volume, spread and volatility.")
        self.coin_box = QVBoxLayout()
        lay.addLayout(self.coin_box)
        self.coin_checks: list[QCheckBox] = []
        lay.addStretch(1)
        self.stack.addWidget(w)

    def _fill_coins(self) -> None:
        while self.coin_box.count():
            it = self.coin_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.coin_checks = []
        saved = set(self.store.get_setting(f"market.symbols.{self._exchange_id()}", []))
        for i, s in enumerate(EXCHANGES[self._exchange_id()]["symbols"]):
            c = QCheckBox(s)
            c.setChecked(s in saved if saved else i < 2)
            self.coin_checks.append(c)
            self.coin_box.addWidget(c)

    def _build_strategy(self) -> None:
        w, lay = _page("Choose a strategy", "You can change every parameter and run backtests later on the Strategy screen. Decisions are made once a day on daily candles; faster trading mostly pays fees.")
        self.strat = QComboBox()
        for sid, cls in STRATEGIES.items():
            self.strat.addItem(cls.name, sid)
        self.strat.setCurrentIndex(max(0, self.strat.findData("ema_cross")))
        lay.addWidget(self.strat)
        self.strat_desc = QLabel("")
        self.strat_desc.setWordWrap(True)
        self.strat_desc.setObjectName("muted")
        lay.addWidget(self.strat_desc)
        self.strat.currentIndexChanged.connect(lambda _i: self.strat_desc.setText(STRATEGIES[self.strat.currentData()].description))
        self.strat_desc.setText(STRATEGIES["ema_cross"].description)
        self.gate = QCheckBox("Only trade in the right regime (ADX gate) — recommended")
        self.gate.setChecked(True)
        self.vt = QCheckBox("Scale down in high volatility — recommended")
        self.vt.setChecked(True)
        lay.addWidget(self.gate)
        lay.addWidget(self.vt)
        lay.addStretch(1)
        self.stack.addWidget(w)

    def _build_review(self) -> None:
        w, lay = _page("Review", "Press Finish to save these settings and start the bot in TEST mode.")
        self.review = QLabel("")
        self.review.setWordWrap(True)
        self.review.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.review)
        lay.addStretch(1)
        self.stack.addWidget(w)

    # ------------------------------------------------------------ navigation

    def _exchange_id(self) -> str:
        return list(EXCHANGES)[max(0, self.ex_group.checkedId())]

    def _go(self, i: int) -> None:
        if i == 2:
            self.key_panel.set_exchange(self._exchange_id())
        if i == 4:
            self._fill_coins()
        if i == 6:
            self._fill_review()
        self.stack.setCurrentIndex(i)
        dots = " ".join(("●" if k <= i else "○") for k in range(len(STEPS)))
        self.progress.setText(f"{dots}    Step {i + 1} of {len(STEPS)} — {STEPS[i]}")
        self.back_btn.setEnabled(i > 0)
        self.next_btn.setText("Finish and start in TEST mode" if i == len(STEPS) - 1 else "Next")

    def _back(self) -> None:
        self._go(max(0, self.stack.currentIndex() - 1))

    def _next(self) -> None:
        i = self.stack.currentIndex()
        if i == 4 and not any(c.isChecked() for c in self.coin_checks):
            return
        if i == len(STEPS) - 1:
            self._finish()
            return
        self._go(i + 1)

    def _config(self) -> tuple[StrategyConfig, RiskLimits, list[str]]:
        cfg = StrategyConfig(strategies=[StrategySpec(id=self.strat.currentData())], regime_gate=self.gate.isChecked(), vol_target=self.vt.isChecked())
        saved = self.store.get_setting("strategy.config")
        if saved:
            try:
                prev = StrategyConfig.model_validate(saved)
                cfg.risk = prev.risk
            except Exception:  # noqa: BLE001
                pass
        limits = RiskLimits(budget=self.budget.value(), per_position_pct=self.per_pos.value(), daily_loss_pct=self.daily.value(), max_drawdown_pct=self.dd.value())
        coins = [c.text() for c in self.coin_checks if c.isChecked()]
        return cfg, limits, coins

    def _fill_review(self) -> None:
        cfg, limits, coins = self._config()
        from cryptobot import secrets

        key = "saved in keychain" if secrets.has_api_key(self._exchange_id()) else "none (TEST mode only)"
        self.review.setText(
            f"<b>Exchange</b>: {EXCHANGES[self._exchange_id()]['name']}<br>"
            f"<b>API key</b>: {key}<br>"
            f"<b>Budget</b>: ${limits.budget:,.2f} · per coin {limits.per_position_pct:g}% · daily loss {limits.daily_loss_pct:g}% · drawdown {limits.max_drawdown_pct:g}%<br>"
            f"<b>Coins</b>: {', '.join(coins)}<br>"
            f"<b>Strategy</b>: {cfg.describe()} on daily candles<br>"
            f"<b>Mode</b>: <span style='color:{GREEN}'>TEST</span> (simulated orders)"
        )

    def _finish(self) -> None:
        cfg, limits, coins = self._config()
        ex = self._exchange_id()
        self.store.set_setting("market.exchange", ex)
        self.store.set_setting("market.symbol", coins[0])
        self.store.set_setting(f"market.symbols.{ex}", coins)
        self.store.set_setting("risk.limits", limits.model_dump())
        self.store.set_setting("strategy.config", cfg.to_json())
        self.store.set_setting("setup.completed", True)
        self.store.set_setting("trader.running", True)
        self.store.audit("setup", "wizard completed", exchange=ex, coins=coins, limits=limits.model_dump())
        self.result_exchange, self.result_symbols = ex, coins
        self.accept()
