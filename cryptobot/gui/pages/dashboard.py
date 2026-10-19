"""Dashboard: mode, START/STOP/EMERGENCY, budget gauge, positions, equity, trades."""
from __future__ import annotations

import logging
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryptobot import secrets
from cryptobot.config import EXCHANGES
from cryptobot.engine.modes import buy_gate
from cryptobot.gui.widgets.mode_dialog import ModeSwitchDialog, ask_back_to_test
from cryptobot.core.events import AlertEvent, BudgetEvent, EquityEvent, FillEvent, PositionsEvent, SignalEvent, TraderStateEvent
from cryptobot.core.models import Mode
from cryptobot.db.store import Store
from cryptobot.engine.risk import RiskLimits
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, BLUE, BORDER, GREEN, GREY, MUTED, PANEL2, RED, TEXT
from cryptobot.gui.widgets.mode_badge import ModeBadge
from cryptobot.strategy.config import StrategyConfig

log = logging.getLogger(__name__)


def _money(v: float, sign: bool = False) -> str:
    return f"{v:+,.2f}" if sign else f"${v:,.2f}"


def _px(v: float | None) -> str:
    if v is None:
        return "—"
    d = 2 if v >= 1000 else 4 if v >= 1 else 6
    return f"{v:,.{d}f}"


class StatTile(QFrame):
    def __init__(self, label: str, tooltip: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        self.lab = QLabel(label)
        self.lab.setObjectName("muted")
        self.val = QLabel("—")
        self.val.setStyleSheet("font-size: 20px; font-weight: 600;")
        lay.addWidget(self.lab)
        lay.addWidget(self.val)
        if tooltip:
            self.setToolTip(tooltip)

    def set(self, text: str, color: str | None = None) -> None:
        self.val.setText(text)
        self.val.setStyleSheet(f"font-size: 20px; font-weight: 600; color: {color or TEXT};")


class DashboardPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self.state: TraderStateEvent | None = None
        self._last_status = "stopped"
        self._equity_x: list[float] = []
        self._equity_y: list[float] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # -- control row ----------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(12)
        self.badge = ModeBadge(big=True)
        top.addWidget(self.badge)
        self.test_btn = QPushButton("TEST")
        self.buy_btn = QPushButton("BUY")
        for b in (self.test_btn, self.buy_btn):
            b.setCheckable(True)
            b.setFixedHeight(44)
            b.setMinimumWidth(80)
        self.test_btn.setChecked(True)
        self.test_btn.setStyleSheet(f"QPushButton:checked {{ background: {BLUE}; border-color: {BLUE}; color: white; font-weight: 700; }}")
        self.buy_btn.setStyleSheet(f"QPushButton:checked {{ background: {RED}; border-color: {RED}; color: white; font-weight: 700; }}")
        self.buy_btn.setToolTip("BUY mode: REAL orders. Unlocks after 14 days of TEST mode, with an API key that can trade, and asks you to type BUY.")
        self.test_btn.setToolTip("TEST mode: live prices, simulated orders, no real money.")
        self.test_btn.clicked.connect(self._want_test)
        self.buy_btn.clicked.connect(self._want_buy)
        self._set_toggle(self.store.get_setting("trader.mode", "TEST"))
        mode_box = QHBoxLayout()
        mode_box.setSpacing(0)
        mode_box.addWidget(self.test_btn)
        mode_box.addWidget(self.buy_btn)
        top.addLayout(mode_box)
        top.addSpacing(16)
        self.start_btn = QPushButton("▶  START")
        self.start_btn.setFixedHeight(44)
        self.start_btn.setMinimumWidth(130)
        self.start_btn.setStyleSheet(f"background: {GREEN}; color: #06210f; font-weight: 700; font-size: 14px;")
        self.start_btn.clicked.connect(self.start_bot)
        self.stop_btn = QPushButton("■  STOP")
        self.stop_btn.setFixedHeight(44)
        self.stop_btn.setMinimumWidth(130)
        self.stop_btn.setStyleSheet(f"font-weight: 700; font-size: 14px;")
        self.stop_btn.clicked.connect(self.stop_bot)
        self.stop_btn.setEnabled(False)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        top.addStretch(1)
        self.em_btn = QPushButton("⚠  EMERGENCY STOP")
        self.em_btn.setFixedHeight(44)
        self.em_btn.setStyleSheet(f"background: {RED}; color: white; font-weight: 700; font-size: 14px;")
        self.em_btn.setToolTip("Sell every open position back to cash and stop the bot. Asks for confirmation.")
        self.em_btn.clicked.connect(self.emergency)
        top.addWidget(self.em_btn)
        root.addLayout(top)

        self.status_line = QLabel("Not running. Press START to begin trading in TEST mode with the saved strategy and limits.")
        self.status_line.setObjectName("muted")
        self.status_line.setWordWrap(True)
        root.addWidget(self.status_line)

        # -- alert bar ------------------------------------------------------
        self.alert = QFrame()
        self.alert.setStyleSheet(f"QFrame {{ background: #3a1a1a; border: 1px solid {RED}; border-radius: 8px; }}")
        al = QHBoxLayout(self.alert)
        al.setContentsMargins(12, 8, 12, 8)
        self.alert_text = QLabel("")
        self.alert_text.setWordWrap(True)
        self.alert_text.setStyleSheet("background: transparent;")
        al.addWidget(self.alert_text, 1)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.clicked.connect(self.ctrl.resume)
        al.addWidget(self.resume_btn)
        self.alert.setVisible(False)
        root.addWidget(self.alert)

        # -- middle: tiles + equity ----------------------------------------
        mid = QHBoxLayout()
        mid.setSpacing(10)
        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.t_alloc = StatTile("Budget allocated", "The hard cap. The bot can never put more than this at risk.")
        self.t_inuse = StatTile("In use", "Cost of open positions, fees included.")
        self.t_avail = StatTile("Available", "Budget not yet used.")
        self.t_today = StatTile("Today's P&L", "Change in equity since local midnight (Eastern).")
        self.t_equity = StatTile("Equity", "Cash plus the current value of positions.")
        self.t_dd = StatTile("Drawdown from peak", "How far equity is below its best level. The bot stops at the limit on the Risk screen.")
        for i, t in enumerate((self.t_alloc, self.t_inuse, self.t_avail, self.t_today, self.t_equity, self.t_dd)):
            tiles.addWidget(t, i // 2, i % 2)
        tiles_w = QWidget()
        tiles_w.setLayout(tiles)
        tiles_w.setFixedWidth(360)
        mid.addWidget(tiles_w)
        eq_card = QFrame()
        eq_card.setObjectName("card")
        el = QVBoxLayout(eq_card)
        el.setContentsMargins(8, 8, 8, 8)
        self.eq_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.eq_plot.showGrid(x=True, y=True, alpha=0.08)
        self.eq_plot.setLabel("left", "TEST equity ($)")
        self.eq_curve = self.eq_plot.plot([], [], pen=pg.mkPen(BLUE, width=2))
        self.eq_plot.setMouseEnabled(x=True, y=False)
        self.eq_plot.enableAutoRange(axis="y")
        self.eq_budget = pg.InfiniteLine(angle=0, pen=pg.mkPen(GREY, style=Qt.PenStyle.DashLine))
        self.eq_plot.addItem(self.eq_budget)
        el.addWidget(self.eq_plot)
        mid.addWidget(eq_card, 1)
        root.addLayout(mid, 2)

        # -- bottom: positions + trades -----------------------------------
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        pos_card = QFrame()
        pos_card.setObjectName("card")
        pl = QVBoxLayout(pos_card)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.addWidget(QLabel("Open positions"))
        self.pos_table = QTableWidget(0, 9)
        self.pos_table.setHorizontalHeaderLabels(["Coin", "Mode", "Amount", "Entry", "Now", "P&L $", "P&L %", "Stop-loss", "Take-profit"])
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.pos_table.horizontalHeader().setStretchLastSection(True)
        self.pos_table.verticalHeader().setVisible(False)
        self.pos_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        pl.addWidget(self.pos_table)
        bottom.addWidget(pos_card, 3)
        tr_card = QFrame()
        tr_card.setObjectName("card")
        tl = QVBoxLayout(tr_card)
        tl.setContentsMargins(8, 8, 8, 8)
        tl.addWidget(QLabel("Recent trades and signals"))
        self.trades = QTableWidget(0, 5)
        self.trades.setHorizontalHeaderLabels(["Time", "Mode", "Side", "Detail", "P&L"])
        self.trades.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.trades.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.trades.verticalHeader().setVisible(False)
        self.trades.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tl.addWidget(self.trades)
        bottom.addWidget(tr_card, 4)
        root.addLayout(bottom, 2)

        # -- wiring ---------------------------------------------------------
        ctrl.trader_state.connect(self._on_state)
        ctrl.budget.connect(self._on_budget)
        ctrl.positions.connect(self._on_positions)
        ctrl.fill.connect(self._on_fill)
        ctrl.signal.connect(self._on_signal)
        ctrl.alert.connect(self._on_alert)
        ctrl.equity.connect(self._on_equity)
        self._load_history()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_resume)
        self._timer.start(5000)

    # ------------------------------------------------------------ actions

    def _config(self) -> tuple[StrategyConfig, RiskLimits, float]:
        raw = self.store.get_setting("strategy.config")
        cfg = StrategyConfig.model_validate(raw) if raw else StrategyConfig()
        lim_raw = self.store.get_setting("risk.limits")
        limits = RiskLimits.model_validate(lim_raw) if lim_raw else RiskLimits()
        slip = float(self.store.get_setting("backtest.settings", {}).get("slippage_pct", 0.05))
        return cfg, limits, slip

    def _set_toggle(self, mode: str) -> None:
        self.test_btn.setChecked(mode != "BUY")
        self.buy_btn.setChecked(mode == "BUY")

    def _now_ms(self) -> int:
        return int(datetime.now().timestamp() * 1000)

    def _credentials(self) -> dict | None:
        c = secrets.load_api_key(self.ctrl.exchange)
        if not c:
            return None
        return {"key": c[0], "secret": c[1], "passphrase": c[2]}

    def start_bot(self) -> None:
        cfg, limits, slip = self._config()
        reset_peak = self._last_status == "halted_drawdown"
        mode = self.store.get_setting("trader.mode", "TEST")
        creds = None
        if mode == "BUY":
            ok, reasons, _ = buy_gate(self.store, self._now_ms(), limits.budget, secrets.has_api_key(self.ctrl.exchange), self.store.get_setting(f"apikey.can_trade.{self.ctrl.exchange}"))
            creds = self._credentials() if ok else None
            if not ok or creds is None:
                mode = "TEST"
                self.store.set_setting("trader.mode", "TEST")
                self._set_toggle("TEST")
                self._add_row(self._now_ms(), "", "ALERT", "BUY mode unavailable, starting in TEST: " + " ".join(reasons), None, RED)
        self.ctrl.start_trading(cfg, limits, slip, reset_peak=reset_peak, mode=mode, credentials=creds)
        self.store.set_setting("trader.running", True)
        self.status_line.setText("Starting…")

    def _want_buy(self) -> None:
        if self.store.get_setting("trader.mode", "TEST") == "BUY":
            self._set_toggle("BUY")
            return
        cfg, limits, _ = self._config()
        ex = self.ctrl.exchange
        ok, reasons, stats = buy_gate(self.store, self._now_ms(), limits.budget, secrets.has_api_key(ex), self.store.get_setting(f"apikey.can_trade.{ex}"))
        dlg = ModeSwitchDialog(EXCHANGES.get(ex, {}).get("name", ex), stats, reasons, limits.budget, self)
        accepted = dlg.exec() and ok
        if not accepted:
            self._set_toggle("TEST")
            return
        self.store.log_mode_switch(self._now_ms(), "TEST", "BUY", stats.__dict__)
        self.store.audit("mode", "switched TEST → BUY", level="WARNING", **stats.__dict__)
        self.store.set_setting("trader.mode", "BUY")
        self._set_toggle("BUY")
        if self._last_status != "stopped":
            self.ctrl.stop_trading()
            self.start_bot()

    def _want_test(self) -> None:
        if self.store.get_setting("trader.mode", "TEST") != "BUY":
            self._set_toggle("TEST")
            return
        choice = ask_back_to_test(self, self.pos_table.rowCount()) if self._last_status != "stopped" else "keep"
        if choice is None:
            self._set_toggle("BUY")
            return
        self.store.log_mode_switch(self._now_ms(), "BUY", "TEST", {"positions": choice})
        self.store.audit("mode", f"switched BUY → TEST ({choice} positions)")
        self.store.set_setting("trader.mode", "TEST")
        self._set_toggle("TEST")
        if self._last_status != "stopped":
            if choice == "close":
                self.ctrl.emergency_stop()
            else:
                self.ctrl.stop_trading()
            self.start_bot()

    def stop_bot(self) -> None:
        self.ctrl.stop_trading()
        self.store.set_setting("trader.running", False)

    def emergency(self) -> None:
        n = self.pos_table.rowCount()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Emergency stop")
        box.setText(f"Sell all {n} open position(s) at the current price and stop the bot?")
        box.setInformativeText("In TEST mode this is simulated. In BUY mode it cancels open orders and sends REAL market sell orders.")
        sell = box.addButton("Sell everything now", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is sell:
            self.ctrl.emergency_stop()
            self.store.set_setting("trader.running", False)

    # ------------------------------------------------------------ events

    def _on_state(self, ev: TraderStateEvent) -> None:
        self.state = ev
        self._last_status = ev.status
        mode = Mode(ev.mode)
        self.badge.set_mode(mode)
        if ev.status != "stopped":
            self._set_toggle(ev.mode)
        running = ev.status in ("running", "halted_daily", "halted_drawdown")
        self.start_btn.setEnabled(not running or ev.status in ("paused", "halted_drawdown"))
        self.stop_btn.setEnabled(running)
        self.start_btn.setText("▶  RESUME" if ev.status == "paused" else "▶  RESTART" if ev.status == "halted_drawdown" else "▶  START")
        exch = EXCHANGES.get(ev.exchange, {}).get("name", ev.exchange)
        if ev.status == "running":
            nxt = datetime.fromtimestamp(ev.next_decision_ms / 1000).strftime("%a %H:%M") if ev.next_decision_ms else "—"
            self.status_line.setText(f"Running {ev.mode} on {exch} · {', '.join(ev.symbols)} · {ev.strategy} · next decision after the bar closes at {nxt}. {ev.message}")
            self.alert.setVisible(False)
        elif ev.status in ("halted_daily", "halted_drawdown"):
            self.status_line.setText(f"HALTED ({ev.status.replace('halted_', '')} limit) on {exch} · {', '.join(ev.symbols)}.")
            self.alert_text.setText(ev.message or "Trading halted by a risk limit.")
            self.resume_btn.setVisible(ev.status == "halted_daily")
            self.alert.setVisible(True)
            self._refresh_resume()
        else:
            self.status_line.setText(f"Stopped. {ev.message}".strip())
            self.alert.setVisible(False)

    def _refresh_resume(self) -> None:
        if self.state and self.state.status == "halted_daily":
            ok = datetime.now().timestamp() * 1000 >= self.state.halted_until_ms
            self.resume_btn.setEnabled(ok)
            when = datetime.fromtimestamp(self.state.halted_until_ms / 1000).strftime("%a %H:%M")
            self.resume_btn.setToolTip("" if ok else f"Resume unlocks at {when} (local midnight).")

    def _on_budget(self, ev: BudgetEvent) -> None:
        self.t_alloc.set(_money(ev.allocated))
        self.t_inuse.set(_money(ev.in_use))
        self.t_avail.set(_money(ev.available))
        self.t_today.set(_money(ev.today_pnl, sign=True), GREEN if ev.today_pnl > 0 else RED if ev.today_pnl < 0 else None)
        self.t_equity.set(_money(ev.equity), GREEN if ev.equity > ev.allocated else RED if ev.equity < ev.allocated else None)
        self.t_dd.set(f"-{ev.drawdown_pct:.1%}", RED if ev.drawdown_pct > 0.1 else AMBER if ev.drawdown_pct > 0.05 else None)
        self.eq_budget.setPos(ev.allocated)

    def _on_positions(self, ev: PositionsEvent) -> None:
        self.pos_table.setRowCount(len(ev.positions))
        for i, p in enumerate(ev.positions):
            vals = [p.symbol, p.mode, f"{p.amount:.5f}", _px(p.entry_price), _px(p.current_price), _money(p.pnl, True), f"{p.pnl_pct:+.2%}", _px(p.stop), _px(p.take_profit)]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 1:
                    it.setForeground(pg.mkColor(BLUE if p.mode == "TEST" else RED))
                if j in (5, 6):
                    it.setForeground(pg.mkColor(GREEN if p.pnl >= 0 else RED))
                self.pos_table.setItem(i, j, it)

    def _add_row(self, ts_ms: int, mode: str, side: str, detail: str, pnl: float | None, color: str | None = None) -> None:
        self.trades.insertRow(0)
        t = datetime.fromtimestamp(ts_ms / 1000).strftime("%b %d %H:%M:%S")
        vals = [t, mode, side, detail, _money(pnl, True) if pnl is not None else ""]
        for j, v in enumerate(vals):
            it = QTableWidgetItem(v)
            if j == 1:
                it.setForeground(pg.mkColor(BLUE if mode == "TEST" else RED if mode == "BUY" else MUTED))
            if j == 2 and color:
                it.setForeground(pg.mkColor(color))
            if j == 4 and pnl is not None:
                it.setForeground(pg.mkColor(GREEN if pnl >= 0 else RED))
            self.trades.setItem(0, j, it)
        while self.trades.rowCount() > 300:
            self.trades.removeRow(self.trades.rowCount() - 1)

    def _on_fill(self, ev: FillEvent) -> None:
        extra = f" · order {ev.order_id}" if ev.order_id else ""
        extra += " · PARTIAL" if ev.status == "partial" else ""
        self._add_row(ev.ts_ms, ev.mode, ev.side.upper(), f"{ev.symbol} {ev.amount:.6f} @ {_px(ev.price)} · fee {ev.fee:.4f} · {ev.reason}{extra}", ev.pnl, GREEN if ev.side == "buy" else RED)

    def _on_signal(self, ev: SignalEvent) -> None:
        self._add_row(ev.ts_ms, ev.mode, "signal", f"{ev.symbol}: {ev.text}", None, MUTED)

    def _on_alert(self, ev: AlertEvent) -> None:
        self._add_row(ev.ts_ms, "", "ALERT", f"{ev.title}: {ev.message}", None, RED)

    def _on_equity(self, ev: EquityEvent) -> None:
        self._equity_x.append(ev.ts_ms / 1000)
        self._equity_y.append(ev.equity)
        self._draw_equity()

    def _draw_equity(self) -> None:
        self.eq_curve.setData(self._equity_x, self._equity_y)
        if self._equity_x:
            span = max(3600.0, self._equity_x[-1] - self._equity_x[0])
            self.eq_plot.setXRange(self._equity_x[-1] - span, self._equity_x[-1] + span * 0.05, padding=0)

    def _load_history(self) -> None:
        for ts, eq in self.store.load_equity(Mode.TEST.value):
            self._equity_x.append(ts / 1000)
            self._equity_y.append(eq)
        self._draw_equity()
        for t in reversed(self.store.load_trades(None, limit=50)):
            self._add_row(t["ts_ms"], t["mode"], t["side"].upper(), f"{t['symbol']} {t['amount']:.6f} @ {_px(t.get('fill_price') or t['price'])} · fee {t['fee']:.4f} · {t['reason']}", t["pnl"], GREEN if t["side"] == "buy" else RED)

