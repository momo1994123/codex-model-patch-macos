#!/bin/bash
# Apply the Codex/ChatGPT model-pill patch on macOS, then ad-hoc re-sign the app.
#
# Fixes: the composer model pill shows "自定义"/"custom" instead of the real
# model name when a third-party / custom provider is configured.
#
# Requires: python3, codesign (Xcode CLT). Run with sudo only if the app dir
# is not writable by you -- normally it is not needed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${CHATGPT_APP:-/Applications/ChatGPT.app}"
ASAR="$APP/Contents/Resources/app.asar"
ENT="$HERE/entitlements.trimmed.plist"

if [ ! -f "$ASAR" ]; then
  echo "error: app.asar not found at $ASAR" >&2
  echo "       set CHATGPT_APP=/path/to/ChatGPT.app if it is installed elsewhere" >&2
  exit 1
fi

echo "[1/4] quitting ChatGPT"
osascript -e 'quit app "ChatGPT"' 2>/dev/null || true
sleep 3
pkill -f "$APP/Contents/MacOS/ChatGPT" 2>/dev/null || true
sleep 2

echo "[2/4] patching app.asar"
python3 "$HERE/codex_model_patch.py" patch "$ASAR"

echo "[3/4] re-signing (ad-hoc, trimmed entitlements)"
# Modifying app.asar breaks Apple's sealed-resource signature. The original
# signature carries restricted entitlements (application-identifier,
# team-identifier, aps-environment, keychain-access-groups) that cannot be
# re-applied ad-hoc -- the app would be SIGKILLed with "Code Signature Invalid".
# entitlements.trimmed.plist keeps the entitlements the app actually needs.
codesign --force --deep --sign - --options runtime --entitlements "$ENT" "$APP"
codesign --verify --verbose=2 "$APP" 2>&1 | tail -2

echo "[4/4] relaunching"
open -a "$APP"

echo
echo "done. the model pill should now show your model's display name."
echo "note: app updates overwrite app.asar -> re-run this script afterwards."
