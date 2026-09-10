#!/bin/bash
# Build a distributable agentacct.app + DMG: freeze the CLI, build the Swift app
# with the CLI embedded, optionally sign + notarize for local testing, then
# package a DMG. Pass --release to require both signing and notarization.
#
# Signing/notarization are ENV-GATED so this same script produces:
#   - an UNSIGNED DMG today (DEVELOPER_ID unset) — installable with a
#     right-click-Open past Gatekeeper, fine for local/testing;
#   - a SIGNED + NOTARIZED DMG once an Apple Developer ID exists, by exporting
#     DEVELOPER_ID (and the notarytool creds) and re-running — no code changes.
#
# Required to sign+notarize (all from the Apple Developer account):
#   export DEVELOPER_ID="Developer ID Application: Your Name (TEAMID)"
#   export RELEASE_TEAM_ID="TEAMID"             # trusted 10-character Apple Team ID
#   export NOTARY_PROFILE="agentacct-notary"   # a stored `notarytool` keychain profile
# (create the profile once: xcrun notarytool store-credentials agentacct-notary \
#   --apple-id you@example.com --team-id TEAMID --password <app-specific-password>)
set -euo pipefail

RELEASE_BUILD=false
case "${1:-}" in
    --release)
        RELEASE_BUILD=true
        shift
        ;;
    "") ;;
    *)
        echo "usage: packaging/build-dmg.sh [--release]" >&2
        exit 2
        ;;
esac
if (( $# != 0 )); then
    echo "usage: packaging/build-dmg.sh [--release]" >&2
    exit 2
fi
if $RELEASE_BUILD && [[ -z "${DEVELOPER_ID:-}" ]]; then
    echo "ERROR: --release requires DEVELOPER_ID; refusing to build an unsigned release DMG" >&2
    exit 2
fi
if $RELEASE_BUILD && [[ -z "${NOTARY_PROFILE:-}" ]]; then
    echo "ERROR: --release requires NOTARY_PROFILE; refusing to build an unnotarized release DMG" >&2
    exit 2
fi
if $RELEASE_BUILD && [[ -z "${RELEASE_TEAM_ID:-}" ]]; then
    echo "ERROR: --release requires RELEASE_TEAM_ID; refusing to trust an unspecified App signer" >&2
    exit 2
fi
if [[ -n "${RELEASE_TEAM_ID:-}" && ! "$RELEASE_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]]; then
    echo "ERROR: RELEASE_TEAM_ID must be the trusted 10-character Apple Team ID" >&2
    exit 2
fi
if [[ -n "${DEVELOPER_ID:-}" && -z "${RELEASE_TEAM_ID:-}" ]]; then
    echo "ERROR: signing requires RELEASE_TEAM_ID so the App signer can be pinned" >&2
    exit 2
fi

SIGNING_IDENTITY_HASH=""
if [[ -n "${DEVELOPER_ID:-}" ]]; then
    echo "==> release credential preflight"
    SIGNING_IDENTITIES="$(security find-identity -v -p codesigning 2>/dev/null || true)"
    SIGNING_MATCHES=0
    while IFS= read -r identity_line; do
        if [[ "$identity_line" =~ ^[[:space:]]*[0-9]+\)[[:space:]]+([0-9A-Fa-f]{40})[[:space:]]+\"(.*)\"$ ]] \
            && [[ "${BASH_REMATCH[2]}" == "$DEVELOPER_ID" ]]; then
            SIGNING_IDENTITY_HASH="${BASH_REMATCH[1]}"
            SIGNING_MATCHES=$((SIGNING_MATCHES + 1))
        fi
    done <<<"$SIGNING_IDENTITIES"
    if (( SIGNING_MATCHES != 1 )); then
        echo "ERROR: DEVELOPER_ID must exactly match one available code-signing identity; refusing to start the build" >&2
        exit 2
    fi
fi
if $RELEASE_BUILD; then
    if ! xcrun notarytool history \
        --keychain-profile "$NOTARY_PROFILE" --output-format json >/dev/null; then
        echo "ERROR: NOTARY_PROFILE could not authenticate; refusing to start the release build" >&2
        exit 2
    fi
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/source-provenance.sh"
APP_SRC="$REPO_ROOT/apps/agentacct/.build/agentacct.app"
if $RELEASE_BUILD; then
    ARTIFACT_CLASS="release"
    FINAL_OUT_DIR="$HERE/release"
    COMPLETION_MARKER_NAME=".agentacct-release-complete"
else
    ARTIFACT_CLASS="local"
    FINAL_OUT_DIR="$HERE/local-build"
    COMPLETION_MARKER_NAME=".agentacct-local-build-complete"
fi
agentacct_require_clean_source "$REPO_ROOT"
EXPECTED_SOURCE_COMMIT="$(agentacct_source_commit "$REPO_ROOT")"
EXPECTED_SOURCE_DESCRIPTION="$(agentacct_source_description "$REPO_ROOT")"
PROJECT_VERSION="$(agentacct_project_version "$REPO_ROOT")"
BUILD_ROOT="$(mktemp -d "$HERE/.dmg-build.XXXXXX")"
PUBLISH_BACKUP=""
PUBLISH_COMMITTED=false
PRESERVE_BUILD_ROOT=false

cleanup_release_build() {
    local status=$?
    trap - EXIT INT TERM

    # A signal or ordinary shell failure between the two publish renames must
    # not strand the last successful selected output in a hidden backup directory.
    # If restoration itself fails, preserve both sides for manual recovery.
    if [[ -n "$PUBLISH_BACKUP" ]]; then
        if [[ -L "$PUBLISH_BACKUP" || ! -d "$PUBLISH_BACKUP" ]]; then
            echo "ERROR: $ARTIFACT_CLASS backup identity changed or disappeared during publish: $PUBLISH_BACKUP" >&2
            PRESERVE_BUILD_ROOT=true
        elif $PUBLISH_COMMITTED && [[ -d "$FINAL_OUT_DIR" ]]; then
            if ! rm -rf "$PUBLISH_BACKUP"; then
                echo "ERROR: completed $ARTIFACT_CLASS output is active, but prior output remains at $PUBLISH_BACKUP" >&2
                PRESERVE_BUILD_ROOT=true
            fi
        elif [[ ! -e "$FINAL_OUT_DIR" && ! -L "$FINAL_OUT_DIR" ]]; then
            if "$RENAME_NO_REPLACE" "$PUBLISH_BACKUP" "$FINAL_OUT_DIR"; then
                echo "restored the previous $ARTIFACT_CLASS output after an interrupted publish" >&2
            else
                echo "ERROR: interrupted $ARTIFACT_CLASS publish could not restore the previous output; backup preserved at $PUBLISH_BACKUP" >&2
                PRESERVE_BUILD_ROOT=true
            fi
        elif ! $PUBLISH_COMMITTED; then
            # The destination appeared before the script could commit its
            # transaction state (or another actor occupied it). Do not guess
            # which directory should win and never discard the only backup.
            echo "ERROR: $ARTIFACT_CLASS publish ended with an uncommitted destination; previous output preserved at $PUBLISH_BACKUP" >&2
            PRESERVE_BUILD_ROOT=true
        fi
    fi

    if $PRESERVE_BUILD_ROOT; then
        echo "$ARTIFACT_CLASS transaction workspace preserved at $BUILD_ROOT" >&2
    else
        rm -rf "$BUILD_ROOT"
    fi
    return "$status"
}

trap cleanup_release_build EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
OUT_DIR="$BUILD_ROOT/output"
DMG_STAGE="$OUT_DIR/dmg-stage"
RENAME_NO_REPLACE="$BUILD_ROOT/rename-no-replace"

# Compile the macOS no-replace rename primitive before any prior release is
# moved aside. Unlike plain `mv`, it cannot report success by nesting OUT_DIR
# inside a destination directory that appeared concurrently.
xcrun clang -std=c11 -Wall -Wextra -Werror \
    "$HERE/rename-no-replace.c" -o "$RENAME_NO_REPLACE"

# Build entirely off to the side. The selected local or release directory
# always denotes its last fully completed build; a failed
# freeze/build/sign/notarize attempt cannot partially overwrite it or make a
# half-built DMG look publishable.
mkdir -p "$OUT_DIR"

echo "==> [1/5] freezing the CLI"
bash "$HERE/freeze-cli.sh"
agentacct_assert_source_identity \
    "$REPO_ROOT" "$EXPECTED_SOURCE_COMMIT" "$EXPECTED_SOURCE_DESCRIPTION"

echo "==> [2/5] building the app (embeds the frozen CLI)"
bash "$REPO_ROOT/apps/agentacct/Scripts/build-app.sh"
[[ -d "$APP_SRC/Contents/Resources/cli" ]] || { echo "ERROR: CLI was not embedded"; exit 1; }
agentacct_assert_source_identity \
    "$REPO_ROOT" "$EXPECTED_SOURCE_COMMIT" "$EXPECTED_SOURCE_DESCRIPTION"
agentacct_verify_source_provenance "$REPO_ROOT" "$APP_SRC/Contents/Resources/cli"

CLI_VERSION="$(agentacct_cli_version "$APP_SRC/Contents/Resources/cli/agentacct")"
PLIST_VERSION="$(plutil -extract CFBundleShortVersionString raw -o - "$APP_SRC/Contents/Info.plist")"
PLIST_COMMIT="$(plutil -extract AgentacctGitCommit raw -o - "$APP_SRC/Contents/Info.plist")"
PLIST_DESCRIPTION="$(plutil -extract AgentacctBuildDescription raw -o - "$APP_SRC/Contents/Info.plist")"
EMBEDDED_COMMIT="$(<"$APP_SRC/Contents/Resources/cli/.agentacct-source-commit")"
EMBEDDED_DESCRIPTION="$(<"$APP_SRC/Contents/Resources/cli/.agentacct-source-description")"
if [[ "$CLI_VERSION" != "$PROJECT_VERSION" || "$PLIST_VERSION" != "$PROJECT_VERSION" \
    || "$PLIST_COMMIT" != "$EXPECTED_SOURCE_COMMIT" \
    || "$EMBEDDED_COMMIT" != "$EXPECTED_SOURCE_COMMIT" \
    || "$PLIST_DESCRIPTION" != "$EXPECTED_SOURCE_DESCRIPTION" \
    || "$EMBEDDED_DESCRIPTION" != "$EXPECTED_SOURCE_DESCRIPTION" ]]; then
    echo "ERROR: release identity mismatch: project=$PROJECT_VERSION app=$PLIST_VERSION embedded-cli=$CLI_VERSION expected-commit=$EXPECTED_SOURCE_COMMIT app-commit=$PLIST_COMMIT embedded-commit=$EMBEDDED_COMMIT" >&2
    exit 1
fi
VERSION="$PROJECT_VERSION"
echo "==> packaging agentacct $VERSION"

mkdir -p "$DMG_STAGE"
ditto "$APP_SRC" "$DMG_STAGE/agentacct.app"

# ---- [3/5] sign (env-gated) ------------------------------------------------
SIGNED=false
NOTARIZED=false
if [[ -n "${DEVELOPER_ID:-}" ]]; then
    echo "==> [3/5] signing with: $DEVELOPER_ID"
    ENTITLEMENTS="$HERE/entitlements.plist"
    # Sign inside-out: the embedded CLI (binary + every dylib/so in _internal)
    # first, then the .app. Hardened runtime is REQUIRED for notarization; the
    # CLI needs the disable-library-validation + allow-jit entitlements because
    # a PyInstaller binary loads unsigned-at-build .so files and Python JITs.
    # Sign every Mach-O in the embedded CLI. Failures are NOT swallowed — a lib
    # that won't sign must surface here, not silently fail notarization later.
    find "$DMG_STAGE/agentacct.app/Contents/Resources/cli" \
        -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) \
        -exec codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" --sign "$SIGNING_IDENTITY_HASH" {} +
    codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" --sign "$SIGNING_IDENTITY_HASH" \
        "$DMG_STAGE/agentacct.app/Contents/Resources/cli/agentacct"
    codesign --force --deep --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" --sign "$SIGNING_IDENTITY_HASH" \
        "$DMG_STAGE/agentacct.app"
    RELEASE_REQUIREMENT="=anchor apple generic and identifier \"dev.agentacct.app\" and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$RELEASE_TEAM_ID\""
    codesign --verify --deep --strict --verbose=2 \
        -R "$RELEASE_REQUIREMENT" "$DMG_STAGE/agentacct.app"
    ACTUAL_TEAM_ID="$(codesign -d --verbose=4 "$DMG_STAGE/agentacct.app" 2>&1 \
        | /usr/bin/sed -n 's/^TeamIdentifier=//p')"
    if [[ "$ACTUAL_TEAM_ID" != "$RELEASE_TEAM_ID" ]]; then
        echo "ERROR: signed App TeamIdentifier does not match RELEASE_TEAM_ID" >&2
        exit 1
    fi
    echo "    signature and release signer verified"
    SIGNED=true
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
    bash "$HERE/verify-dmg.sh" \
        "$DMG" "$VERSION" "$RELEASE_TEAM_ID" "$EXPECTED_SOURCE_COMMIT"
    echo "    notarized + stapled"
    NOTARIZED=true
else
    echo "==> [5/5] SKIP notarization (need DEVELOPER_ID + NOTARY_PROFILE)"
    echo "    Unsigned DMG: first launch needs right-click -> Open (Gatekeeper)."
fi

rm -rf "$DMG_STAGE"
agentacct_assert_source_identity \
    "$REPO_ROOT" "$EXPECTED_SOURCE_COMMIT" "$EXPECTED_SOURCE_DESCRIPTION"
if $RELEASE_BUILD && { ! $SIGNED || ! $NOTARIZED; }; then
    echo "ERROR: release output did not complete both signing and notarization" >&2
    exit 1
fi
printf 'artifact_class=%s\nversion=%s\nsource_commit=%s\nsource_description=%s\nsigned=%s\nnotarized=%s\n' \
    "$ARTIFACT_CLASS" "$VERSION" "$EXPECTED_SOURCE_COMMIT" "$EXPECTED_SOURCE_DESCRIPTION" \
    "$SIGNED" "$NOTARIZED" >"$OUT_DIR/$COMPLETION_MARKER_NAME"

# Publish the completed directory by rename on the same filesystem. If the
# rename fails, restore the previous successful output; if even restoration
# fails, preserve that backup outside BUILD_ROOT for manual recovery.
if [[ -L "$FINAL_OUT_DIR" || ( -e "$FINAL_OUT_DIR" && ! -d "$FINAL_OUT_DIR" ) ]]; then
    echo "ERROR: $FINAL_OUT_DIR is not a regular directory; refusing to replace it" >&2
    exit 1
fi
PUBLISH_BACKUP_CANDIDATE="$HERE/.${ARTIFACT_CLASS}-previous.$$"
if [[ -e "$PUBLISH_BACKUP_CANDIDATE" || -L "$PUBLISH_BACKUP_CANDIDATE" ]]; then
    echo "ERROR: $ARTIFACT_CLASS transaction backup already exists: $PUBLISH_BACKUP_CANDIDATE" >&2
    exit 1
fi
if [[ -d "$FINAL_OUT_DIR" ]]; then
    if ! "$RENAME_NO_REPLACE" "$FINAL_OUT_DIR" "$PUBLISH_BACKUP_CANDIDATE"; then
        echo "ERROR: could not preserve the previous $ARTIFACT_CLASS output; refusing to publish" >&2
        exit 1
    fi
    # Arm EXIT recovery only after the helper proves this path contains the
    # output we moved. A pre-existing/raced path must never be mistaken for an
    # app-owned backup by cleanup.
    PUBLISH_BACKUP="$PUBLISH_BACKUP_CANDIDATE"
fi
if ! "$RENAME_NO_REPLACE" "$OUT_DIR" "$FINAL_OUT_DIR"; then
    if [[ -d "$PUBLISH_BACKUP" && ! -L "$PUBLISH_BACKUP" \
        && ! -e "$FINAL_OUT_DIR" && ! -L "$FINAL_OUT_DIR" ]]; then
        if ! "$RENAME_NO_REPLACE" "$PUBLISH_BACKUP" "$FINAL_OUT_DIR"; then
            echo "ERROR: $ARTIFACT_CLASS publish and rollback failed; previous output preserved at $PUBLISH_BACKUP" >&2
            exit 1
        fi
        PUBLISH_BACKUP=""
    fi
    echo "ERROR: could not publish the completed $ARTIFACT_CLASS directory" >&2
    exit 1
fi
PUBLISH_COMMITTED=true
rm -rf "$PUBLISH_BACKUP"
PUBLISH_BACKUP=""

echo "==> done: $FINAL_OUT_DIR"
