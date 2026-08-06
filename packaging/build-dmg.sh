#!/bin/bash
# Build a distributable agentacct.app + DMG: freeze the CLI, build the Swift app
# with the CLI embedded, (optionally) sign + notarize, then package a DMG.
#
# Signing/notarization are ENV-GATED so this same script produces:
#   - an UNSIGNED DMG today (DEVELOPER_ID unset) — installable with a
#     right-click-Open past Gatekeeper, fine for local/testing;
#   - a SIGNED + NOTARIZED DMG once an Apple Developer ID exists, by exporting
#     DEVELOPER_ID (and the notarytool creds) and re-running — no code changes.
#
# Required to sign+notarize (all from the Apple Developer account):
#   export DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)"
#   export NOTARY_PROFILE="agentacct-notary"   # a stored `notarytool` keychain profile
# (create the profile once: xcrun notarytool store-credentials agentacct-notary \
#   --apple-id you@example.com --team-id TEAMID --password <app-specific-password>)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
APP_SRC="$REPO_ROOT/apps/agentacct/.build/agentacct.app"
OUT_DIR="$HERE/release"
DMG_STAGE="$OUT_DIR/dmg-stage"

echo "==> [1/5] freezing the CLI"
bash "$HERE/freeze-cli.sh"

echo "==> [2/5] building the app (embeds the frozen CLI)"
bash "$REPO_ROOT/apps/agentacct/Scripts/build-app.sh"
[[ -d "$APP_SRC/Contents/Resources/cli" ]] || { echo "ERROR: CLI was not embedded"; exit 1; }

VERSION="$("$APP_SRC/Contents/Resources/cli/agentacct" --version | awk '{print $2}')"
echo "==> packaging agentacct $VERSION"

rm -rf "$OUT_DIR"; mkdir -p "$DMG_STAGE"
ditto "$APP_SRC" "$DMG_STAGE/agentacct.app"

# ---- [3/5] sign (env-gated) ------------------------------------------------
if [[ -n "${DEVELOPER_ID:-}" ]]; then
    echo "==> [3/5] signing with: $DEVELOPER_ID"
    ENTITLEMENTS="$HERE/entitlements.plist"
    # Sign inside-out: the embedded CLI (binary + every dylib/so in _internal)
    # first, then the .app. Hardened runtime is REQUIRED for notarization; the
    # CLI needs the disable-library-validation + allow-jit entitlements because
    # a PyInstaller binary loads unsigned-at-build .so files and Python JITs.
    find "$DMG_STAGE/agentacct.app/Contents/Resources/cli" \
        -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) \
        -exec codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" --sign "$DEVELOPER_ID" {} + 2>/dev/null || true
    codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" --sign "$DEVELOPER_ID" \
        "$DMG_STAGE/agentacct.app/Contents/Resources/cli/agentacct"
    codesign --force --deep --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" --sign "$DEVELOPER_ID" \
        "$DMG_STAGE/agentacct.app"
    codesign --verify --deep --strict "$DMG_STAGE/agentacct.app" && echo "    signature verified"
else
    echo "==> [3/5] SKIP signing (DEVELOPER_ID unset) — producing an UNSIGNED build"
fi

# ---- [4/5] DMG -------------------------------------------------------------
echo "==> [4/5] building DMG"
ln -s /Applications "$DMG_STAGE/Applications"
DMG="$OUT_DIR/agentacct-$VERSION.dmg"
hdiutil create -volname "agentacct $VERSION" -srcfolder "$DMG_STAGE" \
    -ov -format UDZO "$DMG" >/dev/null
echo "    $DMG"

# ---- [5/5] notarize (env-gated) -------------------------------------------
if [[ -n "${DEVELOPER_ID:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
    echo "==> [5/5] notarizing (submit + wait + staple)"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
    xcrun stapler staple "$DMG_STAGE/agentacct.app"  # staple the app too
    echo "    notarized + stapled"
else
    echo "==> [5/5] SKIP notarization (need DEVELOPER_ID + NOTARY_PROFILE)"
    echo "    Unsigned DMG: first launch needs right-click -> Open (Gatekeeper)."
fi

echo "==> done: $OUT_DIR"
