"""Early Warning screen: live accumulation candidates, historical event explorer, paper book, research."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cryptobot.config import AppPaths
from cryptobot.core.events import EWCandidateView, EWStatusEvent
from cryptobot.db.store import Store
from cryptobot.earlywarning.config import COMPONENT_NAMES, COMPONENTS, EWConfig
from cryptobot.earlywarning.report import build_report
from cryptobot.earlywarning.store import EWStore
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import AMBER, BLUE, GREEN, GREY, MUTED, RED

STATE_COLOR = {"NO DATA": GREY, "QUIET": MUTED, "POSSIBLE ACCUMULATION": AMBER, "ACCUMULATION CONFIRMED": GREEN, "BREAKOUT DEVELOPING": BLUE, "SURGE ACTIVE": BLUE, "POSSIBLE DISTRIBUTION": RED, "REVERSAL WARNING": RED}


def _ts(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d %H:%M") if ms else "—"


def _pct(v: float | None, d: int = 2) -> str:
    return "—" if v is None else f"{v:+.{d}f}%"


def _num(v: float | None, d: int = 2) -> str:
    return "—" if v is None else f"{v:.{d}f}"


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    return t


def _fill(t: QTableWidget, rows: list[list[Any]], color_col: int | None = None, color_fn=None) -> None:
    t.setRowCount(0)
    for r, vals in enumerate(rows):
        t.insertRow(r)
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            if color_col is not None and c == color_col and color_fn is not None:
                col = color_fn(v)
                if col:
                    it.setForeground(col)
            t.setItem(r, c, it)


class _ResearchRunner(QObject):
    line = Signal(str)
    done = Signal(bool)

    def __init__(self, db_path, demo: bool, days: int) -> None:
        super().__init__()
        self.db_path, self.demo, self.days = db_path, demo, days

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            from cryptobot.earlywarning.cli import research

            store = EWStore(self.db_path)
            cfg = EWConfig.model_validate(store.get("config") or {})
            cfg.days = self.days
            research(store, cfg, demo=self.demo, progress=self.line.emit)
            text = build_report(store, cfg, demo=self.demo)
            self.db_path.with_name("earlywarning-report.md").write_text(text)
            self.line.emit("report written")
            self.done.emit(True)
        except Exception as e:  # noqa: BLE001
            self.line.emit(f"research failed: {e}")
            self.done.emit(False)


class EarlyWarningPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store, paths: AppPaths | None = None) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.main_store = store
        self.db_path = (paths or AppPaths.default()).data_dir / "earlywarning.db"
        self.ew = EWStore(self.db_path)
        self.cfg = EWConfig.model_validate(self.ew.get("config") or {})
        self._views: dict[str, EWCandidateView] = {}
        self._runner: _ResearchRunner | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)
        head = QHBoxLayout()
        h = QLabel("Early Warning — accumulation & surge detection (paper only)")
        h.setObjectName("h1")
        head.addWidget(h)
        head.addStretch(1)
        self.start_btn = QPushButton("▶  Start scanner (paper)")
        self.start_btn.setStyleSheet(f"background: {GREEN}; color: #06210f; font-weight: 700;")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        head.addWidget(self.start_btn)
        head.addWidget(self.stop_btn)
        root.addLayout(head)
        self.status_line = QLabel("Not running.")
        self.status_line.setWordWrap(True)
        root.addWidget(self.status_line)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live(), "Live candidates")
        self.tabs.addTab(self._build_explorer(), "Event explorer")
        self.tabs.addTab(self._build_paper(), "Paper trading")
        self.tabs.addTab(self._build_research(), "Research")
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)
        ctrl.ew_status.connect(self._on_status)
        self._refresh_research_labels()

    # ----------------------------------------------------------- builders

    def _build_live(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        banner = QLabel("Every eligible Binance.US spot market is screened from 24-hour tickers; the strongest candidates get live trade and order-book streams. "
                        "States are model labels, not facts about anyone's intentions. Scores need valid, fresh data and enough history; otherwise INSUFFICIENT DATA. "
                        "Surge probabilities and targets appear only when the research run validated them out of sample.")
        banner.setWordWrap(True)
        banner.setObjectName("muted")
        lay.addWidget(banner)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.cand = _table(["Coin", "State", "EAS", "Price", "5m", "15m", "30m", "60m", "Rel. volume", "Buy/sell", "Trade freq.", "Similar past", "Surge prob.", "Target (net)", "Est. net", "Warnings"])
        self.cand.itemSelectionChanged.connect(self._show_candidate)
        split.addWidget(self.cand)
        self.cand_detail = QPlainTextEdit()
        self.cand_detail.setReadOnly(True)
        self.cand_detail.setPlaceholderText("Select a candidate for its component scores, scanner reasons and liquidity.")
        split.addWidget(self.cand_detail)
        split.setSizes([900, 380])
        lay.addWidget(split, 1)
        self.scan_line = QLabel("")
        self.scan_line.setObjectName("muted")
        self.scan_line.setWordWrap(True)
        lay.addWidget(self.scan_line)
        self.trans = _table(["Time", "Coin", "From", "To", "EAS", "Note"])
        self.trans.setMaximumHeight(160)
        lay.addWidget(self.trans)
        return w

    def _build_explorer(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        top = QHBoxLayout()
        self.ex_symbol = QComboBox()
        self.ex_symbol.currentIndexChanged.connect(self._load_events)
        top.addWidget(QLabel("Coin"))
        top.addWidget(self.ex_symbol)
        top.addStretch(1)
        self.ex_count = QLabel("")
        self.ex_count.setObjectName("muted")
        top.addWidget(self.ex_count)
        lay.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        self.ev_table = _table(["Start (local)", "Pre price", "Peak", "Peak bid ≈", "Gain", "Min to peak", "Min to reversal", "Decline after", "Volume", "Earliest warning", "Would enter (A)", "Hypothetical (A/partial)"])
        self.ev_table.itemSelectionChanged.connect(self._show_event)
        split.addWidget(self.ev_table)
        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.ev_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.ev_plot.showGrid(x=True, y=True, alpha=0.08)
        self.ev_plot.setLabel("left", "price")
        self.ev_price = self.ev_plot.plot([], [], pen=pg.mkPen(BLUE, width=1.5))
        self.ev_vb2 = pg.ViewBox()
        self.ev_plot.scene().addItem(self.ev_vb2)
        self.ev_plot.getAxis("right").linkToView(self.ev_vb2)
        self.ev_plot.showAxis("right")
        self.ev_plot.setLabel("right", "EAS (0-100) / rel. volume x10")
        self.ev_vb2.setXLink(self.ev_plot)
        self.ev_eas = pg.PlotDataItem([], [], pen=pg.mkPen(GREEN, width=2), symbol="o", symbolSize=5, symbolBrush=GREEN)
        self.ev_vol = pg.PlotDataItem([], [], pen=pg.mkPen(AMBER, width=1, style=Qt.PenStyle.DashLine))
        self.ev_vb2.addItem(self.ev_eas)
        self.ev_vb2.addItem(self.ev_vol)
        self.ev_plot.getViewBox().sigResized.connect(lambda: self.ev_vb2.setGeometry(self.ev_plot.getViewBox().sceneBoundingRect()))
        self.ev_lines: list[pg.InfiniteLine] = []
        bl.addWidget(self.ev_plot, 3)
        self.ev_detail = QPlainTextEdit()
        self.ev_detail.setReadOnly(True)
        bl.addWidget(self.ev_detail, 2)
        split.addWidget(bottom)
        split.setSizes([260, 420])
        lay.addWidget(split, 1)
        return w

    def _build_paper(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        f = QFormLayout(card)
        self.p_equity = QLabel("—")
        self.p_today = QLabel("—")
        self.p_trades = QLabel("—")
        self.p_limits = QLabel("—")
        self.p_method = QLabel("—")
        self.p_rej = QLabel("—")
        self.p_rej.setWordWrap(True)
        for lab, wdg in (("Equity", self.p_equity), ("Today (realized / open)", self.p_today), ("Trades / win rate / P&L / fees", self.p_trades), ("Limits", self.p_limits), ("Entry / exit method", self.p_method), ("Why no trade (counts)", self.p_rej)):
            f.addRow(lab, wdg)
        lay.addWidget(card)
        self.p_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.p_plot.showGrid(x=True, y=True, alpha=0.08)
        self.p_plot.setLabel("left", "Paper equity ($)")
        self.p_curve = self.p_plot.plot([], [], pen=pg.mkPen(GREEN, width=2))
        lay.addWidget(self.p_plot, 1)
        self.p_table = _table(["Time", "Coin", "Side", "Price", "Fill", "Amount", "Fee", "P&L", "Reason"])
        lay.addWidget(self.p_table, 1)
        return w

    def _build_research(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        top = QHBoxLayout()
        self.r_days = QSpinBox()
        self.r_days.setRange(7, 365)
        self.r_days.setValue(self.cfg.days)
        self.r_days.setSuffix(" days")
        top.addWidget(QLabel("History"))
        top.addWidget(self.r_days)
        self.r_run = QPushButton("Run research on Binance.US (MXC, POND, FLUX, COMP)")
        self.r_run.setToolTip("Downloads candles and aggregated trades (resumable), finds surges, studies pre-surge patterns against ordinary periods, validates targets and methods out of sample, and writes the report. Can take a while.")
        self.r_run.clicked.connect(lambda: self._run_research(False))
        self.r_demo = QPushButton("Run on synthetic demo data")
        self.r_demo.setToolTip("No network. Generated data to exercise the pipeline and the screens; says nothing about real markets.")
        self.r_demo.clicked.connect(lambda: self._run_research(True))
        top.addWidget(self.r_run)
        top.addWidget(self.r_demo)
        top.addStretch(1)
        lay.addLayout(top)
        self.r_log = QPlainTextEdit()
        self.r_log.setReadOnly(True)
        self.r_log.setMaximumHeight(120)
        lay.addWidget(self.r_log)
        self.r_report = QPlainTextEdit()
        self.r_report.setReadOnly(True)
        lay.addWidget(self.r_report, 1)
        return w

    # ----------------------------------------------------------- commands

    def start(self) -> None:
        self.cfg.days = self.r_days.value()
        self.ew.set("config", self.cfg.model_dump())
        self.ctrl.ew_start(self.cfg.model_dump(), "binanceus")
        self.main_store.set_setting("earlywarning.running", True)
        self.status_line.setText("Starting scanner…")

    def stop(self) -> None:
        self.ctrl.ew_stop()
        self.main_store.set_setting("earlywarning.running", False)

    def _run_research(self, demo: bool) -> None:
        if self._runner is not None:
            return
        self.r_log.clear()
        self.r_run.setEnabled(False)
        self.r_demo.setEnabled(False)
        self._runner = _ResearchRunner(self.db_path, demo, self.r_days.value())
        self._runner.line.connect(self.r_log.appendPlainText)
        self._runner.done.connect(self._research_done)
        self._runner.start()

    def _research_done(self, ok: bool) -> None:
        self._runner = None
        self.r_run.setEnabled(True)
        self.r_demo.setEnabled(True)
        self._refresh_research_labels()
        self._load_symbols()

    def _refresh_research_labels(self) -> None:
        try:
            self.r_report.setPlainText(build_report(self.ew, self.cfg))
        except Exception as e:  # noqa: BLE001
            self.r_report.setPlainText(f"no report yet ({e})")
        self._load_symbols()

    # -------------------------------------------------------------- live

    def _on_status(self, ev: EWStatusEvent) -> None:
        self.start_btn.setEnabled(not ev.running)
        self.stop_btn.setEnabled(ev.running)
        conn = "connected" if ev.connected else "not connected"
        self.status_line.setText(f"{'Running' if ev.running else 'Not running'} · exchange {conn} · {ev.message}")
        self.status_line.setStyleSheet(f"color: {GREEN if ev.running and ev.connected else AMBER if ev.running else MUTED};")
        self._views = {c.symbol: c for c in ev.candidates}
        rows = []
        for c in ev.candidates:
            comps_ok = c.components.get("status") == "ok"
            rows.append([c.symbol, c.state, f"{c.eas:.0f}" if c.eas is not None else "INSUFFICIENT DATA", f"{c.price:.6g}", _pct(c.changes.get(5)), _pct(c.changes.get(15)), _pct(c.changes.get(30)), _pct(c.changes.get(60)),
                         _num(c.rel_volume) + "x" if c.rel_volume is not None else "—", _num(c.buy_sell_ratio), _num(c.freq_accel) + "x" if c.freq_accel is not None else "—",
                         f"{c.similarity.get('n', 0)} ({c.similarity.get('note')})" if comps_ok else "—", f"{c.surge_probability:.0%}" if c.surge_probability is not None else "not validated",
                         _pct(c.target_net_pct) if c.target_net_pct is not None else c.target_label, _pct(c.est_net_pct) if c.est_net_pct is not None else "—", "; ".join(c.warnings)])
        _fill(self.cand, rows, 1, lambda v: Qt.GlobalColor.green if v in ("ACCUMULATION CONFIRMED",) else Qt.GlobalColor.yellow if v in ("POSSIBLE ACCUMULATION", "BREAKOUT DEVELOPING") else Qt.GlobalColor.red if v in ("POSSIBLE DISTRIBUTION", "REVERSAL WARNING") else None)
        sc = ev.scanner
        top = ", ".join(f"{s} ({'excluded: ' + x if x else ', '.join(r) or 'baseline'})" for s, _sc, r, x in sc.get("top", [])[:8])
        self.scan_line.setText(f"Scanner: {sc.get('eligible', 0)} eligible markets, {sc.get('excluded', 0)} excluded, streaming {len(sc.get('selected', []))} · last scan {_ts(sc.get('last_scan_ms'))}, next {_ts(sc.get('next_scan_ms'))}\nTop: {top}")
        _fill(self.trans, [[_ts(t["ts_ms"]), t["symbol"], t["from_state"], t["to_state"], f"{t['eas']:.0f}" if t.get("eas") is not None else "—", t.get("note") or ""] for t in ev.transitions])
        p = ev.paper
        pnl = p.get("equity", 0) - p.get("budget", 0)
        self.p_equity.setText(f"${p.get('equity', 0):,.2f} ({pnl:+,.2f})  cash ${p.get('cash', 0):,.2f}  open: " + ("; ".join(f"{x['symbol']} {x['units']:.4g} @ {x['entry_px']:.6g} stop {x['stop']:.6g} t1 {x['t1']:.6g}" for x in p.get("positions", [])) or "none"))
        self.p_equity.setStyleSheet(f"color: {GREEN if pnl > 0 else RED if pnl < 0 else MUTED}; font-weight: 600;")
        self.p_today.setText(f"{p.get('realized_today', 0):+,.2f} / {p.get('unrealized', 0):+,.2f}" + ("  · DAILY LOSS LIMIT" if p.get("halted") else ""))
        wr = p["wins"] / p["trades"] if p.get("trades") else 0.0
        self.p_trades.setText(f"{p.get('trades', 0)} / {wr:.0%} / {p.get('net_pnl', 0):+,.2f} / {p.get('fees', 0):.2f}")
        paused = p.get("paused_until_ms", 0)
        self.p_limits.setText(f"{self.cfg.risk.risk_per_trade_pct:g}% per trade · {self.cfg.risk.daily_loss_pct:g}% daily · {self.cfg.risk.max_positions} positions · pause after {self.cfg.risk.max_consecutive_losses} losses" + (f" · PAUSED until {_ts(paused)}" if paused and paused > datetime.now().timestamp() * 1000 else "") + f" · {p.get('consecutive_losses', 0)} losses in a row")
        self.p_method.setText(f"{p.get('entry_method')} / {p.get('exit_method')}")
        rej = p.get("rejections") or {}
        self.p_rej.setText(", ".join(f"{k} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])) or "—")
        s = ev.stats
        self.scan_line.setText(self.scan_line.text() + f"\nSignals: {s.get('signals', 0)} ({s.get('pending', 0)} pending) · true positives {s.get('true_positives', 0)} · false positives {s.get('false_positives', 0)} · missed surges {s.get('missed_surges', 0)} · precision {s['precision']:.0%}" if s.get("precision") is not None else self.scan_line.text() + f"\nSignals: {s.get('signals', 0)} ({s.get('pending', 0)} pending) · none resolved yet · missed surges {s.get('missed_surges', 0)}")
        if ev.research.get("demo"):
            self.status_line.setText(self.status_line.text() + " · research results are from SYNTHETIC demo data")

    def _show_candidate(self) -> None:
        items = self.cand.selectedItems()
        if not items:
            return
        c = self._views.get(self.cand.item(items[0].row(), 0).text())
        if c is None:
            return
        lines = [f"{c.symbol} — {c.state}", f"data: {c.data_status} · updated {_ts(c.updated_ms)}", ""]
        if c.components.get("status") == "ok":
            lines.append(f"Early Accumulation Score {c.eas:.1f}")
            for k in COMPONENTS:
                lines.append(f"  {COMPONENT_NAMES[k]:<32} {float(c.components[k]):5.0f}")
        else:
            lines.append(f"INSUFFICIENT DATA — {c.components.get('detail', '')}")
        lines += ["", f"price {c.price:.6g} · spread {_num(c.spread_pct, 3)}% · 1-min quote volume {_num(c.quote_volume_1m, 0)}",
                  f"changes 5m {_pct(c.changes.get(5))} · 15m {_pct(c.changes.get(15))} · 30m {_pct(c.changes.get(30))} · 60m {_pct(c.changes.get(60))}",
                  f"relative volume {_num(c.rel_volume)}x · buy/sell {_num(c.buy_sell_ratio)} · trade frequency {_num(c.freq_accel)}x",
                  f"historical similarity: {c.similarity}", f"surge probability: {f'{c.surge_probability:.0%}' if c.surge_probability is not None else 'not validated'}",
                  f"target: {c.target_label}", f"estimated net after costs: {_pct(c.est_net_pct)}", "", "scanner: " + ("; ".join(c.scanner_reasons) or "—"), "warnings: " + ("; ".join(c.warnings) or "none")]
        self.cand_detail.setPlainText("\n".join(lines))

    # ---------------------------------------------------------- explorer

    def _load_symbols(self) -> None:
        cur = self.ex_symbol.currentText()
        self.ex_symbol.blockSignals(True)
        self.ex_symbol.clear()
        for m in self.ew.markets():
            if m.get("available"):
                self.ex_symbol.addItem(m["symbol"])
        if cur:
            i = self.ex_symbol.findText(cur)
            if i >= 0:
                self.ex_symbol.setCurrentIndex(i)
        self.ex_symbol.blockSignals(False)
        self._load_events()

    def _load_events(self) -> None:
        sym = self.ex_symbol.currentText()
        self.ev_table.setRowCount(0)
        if not sym:
            self.ex_count.setText("run the research first")
            return
        self._events = self.ew.events(sym)
        leads = {}
        for w in self.ew.windows(sym, kind="lead"):
            leads.setdefault(w["event_id"], {})[w["lookback_min"]] = w["eas"]
        rows = []
        for e in self._events:
            prof = leads.get(e["id"], {})
            run = None
            for off in sorted(prof):
                if prof[off] is not None and prof[off] >= self.cfg.score.possible_eas:
                    run = off
                else:
                    break
            e["_lead"] = run
            e["_profile"] = prof
            hyp = self._hypothetical(sym, e)
            e["_hyp"] = hyp
            rows.append([_ts(e["start_ms"]), f"{e['pre_price']:.6g}", f"{e['peak_price']:.6g}", f"{e['peak_bid']:.6g}" if e.get("peak_bid") else "—", f"{e['gain_pct']:+.1f}%", f"{e['minutes_to_peak']:.0f}",
                         f"{e['minutes_to_reversal']:.0f}" if e.get("minutes_to_reversal") is not None else "—", _pct(e.get("max_decline_pct"), 1), f"{e.get('quote_volume') or 0:,.0f}",
                         f"{run} min before" if run else "not before the start", hyp["enter"], hyp["result"]])
        _fill(self.ev_table, rows)
        self.ex_count.setText(f"{len(self._events)} events")

    def _hypothetical(self, sym: str, e: dict[str, Any]) -> dict[str, str]:
        """Would entry method A have fired, and what would the partial-exit paper trade have done?"""
        from cryptobot.earlywarning.paper import simulate_trade

        prof = e.get("_profile") or {}
        trigger_off = next((off for off in sorted(prof) if prof[off] is not None and prof[off] >= self.cfg.score.confirmed_eas), None)
        if trigger_off is None:
            return {"enter": "no (EAS never reached the confirmed level before the start)", "result": "—"}
        t0 = e["start_ms"] - trigger_off * 60_000
        k = self.ew.klines(sym, "1m", start_ms=t0 - 3 * 86_400_000, end_ms=e["peak_ms"] + 6 * 3_600_000)
        if len(k) == 0:
            return {"enter": f"yes, {trigger_off} min before", "result": "no candles"}
        tr = simulate_trade(k, None, sym, t0, "A", "partial", self.cfg, budget=self.cfg.risk.budget)
        if tr is None:
            return {"enter": f"yes, {trigger_off} min before", "result": "no trade (liquidity or costs)"}
        return {"enter": f"yes, {trigger_off} min before", "result": f"{tr.pnl_pct:+.2f}% ({tr.reason}, MFE {tr.mfe_pct:+.1f}%, MAE {tr.mae_pct:+.1f}%)"}

    def _show_event(self) -> None:
        items = self.ev_table.selectedItems()
        if not items or not getattr(self, "_events", None):
            return
        e = self._events[items[0].row()]
        sym = self.ex_symbol.currentText()
        lo, hi = e["start_ms"] - 6 * 3_600_000, e["peak_ms"] + 3 * 3_600_000
        k = self.ew.klines(sym, "1m", start_ms=lo, end_ms=hi)
        if len(k):
            self.ev_price.setData(k["ts"] / 1000.0, k["close"])
            base = float(k["quote_volume"].mean()) or 1.0
            # relative volume (x10 so it shares the 0-100 axis), smoothed over 15 minutes
            rv = k["quote_volume"] / base
            kernel = 15
            sm = [float(rv[max(0, i - kernel):i + 1].mean()) * 10 for i in range(len(rv))]
            self.ev_vol.setData(k["ts"] / 1000.0, sm)
        prof = e.get("_profile") or {}
        xs = [(e["start_ms"] - off * 60_000) / 1000.0 for off in sorted(prof, reverse=True) if prof[off] is not None]
        ys = [prof[off] for off in sorted(prof, reverse=True) if prof[off] is not None]
        self.ev_eas.setData(xs, ys)
        for ln in self.ev_lines:
            self.ev_plot.removeItem(ln)
        self.ev_lines = [pg.InfiniteLine(pos=e["start_ms"] / 1000.0, angle=90, pen=pg.mkPen(GREEN, style=Qt.PenStyle.DashLine)), pg.InfiniteLine(pos=e["peak_ms"] / 1000.0, angle=90, pen=pg.mkPen(RED, style=Qt.PenStyle.DashLine))]
        for ln in self.ev_lines:
            self.ev_plot.addItem(ln)
        self.ev_vb2.setYRange(0, 100)
        self.ev_vb2.setGeometry(self.ev_plot.getViewBox().sceneBoundingRect())
        wins = {w["lookback_min"]: w for w in self.ew.windows(sym, kind="event") if w["event_id"] == e["id"]}
        lines = [f"{sym} surge starting {_ts(e['start_ms'])} (thresholds {', '.join(e.get('thresholds', []))})",
                 f"price before {e['pre_price']:.6g} → peak {e['peak_price']:.6g} ({e['gain_pct']:+.1f}%) in {e['minutes_to_peak']:.0f} min; executable bid at peak ≈ {e['peak_bid']:.6g}" if e.get("peak_bid") else f"price before {e['pre_price']:.6g} → peak {e['peak_price']:.6g} ({e['gain_pct']:+.1f}%)",
                 f"reversal after {e['minutes_to_reversal']:.0f} min; max decline after peak {_pct(e.get('max_decline_pct'), 1)}" if e.get("minutes_to_reversal") is not None else f"no reversal within the search window; max decline after peak {_pct(e.get('max_decline_pct'), 1)}",
                 f"earliest unbroken warning: {e['_lead']} min before the start" if e.get("_lead") else "no unbroken warning before the start", "",
                 "Minutes before start → EAS (60-min window): " + ", ".join(f"{off}: {v:.0f}" for off, v in sorted(prof.items(), reverse=True) if v is not None), "", "Pre-surge windows (lookback → relative volume, buy/sell imbalance, trade freq, compression, persistence, EAS):"]
        for lb in sorted(wins, reverse=True):
            f = wins[lb]["features"]
            lines.append(f"  {lb:>5} min: vol {_num(f.get('rel_volume'))}x · imb {_num(f.get('imbalance'))} · freq {_num(f.get('freq_change'))}x · compression {_num(f.get('compression'))} · persistence {_num(f.get('persistence'))} · EAS {_num(wins[lb]['eas'], 0)}")
        hyp = e.get("_hyp") or {}
        lines += ["", f"Would the strategy have entered (method A)? {hyp.get('enter', '—')}", f"Realistic hypothetical outcome (A / partial exits, bid-side fills, fees, slippage): {hyp.get('result', '—')}"]
        self.ev_detail.setPlainText("\n".join(lines))

    # ------------------------------------------------------------- paper

    def _on_tab(self, idx: int) -> None:
        if idx == 1:
            self._load_symbols()
        elif idx == 2:
            eq = self.ew.equity("EW")
            if eq:
                self.p_curve.setData([t / 1000 for t, _e in eq], [e for _t, e in eq])
            _fill(self.p_table, [[_ts(t["ts_ms"]), t["symbol"], t["side"], f"{t['price']:.6g}", f"{t['fill_price']:.6g}", f"{t['amount']:.6g}", f"{t['fee']:.4f}", f"{t['pnl']:+.2f}" if t.get("pnl") is not None else "", t.get("reason") or ""] for t in self.ew.paper_trades("EW", limit=500)])
        elif idx == 3:
            self._refresh_research_labels()
