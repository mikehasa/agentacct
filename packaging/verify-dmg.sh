#!/bin/bash
# Verify the exact DMG file and the actual volume hdiutil mounted for it.
set -euo pipefail

if (( $# < 3 || $# > 4 )); then
    echo "usage: packaging/verify-dmg.sh DMG EXPECTED_VERSION EXPECTED_TEAM_ID [EXPECTED_SOURCE_COMMIT]" >&2
    exit 2
fi

DMG="$1"
EXPECTED_VERSION="$2"
EXPECTED_TEAM_ID="$3"
EXPECTED_SOURCE_COMMIT="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/source-provenance.sh"

[[ -f "$DMG" && ! -L "$DMG" ]] \
    || { echo "ERROR: DMG is not a regular file: $DMG" >&2; exit 2; }
[[ "$EXPECTED_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "ERROR: invalid expected version: $EXPECTED_VERSION" >&2; exit 2; }
[[ "$EXPECTED_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] \
    || { echo "ERROR: invalid expected Apple Team ID" >&2; exit 2; }
if [[ -n "$EXPECTED_SOURCE_COMMIT" && ! "$EXPECTED_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: invalid expected source commit: $EXPECTED_SOURCE_COMMIT" >&2
    exit 2
fi

VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agentacct-dmg-verify.XXXXXX")"
ATTACH_PLIST="$VERIFY_ROOT/attach.plist"
MOUNT_POINT=""
ATTACHED_DEVICE=""

cleanup_verify() {
    local status=$?
    trap - EXIT INT TERM
    local detach_target="${MOUNT_POINT:-${ATTACHED_DEVICE:-}}"
    if [[ -n "$detach_target" ]]; then
        hdiutil detach "$detach_target" -quiet || {
            echo "ERROR: could not detach verified DMG mount: $detach_target" >&2
            status=1
        }
    fi
    rm -rf "$VERIFY_ROOT"
    return "$status"
}
trap cleanup_verify EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

xcrun stapler validate "$DMG"
hdiutil attach "$DMG" -nobrowse -readonly -plist >"$ATTACH_PLIST"

# A same-named volume may already be mounted, in which case hdiutil chooses a
# suffixed path. Read the attach result instead of guessing /Volumes/<name>.
for index in $(seq 0 32); do
    device="$(plutil -extract "system-entities.$index.dev-entry" raw -o - "$ATTACH_PLIST" 2>/dev/null || true)"
    mount="$(plutil -extract "system-entities.$index.mount-point" raw -o - "$ATTACH_PLIST" 2>/dev/null || true)"
    if [[ -z "$ATTACHED_DEVICE" && -n "$device" ]]; then
        ATTACHED_DEVICE="$device"
    fi
    if [[ -n "$mount" && -d "$mount/agentacct.app" && ! -L "$mount/agentacct.app" ]]; then
        MOUNT_POINT="$mount"
        break
    fi
done
[[ -n "$MOUNT_POINT" ]] \
    || { echo "ERROR: attached DMG does not contain a regular agentacct.app" >&2; exit 1; }

APP="$MOUNT_POINT/agentacct.app"
PLIST="$APP/Contents/Info.plist"
EMBEDDED_CLI="$APP/Contents/Resources/cli"
[[ -f "$PLIST" && ! -L "$PLIST" ]] \
    || { echo "ERROR: mounted App has no regular Info.plist" >&2; exit 1; }
[[ -d "$EMBEDDED_CLI" && ! -L "$EMBEDDED_CLI" ]] \
    || { echo "ERROR: mounted App has no regular embedded CLI directory" >&2; exit 1; }
[[ -f "$EMBEDDED_CLI/agentacct" && ! -L "$EMBEDDED_CLI/agentacct" && -x "$EMBEDDED_CLI/agentacct" ]] \
    || { echo "ERROR: mounted App has no regular embedded CLI executable" >&2; exit 1; }
[[ -f "$EMBEDDED_CLI/.agentacct-source-commit" \
    && ! -L "$EMBEDDED_CLI/.agentacct-source-commit" \
    && -f "$EMBEDDED_CLI/.agentacct-source-description" \
    && ! -L "$EMBEDDED_CLI/.agentacct-source-description" ]] \
    || { echo "ERROR: mounted App has invalid embedded source markers" >&2; exit 1; }

codesign --verify --deep --strict "$APP"
ACTUAL_TEAM_ID="$(codesign -d --verbose=4 "$APP" 2>&1 \
    | /usr/bin/sed -n 's/^TeamIdentifier=//p')"
if [[ "$ACTUAL_TEAM_ID" != "$EXPECTED_TEAM_ID" ]]; then
    echo "ERROR: mounted App TeamIdentifier does not match the trusted release Team ID" >&2
    exit 1
fi
RELEASE_REQUIREMENT="=anchor apple generic and identifier \"dev.agentacct.app\" and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$EXPECTED_TEAM_ID\""
codesign --verify --deep --strict --verbose=2 -R "$RELEASE_REQUIREMENT" "$APP"
PAYLOAD_REQUIREMENT="=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"$EXPECTED_TEAM_ID\""
codesign --verify --strict --verbose=2 -R "$PAYLOAD_REQUIREMENT" "$EMBEDDED_CLI/agentacct"
spctl -a -vv --type execute "$APP"

BUNDLE_IDENTIFIER="$(plutil -extract CFBundleIdentifier raw -o - "$PLIST")"
PACKAGE_TYPE="$(plutil -extract CFBundlePackageType raw -o - "$PLIST")"
BUNDLE_EXECUTABLE="$(plutil -extract CFBundleExecutable raw -o - "$PLIST")"
APP_VERSION="$(plutil -extract CFBundleShortVersionString raw -o - "$PLIST")"
CLI_VERSION="$(agentacct_cli_version "$EMBEDDED_CLI/agentacct")"
APP_COMMIT="$(plutil -extract AgentacctGitCommit raw -o - "$PLIST")"
EMBEDDED_COMMIT="$(<"$EMBEDDED_CLI/.agentacct-source-commit")"

if [[ "$BUNDLE_IDENTIFIER" != "dev.agentacct.app" \
    || "$PACKAGE_TYPE" != "APPL" \
    || "$BUNDLE_EXECUTABLE" != "agentacct" ]]; then
    echo "ERROR: mounted DMG App identity mismatch: bundle=$BUNDLE_IDENTIFIER type=$PACKAGE_TYPE executable=$BUNDLE_EXECUTABLE" >&2
    exit 1
fi
if [[ "$APP_VERSION" != "$EXPECTED_VERSION" || "$CLI_VERSION" != "$EXPECTED_VERSION" ]]; then
    echo "ERROR: mounted DMG version mismatch: expected=$EXPECTED_VERSION app=$APP_VERSION embedded-cli=$CLI_VERSION" >&2
    exit 1
fi
if [[ -n "$EXPECTED_SOURCE_COMMIT" \
    && ( "$APP_COMMIT" != "$EXPECTED_SOURCE_COMMIT" || "$EMBEDDED_COMMIT" != "$EXPECTED_SOURCE_COMMIT" ) ]]; then
    echo "ERROR: mounted DMG source mismatch: expected=$EXPECTED_SOURCE_COMMIT app=$APP_COMMIT embedded-cli=$EMBEDDED_COMMIT" >&2
    exit 1
fi

echo "verified DMG: $DMG"
echo "mounted app: $MOUNT_POINT/agentacct.app"
echo "identity: agentacct $APP_VERSION ($APP_COMMIT)"
