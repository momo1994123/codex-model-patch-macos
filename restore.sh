#!/bin/bash
# Revert the asar patch, then re-sign ad-hoc so the app still launches.
#
# This restores the ORIGINAL app.asar from the .bak_codexmodels backup. Note:
# the app bundle's Apple signature was already replaced by an ad-hoc signature
# when the patch was applied, so a full revert to the vendor signature is only
# possible from a complete app-bundle backup (see README "Full rollback").
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${CHATGPT_APP:-/Applications/ChatGPT.app}"
ASAR="$APP/Contents/Resources/app.asar"
ENT="$HERE/entitlements.trimmed.plist"

echo "[1/3] quitting ChatGPT"
osascript -e 'quit app "ChatGPT"' 2>/dev/null || true
sleep 3
pkill -f "$APP/Contents/MacOS/ChatGPT" 2>/dev/null || true
sleep 2

echo "[2/3] restoring app.asar"
python3 "$HERE/codex_model_patch.py" restore "$ASAR"

echo "[3/3] re-signing ad-hoc so it launches"
codesign --force --deep --sign - --options runtime --entitlements "$ENT" "$APP"
open -a "$APP"
echo "done."
