#!/bin/bash
# Build agentacct.app from the SwiftPM executable (no Xcode project needed).
# Output: apps/agentacct/.build/agentacct.app — unsigned; run in place, or
# pass --install to copy it to /Applications and relaunch (so Spotlight and
# login items always point at the newest build). LSUIElement = menu-bar-only.
set -euo pipefail

INSTALL=false
if [[ "${1:-}" == "--install" ]]; then INSTALL=true; fi

cd "$(dirname "$0")/.."
swift build -c release

APP=".build/agentacct.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp ".build/release/agentacct" "$APP/Contents/MacOS/agentacct"

# Embed the frozen standalone CLI when it has been built (packaging/freeze-cli.sh
# writes packaging/dist/agentacct/). This is what lets a machine with no Python
# run the MCP server + daemon: the app installs this into ~/.local/share on
# first setup. Dev builds skip it (fast); the DMG build always freezes first.
FROZEN_CLI="$(cd ../.. && pwd)/packaging/dist/agentacct"
if [[ -d "$FROZEN_CLI" ]]; then
    mkdir -p "$APP/Contents/Resources"
    ditto "$FROZEN_CLI" "$APP/Contents/Resources/cli"
    echo "embedded frozen CLI: Contents/Resources/cli ($(du -sh "$APP/Contents/Resources/cli" | awk '{print $1}'))"
else
    echo "note: no frozen CLI at $FROZEN_CLI — app built without the embedded installer (run packaging/freeze-cli.sh for a distributable build)"
fi

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

if $INSTALL; then
    # The Swift app only (the python daemon is a separate process and is
    # never touched here). ditto preserves the bundle; relaunch so the menu
    # bar runs the newest build.
    killall agentacct 2>/dev/null || true
    ditto --rsrc "$APP" /Applications/agentacct.app
    open /Applications/agentacct.app
    echo "installed: /Applications/agentacct.app (relaunched)"
fi
