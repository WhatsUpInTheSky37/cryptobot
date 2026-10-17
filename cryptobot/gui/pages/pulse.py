"""Crypto Pulse screen: live pressure signals, the Market Memory, and paper results."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryptobot.core.events import PulseEventRecord, PulseSnapshot, PulseStatusEvent
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, BLUE, BORDER, GREEN, GREY, MUTED, PANEL2, RED
from cryptobot.pulse.config import PulseConfig

STATE_COLOR = {"off": GREY, "warming": MUTED, "idle": MUTED, "monitor": AMBER, "prepared": BLUE, "entered": GREEN}


class _Bar(QProgressBar):
    def __init__(self, label: str, tip: str) -> None:
        super().__init__()
        self.setRange(0, 100)
        self.setFormat(f"{label}  %v")
        self.setTextVisible(True)
        self.setFixedHeight(18)
        self.setToolTip(tip)
        self.setStyleSheet(f"QProgressBar {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 4px; text-align: center; font-size: 11px; }} QProgressBar::chunk {{ background: {BLUE}; border-radius: 3px; }}")


class _SymbolCard(QFrame):
    def __init__(self, symbol: str) -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.title = QLabel(symbol)
        self.title.setStyleSheet("font-weight: 700; font-size: 15px;")
        top.addWidget(self.title)
        top.addStretch(1)
        self.state = QLabel("off")
        top.addWidget(self.state)
        lay.addLayout(top)
        self.prs = QLabel("PRS —")
        self.prs.setStyleSheet("font-size: 22px; font-weight: 700;")
        lay.addWidget(self.prs)
        self.bars = {
            "f": _Bar("Pressure (F)", "Persistence of aggressive buying over the last 60 s, ranked against this coin's recent history."),
            "a": _Bar("Absorption (A)", "Volume per % of price movement: high means lots of trading with little price change."),
            "w": _Bar("Withdrawal (W)", "Sell-side liquidity within 0.1% of the mid disappearing over the last 10 s."),
            "r": _Bar("Replenishment weakening (R)", "Sell-side depth sitting below its 60-second average (not refilling)."),
            "c": _Bar("Confirmation (C)", "Whether the related market (BTC↔ETH) shows the same buying pressure."),
        }
        for b in self.bars.values():
            lay.addWidget(b)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)

    def update(self, s: PulseSnapshot) -> None:
        self.state.setText(s.state)
        self.state.setStyleSheet(f"color: {STATE_COLOR.get(s.state, MUTED)}; font-weight: 600;")
        color = GREEN if s.prs_long >= 85 else BLUE if s.prs_long >= 70 else AMBER if s.prs_long >= 50 else MUTED
        self.prs.setText(f"PRS {s.prs_long:.0f}  ·  bearish {s.prs_short:.0f}")
        self.prs.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {color};")
        for k, b in self.bars.items():
            b.setValue(int(round(getattr(s, k))))
        self.meta.setText(f"mid {s.mid:,.2f} · imbalance {s.imbalance_60:+.2f} · spread {s.spread_pct:.3f}% · 1m vol {s.vol_pct:.2f}%  {s.note}")


class PulsePage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self.cards: dict[str, _SymbolCard] = {}
        raw = store.get_setting("pulse.config") or {}
        try:
            self.cfg = PulseConfig.model_validate(raw)
        except Exception:  # noqa: BLE001
            self.cfg = PulseConfig()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        head = QHBoxLayout()
        h = QLabel("Crypto Pulse — Liquidity Pressure Divergence (paper only)")
        h.setObjectName("h1")
        head.addWidget(h)
        head.addStretch(1)
        self.start_btn = QPushButton("▶  Start Pulse (paper)")
        self.start_btn.setStyleSheet(f"background: {GREEN}; color: #06210f; font-weight: 700;")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        head.addWidget(self.start_btn)
        head.addWidget(self.stop_btn)
        root.addLayout(head)
        self.banner = QLabel(
            "Pulse watches aggressive trades and order-book liquidity for pressure that price is not responding to, waits for the release, "
            "and paper-trades it with a tight stop and a 2:1 target. It cannot be backtested from candles, so it starts as observation: every setup "
            "is recorded with what price did next, and the win rate it builds up is the only evidence that matters. Spot cannot short, so bearish "
            "setups are recorded but never traded."
        )
        self.banner.setWordWrap(True)
        self.banner.setObjectName("muted")
        root.addWidget(self.banner)

        body = QHBoxLayout()
        body.setSpacing(10)
        left = QVBoxLayout()
        self.cards_box = QGridLayout()
        self.cards_box.setSpacing(8)
        left.addLayout(self.cards_box)
        # settings
        sg = QFrame()
        sg.setObjectName("card")
        sf = QFormLayout(sg)
        sf.setContentsMargins(12, 8, 12, 8)
        self.budget = QDoubleSpinBox()
        self.budget.setRange(10, 100_000)
        self.budget.setPrefix("$")
        self.budget.setValue(self.cfg.budget)
        self.budget.setToolTip("Paper budget for Pulse. Separate from the main bot's budget.")
        sf.addRow("Paper budget", self.budget)
        self.risk = QDoubleSpinBox()
        self.risk.setRange(0.05, 5)
        self.risk.setSingleStep(0.05)
        self.risk.setSuffix("% of budget")
        self.risk.setValue(self.cfg.risk_per_trade_pct)
        self.risk.setToolTip("Money risked per trade (distance to the stop). 0.25% as proposed.")
        sf.addRow("Risk per trade", self.risk)
        self.enter = QDoubleSpinBox()
        self.enter.setRange(50, 100)
        self.enter.setValue(self.cfg.enter_prs)
        self.enter.setToolTip("Minimum Pressure Release Score to enter (85 proposed). Lower = more trades, weaker setups.")
        sf.addRow("Enter at PRS ≥", self.enter)
        self.research = QCheckBox("Research mode: paper-trade confirmed setups even when EV is negative")
        self.research.setChecked(self.cfg.research_mode)
        self.research.setToolTip("Off = only trade when similar past setups give a positive expected value after fees. With no history yet, that means no trades at all.")
        sf.addRow(self.research)
        left.addWidget(sg)
        body.addLayout(left, 3)

        right = QVBoxLayout()
        stats = QFrame()
        stats.setObjectName("card")
        st = QFormLayout(stats)
        st.setContentsMargins(12, 8, 12, 8)
        self.s_equity = QLabel("—")
        self.s_events = QLabel("—")
        self.s_hit = QLabel("—")
        self.s_breakeven = QLabel("—")
        self.s_ev = QLabel("—")
        self.s_trades = QLabel("—")
        self.s_msg = QLabel("Not running.")
        self.s_msg.setWordWrap(True)
        st.addRow("Paper equity", self.s_equity)
        st.addRow("Setups recorded / resolved", self.s_events)
        st.addRow("Hit rate (target before stop)", self.s_hit)
        st.addRow("Break-even win rate at current costs", self.s_breakeven)
        st.addRow("Expected value per trade", self.s_ev)
        st.addRow("Paper trades / P&L", self.s_trades)
        st.addRow("Status", self.s_msg)
        right.addWidget(stats)
        self.events = QTableWidget(0, 8)
        self.events.setHorizontalHeaderLabels(["Time", "Coin", "Dir", "PRS", "Status", "Outcome", "MFE / MAE", "Detail"])
        self.events.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.events.horizontalHeader().setStretchLastSection(True)
        self.events.verticalHeader().setVisible(False)
        self.events.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        right.addWidget(self.events, 1)
        body.addLayout(right, 4)
        root.addLayout(body, 1)

        ctrl.pulse_status.connect(self._on_status)
        ctrl.pulse_event.connect(self._on_event)
        self._load_events()

    # ------------------------------------------------------------------

    def _config(self) -> PulseConfig:
        self.cfg = self.cfg.model_copy(update={"budget": self.budget.value(), "risk_per_trade_pct": self.risk.value(), "enter_prs": self.enter.value(), "research_mode": self.research.isChecked()})
        self.store.set_setting("pulse.config", self.cfg.model_dump())
        return self.cfg

    def start(self) -> None:
        cfg = self._config()
        slip = float(self.store.get_setting("backtest.settings", {}).get("slippage_pct", 0.05))
        self.ctrl.pulse_start(cfg.model_dump(), slip)
        self.store.set_setting("pulse.running", True)
        self.s_msg.setText("Starting…")

    def stop(self) -> None:
        self.ctrl.pulse_stop()
        self.store.set_setting("pulse.running", False)

    def _on_status(self, ev: PulseStatusEvent) -> None:
        self.start_btn.setEnabled(not ev.running)
        self.stop_btn.setEnabled(ev.running)
        for s in ev.snapshots:
            if s.symbol not in self.cards:
                card = _SymbolCard(s.symbol)
                self.cards[s.symbol] = card
                n = len(self.cards) - 1
                self.cards_box.addWidget(card, n // 2, n % 2)
            self.cards[s.symbol].update(s)
        pnl = ev.paper_equity - ev.paper_budget
        self.s_equity.setText(f"${ev.paper_equity:,.2f}  ({pnl:+,.2f})")
        self.s_equity.setStyleSheet(f"color: {GREEN if pnl > 0 else RED if pnl < 0 else MUTED};")
        self.s_events.setText(f"{ev.events_recorded} / {ev.events_resolved}")
        self.s_hit.setText(f"{ev.hit_rate:.0%}" if ev.events_resolved else "— (no resolved setups yet)")
        self.s_breakeven.setText(f"{min(ev.required_win_rate, 9.99):.0%}  (stop {ev.stop_pct:.2f}% / target {ev.target_pct:.2f}% / costs {ev.cost_pct:.2f}%)" + ("  — target below costs: no trades possible" if ev.target_pct < self.cfg.min_target_cost_mult * ev.cost_pct else ""))
        self.s_breakeven.setStyleSheet(f"color: {RED if ev.required_win_rate >= 0.8 else AMBER if ev.required_win_rate >= 0.6 else GREEN};")
        self.s_ev.setText(f"{ev.ev_pct:+.3f}% per trade")
        self.s_ev.setStyleSheet(f"color: {GREEN if ev.ev_pct > 0 else RED};")
        self.s_trades.setText(f"{ev.trades} / {ev.trade_pnl:+,.2f}" + (f" · {ev.consecutive_losses} losses in a row" if ev.consecutive_losses else ""))
        msg = ev.message or ("Running." if ev.running else "Not running.")
        if ev.paused_until_ms and ev.paused_until_ms > datetime.now().timestamp() * 1000:
            msg += f" Paused until {datetime.fromtimestamp(ev.paused_until_ms / 1000).strftime('%H:%M')}."
        self.s_msg.setText(msg)

    def _row(self, ts_ms: int, symbol: str, direction: str, prs: float, status: str, outcome: str, mfe, mae, detail: str, at_top: bool = True) -> None:
        r = 0 if at_top else self.events.rowCount()
        self.events.insertRow(r)
        mm = f"{mfe:+.2f}% / {mae:+.2f}%" if mfe is not None and mae is not None else ""
        vals = [datetime.fromtimestamp(ts_ms / 1000).strftime("%b %d %H:%M:%S"), symbol, direction, f"{prs:.0f}" if prs else "", status, outcome, mm, detail]
        for j, v in enumerate(vals):
            it = QTableWidgetItem(v)
            if j == 4:
                it.setForeground(Qt.GlobalColor.green if status == "entered" else Qt.GlobalColor.yellow if status == "rejected" else Qt.GlobalColor.gray)
            if j == 5:
                it.setForeground(Qt.GlobalColor.green if outcome == "target" else Qt.GlobalColor.red if outcome == "stop" else Qt.GlobalColor.gray)
            self.events.setItem(r, j, it)
        while self.events.rowCount() > 400:
            self.events.removeRow(self.events.rowCount() - 1)

    def _on_event(self, ev: PulseEventRecord) -> None:
        if ev.status:
            self._row(ev.ts_ms, ev.symbol, ev.direction, ev.prs, ev.status, ev.outcome, ev.mfe_pct, ev.mae_pct, ev.reason)
        else:
            # resolution of an earlier event: update the matching row if visible
            for r in range(self.events.rowCount()):
                it = self.events.item(r, 0)
                if it and it.text() == datetime.fromtimestamp(ev.ts_ms / 1000).strftime("%b %d %H:%M:%S") and self.events.item(r, 1).text() == ev.symbol and self.events.item(r, 2).text() == ev.direction:
                    self.events.item(r, 5).setText(ev.outcome)
                    self.events.item(r, 5).setForeground(Qt.GlobalColor.green if ev.outcome == "target" else Qt.GlobalColor.red if ev.outcome == "stop" else Qt.GlobalColor.gray)
                    self.events.item(r, 6).setText(f"{ev.mfe_pct:+.2f}% / {ev.mae_pct:+.2f}%")
                    break

    def _load_events(self) -> None:
        for e in self.store.load_pulse_events(limit=200):
            self._row(e["ts_ms"], e["symbol"], e["direction"], e["prs"], e["status"], e["outcome"], e["mfe_pct"], e["mae_pct"], e["reason"], at_top=False)
