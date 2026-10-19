#!/usr/bin/env bash
# Builds dist/CryptoBot.app and dist/CryptoBot.dmg on macOS.
# Requires: uv (or an existing .venv), Xcode command line tools (codesign, hdiutil, iconutil).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv sync --frozen --extra dev --extra build --python 3.12
.venv/bin/python packaging/make_icons.py
rm -rf build dist/CryptoBot dist/CryptoBot.app dist/CryptoBot.dmg
.venv/bin/pyinstaller --noconfirm --clean packaging/cryptobot.spec
# Ad-hoc signature so the app runs locally. Replace "-" with a Developer ID for distribution, then notarize.
codesign --force --deep --sign "${CODESIGN_IDENTITY:--}" dist/CryptoBot.app
hdiutil create -volname CryptoBot -srcfolder dist/CryptoBot.app -ov -format UDZO dist/CryptoBot.dmg >/dev/null
echo "Built dist/CryptoBot.app and dist/CryptoBot.dmg"

