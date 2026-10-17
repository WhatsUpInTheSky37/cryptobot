"""Candlestick chart built on pyqtgraph."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPicture
from PySide6.QtWidgets import QVBoxLayout, QWidget

from cryptobot.core.models import Bar
from cryptobot.gui.theme import BLUE, BORDER, GREEN, MUTED, RED


class CandlestickItem(pg.GraphicsObject):
    def __init__(self) -> None:
        super().__init__()
        self._picture = QPicture()
        self._rect = QRectF()
        self._pen_up = pg.mkPen(GREEN, width=1)
        self._pen_dn = pg.mkPen(RED, width=1)
        self._brush_up = pg.mkBrush(GREEN)
        self._brush_dn = pg.mkBrush(RED)
        self._pen_flat = pg.mkPen(MUTED, width=1)

    def set_bars(self, x: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, width: float) -> None:
        self.prepareGeometryChange()
        pic = QPicture()
        p = QPainter(pic)
        w = width * 0.7
        half = w / 2
        for xi, oi, hi, li, ci in zip(x, o, h, l, c):
            up = ci >= oi
            p.setPen(self._pen_up if up else self._pen_dn)
            p.setBrush(self._brush_up if up else self._brush_dn)
            if hi != li:
                p.drawLine(pg.QtCore.QPointF(xi, li), pg.QtCore.QPointF(xi, hi))
            body_h = ci - oi
            if body_h == 0:
                if hi == li:
                    p.setPen(self._pen_flat)  # no trades this bar: quiet marker
                p.drawLine(pg.QtCore.QPointF(xi - half, oi), pg.QtCore.QPointF(xi + half, oi))
            else:
                p.drawRect(QRectF(xi - half, oi, w, body_h))
        p.end()
        self._picture = pic
        if len(x):
            self._rect = QRectF(float(x[0]) - width, float(l.min()), float(x[-1] - x[0]) + 2 * width, float(h.max() - l.min()))
        else:
            self._rect = QRectF()
        self.update()

    def paint(self, p: QPainter, *args) -> None:  # noqa: ANN002
        p.drawPicture(0, 0, self._picture)

    def boundingRect(self) -> QRectF:
        return self._rect


class CandleChart(QWidget):
    """Shows the most recent ``visible`` bars of one series and follows the live edge."""

    def __init__(self, visible: int = 240) -> None:
        super().__init__()
        self.visible = visible
        self._bars: list[Bar] = []
        self._tf = 1
        self._dirty = False

        axis = pg.DateAxisItem(orientation="bottom")
        self.plot = pg.PlotWidget(axisItems={"bottom": axis})
        self.plot.showGrid(x=True, y=True, alpha=0.08)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.hideButtons()
        self.plot.getPlotItem().getAxis("left").setWidth(70)
        self.plot.getPlotItem().getAxis("left").setPen(pg.mkPen(BORDER))
        self.plot.getPlotItem().getAxis("bottom").setPen(pg.mkPen(BORDER))
        self.plot.getPlotItem().getAxis("left").setTextPen(pg.mkPen(MUTED))
        self.plot.getPlotItem().getAxis("bottom").setTextPen(pg.mkPen(MUTED))

        self.candles = CandlestickItem()
        self.plot.addItem(self.candles)
        self.last_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(BLUE, width=1, style=Qt.PenStyle.DashLine))
        self.last_label = pg.InfLineLabel(self.last_line, text="", position=0.98, color=BLUE, fill=pg.mkBrush("#0f1115"))
        self.plot.addItem(self.last_line)
        self.empty_text = pg.TextItem("Waiting for live data…", color=MUTED, anchor=(0.5, 0.5))
        self.plot.addItem(self.empty_text)
        self.markers = pg.ScatterPlotItem(size=12, pxMode=True)
        self.plot.addItem(self.markers)
        self._fills: list[tuple[int, float, str, str]] = []  # ts_ms, price, side, mode

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def set_series(self, bars: Sequence[Bar], timeframe_s: int) -> None:
        self._bars = list(bars)
        self._tf = timeframe_s
        self._dirty = True

    def add_fill(self, ts_ms: int, price: float, side: str, mode: str) -> None:
        """Mark a bot trade: ▲ buy / ▼ sell; hollow = TEST, solid = BUY."""
        self._fills.append((ts_ms, price, side, mode))
        self._fills = self._fills[-500:]
        self._dirty = True

    def _draw_markers(self) -> None:
        spots = []
        for ts, price, side, mode in self._fills:
            color = GREEN if side == "buy" else RED
            spots.append({
                "pos": (ts / 1000.0, price),
                "symbol": "t1" if side == "buy" else "t",
                "pen": pg.mkPen(color, width=1.5),
                "brush": pg.mkBrush(color) if mode == "BUY" else pg.mkBrush(None),
            })
        self.markers.setData(spots)

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        bars = self._bars[-self.visible :]
        if not bars:
            self.candles.set_bars(np.array([]), np.array([]), np.array([]), np.array([]), np.array([]), float(self._tf))
            self.empty_text.setVisible(True)
            return
        self.empty_text.setVisible(False)
        x = np.array([b.ts_ms / 1000.0 + self._tf / 2 for b in bars])
        o = np.array([b.open for b in bars])
        h = np.array([b.high for b in bars])
        l = np.array([b.low for b in bars])
        c = np.array([b.close for b in bars])
        self.candles.set_bars(x, o, h, l, c, float(self._tf))
        self._draw_markers()
        last = bars[-1]
        self.last_line.setPos(last.close)
        self.last_label.setText(f"{last.close:,.{_decimals(last.close)}f}")
        lo, hi = float(l.min()), float(h.max())
        pad = (hi - lo) * 0.08 or max(abs(hi) * 0.001, 1e-6)
        self.plot.setYRange(lo - pad, hi + pad, padding=0)
        self.plot.setXRange(float(x[0]) - self._tf, float(x[-1]) + self._tf * 3, padding=0)


def _decimals(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 1:
        return 4
    return 6
