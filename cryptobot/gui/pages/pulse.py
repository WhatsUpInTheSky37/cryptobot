"""Crypto Pulse screen: live pressure signals with data health, three paper modes,
performance, rejected-trade analysis, and the Market Memory browser."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cryptobot.config import DEFAULT_EXCHANGE, EXCHANGES
from cryptobot.core.events import PulseEventRecord, PulseModeStatus, PulseSnapshot, PulseStatusEvent
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, BLUE, BORDER, GREEN, GREY, MUTED, PANEL2, RED
from cryptobot.pulse import analysis
from cryptobot.pulse.config import INDICATOR_NAMES, INDICATORS, OUTCOME_HORIZONS_MS, PulseConfig
from cryptobot.pulse.health import INSUFFICIENT

STATE_COLOR = {"off": GREY, "warming": MUTED, "excluded": RED, "idle": MUTED, "monitor": AMBER, "prepared": BLUE, "entered": GREEN}
MODE_COLORS = {"A": GREEN, "B": BLUE, "C": AMBER}
TIPS = {
    "f": "Persistence of aggressive buying over the last 60 s, ranked against this coin's own recent history.",
    "a": "Volume per % of price movement: lots of trading with little price change means the move is being absorbed.",
    "w": "Sell-side liquidity within 0.1% of the mid disappearing over the last 10 s.",
    "r": "Sell-side depth sitting below its 60-second average (not refilling).",
    "c": "Whether the related market (BTC↔ETH) shows the same pressure.",
    "acc": "Order-flow acceleration: imbalance in the last quarter of the window versus the three quarters before.",
    "vpr": "Volume-adjusted price response: how far price moved per unit of volume (high after the release).",
    "sx": "Spread expansion versus the window median. More is worse: it counts against the score.",
    "vx": "Short-term volatility: the last minute's realised movement versus the last ten minutes'.",
}


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d %H:%M:%S")


def _pct(v: float | None, digits: int = 3) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def _table(headers: list[str], stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(stretch_last)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    return t


def _fill(t: QTableWidget, rows: list[list[str]], colors: dict[tuple[int, int], str] | None = None) -> None:
    t.setRowCount(0)
    for r, vals in enumerate(rows):
        t.insertRow(r)
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            if colors and (r, c) in colors:
                it.setForeground(Qt.GlobalColor.green if colors[(r, c)] == "good" else Qt.GlobalColor.red if colors[(r, c)] == "bad" else Qt.GlobalColor.gray)
            t.setItem(r, c, it)


def _card() -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.setSpacing(4)
    return f, lay


class _Bar(QProgressBar):
    def __init__(self, label: str, tip: str) -> None:
        super().__init__()
        self.setRange(0, 100)
        self.setFormat(f"{label}  %v")
        self.setTextVisible(True)
        self.setFixedHeight(16)
        self.setToolTip(tip)
        self.setStyleSheet(f"QProgressBar {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 4px; text-align: center; font-size: 10px; }} QProgressBar::chunk {{ background: {BLUE}; border-radius: 3px; }}")


class _SymbolCard(QFrame):
    def __init__(self, symbol: str) -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)
        top = QHBoxLayout()
        self.title = QLabel(symbol)
        self.title.setStyleSheet("font-weight: 700; font-size: 15px;")
        top.addWidget(self.title)
        self.regime = QLabel("")
        self.regime.setObjectName("muted")
        top.addWidget(self.regime)
        top.addStretch(1)
        self.health = QLabel("")
        top.addWidget(self.health)
        self.state = QLabel("off")
        top.addWidget(self.state)
        lay.addLayout(top)
        self.prs = QLabel("PRS —")
        self.prs.setStyleSheet("font-size: 20px; font-weight: 700;")
        lay.addWidget(self.prs)
        self.bars = {k: _Bar(INDICATOR_NAMES[k], TIPS[k]) for k in INDICATORS}
        for b in self.bars.values():
            lay.addWidget(b)
        self.ev = QLabel("")
        self.ev.setWordWrap(True)
        lay.addWidget(self.ev)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)

    def update(self, s: PulseSnapshot) -> None:
        self.state.setText(s.state)
        self.state.setStyleSheet(f"color: {STATE_COLOR.get(s.state, MUTED)}; font-weight: 600;")
        self.regime.setText(f"· {s.regime}")
        if not s.valid:
            self.health.setText(INSUFFICIENT if s.state != "off" else "")
            self.health.setStyleSheet(f"color: {RED}; font-weight: 700;")
            self.prs.setText("PRS —")
            self.prs.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {MUTED};")
            for b in self.bars.values():
                b.setValue(0)
                b.setFormat(f"{b.format().split('  ')[0]}  —")
            self.ev.setText("No signal: " + (s.health_detail or s.health))
            self.ev.setStyleSheet(f"color: {RED};")
            self.meta.setText(f"mid {s.mid:,.2f} · {s.trades_per_min:.1f} trades/min · book seq {s.book_seq}  {s.note}")
            return
        self.health.setText("DATA OK")
        self.health.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
        color = GREEN if s.prs_long >= 85 else BLUE if s.prs_long >= 70 else AMBER if s.prs_long >= 50 else MUTED
        self.prs.setText(f"Bullish {s.prs_long:.0f}  ·  bearish {s.prs_short:.0f}")
        self.prs.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {color};")
        for k, b in self.bars.items():
            v = s.scores_long.get(k)
            b.setValue(int(round(v)) if v is not None else 0)
            b.setFormat(f"{INDICATOR_NAMES[k]}  %v" + (f" / bear {s.scores_short.get(k, 0):.0f}" if k in ("f", "w", "r", "c", "acc", "vpr") else ""))
        a = s.assessment_long or {}
        if a:
            ev = a.get("ev_pct")
            txt = (f"Expected net profit after costs: {_pct(ev)}  (target {a.get('target_pct', 0):.2f}% / stop {a.get('stop_pct', 0):.2f}% / costs {a.get('cost_pct', 0):.3f}%"
                   f" = fees {a.get('fee_pct', 0):.3f} + spread {a.get('spread_pct', 0):.3f} + slippage {a.get('slippage_pct', 0):.3f})")
            if ev is None:
                txt += f"\n{a.get('reason', '')}"
            else:
                txt += f"\n{a.get('evidence', '')}; break-even {a.get('break_even_p', 0):.0%}; R/R after costs {a.get('rr') or 0:.2f}"
            self.ev.setText(txt)
            self.ev.setStyleSheet(f"color: {GREEN if ev is not None and ev > 0 else AMBER if ev is None else RED};")
        self.meta.setText(f"mid {s.mid:,.2f} · bid/ask {s.bid:,.2f}/{s.ask:,.2f} · imbalance {s.imbalance_60:+.2f} · spread {s.spread_pct:.3f}% · 1m vol {s.vol_pct:.2f}% · {s.trades_per_min:.0f} trades/min  {s.note}")


class _ModeCard(QFrame):
    def __init__(self, key: str, label: str) -> None:
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        h = QLabel(f"Mode {key} · {label}")
        h.setStyleSheet(f"font-weight: 700; color: {MODE_COLORS.get(key, BLUE)};")
        lay.addWidget(h)
        self.equity = QLabel("—")
        self.equity.setStyleSheet("font-size: 18px; font-weight: 700;")
        lay.addWidget(self.equity)
        self.line1 = QLabel("")
        self.line2 = QLabel("")
        self.line3 = QLabel("")
        for w in (self.line1, self.line2, self.line3):
            w.setObjectName("muted")
            w.setWordWrap(True)
            lay.addWidget(w)

    def update(self, m: PulseModeStatus) -> None:
        pnl = m.equity - m.budget
        self.equity.setText(f"${m.equity:,.2f}  ({pnl:+,.2f})")
        self.equity.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {GREEN if pnl > 0 else RED if pnl < 0 else MUTED};")
        wr = m.wins / m.trades if m.trades else 0.0
        self.line1.setText(f"today {m.realized_today:+,.2f} realized / {m.unrealized:+,.2f} open · open risk {m.open_risk_pct:.2f}% · {m.open_positions} position(s)")
        self.line2.setText(f"{m.trades} trades · {wr:.0%} wins · P&L {m.net_pnl:+,.2f} · accepted {m.accepted} / rejected {m.rejected}")
        status = []
        if m.halted:
            status.append("daily loss limit reached")
        if m.paused_until_ms and m.paused_until_ms > datetime.now().timestamp() * 1000:
            status.append(f"paused until {datetime.fromtimestamp(m.paused_until_ms / 1000).strftime('%H:%M')}")
        if m.consecutive_losses:
            status.append(f"{m.consecutive_losses} loss(es) in a row")
        pos = "; ".join(f"{p['symbol']} {p['units']:.5f} @ {p['entry_px']:,.2f} (stop {p['stop']:,.2f}, target {p['target']:,.2f})" for p in m.positions)
        self.line3.setText(" · ".join(status) + ((" · " if status else "") + pos if pos else "") or (m.message or ""))
        self.line3.setStyleSheet(f"color: {RED if m.halted else AMBER if status else MUTED};")


class PulsePage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self.cards: dict[str, _SymbolCard] = {}
        self.mode_cards: dict[str, _ModeCard] = {}
        self._events_by_row: dict[int, int] = {}
        raw = store.get_setting("pulse.config") or {}
        h = raw.get("health") or {}
        if h.get("stale_trade_s") == 90 and h.get("min_trades_per_min") == 2.0:
            raw.pop("health", None)  # 0.4.0 saved the old liquid-exchange defaults; let the thin-exchange defaults apply
        try:
            self.cfg = PulseConfig.model_validate(raw)
        except Exception:  # noqa: BLE001
            self.cfg = PulseConfig()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)
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
        self.health_line = QLabel("Not running.")
        self.health_line.setWordWrap(True)
        root.addWidget(self.health_line)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live(), "Live")
        self.tabs.addTab(self._build_performance(), "Performance")
        self.tabs.addTab(self._build_rejected(), "Why didn't we trade?")
        self.tabs.addTab(self._build_memory(), "Market Memory")
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)

        ctrl.pulse_status.connect(self._on_status)
        ctrl.pulse_event.connect(self._on_event)
        self._load_events()

    # ------------------------------------------------------------- builders

    def _build_live(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(8)
        banner = QLabel(
            "Pulse watches aggressive trades and order-book liquidity for pressure that price is not yet responding to. Every coin is checked for live, "
            "synchronised data first; anything stale shows INSUFFICIENT DATA and produces no signal. Each setup is recorded with what price did next "
            "(the Market Memory) and handed to three independent paper books. A trade needs enough independent similar history and a positive expected "
            "value after fees, spread and slippage; with no history there are no trades, only evidence. Spot cannot short, so bearish setups are research only."
        )
        banner.setWordWrap(True)
        banner.setObjectName("muted")
        lay.addWidget(banner)
        modes = QHBoxLayout()
        for m in self.cfg.modes:
            c = _ModeCard(m.key, m.label)
            self.mode_cards[m.key] = c
            modes.addWidget(c)
        lay.addLayout(modes)
        body = QHBoxLayout()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.cards_box = QGridLayout(inner)
        self.cards_box.setSpacing(8)
        self.cards_box.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(inner)
        body.addWidget(scroll, 4)
        side = QVBoxLayout()
        sg, sf_box = _card()
        sf = QFormLayout()
        sf.setContentsMargins(0, 0, 0, 0)
        sf_box.addLayout(sf)
        self.exchange = QComboBox()
        self.exchange.addItem("Same as Market page", "")
        for ex_id, meta in EXCHANGES.items():
            self.exchange.addItem(meta["name"], ex_id)
        saved_ex = self.store.get_setting("pulse.exchange", "")
        i = self.exchange.findData(saved_ex)
        self.exchange.setCurrentIndex(i if i >= 0 else 0)
        self.exchange.setToolTip("Pulse is paper-only and needs no API key, so it can watch a liquid exchange (Coinbase, Kraken) while the trader stays on yours. 'Same as Market page' shares the trader's feed.")
        self.exchange.currentIndexChanged.connect(self._exchange_changed)
        sf.addRow("Exchange", self.exchange)
        self.symbols = QLineEdit()
        self.symbols.setPlaceholderText("coins, comma separated, e.g. BTC/USD, ETH/USD")
        self.symbols.setToolTip("Coins Pulse watches on that exchange. Liquid pairs give meaningful order-flow scores; thin ones show INSUFFICIENT DATA.")
        saved_syms = self.store.get_setting("pulse.symbols")
        self.symbols.setText(", ".join(saved_syms) if saved_syms else "")
        self._exchange_changed(self.exchange.currentIndex())
        sf.addRow("Coins", self.symbols)
        self.budget = QDoubleSpinBox()
        self.budget.setRange(10, 100_000)
        self.budget.setPrefix("$")
        self.budget.setValue(self.cfg.budget)
        self.budget.setToolTip("Paper budget for EACH of the three modes. Separate from the main bot's budget.")
        sf.addRow("Paper budget per mode", self.budget)
        self.risk = QDoubleSpinBox()
        self.risk.setRange(0.05, 2)
        self.risk.setSingleStep(0.05)
        self.risk.setSuffix("% of equity")
        self.risk.setValue(self.cfg.risk.risk_per_trade_pct)
        self.risk.setToolTip("Money risked per trade (distance to the stop) as a share of current equity. Never scaled by winning or losing streaks.")
        sf.addRow("Risk per trade", self.risk)
        self.daily = QDoubleSpinBox()
        self.daily.setRange(0.1, 10)
        self.daily.setSingleStep(0.1)
        self.daily.setSuffix("%")
        self.daily.setValue(self.cfg.risk.daily_loss_pct)
        self.daily.setToolTip("Stop opening trades for the day when today's realized plus unrealized loss reaches this share of equity.")
        sf.addRow("Daily loss limit", self.daily)
        self.maxpos = QSpinBox()
        self.maxpos.setRange(1, 10)
        self.maxpos.setValue(self.cfg.risk.max_positions)
        sf.addRow("Max open positions", self.maxpos)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(10, 3600)
        self.cooldown.setSuffix(" s")
        self.cooldown.setValue(self.cfg.event_cooldown_s)
        self.cooldown.setToolTip("Minimum time between two recorded signals of the same kind on the same coin.")
        sf.addRow("Signal cooldown", self.cooldown)
        self.stale = QSpinBox()
        self.stale.setRange(30, 1800)
        self.stale.setSuffix(" s")
        self.stale.setValue(self.cfg.health.stale_trade_s)
        self.stale.setToolTip("A coin with no public trade for this long shows INSUFFICIENT DATA. Thin exchanges (Binance.US) need a generous value; liquid ones (Coinbase, Kraken) can be strict.")
        sf.addRow("Stale after no trade for", self.stale)
        self.min_tpm = QDoubleSpinBox()
        self.min_tpm.setRange(0.0, 100.0)
        self.min_tpm.setSingleStep(0.5)
        self.min_tpm.setSuffix(" trades/min")
        self.min_tpm.setValue(self.cfg.health.min_trades_per_min)
        self.min_tpm.setToolTip("Below this activity a market is excluded for a while. 0 disables the activity filter.")
        sf.addRow("Exclude below", self.min_tpm)
        side.addWidget(sg)
        st_card, stl = _card()
        self.s_memory = QLabel("—")
        self.s_memory.setWordWrap(True)
        self.s_ml = QLabel("—")
        self.s_ml.setWordWrap(True)
        self.s_msg = QLabel("Not running.")
        self.s_msg.setWordWrap(True)
        stl.addWidget(QLabel("Market Memory"))
        stl.addWidget(self.s_memory)
        stl.addWidget(QLabel("Machine learning (Mode C)"))
        stl.addWidget(self.s_ml)
        self.train_btn = QPushButton("Train model now")
        self.train_btn.setToolTip("Walk-forward validates a model on the resolved signals and keeps it only if it beats the rules out of sample. Needs Pulse running.")
        self.train_btn.clicked.connect(self.ctrl.pulse_train)
        stl.addWidget(self.train_btn)
        stl.addWidget(QLabel("Status"))
        stl.addWidget(self.s_msg)
        side.addWidget(st_card)
        side.addStretch(1)
        body.addLayout(side, 2)
        lay.addLayout(body, 1)
        return w

    def _build_performance(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("All three modes see the same signals and pay the same fees, spread and slippage. Nothing here picks a winner: compare, then decide."))
        top.addStretch(1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_analysis)
        top.addWidget(b)
        lay.addLayout(top)
        self.perf_table = _table(["Mode", "Equity", "Return", "Trades", "Win rate", "Profit factor", "Expectancy / trade", "Avg win", "Avg loss", "Max drawdown", "Profitable days", "Fees paid"])
        self.perf_table.setMaximumHeight(140)
        lay.addWidget(self.perf_table)
        self.eq_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.eq_plot.showGrid(x=True, y=True, alpha=0.08)
        self.eq_plot.setLabel("left", "Paper equity ($)")
        self.eq_plot.addLegend()
        self.eq_curves = {k: self.eq_plot.plot([], [], pen=pg.mkPen(MODE_COLORS[k], width=2), name=f"Mode {k}") for k in MODE_COLORS}
        lay.addWidget(self.eq_plot, 1)
        bottom = QHBoxLayout()
        self.coin_table = _table(["Coin", "Signals", "Resolved", "Hit rate", "Avg net after costs", "Sum net", "Avg MFE", "Avg MAE"])
        self.regime_table = _table(["Regime", "Signals", "Resolved", "Hit rate", "Avg net after costs", "Sum net", "Avg MFE", "Avg MAE"])
        for t, title in ((self.coin_table, "Signal outcomes by coin (long)"), (self.regime_table, "Signal outcomes by market regime (long)")):
            c, cl = _card()
            cl.addWidget(QLabel(title))
            cl.addWidget(t)
            bottom.addWidget(c)
        lay.addLayout(bottom, 1)
        return w

    def _build_rejected(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lab = QLabel("Every rejected signal keeps being tracked with the same execution assumptions as a real paper trade. "
                     "\"Profitable\" counts only signals whose hypothetical return is positive after fees, spread and slippage, not every favourable wiggle.")
        lab.setWordWrap(True)
        lab.setObjectName("muted")
        lay.addWidget(lab)
        self.rej_table = _table(["Mode", "Reason", "Rejected", "Resolved", "Hit target first", "Would have been profitable", "Avg net after costs", "Sum net"])
        lay.addWidget(self.rej_table, 1)
        return w

    def _build_memory(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        filt = QHBoxLayout()
        self.f_symbol = QComboBox()
        self.f_symbol.addItem("All coins", None)
        self.f_dir = QComboBox()
        for label, val in (("Long + short", None), ("Long", "long"), ("Short (research)", "short")):
            self.f_dir.addItem(label, val)
        self.f_outcome = QComboBox()
        for label in ("Any outcome", "pending", "target", "stop", "expired", "abandoned"):
            self.f_outcome.addItem(label, None if label.startswith("Any") else label)
        for c in (self.f_symbol, self.f_dir, self.f_outcome):
            c.currentIndexChanged.connect(self._load_events)
            filt.addWidget(c)
        filt.addStretch(1)
        self.mem_count = QLabel("")
        self.mem_count.setObjectName("muted")
        filt.addWidget(self.mem_count)
        lay.addLayout(filt)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.events = _table(["Time", "Coin", "Dir", "PRS", "Regime", "Kind", "Outcome", "Net after costs", "MFE / MAE", "Evidence", "Decisions"])
        self.events.itemSelectionChanged.connect(self._show_event)
        split.addWidget(self.events)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Select a signal to see the complete reasoning: indicator values, costs, historical evidence, each mode's decision, and what price did afterwards.")
        split.addWidget(self.detail)
        split.setSizes([700, 500])
        lay.addWidget(split, 1)
        self.mem_edge = QLabel("")
        self.mem_edge.setObjectName("muted")
        self.mem_edge.setWordWrap(True)
        self.mem_edge.setToolTip("AUC per indicator over the resolved long signals shown: 0.5 is no information; above 0.5 means higher values went with hitting the target first.")
        lay.addWidget(self.mem_edge)
        return w

    # ------------------------------------------------------------ commands

    def _exchange_changed(self, _idx: int) -> None:
        ex_id = self.exchange.currentData()
        self.symbols.setEnabled(bool(ex_id))
        if not ex_id:
            self.symbols.setText("")
            self.symbols.setPlaceholderText("uses the Market page's exchange and coins")
            return
        if not self.symbols.text().strip() or self.store.get_setting("pulse.exchange", "") != ex_id:
            self.symbols.setText(", ".join(EXCHANGES[ex_id]["symbols"]))

    def _feed_choice(self) -> tuple[str | None, list[str] | None]:
        ex_id = self.exchange.currentData()
        if not ex_id:
            return None, None
        syms = [t.strip().upper() for t in self.symbols.text().replace(";", ",").split(",") if t.strip()]
        return ex_id, (syms or list(EXCHANGES.get(ex_id, EXCHANGES[DEFAULT_EXCHANGE])["symbols"]))

    def _config(self) -> PulseConfig:
        risk = self.cfg.risk.model_copy(update={"risk_per_trade_pct": self.risk.value(), "daily_loss_pct": self.daily.value(), "max_positions": self.maxpos.value()})
        health = self.cfg.health.model_copy(update={"stale_trade_s": self.stale.value(), "min_trades_per_min": self.min_tpm.value()})
        self.cfg = self.cfg.model_copy(update={"budget": self.budget.value(), "risk_per_trade_pct": self.risk.value(), "daily_loss_pct": self.daily.value(),
                                               "max_positions": self.maxpos.value(), "event_cooldown_s": self.cooldown.value(), "risk": risk, "health": health})
        self.cfg = PulseConfig.model_validate(self.cfg.model_dump())
        self.store.set_setting("pulse.config", self.cfg.model_dump())
        return self.cfg

    def start(self) -> None:
        cfg = self._config()
        slip = float(self.store.get_setting("backtest.settings", {}).get("slippage_pct", 0.05))
        ex_id, syms = self._feed_choice()
        self.store.set_setting("pulse.exchange", ex_id or "")
        self.store.set_setting("pulse.symbols", syms or [])
        self.ctrl.pulse_start(cfg.model_dump(), slip, ex_id, syms)
        self.store.set_setting("pulse.running", True)
        self.s_msg.setText("Starting…" + (f" on {EXCHANGES[ex_id]['name']}: {', '.join(syms)}" if ex_id and syms else ""))

    def stop(self) -> None:
        self.ctrl.pulse_stop()
        self.store.set_setting("pulse.running", False)

    # -------------------------------------------------------------- events

    def _on_status(self, ev: PulseStatusEvent) -> None:
        self.start_btn.setEnabled(not ev.running)
        self.stop_btn.setEnabled(ev.running)
        current = [s.symbol for s in ev.snapshots]
        stale = [sym for sym in self.cards if sym not in current]
        if stale:  # the market changed: rebuild the grid without the old coins
            for sym in stale:
                w = self.cards.pop(sym)
                self.cards_box.removeWidget(w)
                w.deleteLater()
            for i, w in enumerate(self.cards.values()):
                self.cards_box.removeWidget(w)
                self.cards_box.addWidget(w, i // 2, i % 2)
        for s in ev.snapshots:
            if s.symbol not in self.cards:
                card = _SymbolCard(s.symbol)
                self.cards[s.symbol] = card
                n = len(self.cards) - 1
                self.cards_box.addWidget(card, n // 2, n % 2)
                if self.f_symbol.findData(s.symbol) < 0:
                    self.f_symbol.addItem(s.symbol, s.symbol)
            self.cards[s.symbol].update(s)
        for m in ev.modes:
            if m.key not in self.mode_cards:
                continue
            self.mode_cards[m.key].update(m)
        conn = "connected" if ev.connected else "DISCONNECTED"
        self.health_line.setText(f"Exchange {conn} · {ev.health_summary}" if ev.running else "Not running.")
        self.health_line.setStyleSheet(f"color: {GREEN if ev.connected and 'INSUFFICIENT' not in ev.health_summary else AMBER if ev.connected else RED};")
        mem = ev.memory or {}
        self.s_memory.setText(
            f"{mem.get('events', 0)} signals recorded · {mem.get('pending', 0)} being tracked · {mem.get('resolved', 0)} resolved · "
            f"{mem.get('effective_long', 0)} independent long setups (need {mem.get('min_similar', 30)} similar ones before any estimate) · "
            f"long hit rate so far {mem.get('hit_rate_long', 0.0):.0%}"
        )
        mli = ev.ml or {}
        self.s_ml.setText((f"backend {mli.get('backend')} · " if mli else "") + ("model in use" if mli.get("usable") else "no validated model") + (f" · {mli.get('note')}" if mli.get("note") else f" · needs {mli.get('min_events', 300)} resolved signals"))
        self.s_ml.setStyleSheet(f"color: {GREEN if mli.get('usable') else MUTED};")
        self.train_btn.setEnabled(ev.running)
        msg = ev.message or ("Running." if ev.running else "Not running.")
        self.s_msg.setText(msg)

    def _row_values(self, e: dict[str, Any]) -> list[str]:
        mm = f"{e['mfe_pct']:+.2f}% / {e['mae_pct']:+.2f}%" if e.get("mfe_pct") is not None and e.get("mae_pct") is not None else ""
        ev_txt = ("sufficient" if e.get("evidence") == "sufficient" else "insufficient") if e.get("evidence") else ""
        decs = e.get("_decisions") or ""
        return [_ts(e["ts_ms"]), e["symbol"], e["direction"], f"{e['prs']:.0f}" if e.get("prs") else "", e.get("regime") or "", e.get("status") or "", e.get("outcome") or "",
                _pct(e.get("net_pct"), 2), mm, ev_txt, decs]

    def _insert_row(self, e: dict[str, Any], at_top: bool) -> None:
        r = 0 if at_top else self.events.rowCount()
        self.events.insertRow(r)
        for j, v in enumerate(self._row_values(e)):
            it = QTableWidgetItem(v)
            if j == 0:
                it.setData(Qt.ItemDataRole.UserRole, int(e["id"]))
            if j == 6:
                it.setForeground(Qt.GlobalColor.green if v == "target" else Qt.GlobalColor.red if v == "stop" else Qt.GlobalColor.gray)
            if j == 7 and e.get("net_pct") is not None:
                it.setForeground(Qt.GlobalColor.green if e["net_pct"] > 0 else Qt.GlobalColor.red)
            self.events.setItem(r, j, it)
        while self.events.rowCount() > 500:
            self.events.removeRow(self.events.rowCount() - 1)

    def _on_event(self, ev: PulseEventRecord) -> None:
        if ev.status:
            decs = " ".join(f"{k}:{'✓' if ok else '✗'}" for k, (ok, _r) in sorted(ev.decisions.items()))
            e = {"id": ev.id, "ts_ms": ev.ts_ms, "symbol": ev.symbol, "direction": ev.direction, "prs": ev.prs, "regime": ev.regime, "status": ev.status, "outcome": ev.outcome,
                 "net_pct": ev.net_pct, "mfe_pct": ev.mfe_pct, "mae_pct": ev.mae_pct, "evidence": "sufficient" if ev.similar_n else "insufficient", "_decisions": decs}
            self._insert_row(e, at_top=True)
            return
        for r in range(self.events.rowCount()):
            it = self.events.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == ev.id:
                self.events.item(r, 6).setText(ev.outcome)
                self.events.item(r, 6).setForeground(Qt.GlobalColor.green if ev.outcome == "target" else Qt.GlobalColor.red if ev.outcome == "stop" else Qt.GlobalColor.gray)
                self.events.item(r, 7).setText(_pct(ev.net_pct, 2))
                if ev.net_pct is not None:
                    self.events.item(r, 7).setForeground(Qt.GlobalColor.green if ev.net_pct > 0 else Qt.GlobalColor.red)
                if ev.mfe_pct is not None and ev.mae_pct is not None:
                    self.events.item(r, 8).setText(f"{ev.mfe_pct:+.2f}% / {ev.mae_pct:+.2f}%")
                break

    def _load_events(self) -> None:
        self.events.setRowCount(0)
        rows = self.store.load_pulse_events(limit=500, symbol=self.f_symbol.currentData(), direction=self.f_dir.currentData())
        want = self.f_outcome.currentData()
        if want:
            rows = [e for e in rows if e.get("outcome") == want]
        decs: dict[int, list[str]] = {}
        for d in self.store.load_decisions(limit=5000):
            decs.setdefault(d["event_id"], []).append(f"{d['mode']}:{'✓' if d['accepted'] else '✗'}")
        for e in rows:
            e["_decisions"] = " ".join(sorted(decs.get(e["id"], [])))
            self._insert_row(e, at_top=False)
        self.mem_count.setText(f"{len(rows)} signals shown")
        edge = analysis.indicator_edge(rows)
        if edge["resolved"]:
            self.mem_edge.setText(f"Indicator edge over {edge['resolved']} resolved long signals ({edge['hits']} hit the target first): "
                                  + ", ".join(f"{r['name']} {r['auc']:.2f}" for r in edge["rows"]) + (f" · {edge['note']}" if edge["note"] else ""))
        else:
            self.mem_edge.setText("Indicator edge: no resolved long signals yet.")

    def _show_event(self) -> None:
        items = self.events.selectedItems()
        if not items:
            return
        eid = self.events.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        e = self.store.pulse_event(int(eid)) if eid is not None else None
        if not e:
            return
        self.detail.setPlainText(self.explain(e, self.store.load_decisions(event_id=e["id"])))

    @staticmethod
    def explain(e: dict[str, Any], decisions: list[dict[str, Any]]) -> str:
        lines = [f"Signal #{e['id']}  {_ts(e['ts_ms'])}  {e['symbol']}  {e['direction']}  ({e.get('status')})", ""]
        lines.append(f"Market: regime {e.get('regime')}, mid {e['mid']:,.4f}, bid/ask {e.get('bid', 0):,.4f}/{e.get('ask', 0):,.4f}, spread {e['spread_pct']:.4f}%, "
                     f"1-minute range {e['vol_pct']:.3f}%, 60 s imbalance {e['imbalance']:+.2f}, data health {e.get('health', 'ok')}, breakout confirmed: {'yes' if e.get('confirmed') else 'no'}")
        lines.append("")
        lines.append(f"Pressure Release Score {e['prs']:.1f}")
        for k in INDICATORS:
            lines.append(f"  {INDICATOR_NAMES[k]:<32} {float(e.get(k) or 0):6.1f}")
        try:
            feats = json.loads(e.get("features_json") or "{}")
            raw = feats.get("raw_long") or {}
            if raw:
                lines.append("  raw values: " + ", ".join(f"{k}={v:.4g}" for k, v in raw.items()))
        except Exception:  # noqa: BLE001
            pass
        lines.append("")
        lines.append(f"Plan: expected move {e.get('expected_move_pct', 0):.3f}% → target {e['target_pct']:.3f}%, stop {e['stop_pct']:.3f}%")
        lines.append(f"Costs (round trip): fees {e.get('fee_pct', 0):.3f}% + spread {e.get('spread_cost_pct', 0):.3f}% + slippage {e.get('slip_pct', 0):.3f}% = {e['cost_pct']:.3f}%")
        if e.get("evidence") == "sufficient":
            lines.append(f"Evidence: p(target first) {e['similar_p']:.0%} [{e.get('ci_low', 0):.0%}–{e.get('ci_high', 0):.0%}] from {e.get('similar_eff', 0)} independent similar setups "
                         f"({e.get('similar_n', 0)} raw); p(stop) {e.get('p_stop', 0):.0%}; EV {e['ev_pct']:+.3f}% per trade; R/R after costs {e.get('rr_after_costs', 0):.2f}")
        else:
            lines.append(f"Evidence: INSUFFICIENT HISTORICAL EVIDENCE ({e.get('similar_eff', 0)} independent similar setups)")
        lines.append(f"Reference verdict: {e.get('reason', '')}")
        lines.append("")
        lines.append("Decisions:")
        if decisions:
            for d in sorted(decisions, key=lambda d: d["mode"]):
                pnl = f"  → paper P&L {d['trade_pnl']:+.2f}" if d.get("trade_pnl") is not None else ""
                lines.append(f"  Mode {d['mode']}: {'TAKEN' if d['accepted'] else 'rejected'} — {analysis.REASON_TEXT.get(d.get('code', ''), d.get('code', ''))}: {d.get('reason', '')}{pnl}")
        else:
            lines.append("  none (research-only setup)")
        lines.append("")
        lines.append(f"What happened next: outcome {e.get('outcome')}" + (f", {e['first_hit']} hit first after {(e['first_hit_ms'] - e['ts_ms']) / 1000:.0f} s" if e.get("first_hit") and e.get("first_hit_ms") else ""))
        if e.get("mfe_pct") is not None:
            lines.append(f"  best {e['mfe_pct']:+.3f}% after {(e.get('time_to_mfe_ms') or 0) / 1000:.0f} s · worst {e['mae_pct']:+.3f}% after {(e.get('time_to_mae_ms') or 0) / 1000:.0f} s")
        rets = [(name, e.get(f"ret_{name}")) for name in OUTCOME_HORIZONS_MS]
        if any(v is not None for _n, v in rets):
            lines.append("  " + " · ".join(f"{n}: {_pct(v, 3)}" for n, v in rets))
        if e.get("net_pct") is not None:
            lines.append(f"  hypothetical net return after costs: {e['net_pct']:+.3f}%")
        return "\n".join(lines)

    # ------------------------------------------------------------ analysis

    def _on_tab(self, idx: int) -> None:
        if idx in (1, 2):
            self.refresh_analysis()
        elif idx == 3:
            self._load_events()

    def refresh_analysis(self) -> None:
        rows, colors = [], {}
        for i, m in enumerate(self.cfg.modes):
            mode = f"PULSE-{m.key}"
            trades = self.store.load_trades(mode, limit=10_000)
            eq = self.store.load_equity(mode, limit=20_000)
            s = analysis.mode_stats(trades, eq, m.budget)
            pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
            rows.append([f"{m.key} · {m.label}", f"${(eq[-1][1] if eq else m.budget):,.2f}", f"{s['return_pct']:+.2f}%", s["trades"], f"{s['win_rate']:.0%}", pf, f"{s['expectancy']:+.3f}",
                         f"{s['avg_win']:.2f}", f"{s['avg_loss']:.2f}", f"{s['max_drawdown_pct']:.2f}%", f"{s['profitable_days_pct']:.0f}% of {s['trading_days']}", f"{s['fees']:.2f}"])
            colors[(i, 2)] = "good" if s["return_pct"] > 0 else "bad" if s["return_pct"] < 0 else "n"
            if eq:
                self.eq_curves[m.key].setData([t / 1000 for t, _e in eq], [e for _t, e in eq])
        _fill(self.perf_table, rows, colors)
        events = self.store.load_pulse_events(limit=20_000, direction="long")

        def fmt(g: list[dict], key: str) -> list[list[str]]:
            return [[r[key], r["signals"], r["resolved"], f"{r['hit_rate']:.0%}" if r["hit_rate"] is not None else "—", _pct(r["avg_net_pct"]), _pct(r["sum_net_pct"], 2),
                     _pct(r["avg_mfe_pct"], 2), _pct(r["avg_mae_pct"], 2)] for r in g]

        _fill(self.coin_table, fmt(analysis.group_outcomes(events, "symbol"), "symbol"))
        _fill(self.regime_table, fmt(analysis.group_outcomes(events, "regime"), "regime"))
        by_id = {e["id"]: e for e in events}
        rej = analysis.rejected_analysis(self.store.load_decisions(limit=50_000), by_id)
        _fill(self.rej_table, [[r["mode"], r["reason"], r["count"], r["resolved"], f"{r['hit_rate']:.0%}" if r["hit_rate"] is not None else "—", r["profitable"], _pct(r["avg_net_pct"]), _pct(r["sum_net_pct"], 2)] for r in rej])
