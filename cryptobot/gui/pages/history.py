"""Trade history & reports: TEST / BUY / both, comparison, period summaries, exports."""
from __future__ import annotations

from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryptobot import reports
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import BLUE, GREEN, MUTED, RED

COLS = ["Time", "Mode", "Coin", "Side", "Amount", "Expected", "Filled at", "Slippage", "Fee", "Reason", "P&L", "Order id", "Status"]


def _px(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    d = 2 if v >= 1000 else 4 if v >= 1 else 6
    return f"{v:,.{d}f}"


class HistoryPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.store = store
        self.trades: list[dict] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        h = QLabel("History & Reports")
        h.setObjectName("h1")
        root.addWidget(h)

        bar = QHBoxLayout()
        self.filter = QButtonGroup(self)
        for i, label in enumerate(("TEST", "BUY", "PULSE", "All")):
            b = QPushButton(label)
            b.setObjectName("tf")
            b.setCheckable(True)
            self.filter.addButton(b, i)
            bar.addWidget(b)
        self.filter.button(3).setChecked(True)
        self.filter.idClicked.connect(lambda _i: self.refresh())
        bar.addSpacing(16)
        bar.addWidget(QLabel("Summary"))
        self.period = QComboBox()
        for p in ("daily", "weekly", "monthly"):
            self.period.addItem(p.capitalize(), p)
        self.period.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.period)
        bar.addStretch(1)
        self.csv_btn = QPushButton("Export CSV…")
        self.csv_btn.clicked.connect(self._export_csv)
        bar.addWidget(self.csv_btn)
        self.tax_btn = QPushButton("Export BUY trades for taxes…")
        self.tax_btn.setToolTip("FIFO lots: one row per buy-lot/sell pair with cost basis, proceeds, gain and short/long term.")
        self.tax_btn.clicked.connect(self._export_tax)
        bar.addWidget(self.tax_btn)
        root.addLayout(bar)
        self.msg = QLabel("")
        self.msg.setObjectName("muted")
        root.addWidget(self.msg)

        body = QHBoxLayout()
        body.setSpacing(10)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        body.addWidget(self.table, 5)

        right = QVBoxLayout()
        cmp_card = QFrame()
        cmp_card.setObjectName("card")
        cl = QVBoxLayout(cmp_card)
        cl.setContentsMargins(8, 8, 8, 8)
        self.cmp_title = QLabel("TEST vs BUY, same period")
        cl.addWidget(self.cmp_title)
        self.cmp = QTableWidget(0, 2)
        self.cmp.setHorizontalHeaderLabels(["TEST", "BUY"])
        self.cmp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.cmp.verticalHeader().setMinimumWidth(120)
        self.cmp.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        cl.addWidget(self.cmp)
        right.addWidget(cmp_card, 1)
        sum_card = QFrame()
        sum_card.setObjectName("card")
        sl = QVBoxLayout(sum_card)
        sl.setContentsMargins(8, 8, 8, 8)
        self.sum_title = QLabel("Summary")
        sl.addWidget(self.sum_title)
        self.summary = QTableWidget(0, 6)
        self.summary.setHorizontalHeaderLabels(["Period", "Fills", "Round trips", "Win rate", "P&L", "Fees"])
        self.summary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.summary.verticalHeader().setVisible(False)
        self.summary.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        sl.addWidget(self.summary, 1)
        self.chart = pg.PlotWidget()
        self.chart.setMinimumHeight(140)
        self.chart.showGrid(x=False, y=True, alpha=0.1)
        self.chart.setMouseEnabled(x=False, y=False)
        self.chart.hideButtons()
        self.chart.setLabel("left", "P&L ($)")
        self.chart.getAxis("left").enableAutoSIPrefix(False)
        self.bars = None
        sl.addWidget(self.chart, 1)
        right.addWidget(sum_card, 2)
        body.addLayout(right, 3)
        root.addLayout(body, 1)

        ctrl.fill.connect(lambda _e: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------

    def _mode(self) -> str | None:
        return {0: "TEST", 1: "BUY", 2: "PULSE", 3: None}[self.filter.checkedId()]

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        mode = self._mode()
        self.trades = self.store.load_trades(mode, limit=5000)
        self.table.setRowCount(len(self.trades))
        for i, t in enumerate(self.trades):
            vals = [
                datetime.fromtimestamp(t["ts_ms"] / 1000).strftime("%Y-%m-%d %H:%M:%S"), t["mode"], t["symbol"], t["side"].upper(),
                f"{t['amount']:.6f}", _px(t["price"]), _px(t.get("fill_price") or t["price"]),
                f"{float(t['slippage'] or 0):+.3f}%", f"{t['fee']:.4f}", t["reason"] or "",
                f"{t['pnl']:+.2f}" if t["pnl"] is not None else "", t.get("order_id") or "", t.get("status") or "",
            ]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 1:
                    it.setForeground(Qt.GlobalColor.cyan if t["mode"] == "TEST" else Qt.GlobalColor.red)
                if j == 3:
                    it.setForeground(Qt.GlobalColor.green if t["side"] == "buy" else Qt.GlobalColor.red)
                if j == 10 and t["pnl"] is not None:
                    it.setForeground(Qt.GlobalColor.green if t["pnl"] >= 0 else Qt.GlobalColor.red)
                self.table.setItem(i, j, it)
        # comparison always uses both modes
        tt = self.store.load_trades("TEST", limit=100_000)
        bt = self.store.load_trades("BUY", limit=100_000)
        c = reports.comparison(tt, bt)
        since = datetime.fromtimestamp(c["since_ms"] / 1000).strftime("%b %d, %Y") if c["since_ms"] else "—"
        self.cmp_title.setText(f"TEST vs BUY since first BUY trade ({since})" if bt else "TEST vs BUY — no BUY trades yet")
        rows = [("Fills", "fills", "int"), ("Round trips", "round_trips", "int"), ("Win rate", "win_rate", "pct"), ("P&L", "pnl", "usd"), ("Fees", "fees", "usd"), ("Avg slippage", "avg_slippage_pct", "pctraw"), ("Partial fills", "partial_fills", "int"), ("Volume", "volume", "usd")]
        self.cmp.setRowCount(len(rows))
        self.cmp.setVerticalHeaderLabels([r[0] for r in rows])
        for i, (_l, k, f) in enumerate(rows):
            for j, m in enumerate(("TEST", "BUY")):
                v = c[m][k]
                text = {"int": lambda x: f"{int(x)}", "pct": lambda x: f"{x:.0%}", "usd": lambda x: f"${x:,.2f}", "pctraw": lambda x: f"{x:+.3f}%"}[f](v)
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.cmp.setItem(i, j, it)
        # period summary for the current filter
        per = self.period.currentData()
        summ = reports.period_summary(self.trades, per)
        self.sum_title.setText(f"{per.capitalize()} summary — {mode or 'all modes'}")
        self._draw_chart(summ, per)
        self.summary.setRowCount(len(summ))
        for i, b in enumerate(reversed(summ)):
            vals = [b["period"], str(b["fills"]), str(b["round_trips"]), f"{b['win_rate']:.0%}", f"{b['pnl']:+.2f}", f"{b['fees']:.2f}"]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 4:
                    it.setForeground(Qt.GlobalColor.green if b["pnl"] >= 0 else Qt.GlobalColor.red)
                self.summary.setItem(i, j, it)

    def _draw_chart(self, summ: list[dict], per: str) -> None:
        if self.bars is not None:
            self.chart.removeItem(self.bars)
            self.bars = None
        tail = summ[-30:]
        if not tail:
            return
        xs = list(range(len(tail)))
        ys = [b["pnl"] for b in tail]
        brushes = [pg.mkBrush(GREEN if y >= 0 else RED) for y in ys]
        self.bars = pg.BarGraphItem(x=xs, height=ys, width=0.7, brushes=brushes, pen=pg.mkPen(None))
        self.chart.addItem(self.bars)
        step = max(1, len(tail) // 8)
        labels = [(i, b["period"].replace("week of ", "")[-5:] if per != "monthly" else b["period"]) for i, b in enumerate(tail) if i % step == 0]
        self.chart.getAxis("bottom").setTicks([labels])
        lo, hi = min(0.0, min(ys)), max(0.0, max(ys))
        pad = (hi - lo) * 0.15 or 1.0
        self.chart.setYRange(lo - pad, hi + pad, padding=0)
        self.chart.setXRange(-0.6, len(tail) - 0.4, padding=0)

    def _export_csv(self) -> None:
        mode = self._mode() or "ALL"
        path, _ = QFileDialog.getSaveFileName(self, "Export trades", f"cryptobot_trades_{mode}.csv", "CSV (*.csv)")
        if path:
            n = reports.export_csv(self.trades, path)
            self.msg.setText(f"Exported {n} trades to {path}")

    def _export_tax(self) -> None:
        bt = self.store.load_trades("BUY", limit=100_000)
        if not bt:
            self.msg.setText("No BUY trades to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export for taxes", "cryptobot_tax_lots.csv", "CSV (*.csv)")
        if path:
            n = reports.export_tax_csv(bt, path)
            self.msg.setText(f"Exported {n} tax lots (FIFO) to {path}")
