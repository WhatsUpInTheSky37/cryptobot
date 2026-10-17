# Builds dist\CryptoBot\CryptoBot.exe and (if Inno Setup is installed) dist\CryptoBot-Setup-<version>.exe on Windows.
# Run from PowerShell:  .\scripts\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -e ".[dev,build]"
.\.venv\Scripts\python.exe packaging\make_icons.py
Remove-Item -Recurse -Force build, dist\CryptoBot -ErrorAction SilentlyContinue
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean packaging\cryptobot.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$env:CRYPTOBOT_VERSION = (.\.venv\Scripts\python.exe -c "import cryptobot; print(cryptobot.__version__)")
$isccPath = $null
$cmd = Get-Command iscc -ErrorAction SilentlyContinue
if ($cmd) { $isccPath = $cmd.Source }
foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
  if (-not $isccPath -and (Test-Path $candidate)) { $isccPath = $candidate }
}
if ($isccPath) {
  & $isccPath packaging\installer.iss
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
  Write-Host "Built dist\CryptoBot-Setup-$env:CRYPTOBOT_VERSION.exe"
} else {
  Write-Host "Inno Setup not found: built dist\CryptoBot\CryptoBot.exe (folder app). Install Inno Setup 6 and rerun for a one-click installer."
}
