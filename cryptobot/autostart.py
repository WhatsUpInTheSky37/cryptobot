"""Start CryptoBot when the computer starts (macOS LaunchAgent / Windows Run key)."""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

LABEL = "net.cryptobot.app"


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):  # packaged by PyInstaller
        return [sys.executable]
    return [sys.executable, "-m", "cryptobot"]


def _plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_enabled(home: Path | None = None) -> bool:
    if sys.platform == "darwin":
        return _plist_path(home).exists()
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "CryptoBot")
                return True
        except OSError:
            return False
    return False


def set_enabled(enabled: bool, home: Path | None = None) -> str:
    """Enable/disable. Returns a short description of what was done."""
    if sys.platform == "darwin":
        p = _plist_path(home)
        if enabled:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "Label": LABEL,
                "ProgramArguments": launch_command(),
                "RunAtLoad": True,
                "KeepAlive": False,
                "ProcessType": "Interactive",
                "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            }
            with open(p, "wb") as f:
                plistlib.dump(data, f)
            return f"Login item written to {p}"
        if p.exists():
            p.unlink()
        return "Login item removed"
    if sys.platform.startswith("win"):
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, "CryptoBot", 0, winreg.REG_SZ, " ".join(f'"{c}"' for c in launch_command()))
                return "Startup entry added to the Windows registry"
            try:
                winreg.DeleteValue(k, "CryptoBot")
            except OSError:
                pass
            return "Startup entry removed"
    return "Start-with-computer is not supported on this platform"
