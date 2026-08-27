#!/bin/bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$APP_ROOT/../.." && pwd)"
INFO_PLIST="$APP_ROOT/.build/agentacct.app/Contents/Info.plist"

fail() {
    echo "build-app identity test failed: $*" >&2
    exit 1
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

expected_build_number="$(git -C "$REPO_ROOT" rev-list --count HEAD)"
expected_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"
expected_description="$(git -C "$REPO_ROOT" describe --tags --always --dirty)"

"$APP_ROOT/Scripts/build-app.sh" >/dev/null

read_plist() {
    plutil -extract "$1" raw -o - "$INFO_PLIST" 2>/dev/null
}

[[ "$(read_plist CFBundleShortVersionString || true)" == "$release_version" ]] \
    || fail "CFBundleShortVersionString does not match pyproject.toml"
[[ "$(read_plist CFBundleVersion || true)" == "$expected_build_number" ]] \
    || fail "CFBundleVersion does not match the Git commit count"
[[ "$(read_plist AgentacctGitCommit || true)" == "$expected_commit" ]] \
    || fail "AgentacctGitCommit does not identify HEAD"
[[ "$(read_plist AgentacctBuildDescription || true)" == "$expected_description" ]] \
    || fail "AgentacctBuildDescription does not match git describe"

echo "build-app identity tests passed"
