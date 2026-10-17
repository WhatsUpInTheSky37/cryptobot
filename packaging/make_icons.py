"""Draw the app icon with Qt and write icon.png, icon.ico and (on macOS) icon.icns."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # CI runners have no display

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

HERE = Path(__file__).resolve().parent


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = size * 0.22
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#171a21"))
    p.drawRoundedRect(QRectF(0, 0, size, size), r, r)
    p.setBrush(QColor("#3b82f6"))
    p.drawRoundedRect(QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88), r * 0.8, r * 0.8)
    # three candles: red, green, green
    w = size * 0.12
    for i, (x, top, bot, up) in enumerate([(0.26, 0.42, 0.72, False), (0.44, 0.30, 0.62, True), (0.62, 0.22, 0.50, True)]):
        color = QColor("#22c55e") if up else QColor("#ef4444")
        p.setPen(QPen(color, size * 0.03))
        cx = size * (x + 0.06)
        p.drawLine(int(cx), int(size * (top - 0.08)), int(cx), int(size * (bot + 0.08)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRect(QRectF(size * x, size * top, w, size * (bot - top)))
    p.end()
    return img


def main() -> None:
    QGuiApplication(sys.argv)
    png = HERE / "icon.png"
    draw(1024).save(str(png))
    ico_ok = draw(256).save(str(HERE / "icon.ico"), "ICO")
    print("icon.png written; icon.ico", "written" if ico_ok else "NOT written (no ICO plugin)")
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = HERE / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for s in (16, 32, 64, 128, 256, 512):
            draw(s).save(str(iconset / f"icon_{s}x{s}.png"))
            draw(s * 2).save(str(iconset / f"icon_{s}x{s}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
        shutil.rmtree(iconset)
        print("icon.icns written")


if __name__ == "__main__":
    main()
