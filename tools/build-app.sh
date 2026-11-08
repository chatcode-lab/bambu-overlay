#!/bin/sh
# Builds "Bambu Overlay.app": a menu-bar launcher that runs server.py so macOS can grant it
# Local Network access (see tools/BambuOverlay.swift for why that is needed).
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP="$ROOT/Bambu Overlay.app"
PY="${PYTHON:-$(command -v python3)}"
PORT="$(sed -n 's/^OVERLAY_PORT=//p' .env 2>/dev/null | tail -1)"
PORT="${PORT:-8787}"

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/BambuOverlay" tools/BambuOverlay.swift
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.bambu-overlay</string>
  <key>CFBundleName</key><string>Bambu Overlay</string>
  <key>CFBundleDisplayName</key><string>Bambu Overlay</string>
  <key>CFBundleExecutable</key><string>BambuOverlay</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Reads print status from your Bambu Lab printer (read-only) to draw the stream overlay.</string>
  <key>BOProjectDir</key><string>$(xml_escape "$ROOT")</string>
  <key>BOPython</key><string>$(xml_escape "$PY")</string>
  <key>BOPort</key><string>$(xml_escape "$PORT")</string>
</dict>
</plist>
EOF
codesign --force --sign - "$APP" >/dev/null
echo "Built: $APP"
echo "  python: $PY"
echo "Start it with:  open \"$APP\""
