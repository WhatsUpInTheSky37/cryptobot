from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QSlider, QWidget

from cryptobot.strategy.base import Param


class ParamRow(QWidget):
    """Label + slider + spin box, kept in sync, with a plain-English tooltip."""

    changed = Signal()

    def __init__(self, param: Param, value: float | None = None) -> None:
        super().__init__()
        self.param = param
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lab = QLabel(param.label)
        lab.setFixedWidth(130)
        lab.setToolTip(param.tooltip)
        lay.addWidget(lab)
        self._scale = 1.0 / param.step
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(param.min * self._scale)), int(round(param.max * self._scale)))
        self.slider.setToolTip(param.tooltip)
        lay.addWidget(self.slider, 1)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(param.min, param.max)
        self.spin.setSingleStep(param.step)
        self.spin.setDecimals(0 if param.integer else max(1, len(str(param.step).split(".")[-1])))
        self.spin.setSuffix(param.suffix)
        self.spin.setFixedWidth(84)
        self.spin.setToolTip(param.tooltip)
        lay.addWidget(self.spin)
        self.set_value(param.default if value is None else value)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def value(self) -> float:
        v = self.spin.value()
        return int(round(v)) if self.param.integer else float(v)

    def set_value(self, v: float) -> None:
        self.spin.blockSignals(True)
        self.slider.blockSignals(True)
        self.spin.setValue(float(v))
        self.slider.setValue(int(round(float(v) * self._scale)))
        self.spin.blockSignals(False)
        self.slider.blockSignals(False)

    def _from_slider(self, iv: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(iv / self._scale)
        self.spin.blockSignals(False)
        self.changed.emit()

    def _from_spin(self, v: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(v * self._scale)))
        self.slider.blockSignals(False)
        self.changed.emit()
