"""Coin picker: every pair on the exchange with liquidity and quality columns."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryptobot.config import EXCHANGES
from cryptobot.data.markets import MarketRow, fetch_market_rows
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, BLUE, GREEN, MUTED, RED


def selected_symbols(store: Store, exchange: str) -> list[str]:
    saved = store.get_setting(f"market.symbols.{exchange}")
    if saved:
        return list(saved)
    return [store.get_setting("market.symbol") or EXCHANGES[exchange]["symbols"][0]] if store.get_setting("market.exchange") == exchange else [EXCHANGES[exchange]["symbols"][0]]


class _Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, exchange_id: str) -> None:
        super().__init__()
        self.exchange_id = exchange_id

    def run(self) -> None:
        try:
            self.done.emit(fetch_market_rows(self.exchange_id))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}"[:200])


class CoinsPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self.rows: list[MarketRow] = []
        self.selected: set[str] = set()
        self._exchange = ""
        self._worker: _Worker | None = None
        self._trading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        h = QLabel("Coins")
        h.setObjectName("h1")
        root.addWidget(h)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search pairs…")
        self.search.textChanged.connect(self._render)
        bar.addWidget(self.search, 1)
        self.suggested = QCheckBox("Suggested only")
        self.suggested.setChecked(True)
        self.suggested.setToolTip("Hide thin markets: under $1M daily volume or spread over 0.3%. Thin coins mean bad fills and stop-hunts.")
        self.suggested.toggled.connect(self._render)
        bar.addWidget(self.suggested)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_btn)
        root.addLayout(bar)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        root.addWidget(self.status)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["", "Pair", "Price", "24h volume", "24h change", "Spread", "24h range"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.itemChanged.connect(self._toggled)
        root.addWidget(self.table, 1)

        foot = QHBoxLayout()
        self.sel_label = QLabel("")
        self.sel_label.setWordWrap(True)
        foot.addWidget(self.sel_label, 1)
        self.apply_btn = QPushButton("Apply selection")
        self.apply_btn.setStyleSheet(f"background: {BLUE}; color: white; font-weight: 600;")
        self.apply_btn.clicked.connect(self.apply)
        foot.addWidget(self.apply_btn)
        root.addLayout(foot)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        ctrl.connection.connect(self._on_connection)
        ctrl.trader_state.connect(lambda ev: setattr(self, "_trading", ev.status != "stopped"))

    def _on_connection(self, ev) -> None:
        if ev.exchange != self._exchange:
            self._exchange = ev.exchange
            self.selected = set(selected_symbols(self.store, ev.exchange))
            self._update_sel_label()
            self.refresh()

    def refresh(self) -> None:
        if not self._exchange or (self._worker and self._worker.isRunning()):
            return
        self.refresh_btn.setEnabled(False)
        self.status.setText(f"Loading markets from {EXCHANGES.get(self._exchange, {}).get('name', self._exchange)}…")
        self._worker = _Worker(self._exchange)
        self._worker.done.connect(self._loaded)
        self._worker.failed.connect(lambda m: self.status.setText(f"Could not load markets: {m}"))
        self._worker.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        self._worker.start()

    def _loaded(self, rows: list[MarketRow]) -> None:
        self.rows = rows
        n_sug = sum(1 for r in rows if r.suggested)
        self.status.setText(f"{len(rows)} pairs · {n_sug} suggested (≥ $1M daily volume, spread ≤ 0.3%)")
        self._render()

    def _render(self) -> None:
        q = self.search.text().strip().upper()
        only = self.suggested.isChecked()
        shown = [r for r in self.rows if (not only or r.suggested or r.symbol in self.selected) and (not q or q in r.symbol)]
        self.table.blockSignals(True)
        self.table.setRowCount(len(shown))
        for i, r in enumerate(shown):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if r.symbol in self.selected else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, r.symbol)
            self.table.setItem(i, 0, chk)
            d = 2 if r.price >= 1000 else 4 if r.price >= 1 else 6
            vals = [r.symbol, f"{r.price:,.{d}f}", f"${r.volume_quote_24h / 1e6:,.1f}M", f"{r.change_24h_pct:+.2f}%", f"{r.spread_pct:.3f}%" if r.spread_pct >= 0 else "—", f"{r.range_24h_pct:.1f}%"]
            for j, v in enumerate(vals, start=1):
                it = QTableWidgetItem(v)
                if j > 1:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if j == 4:
                    it.setForeground(Qt.GlobalColor.green if r.change_24h_pct >= 0 else Qt.GlobalColor.red)
                if j == 5 and r.spread_pct > 0.3:
                    it.setForeground(Qt.GlobalColor.yellow)
                if not r.suggested:
                    it.setToolTip("Not suggested: thin volume or wide spread.")
                self.table.setItem(i, j, it)
        self.table.blockSignals(False)

    def _toggled(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        sym = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(sym)
        else:
            self.selected.discard(sym)
        self._update_sel_label()

    def _update_sel_label(self) -> None:
        self.sel_label.setText("Selected: " + (", ".join(sorted(self.selected)) or "none"))

    def apply(self) -> None:
        if not self.selected:
            self.note.setText("Select at least one coin.")
            return
        syms = sorted(self.selected)
        self.store.set_setting(f"market.symbols.{self._exchange}", syms)
        if self.store.get_setting("market.symbol") not in syms:
            self.store.set_setting("market.symbol", syms[0])
        self.ctrl.set_market(self._exchange, syms)
        self.note.setText(
            f"Saved {len(syms)} coin(s). " + ("The bot is running: press STOP then START on the Dashboard to trade the new list." if self._trading else "They will be traded when you press START.")
        )
