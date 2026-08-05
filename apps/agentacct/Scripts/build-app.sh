#!/bin/bash
# Build agentacct.app from the SwiftPM executable (no Xcode project needed).
# Output: apps/AgentacctBar/.build/AgentacctBar.app — unsigned; drag it to
# /Applications or run it in place. LSUIElement makes it menu-bar-only.
set -euo pipefail

cd "$(dirname "$0")/.."
swift build -c release

APP=".build/agentacct.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp ".build/release/agentacct" "$APP/Contents/MacOS/agentacct"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>agentacct</string>
    <key>CFBundleIdentifier</key><string>dev.agentacct.app</string>
    <key>CFBundleName</key><string>agentacct</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "built: $PWD/$APP"
echo "run:   open $PWD/$APP"
