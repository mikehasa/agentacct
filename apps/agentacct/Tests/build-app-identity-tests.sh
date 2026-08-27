#!/bin/bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$APP_ROOT/../.." && pwd)"
INFO_PLIST="$APP_ROOT/.build/agentacct.app/Contents/Info.plist"

fail() {
    echo "build-app identity test failed: $*" >&2
    exit 1
}

record_failure() {
    echo "build-app identity test failed: $*" >&2
    failures=$((failures + 1))
}

source_description() {
    description="$(git -C "$REPO_ROOT" describe --tags --always)"
    if [[ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal)" ]]; then
        description="${description}-dirty"
    fi
    printf '%s\n' "$description"
}

read_plist() {
    plutil -extract "$1" raw -o - "$INFO_PLIST" 2>/dev/null
}

build_app() {
    AGENTACCT_FROZEN_CLI_DIR="$1" "$APP_ROOT/Scripts/build-app.sh" >/dev/null
}

expect_build_rejection() {
    label="$1"
    cli_dir="$2"
    expected_error="$3"
    log_file="$temp_root/rejection.log"

    if build_app "$cli_dir" >"$log_file" 2>&1; then
        record_failure "$label was accepted"
    elif ! grep -Fq "$expected_error" "$log_file"; then
        record_failure "$label did not report: $expected_error"
    fi
}

release_version="$({
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
} || true)"
[[ "$release_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || fail "could not read a semantic version from pyproject.toml"

failures=0
temp_root="$(mktemp -d "${TMPDIR:-/tmp}/agentacct-build-identity.XXXXXX")"
untracked_probe="$APP_ROOT/Sources/agentacct/BuildIdentityUntrackedProbe.swift"
cleanup() {
    rm -f "$untracked_probe"
    rm -rf "$temp_root"
}
trap cleanup EXIT

[[ ! -e "$untracked_probe" ]] || fail "untracked-source probe already exists: $untracked_probe"

expected_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"
expected_description="$(source_description)"
missing_cli="$temp_root/no-frozen-cli"

build_app "$missing_cli"

[[ "$(read_plist CFBundleShortVersionString || true)" == "$release_version" ]] \
    || record_failure "CFBundleShortVersionString does not match pyproject.toml"
[[ "$(read_plist CFBundleVersion || true)" == "$release_version" ]] \
    || record_failure "CFBundleVersion is not the clone-independent release version"
[[ "$(read_plist AgentacctGitCommit || true)" == "$expected_commit" ]] \
    || record_failure "AgentacctGitCommit does not identify HEAD"
[[ "$(read_plist AgentacctBuildDescription || true)" == "$expected_description" ]] \
    || record_failure "AgentacctBuildDescription does not match the source tree"

# git describe --dirty ignores untracked files, but SwiftPM compiles untracked
# sources. The bundle must not claim a clean HEAD when one changes its binary.
printf '%s\n' '// Untracked source must mark the bundle dirty.' >"$untracked_probe"
untracked_description="$(source_description)"
build_app "$missing_cli"
[[ "$(read_plist AgentacctBuildDescription || true)" == "$untracked_description" ]] \
    || record_failure "an untracked Swift source did not mark the bundle dirty"

frozen_cli="$temp_root/frozen-cli"
mkdir -p "$frozen_cli"
printf '%s\n' '#!/bin/bash' 'exit 0' >"$frozen_cli/agentacct"
chmod +x "$frozen_cli/agentacct"
printf '%s\n' "$expected_commit" >"$frozen_cli/.agentacct-source-commit"
printf '%s\n' "$untracked_description" >"$frozen_cli/.agentacct-source-description"
expect_build_rejection \
    "a frozen CLI from a dirty source tree" \
    "$frozen_cli" \
    "frozen CLI provenance requires a clean source tree"
rm -f "$untracked_probe"

# A distributable app may only embed a frozen CLI with complete provenance
# that exactly matches the clean source tree used for the app bundle.
rm -f "$frozen_cli/.agentacct-source-commit" "$frozen_cli/.agentacct-source-description"
expect_build_rejection \
    "a frozen CLI without provenance" \
    "$frozen_cli" \
    "frozen CLI provenance is missing"

printf '%040d\n' 0 >"$frozen_cli/.agentacct-source-commit"
printf '%s\n' "$expected_description" >"$frozen_cli/.agentacct-source-description"
expect_build_rejection \
    "a frozen CLI from a different commit" \
    "$frozen_cli" \
    "frozen CLI provenance does not match the app source"

printf '%s\n' "$expected_commit" >"$frozen_cli/.agentacct-source-commit"
printf '%s\n' "$expected_description" >"$frozen_cli/.agentacct-source-description"
if ! build_app "$frozen_cli"; then
    record_failure "a matching clean frozen CLI was rejected"
elif ! cmp -s "$frozen_cli/agentacct" "$APP_ROOT/.build/agentacct.app/Contents/Resources/cli/agentacct"; then
    record_failure "the matching frozen CLI was not embedded"
fi

(( failures == 0 )) || exit 1

echo "build-app identity tests passed"
