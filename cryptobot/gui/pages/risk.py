"""Risk & Budget screen: the spending limits, applied identically in TEST and BUY."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryptobot.backtest.metrics import max_drawdown
from cryptobot.core.events import BudgetEvent, TraderStateEvent
from cryptobot.core.models import Mode
from cryptobot.db.store import Store
from cryptobot.engine.risk import RiskLimits
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, GREEN, MUTED, RED
from cryptobot.gui.widgets.budget_dialog import BudgetDialog
from cryptobot.strategy.config import StrategyConfig


class RiskPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self.limits = RiskLimits.model_validate(store.get_setting("risk.limits") or {})
        self._running = False

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(10)
        h = QLabel("Risk & Budget")
        h.setObjectName("h1")
        left.addWidget(h)
        note = QLabel("These limits cannot be bypassed by any strategy and apply identically in TEST and BUY modes.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        left.addWidget(note)

        bg = QGroupBox("Trading budget")
        bf = QFormLayout(bg)
        row = QHBoxLayout()
        self.budget_lab = QLabel(f"${self.limits.budget:,.2f}")
        self.budget_lab.setStyleSheet("font-size: 24px; font-weight: 700;")
        row.addWidget(self.budget_lab)
        self.change_btn = QPushButton("Change…")
        self.change_btn.setToolTip("Raising the budget shows your results so far and asks you to type the new amount twice. Steps are limited to +50%.")
        self.change_btn.clicked.connect(self._change_budget)
        row.addWidget(self.change_btn)
        row.addStretch(1)
        bf.addRow(row)
        bf.addRow(QLabel("The hard cap on money at risk, fees included, even if the exchange account holds more."))
        left.addWidget(bg)

        lg = QGroupBox("Limits")
        lf = QFormLayout(lg)
        self.per_pos = QDoubleSpinBox()
        self.per_pos.setRange(1, 100)
        self.per_pos.setSuffix("% of budget")
        self.per_pos.setToolTip("Maximum share of the budget in any single coin. With one coin selected, this is the maximum the bot will invest at all.")
        lf.addRow("Per-position limit", self.per_pos)
        self.daily = QDoubleSpinBox()
        self.daily.setRange(0.5, 50)
        self.daily.setSuffix("% of budget")
        self.daily.setToolTip("If today's loss reaches this, every position is closed and the bot halts until you click Resume tomorrow.")
        lf.addRow("Daily loss limit", self.daily)
        self.dd = QDoubleSpinBox()
        self.dd.setRange(1, 90)
        self.dd.setSuffix("% from peak")
        self.dd.setToolTip("If equity falls this far below its best level, the bot closes everything and stops until you restart it.")
        lf.addRow("Total drawdown limit", self.dd)
        left.addWidget(lg)

        pg_ = QGroupBox("Position protection (shared with the Strategy screen)")
        pf = QFormLayout(pg_)
        self.stop_atr = QDoubleSpinBox()
        self.stop_atr.setRange(0, 10)
        self.stop_atr.setSingleStep(0.5)
        self.stop_atr.setSuffix(" × ATR")
        self.stop_atr.setToolTip("Every position gets a stop-loss this many ATRs below entry. 0 disables (not recommended).")
        pf.addRow("Stop-loss", self.stop_atr)
        self.trail = QDoubleSpinBox()
        self.trail.setRange(0, 50)
        self.trail.setSuffix("%")
        pf.addRow("Trailing stop", self.trail)
        self.tp = QDoubleSpinBox()
        self.tp.setRange(0, 500)
        self.tp.setSuffix("%")
        pf.addRow("Take-profit", self.tp)
        self.slip = QDoubleSpinBox()
        self.slip.setRange(0, 2)
        self.slip.setDecimals(3)
        self.slip.setSingleStep(0.01)
        self.slip.setSuffix("%")
        self.slip.setToolTip("Slippage assumed on simulated fills in TEST mode. Fees come from the exchange.")
        pf.addRow("TEST slippage", self.slip)
        left.addWidget(pg_)

        self.apply_btn = QPushButton("Apply limits")
        self.apply_btn.clicked.connect(self._apply)
        left.addWidget(self.apply_btn)
        self.msg = QLabel("")
        self.msg.setObjectName("muted")
        self.msg.setWordWrap(True)
        left.addWidget(self.msg)
        left.addStretch(1)
        root.addLayout(left, 1)

        right = QVBoxLayout()
        card = QFrame()
        card.setObjectName("card")
        cl = QFormLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        self.s_inuse = QLabel("—")
        self.s_today = QLabel("—")
        self.s_dd = QLabel("—")
        self.s_status = QLabel("Not running")
        cl.addRow("Status", self.s_status)
        cl.addRow("Budget in use", self.s_inuse)
        cl.addRow("Today's loss used", self.s_today)
        cl.addRow("Drawdown used", self.s_dd)
        right.addWidget(card)
        right.addStretch(1)
        root.addLayout(right, 1)

        self._load()
        ctrl.budget.connect(self._on_budget)
        ctrl.trader_state.connect(self._on_state)

    def _load(self) -> None:
        self.per_pos.setValue(self.limits.per_position_pct)
        self.daily.setValue(self.limits.daily_loss_pct)
        self.dd.setValue(self.limits.max_drawdown_pct)
        raw = self.store.get_setting("strategy.config")
        cfg = StrategyConfig.model_validate(raw) if raw else StrategyConfig()
        self.stop_atr.setValue(cfg.risk.stop_atr_mult)
        self.trail.setValue(cfg.risk.trailing_pct)
        self.tp.setValue(cfg.risk.take_profit_pct)
        self.slip.setValue(float(self.store.get_setting("backtest.settings", {}).get("slippage_pct", 0.05)))

    def _change_budget(self) -> None:
        eq = [e for _t, e in self.store.load_equity(Mode.TEST.value)]
        import numpy as np

        stats = {
            "equity": eq[-1] if eq else self.limits.budget,
            "realized": sum((t["pnl"] or 0.0) for t in self.store.load_trades(Mode.TEST.value, limit=10_000)),
            "trades": self.store.count_trades(Mode.TEST.value),
            "drawdown": max_drawdown(np.array(eq)) if eq else 0.0,
        }
        dlg = BudgetDialog(self.limits.budget, stats, self)
        if dlg.exec() and dlg.value is not None:
            self.limits = self.limits.model_copy(update={"budget": dlg.value})
            self.budget_lab.setText(f"${dlg.value:,.2f}")
            self._apply()

    def _apply(self) -> None:
        self.limits = self.limits.model_copy(
            update={"per_position_pct": self.per_pos.value(), "daily_loss_pct": self.daily.value(), "max_drawdown_pct": self.dd.value()}
        )
        self.store.set_setting("risk.limits", self.limits.model_dump())
        raw = self.store.get_setting("strategy.config")
        cfg = StrategyConfig.model_validate(raw) if raw else StrategyConfig()
        cfg.risk.stop_atr_mult = self.stop_atr.value()
        cfg.risk.trailing_pct = self.trail.value()
        cfg.risk.take_profit_pct = self.tp.value()
        self.store.set_setting("strategy.config", cfg.to_json())
        bt = self.store.get_setting("backtest.settings", {})
        bt["slippage_pct"] = self.slip.value()
        self.store.set_setting("backtest.settings", bt)
        self.store.audit("risk", "limits saved", **self.limits.model_dump())
        if self._running:
            self.ctrl.set_limits(self.limits)
            self.msg.setText("Saved and applied to the running bot. Stop-loss changes apply to new positions; restart the bot to change the strategy timeframe.")
        else:
            self.msg.setText("Saved. They take effect when you press START.")

    def _on_budget(self, ev: BudgetEvent) -> None:
        self.s_inuse.setText(f"${ev.in_use:,.2f} of ${ev.allocated:,.2f}")
        used = -ev.today_pnl / ev.allocated if ev.allocated else 0.0
        lim = self.limits.daily_loss_pct / 100
        self.s_today.setText(f"{max(0.0, used):.1%} of {lim:.0%}")
        self.s_today.setStyleSheet(f"color: {RED if used > lim * 0.8 else AMBER if used > lim * 0.5 else GREEN};")
        dl = self.limits.max_drawdown_pct / 100
        self.s_dd.setText(f"{ev.drawdown_pct:.1%} of {dl:.0%}")
        self.s_dd.setStyleSheet(f"color: {RED if ev.drawdown_pct > dl * 0.8 else AMBER if ev.drawdown_pct > dl * 0.5 else GREEN};")

    def _on_state(self, ev: TraderStateEvent) -> None:
        self._running = ev.status != "stopped"
        self.s_status.setText(ev.status.replace("_", " "))
