# Builds dist\CryptoBot\CryptoBot.exe and (if Inno Setup is installed) dist\CryptoBot-Setup-<version>.exe on Windows.
# Run from PowerShell:  .\scripts\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -e ".[dev,build]"
.\.venv\Scripts\python.exe packaging\make_icons.py
Remove-Item -Recurse -Force build, dist\CryptoBot -ErrorAction SilentlyContinue
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean packaging\cryptobot.spec
$env:CRYPTOBOT_VERSION = (.\.venv\Scripts\python.exe -c "import cryptobot; print(cryptobot.__version__)")
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($iscc) {
  & $iscc.Source packaging\installer.iss
  Write-Host "Built dist\CryptoBot-Setup-$env:CRYPTOBOT_VERSION.exe"
} else {
  Write-Host "Inno Setup not found: built dist\CryptoBot\CryptoBot.exe (folder app). Install Inno Setup 6 and rerun for a one-click installer."
}
