"""Live market screen: exchange and coin selection, live candles, last price."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryptobot.config import DEFAULT_EXCHANGE, EXCHANGES
from cryptobot.core.bars import TIMEFRAME_LABELS
from cryptobot.core.events import BarsEvent, ConnectionEvent, FillEvent, TickEvent
from cryptobot.core.models import Bar
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, GREEN, GREY, MUTED, RED
from cryptobot.gui.widgets.candle_chart import CandleChart

MAX_KEEP = 3000
CHART_TIMEFRAMES = (1, 60, 300)


class MarketPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self._series: dict[tuple[str, int], dict[int, Bar]] = {}
        self._tf = 1
        self._last_tick_ms = 0
        self._tick_count = 0
        self._tick_count_shown = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # -- toolbar --------------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.exchange_box = QComboBox()
        for ex_id, meta in EXCHANGES.items():
            self.exchange_box.addItem(meta["name"], ex_id)
        self.symbol_box = QComboBox()
        self.symbol_box.setEditable(True)
        self.symbol_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.symbol_box.setToolTip("Trading pair. Type one and press Enter, e.g. BTC/USD")
        bar.addWidget(QLabel("Exchange"))
        bar.addWidget(self.exchange_box)
        bar.addWidget(QLabel("Pair"))
        bar.addWidget(self.symbol_box)
        bar.addSpacing(12)
        self.tf_group = QButtonGroup(self)
        for tf in CHART_TIMEFRAMES:
            b = QPushButton(TIMEFRAME_LABELS[tf])
            b.setObjectName("tf")
            b.setCheckable(True)
            b.setToolTip(
                "1-second candles are built live from every trade. 1m and 5m also load recent history."
            )
            self.tf_group.addButton(b, tf)
            bar.addWidget(b)
        self.tf_group.button(1).setChecked(True)
        bar.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        bar.addWidget(self.status)
        root.addLayout(bar)

        # -- chart ----------------------------------------------------------
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(8, 8, 8, 8)
        self.chart = CandleChart(visible=240)
        cl.addWidget(self.chart)
        root.addWidget(card, 1)

        # -- footer ---------------------------------------------------------
        foot = QHBoxLayout()
        self.price = QLabel("—")
        self.price.setObjectName("price")
        foot.addWidget(self.price)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        foot.addWidget(self.meta)
        foot.addStretch(1)
        self.rate = QLabel("")
        self.rate.setObjectName("muted")
        foot.addWidget(self.rate)
        root.addLayout(foot)

        # -- wiring ---------------------------------------------------------
        ctrl.bars.connect(self._on_bars)
        ctrl.tick.connect(self._on_tick)
        ctrl.connection.connect(self._on_connection)
        ctrl.fill.connect(self._on_fill)
        self.tf_group.idClicked.connect(self._on_tf)
        self.exchange_box.currentIndexChanged.connect(self._on_exchange_changed)
        self.symbol_box.activated.connect(self._on_symbol_changed)

        saved_ex = store.get_setting("market.exchange", DEFAULT_EXCHANGE)
        idx = self.exchange_box.findData(saved_ex)
        self.exchange_box.blockSignals(True)
        self.exchange_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.exchange_box.blockSignals(False)
        self._fill_symbols(self.exchange_id)
        saved_sym = store.get_setting("market.symbol", self.symbol_box.itemText(0))
        self.symbol_box.setCurrentText(saved_sym)

        self._age_timer = QTimer(self)
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self._refresh_meta)
        self._age_timer.start()

    # -- properties ---------------------------------------------------------

    @property
    def exchange_id(self) -> str:
        return self.exchange_box.currentData()

    @property
    def symbol(self) -> str:
        return self.symbol_box.currentText().strip().upper()

    # -- handlers -----------------------------------------------------------

    def _fill_symbols(self, exchange_id: str) -> None:
        from cryptobot.gui.pages.coins import selected_symbols

        self.symbol_box.blockSignals(True)
        self.symbol_box.clear()
        seen = []
        for s in selected_symbols(self.store, exchange_id) + EXCHANGES[exchange_id]["symbols"]:
            if s not in seen:
                seen.append(s)
                self.symbol_box.addItem(s)
        self.symbol_box.blockSignals(False)

    def _on_exchange_changed(self) -> None:
        self._fill_symbols(self.exchange_id)
        self.symbol_box.setCurrentIndex(0)
        self._apply_market()

    def _on_symbol_changed(self) -> None:
        self._apply_market()

    def _apply_market(self) -> None:
        from cryptobot.gui.pages.coins import selected_symbols

        self.store.set_setting("market.exchange", self.exchange_id)
        self.store.set_setting("market.symbol", self.symbol)
        self._series.clear()
        self._tick_count = 0
        self.price.setText("—")
        self.chart.set_series([], self._tf)
        syms = selected_symbols(self.store, self.exchange_id)
        if self.symbol not in syms:
            syms = syms + [self.symbol]  # chart a pair without adding it to the traded list
        self.ctrl.set_market(self.exchange_id, syms)

    def _on_tf(self, tf: int) -> None:
        self._tf = tf
        self._redraw()

    def _on_bars(self, ev: BarsEvent) -> None:
        if ev.exchange != self.ctrl.exchange:
            return
        key = (ev.symbol, ev.timeframe_s)
        series = self._series.setdefault(key, {})
        if ev.replace:
            series.clear()
        for b in ev.bars:
            series[b.ts_ms] = b
        if len(series) > MAX_KEEP:
            for k in sorted(series)[: len(series) - MAX_KEEP]:
                del series[k]
        if key == (self.symbol, self._tf):
            self._redraw()

    def _on_tick(self, ev: TickEvent) -> None:
        if ev.exchange != self.ctrl.exchange or ev.tick.symbol != self.symbol:
            return
        self._last_tick_ms = ev.tick.ts_ms
        self._tick_count += 1
        p = ev.tick.price
        d = 2 if p >= 1000 else 4 if p >= 1 else 6
        self.price.setText(f"{p:,.{d}f}")

    def _on_fill(self, ev: FillEvent) -> None:
        if ev.symbol == self.symbol:
            self.chart.add_fill(ev.ts_ms, ev.price, ev.side, ev.mode)

    def _on_connection(self, ev: ConnectionEvent) -> None:
        color = {
            "connecting": AMBER,
            "reconnecting": AMBER,
            "connected": GREEN,
            "blocked": RED,
            "error": RED,
            "stopped": GREY,
        }[ev.status]
        text = {
            "connecting": "Connecting…",
            "reconnecting": f"Reconnecting (attempt {ev.attempt})…",
            "connected": "Live",
            "blocked": "Blocked",
            "error": "Error",
            "stopped": "Stopped",
        }[ev.status]
        self.status.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.status.setText(f"● {text}")
        self.status.setToolTip(ev.message)
        if ev.status in ("blocked", "error"):
            self.meta.setStyleSheet(f"color: {RED};")
            self.meta.setText(ev.message)
        elif ev.status == "connected":
            self.meta.setStyleSheet(f"color: {MUTED};")
            self._refresh_meta()

    # -- rendering ----------------------------------------------------------

    def _redraw(self) -> None:
        series = self._series.get((self.symbol, self._tf), {})
        bars = [series[k] for k in sorted(series)]
        self.chart.set_series(bars, self._tf)

    def _refresh_meta(self) -> None:
        series = self._series.get((self.symbol, self._tf), {})
        n = len(series)
        tps = self._tick_count - self._tick_count_shown
        self._tick_count_shown = self._tick_count
        if self._last_tick_ms:
            age = max(0.0, time.time() - self._last_tick_ms / 1000.0)
            age_s = f"last trade {age:.0f}s ago" if age >= 2 else "last trade just now"
        else:
            age_s = "no trades yet"
        if self.status.text().endswith("Live") or not self.status.text():
            self.meta.setText(f"{self.symbol} · {TIMEFRAME_LABELS[self._tf]} · {n} bars · {age_s}")
        self.rate.setText(f"{tps} trades/s")
