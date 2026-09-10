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
source "$REPO_ROOT/packaging/source-provenance.sh"

# pyproject.toml is the release source of truth used by the CLI and publish
# workflow. Git names the exact source tree behind this particular app bundle.
APP_VERSION="$(agentacct_project_version "$REPO_ROOT")"
APP_BUILD_NUMBER="$APP_VERSION"
APP_GIT_COMMIT="$(agentacct_source_commit "$REPO_ROOT")"
APP_BUILD_DESCRIPTION="$(agentacct_source_description "$REPO_ROOT")"

[[ "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "ERROR: invalid project version: $APP_VERSION" >&2; exit 1; }
[[ "$APP_BUILD_NUMBER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "ERROR: invalid app build number: $APP_BUILD_NUMBER" >&2; exit 1; }
[[ "$APP_GIT_COMMIT" =~ ^[0-9a-f]+$ ]] \
    || { echo "ERROR: invalid Git commit: $APP_GIT_COMMIT" >&2; exit 1; }
[[ "$APP_BUILD_DESCRIPTION" =~ ^[0-9A-Za-z._/+:-]+$ ]] \
    || { echo "ERROR: unsafe Git build description: $APP_BUILD_DESCRIPTION" >&2; exit 1; }

# A stale frozen CLI can otherwise be labeled with this app's current source
# identity. Validate it before compiling; absence remains the normal fast dev
# build, while any present distributable input must match exactly.
FROZEN_CLI="${AGENTACCT_FROZEN_CLI_DIR:-$REPO_ROOT/packaging/dist/agentacct}"
EMBED_FROZEN_CLI=false
if [[ -d "$FROZEN_CLI" ]]; then
    agentacct_verify_source_provenance "$REPO_ROOT" "$FROZEN_CLI"
    FROZEN_CLI_VERSION="$(agentacct_cli_version "$FROZEN_CLI/agentacct")"
    if [[ "$FROZEN_CLI_VERSION" != "$APP_VERSION" ]]; then
        echo "ERROR: frozen CLI version $FROZEN_CLI_VERSION does not match app/project version $APP_VERSION; rebuild the frozen CLI" >&2
        exit 1
    fi
    EMBED_FROZEN_CLI=true
fi

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
if $EMBED_FROZEN_CLI; then
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

# Refuse a mixed-source bundle if the worktree changed while Swift compiled or
# while the frozen CLI was copied. The Info.plist identity and embedded CLI
# must still describe the exact clean tree captured before the build.
agentacct_assert_source_identity "$REPO_ROOT" "$APP_GIT_COMMIT" "$APP_BUILD_DESCRIPTION"
if $EMBED_FROZEN_CLI; then
    agentacct_verify_source_provenance "$REPO_ROOT" "$APP/Contents/Resources/cli"
    FINAL_EMBEDDED_CLI_VERSION="$(agentacct_cli_version "$APP/Contents/Resources/cli/agentacct")"
    if [[ "$FINAL_EMBEDDED_CLI_VERSION" != "$APP_VERSION" ]]; then
        echo "ERROR: embedded CLI version changed during app assembly" >&2
        exit 1
    fi
fi

echo "built: $PWD/$APP"
echo "build: $APP_VERSION ($APP_BUILD_DESCRIPTION)"
echo "run:   open $PWD/$APP"

if $INSTALL; then
    # Quit by bundle id, never by the shared process name: both the Swift app
    # and the Python daemon are named "agentacct". If the app does not exit in
    # the bounded wait, stop instead of overwriting a live bundle.
    osascript -e 'tell application id "dev.agentacct.app" to quit' 2>/dev/null || true
    APP_PROCESS_PATTERN='^/Applications/agentacct\.app/Contents/MacOS/agentacct($| )'
    for _ in $(seq 1 30); do
        if ! pgrep -f "$APP_PROCESS_PATTERN" >/dev/null; then
            break
        fi
        sleep 0.1
    done
    if pgrep -f "$APP_PROCESS_PATTERN" >/dev/null; then
        echo "ERROR: installed agentacct app is still running; quit it before --install" >&2
        exit 1
    fi
    INSTALL_TARGET="/Applications/agentacct.app"

    verify_existing_app_bundle() {
        local app_path="$1"
        local info_plist="$app_path/Contents/Info.plist"
        local bundle_identifier
        local package_type
        local bundle_executable

        if [[ ! -d "$app_path" || -L "$app_path" ]]; then
            echo "ERROR: $app_path is not a regular app directory; refusing to replace it" >&2
            return 1
        fi
        if [[ ! -f "$info_plist" || -L "$info_plist" ]]; then
            echo "ERROR: $app_path has no regular Info.plist; refusing to replace an unowned directory" >&2
            return 1
        fi
        if ! bundle_identifier="$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$info_plist" 2>/dev/null)" \
            || ! package_type="$(/usr/bin/plutil -extract CFBundlePackageType raw -o - "$info_plist" 2>/dev/null)" \
            || ! bundle_executable="$(/usr/bin/plutil -extract CFBundleExecutable raw -o - "$info_plist" 2>/dev/null)"; then
            echo "ERROR: $app_path has an unreadable or incomplete Info.plist; refusing to replace it" >&2
            return 1
        fi
        if [[ "$bundle_identifier" != "dev.agentacct.app" \
            || "$package_type" != "APPL" \
            || "$bundle_executable" != "agentacct" ]]; then
            echo "ERROR: $app_path is not the app-owned agentacct bundle; refusing to replace it" >&2
            return 1
        fi
    }

    if [[ -L "$INSTALL_TARGET" || ( -e "$INSTALL_TARGET" && ! -d "$INSTALL_TARGET" ) ]]; then
        echo "ERROR: $INSTALL_TARGET is not a regular app directory; refusing to replace it" >&2
        exit 1
    fi
    if [[ -d "$INSTALL_TARGET" ]] && ! verify_existing_app_bundle "$INSTALL_TARGET"; then
        exit 1
    fi

    # Stage on the destination filesystem, then rename the complete bundle into
    # place. A direct ditto onto an old bundle merges directory contents and can
    # leave removed resources behind. Keep the previous app inside the private
    # transaction directory until the new bundle is activated.
    INSTALL_TRANSACTION_DIR="$(mktemp -d /Applications/.agentacct-install.XXXXXX)"
    INSTALL_STAGE="$INSTALL_TRANSACTION_DIR/agentacct.app"
    INSTALL_BACKUP="$INSTALL_TRANSACTION_DIR/previous.app"
    INSTALL_RENAME_NO_REPLACE="$INSTALL_TRANSACTION_DIR/rename-no-replace"

    # The old bundle and staged bundle share a destination-filesystem
    # transaction directory. If EXIT/INT/TERM lands after the old app was moved
    # but before the staged app is activated, restore only into a truly absent
    # target. Never delete the sole backup when restoration cannot be proven.
    restore_previous_app_on_abort() {
        local install_status=$?
        trap - EXIT INT TERM
        if [[ -d "${INSTALL_BACKUP:-}" ]]; then
            if [[ ! -e "${INSTALL_TARGET:-}" && ! -L "${INSTALL_TARGET:-}" ]]; then
                if "$INSTALL_RENAME_NO_REPLACE" "$INSTALL_BACKUP" "$INSTALL_TARGET"; then
                    echo "restored previous app after interrupted install" >&2
                    rm -rf "$INSTALL_TRANSACTION_DIR"
                else
                    echo "ERROR: interrupted install could not restore the previous app; backup preserved at $INSTALL_BACKUP" >&2
                fi
            else
                echo "ERROR: interrupted install found an occupied target; previous app backup preserved at $INSTALL_BACKUP" >&2
            fi
        elif [[ -d "${INSTALL_TRANSACTION_DIR:-}" ]]; then
            rm -rf "$INSTALL_TRANSACTION_DIR"
        fi
        return "$install_status"
    }
    trap restore_previous_app_on_abort EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    # Plain `mv stage existing-directory` succeeds by nesting the App one level
    # down. Compile the platform no-replace primitive before moving the current
    # install so a concurrently-created target always fails closed.
    xcrun clang -std=c11 -Wall -Wextra -Werror \
        "$REPO_ROOT/packaging/rename-no-replace.c" -o "$INSTALL_RENAME_NO_REPLACE"
    ditto --rsrc "$APP" "$INSTALL_STAGE"
    if [[ -d "$INSTALL_TARGET" ]]; then
        # Revalidate immediately before the move, then validate the private
        # backup again. If the path changed in either window, EXIT recovery
        # restores or preserves what was moved instead of deleting it.
        if ! verify_existing_app_bundle "$INSTALL_TARGET"; then
            exit 1
        fi
        if ! "$INSTALL_RENAME_NO_REPLACE" "$INSTALL_TARGET" "$INSTALL_BACKUP"; then
            echo "ERROR: could not preserve the installed app; refusing to activate the staged app" >&2
            exit 1
        fi
        if ! verify_existing_app_bundle "$INSTALL_BACKUP"; then
            echo "ERROR: installed app identity changed while moving it; recovery will preserve the backup" >&2
            exit 1
        fi
    fi
    if ! "$INSTALL_RENAME_NO_REPLACE" "$INSTALL_STAGE" "$INSTALL_TARGET"; then
        if [[ -d "$INSTALL_BACKUP" ]]; then
            if [[ ! -e "$INSTALL_TARGET" && ! -L "$INSTALL_TARGET" ]]; then
                if ! "$INSTALL_RENAME_NO_REPLACE" "$INSTALL_BACKUP" "$INSTALL_TARGET"; then
                    echo "ERROR: could not activate the staged app or restore the previous app; backup preserved at $INSTALL_BACKUP" >&2
                    exit 1
                fi
            else
                echo "ERROR: could not activate the staged app because the target became occupied; backup preserved at $INSTALL_BACKUP" >&2
                exit 1
            fi
        fi
        rm -rf "$INSTALL_TRANSACTION_DIR"
        echo "ERROR: could not activate the staged app; the previous app was restored when possible" >&2
        exit 1
    fi
    # The complete new app is now live at the stable path. From here cleanup
    # cannot strand the target, so disarm recovery before removing the backup.
    trap - EXIT INT TERM
    rm -rf "$INSTALL_TRANSACTION_DIR"
    open "$INSTALL_TARGET"
    echo "installed: $INSTALL_TARGET (replaced cleanly and relaunched)"
fi
