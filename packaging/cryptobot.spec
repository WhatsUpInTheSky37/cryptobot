# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec. Build with:  pyinstaller --noconfirm --clean packaging/cryptobot.spec"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
from cryptobot import __version__  # noqa: E402

ICON = str(ROOT / "packaging" / ("icon.icns" if sys.platform == "darwin" else "icon.ico"))

hidden = (
    collect_submodules("ccxt")
    + collect_submodules("ccxt.pro")
    + collect_submodules("keyring.backends")
    + ["sqlalchemy.dialects.sqlite", "zoneinfo", "tzdata", "pyqtgraph", "keyring"]
)
datas = [(str(ROOT / "cryptobot" / "help" / "guide.md"), "cryptobot/help")] + collect_data_files("tzdata")

a = Analysis(
    [str(ROOT / "cryptobot" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "IPython", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="CryptoBot",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="CryptoBot")
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="CryptoBot.app",
        icon=ICON,
        bundle_identifier="net.cryptobot.app",
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSHumanReadableCopyright": "CryptoBot. This software can lose money.",
        },
    )
