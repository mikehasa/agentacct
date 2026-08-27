#!/bin/bash
# Build agentacct.app from the SwiftPM executable (no Xcode project needed).
# Output: apps/agentacct/.build/agentacct.app — unsigned; run in place, or
# pass --install to copy it to /Applications and relaunch (so Spotlight and
# login items always point at the newest build). LSUIElement = menu-bar-only.
set -euo pipefail

INSTALL=false
if [[ "${1:-}" == "--install" ]]; then INSTALL=true; fi

cd "$(dirname "$0")/.."
REPO_ROOT="$(cd ../.. && pwd)"

# pyproject.toml is the release source of truth used by the CLI and publish
# workflow. Git names the exact source tree behind this particular app bundle.
APP_VERSION="$(
    awk '
        /^\[project\]$/ { in_project = 1; next }
        /^\[/ { in_project = 0 }
        in_project && /^version[[:space:]]*=/ {
            value = $0
            sub(/^[^=]*=[[:space:]]*"/, "", value)
            sub(/"[[:space:]]*$/, "", value)
            print value
            exit
        }
    ' "$REPO_ROOT/pyproject.toml"
)"
APP_BUILD_NUMBER="$(git -C "$REPO_ROOT" rev-list --count HEAD)"
APP_GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
APP_BUILD_DESCRIPTION="$(git -C "$REPO_ROOT" describe --tags --always --dirty)"

[[ "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "ERROR: invalid project version: $APP_VERSION" >&2; exit 1; }
[[ "$APP_BUILD_NUMBER" =~ ^[0-9]+$ ]] \
    || { echo "ERROR: invalid app build number: $APP_BUILD_NUMBER" >&2; exit 1; }
[[ "$APP_GIT_COMMIT" =~ ^[0-9a-f]+$ ]] \
    || { echo "ERROR: invalid Git commit: $APP_GIT_COMMIT" >&2; exit 1; }
[[ "$APP_BUILD_DESCRIPTION" =~ ^[0-9A-Za-z._/+:-]+$ ]] \
    || { echo "ERROR: unsafe Git build description: $APP_BUILD_DESCRIPTION" >&2; exit 1; }

swift build -c release

APP=".build/agentacct.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp ".build/release/agentacct" "$APP/Contents/MacOS/agentacct"

# Brand app icon (Stamped Tile). Regenerate with Scripts/generate-app-icon.swift.
cp "Resources/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

# Embed the frozen standalone CLI when it has been built (packaging/freeze-cli.sh
# writes packaging/dist/agentacct/). This is what lets a machine with no Python
# run the MCP server + daemon: the app installs this into ~/.local/share on
# first setup. Dev builds skip it (fast); the DMG build always freezes first.
FROZEN_CLI="$REPO_ROOT/packaging/dist/agentacct"
if [[ -d "$FROZEN_CLI" ]]; then
    mkdir -p "$APP/Contents/Resources"
    ditto "$FROZEN_CLI" "$APP/Contents/Resources/cli"
    echo "embedded frozen CLI: Contents/Resources/cli ($(du -sh "$APP/Contents/Resources/cli" | awk '{print $1}'))"
else
    echo "note: no frozen CLI at $FROZEN_CLI — app built without the embedded installer (run packaging/freeze-cli.sh for a distributable build)"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>agentacct</string>
    <key>CFBundleIdentifier</key><string>dev.agentacct.app</string>
    <key>CFBundleName</key><string>agentacct</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$APP_VERSION</string>
    <key>CFBundleVersion</key><string>$APP_BUILD_NUMBER</string>
    <key>AgentacctGitCommit</key><string>$APP_GIT_COMMIT</string>
    <key>AgentacctBuildDescription</key><string>$APP_BUILD_DESCRIPTION</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "built: $PWD/$APP"
echo "build: $APP_VERSION ($APP_BUILD_DESCRIPTION)"
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
