"""Strategy screen: configure, run an honest backtest, read the verdict."""
from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cryptobot.backtest.engine import Costs
from cryptobot.backtest.report import BacktestReport, run_backtest
from cryptobot.config import DEFAULT_EXCHANGE, EXCHANGES
from cryptobot.data.history import DEFAULT_HISTORY_SOURCE, HISTORY_SOURCES, HistoryDownloader
from cryptobot.db.store import Store
from cryptobot.gui.theme import AMBER, BLUE, BORDER, GREEN, GREY, MUTED, RED, TEXT
from cryptobot.gui.widgets.param_row import ParamRow
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import RiskRules, StrategyConfig, StrategySpec
from cryptobot.strategy.library import STRATEGIES

log = logging.getLogger(__name__)

# Typical taker fees per side at the lowest volume tier. The live engine reads real fees from the exchange.
DEFAULT_FEES_PCT = {"kraken": 0.40, "coinbase": 0.60, "binanceus": 0.60, "binance": 0.10}
TIMEFRAMES = [("1 day", 86400), ("4 hours", 14400), ("1 hour", 3600)]


class _BacktestWorker(QThread):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, store_path, source, symbol, tf_s, years, config, budget, costs) -> None:
        super().__init__()
        self.args = (store_path, source, symbol, tf_s, years, config, budget, costs)

    def run(self) -> None:
        store_path, source, symbol, tf_s, years, config, budget, costs = self.args
        try:
            store = Store(store_path)
            bars, note = HistoryDownloader(store, self.progress.emit).download(source, symbol, tf_s, years)
            if len(bars) < 60:
                raise ValueError(f"Only {len(bars)} bars available; need at least 60.")
            self.progress.emit(f"Backtesting on {len(bars)} bars…")
            from cryptobot.engine.risk import RiskLimits
            raw_limits = store.get_setting("risk.limits")
            limits = RiskLimits.model_validate(raw_limits).model_copy(update={"budget": budget}) if raw_limits else RiskLimits(budget=budget)
            rep = run_backtest(BarArrays.from_bars(bars), config, budget, costs, limits=limits)
            if note:
                from cryptobot.backtest.report import Warning_

                rep.warnings.insert(0, Warning_("warn", note))
            self.done.emit(rep)
        except Exception as e:  # noqa: BLE001
            log.exception("backtest failed")
            self.failed.emit(f"{type(e).__name__}: {e}")


class _StrategyBlock(QWidget):
    """Strategy picker + its parameter rows."""

    def __init__(self, title: str, allow_none: bool) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel(title))
        self.combo = QComboBox()
        if allow_none:
            self.combo.addItem("(none)", "")
        for sid, cls in STRATEGIES.items():
            self.combo.addItem(cls.name, sid)
        row.addWidget(self.combo, 1)
        lay.addLayout(row)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setObjectName("muted")
        lay.addWidget(self.desc)
        self.params_box = QVBoxLayout()
        self.params_box.setSpacing(4)
        lay.addLayout(self.params_box)
        self.rows: dict[str, ParamRow] = {}
        self.combo.currentIndexChanged.connect(lambda _i: self._rebuild())
        self._rebuild()

    def _rebuild(self, params: dict[str, float] | None = None) -> None:
        while self.params_box.count():
            item = self.params_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows = {}
        sid = self.combo.currentData()
        if not sid:
            self.desc.setText("")
            return
        cls = STRATEGIES[sid]
        self.desc.setText(cls.description)
        merged = cls.merge(params)
        for p in cls.params:
            r = ParamRow(p, merged[p.name])
            self.rows[p.name] = r
            self.params_box.addWidget(r)

    def spec(self) -> StrategySpec | None:
        sid = self.combo.currentData()
        if not sid:
            return None
        return StrategySpec(id=sid, params={k: r.value() for k, r in self.rows.items()})

    def set_spec(self, spec: StrategySpec | None) -> None:
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(self.combo.findData(spec.id if spec else ""))
        self.combo.blockSignals(False)
        self._rebuild(spec.params if spec else None)


class StrategyPage(QWidget):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.worker: _BacktestWorker | None = None
        self.report: BacktestReport | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # ---------------- left: configuration ----------------
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(420)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(10)
        h = QLabel("Strategy")
        h.setObjectName("h1")
        ll.addWidget(h)

        self.primary = _StrategyBlock("Strategy", allow_none=False)
        ll.addWidget(self.primary)
        self.secondary = _StrategyBlock("Blend with", allow_none=True)
        self.secondary.setToolTip("Optional second strategy. The bot holds the average of the two.")
        ll.addWidget(self.secondary)

        mods = QGroupBox("Modifiers (recommended)")
        mf = QFormLayout(mods)
        self.gate = QCheckBox("Only trade in the right regime (ADX gate)")
        self.gate.setToolTip("Trend strategies trade only when ADX says the market is trending; mean-reversion only when it is not.")
        self.gate.setChecked(True)
        self.adx_thr = QDoubleSpinBox()
        self.adx_thr.setRange(5, 60)
        self.adx_thr.setValue(25)
        self.adx_thr.setToolTip("ADX level that separates 'trending' from 'ranging'. 20–30 is conventional.")
        mf.addRow(self.gate)
        mf.addRow("ADX threshold", self.adx_thr)
        self.vt = QCheckBox("Scale down in high volatility (vol targeting)")
        self.vt.setToolTip("Holds less when recent volatility is above the target, so one wild week cannot blow the budget.")
        self.vt.setChecked(True)
        self.vt_target = QDoubleSpinBox()
        self.vt_target.setRange(5, 200)
        self.vt_target.setSuffix("% / yr")
        self.vt_target.setValue(40)
        self.vt_target.setToolTip("Target annualised volatility. Bitcoin typically runs 50–80%, so 40% means holding roughly half to two-thirds in calm times and less in storms.")
        mf.addRow(self.vt)
        mf.addRow("Target volatility", self.vt_target)
        ll.addWidget(mods)

        prot = QGroupBox("Position protection")
        pf = QFormLayout(prot)
        self.stop_atr = QDoubleSpinBox()
        self.stop_atr.setRange(0, 10)
        self.stop_atr.setSingleStep(0.5)
        self.stop_atr.setValue(2.0)
        self.stop_atr.setSuffix(" × ATR")
        self.stop_atr.setToolTip("Stop-loss distance below entry, in multiples of the Average True Range. 0 disables (not recommended).")
        pf.addRow("Stop-loss", self.stop_atr)
        self.trail = QDoubleSpinBox()
        self.trail.setRange(0, 50)
        self.trail.setSuffix("%")
        self.trail.setValue(0)
        self.trail.setToolTip("Trailing stop below the highest price since entry. 0 disables.")
        pf.addRow("Trailing stop", self.trail)
        self.tp = QDoubleSpinBox()
        self.tp.setRange(0, 500)
        self.tp.setSuffix("%")
        self.tp.setValue(0)
        self.tp.setToolTip("Take profit at this gain above entry. 0 disables. Caps upside; usually hurts trend strategies.")
        pf.addRow("Take-profit", self.tp)
        ll.addWidget(prot)

        costs = QGroupBox("Costs and budget")
        cf = QFormLayout(costs)
        self.fee = QDoubleSpinBox()
        self.fee.setRange(0, 3)
        self.fee.setDecimals(3)
        self.fee.setSingleStep(0.05)
        self.fee.setSuffix("% per side")
        self.fee.setToolTip("Exchange fee on every buy and every sell. Check your exchange's fee page; the live engine reads the real figure.")
        cf.addRow("Fee", self.fee)
        self.slip = QDoubleSpinBox()
        self.slip.setRange(0, 2)
        self.slip.setDecimals(3)
        self.slip.setSingleStep(0.01)
        self.slip.setValue(0.05)
        self.slip.setSuffix("%")
        self.slip.setToolTip("Assumed price slippage on every fill, against you. 0.05% is reasonable for BTC on a major exchange; more for thin coins.")
        cf.addRow("Slippage", self.slip)
        self.budget = QDoubleSpinBox()
        self.budget.setRange(10, 1_000_000)
        self.budget.setPrefix("$")
        self.budget.setValue(100)
        self.budget.setToolTip("Hard cap on money used. Profits above it are kept in cash, not reinvested.")
        cf.addRow("Budget", self.budget)
        ll.addWidget(costs)

        data = QGroupBox("Historical data")
        df = QFormLayout(data)
        self.source = QComboBox()
        for k, v in HISTORY_SOURCES.items():
            self.source.addItem(v, k)
        self.source.setToolTip("Where to download past candles. Prices differ by a hair between exchanges; depth of history differs a lot.")
        df.addRow("Source", self.source)
        self.symbol = QLineEdit("BTC/USD")
        self.symbol.setToolTip("Pair to test. USDT pairs are mapped to USD automatically on Coinbase/Kraken.")
        df.addRow("Pair", self.symbol)
        self.years = QDoubleSpinBox()
        self.years.setRange(0.5, 10)
        self.years.setSingleStep(0.5)
        self.years.setValue(5)
        self.years.setSuffix(" years")
        self.years.setToolTip("How far back to test. Include at least one full bull and bear cycle.")
        df.addRow("History", self.years)
        self.tf = QComboBox()
        for lab, s in TIMEFRAMES:
            self.tf.addItem(lab, s)
        self.tf.setToolTip("Bar size the strategy decides on. Daily is recommended: fewer trades, fewer fees, and the only timeframe with real evidence.")
        df.addRow("Timeframe", self.tf)
        ll.addWidget(data)

        self.run_btn = QPushButton("Run Backtest")
        self.run_btn.setStyleSheet(f"background: {BLUE}; color: white; font-weight: 600; padding: 10px;")
        self.run_btn.clicked.connect(self.run)
        ll.addWidget(self.run_btn)
        self.progress = QLabel("")
        self.progress.setObjectName("muted")
        self.progress.setWordWrap(True)
        ll.addWidget(self.progress)
        self.saved_note = QLabel("")
        self.saved_note.setObjectName("muted")
        self.saved_note.setWordWrap(True)
        ll.addWidget(self.saved_note)
        ll.addStretch(1)
        left_scroll.setWidget(left)
        root.addWidget(left_scroll)

        # ---------------- right: results ----------------
        right = QVBoxLayout()
        right.setSpacing(10)
        self.headline = QLabel("Run a backtest to see results here.")
        self.headline.setObjectName("h1")
        self.headline.setWordWrap(True)
        right.addWidget(self.headline)
        self.sub = QLabel("")
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        right.addWidget(self.sub)

        self.verdicts = QVBoxLayout()
        self.verdicts.setSpacing(4)
        vframe = QFrame()
        vframe.setObjectName("card")
        vl = QVBoxLayout(vframe)
        vl.setContentsMargins(12, 10, 12, 10)
        vl.addLayout(self.verdicts)
        right.addWidget(vframe)

        chart_card = QFrame()
        chart_card.setObjectName("card")
        cl = QVBoxLayout(chart_card)
        cl.setContentsMargins(8, 8, 8, 8)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.showGrid(x=True, y=True, alpha=0.08)
        self.plot.addLegend(offset=(10, 10))
        self.plot.setLabel("left", "Equity ($)")
        self.c_strat = self.plot.plot([], [], pen=pg.mkPen(BLUE, width=2), name="Strategy")
        self.c_bh = self.plot.plot([], [], pen=pg.mkPen(GREY, width=1.5), name="Buy & hold")
        self.c_dca = self.plot.plot([], [], pen=pg.mkPen(AMBER, width=1.5), name="Weekly DCA")
        self.c_oos = self.plot.plot([], [], pen=pg.mkPen(GREEN, width=1.5, style=Qt.PenStyle.DashLine), name="Walk-forward (params chosen on earlier data only)")
        cl.addWidget(self.plot)
        right.addWidget(chart_card, 1)

        tables = QHBoxLayout()
        self.metrics_table = QTableWidget(0, 3)
        self.metrics_table.setHorizontalHeaderLabels(["Strategy", "Buy & hold", "DCA"])
        self.metrics_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.metrics_table.horizontalHeader().setMinimumSectionSize(64)
        self.metrics_table.verticalHeader().setDefaultSectionSize(22)
        self.metrics_table.verticalHeader().setMinimumWidth(140)
        self.metrics_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tables.addWidget(self.metrics_table, 1)
        self.wf_table = QTableWidget(0, 5)
        self.wf_table.setHorizontalHeaderLabels(["Tested on", "Best params", "In-sample", "Out-of-sample", "Yours, OOS"])
        self.wf_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.wf_table.horizontalHeader().setMinimumSectionSize(68)
        self.wf_table.verticalHeader().setVisible(False)
        self.wf_table.verticalHeader().setDefaultSectionSize(22)
        self.wf_table.setWordWrap(False)
        self.wf_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.wf_table.setToolTip("Walk-forward: parameters chosen on earlier data, then tested on data they never saw.")
        tables.addWidget(self.wf_table, 1)
        right.addLayout(tables, 1)
        root.addLayout(right, 1)

        self._load_saved()

    # ---------------- config <-> widgets ----------------

    def config(self) -> StrategyConfig:
        specs = [self.primary.spec()]
        sec = self.secondary.spec()
        if sec is not None:
            specs.append(sec)
        return StrategyConfig(
            strategies=specs,
            timeframe_s=self.tf.currentData(),
            regime_gate=self.gate.isChecked(),
            adx_threshold=self.adx_thr.value(),
            vol_target=self.vt.isChecked(),
            target_annual_vol=self.vt_target.value() / 100.0,
            risk=RiskRules(stop_atr_mult=self.stop_atr.value(), trailing_pct=self.trail.value(), take_profit_pct=self.tp.value()),
        )

    def apply_config(self, cfg: StrategyConfig) -> None:
        self.primary.set_spec(cfg.strategies[0])
        self.secondary.set_spec(cfg.strategies[1] if len(cfg.strategies) > 1 else None)
        self.tf.setCurrentIndex(max(0, self.tf.findData(cfg.timeframe_s)))
        self.gate.setChecked(cfg.regime_gate)
        self.adx_thr.setValue(cfg.adx_threshold)
        self.vt.setChecked(cfg.vol_target)
        self.vt_target.setValue(cfg.target_annual_vol * 100)
        self.stop_atr.setValue(cfg.risk.stop_atr_mult)
        self.trail.setValue(cfg.risk.trailing_pct)
        self.tp.setValue(cfg.risk.take_profit_pct)

    def _load_saved(self) -> None:
        raw = self.store.get_setting("strategy.config")
        if raw:
            try:
                self.apply_config(StrategyConfig.model_validate(raw))
            except Exception:  # noqa: BLE001
                log.exception("bad saved strategy config; using defaults")
        bt = self.store.get_setting("backtest.settings", {})
        ex = self.store.get_setting("market.exchange", DEFAULT_EXCHANGE)
        self.fee.setValue(bt.get("fee_pct", DEFAULT_FEES_PCT.get(ex, 0.4)))
        self.slip.setValue(bt.get("slippage_pct", 0.05))
        self.budget.setValue(bt.get("budget", 100))
        self.years.setValue(bt.get("years", 5))
        self.source.setCurrentIndex(max(0, self.source.findData(bt.get("source", DEFAULT_HISTORY_SOURCE))))
        self.symbol.setText(bt.get("symbol", self.store.get_setting("market.symbol", EXCHANGES[ex]["symbols"][0])))

    def _save(self, cfg: StrategyConfig) -> None:
        self.store.set_setting("strategy.config", cfg.to_json())
        self.store.set_setting(
            "backtest.settings",
            {
                "fee_pct": self.fee.value(),
                "slippage_pct": self.slip.value(),
                "budget": self.budget.value(),
                "years": self.years.value(),
                "source": self.source.currentData(),
                "symbol": self.symbol.text().strip().upper(),
            },
        )
        self.saved_note.setText("Saved. This configuration is what TEST / BUY mode will run.")

    # ---------------- run ----------------

    def run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            cfg = self.config()
        except Exception as e:  # noqa: BLE001
            self.progress.setText(f"Invalid configuration: {e}")
            return
        self._save(cfg)
        costs = Costs(self.fee.value(), self.slip.value())
        self.run_btn.setEnabled(False)
        self.progress.setText("Starting…")
        self.worker = _BacktestWorker(
            self.store.path, self.source.currentData(), self.symbol.text().strip().upper(),
            self.tf.currentData(), self.years.value(), cfg, self.budget.value(), costs,
        )
        self.worker.progress.connect(self.progress.setText)
        self.worker.done.connect(self._show)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(lambda: self.run_btn.setEnabled(True))
        self.worker.start()

    def _failed(self, msg: str) -> None:
        self.progress.setStyleSheet(f"color: {RED};")
        self.progress.setText(msg)

    # ---------------- results ----------------

    def _show(self, rep: BacktestReport) -> None:
        self.report = rep
        self.progress.setStyleSheet(f"color: {MUTED};")
        self.progress.setText("Done.")
        m, bh, dc = rep.metrics, rep.bh_metrics, rep.dca_metrics
        b = rep.bars
        t0 = datetime.fromtimestamp(b.ts_ms[rep.start_index] / 1000).strftime("%b %Y")
        t1 = datetime.fromtimestamp(b.ts_ms[-1] / 1000).strftime("%b %Y")
        self.headline.setText(f"{m.total_return * 100:+.1f}%  vs  buy & hold {bh.total_return * 100:+.1f}%")
        self.sub.setText(
            f"{rep.config.describe()} · {b.symbol} · {t0} – {t1} · fee {rep.costs.fee_pct:g}% / side, slippage "
            f"{rep.costs.slippage_pct:g}% · budget ${rep.budget:,.0f}"
        )
        # verdicts
        while self.verdicts.count():
            item = self.verdicts.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for w in rep.warnings:
            color = {"bad": RED, "warn": AMBER, "ok": GREEN}[w.level]
            lab = QLabel(f"<span style='color:{color}'>●</span>  {w.text}")
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            self.verdicts.addWidget(lab)
        # chart
        x = b.ts_ms / 1000.0
        self.c_strat.setData(x, rep.sim.equity, connect="finite")
        self.c_bh.setData(x, rep.bh_equity, connect="finite")
        self.c_dca.setData(x, rep.dca_equity, connect="finite")
        if rep.walk.oos_equity is not None:
            self.c_oos.setData(x, rep.walk.oos_equity, connect="finite")
        else:
            self.c_oos.setData([], [])
        self.plot.autoRange()
        # metrics
        rows = [
            ("Total return", "pct", "total_return"),
            ("Per year (CAGR)", "pct", "cagr"),
            ("Worst drawdown", "dd", "max_drawdown"),
            ("Sharpe ratio", "num", "sharpe"),
            ("Volatility (annual)", "pct0", "volatility"),
            ("Trades (round trips)", "int", "trades"),
            ("Win rate", "pct0", "win_rate"),
            ("Profit factor", "num", "profit_factor"),
            ("Average trade", "pct", "avg_trade_pct"),
            ("Time in market", "pct0", "exposure_time"),
            ("Fees + slippage paid", "usd", "fees_paid"),
            ("Final equity", "usd", "final_equity"),
        ]
        self.metrics_table.setRowCount(len(rows))
        self.metrics_table.setVerticalHeaderLabels([r[0] for r in rows])
        for i, (_label, fmt, key) in enumerate(rows):
            for j, mm in enumerate((m, bh, dc)):
                v = getattr(mm, key)
                if key in ("trades", "win_rate", "profit_factor", "avg_trade_pct", "exposure_time") and j > 0:
                    text = "—"
                else:
                    text = _fmt(v, fmt)
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.metrics_table.setItem(i, j, it)
        # walk-forward
        wf = rep.walk
        self.wf_table.setRowCount(len(wf.folds))
        for i, f in enumerate(wf.folds):
            a = datetime.fromtimestamp(f.oos_start_ms / 1000).strftime("%b %Y")
            z = datetime.fromtimestamp(f.oos_end_ms / 1000).strftime("%b %Y")
            params = " ".join(f"{v:g}" for v in f.best_params.values())
            vals = [f"{a} – {z}", params, _fmt(f.is_return, "pct"), _fmt(f.oos_return, "pct"), _fmt(f.user_oos_return, "pct")]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j >= 2:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.wf_table.setItem(i, j, it)


def _fmt(v: float, fmt: str) -> str:
    if fmt == "pct":
        return f"{v * 100:+.1f}%"
    if fmt == "pct0":
        return f"{v * 100:.0f}%"
    if fmt == "dd":
        return f"-{v * 100:.0f}%"
    if fmt == "usd":
        return f"${v:,.2f}"
    if fmt == "int":
        return f"{int(v)}"
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"

