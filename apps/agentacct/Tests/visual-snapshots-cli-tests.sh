#!/usr/bin/env bash
set -euo pipefail

tests_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_dir="$(cd "$tests_dir/.." && pwd)"
test_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentacct-visual-cli-test.XXXXXX")"
trap 'rm -rf "$test_dir"' EXIT
mkdir -p "$test_dir/bin" "$test_dir/TestFiles"
touch "$test_dir/TestFiles/DashboardVisualRegressionTests.swift"

cat > "$test_dir/bin/swift" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "test" && "$2" == "-c" && "$3" == "release" && "$4" == "list" ]]; then
  if [[ "${AGENTACCT_FAKE_NO_VISUAL_TESTS:-}" == "1" ]]; then
    echo 'agentacctTests.UnitTests/testUnrelatedBehavior'
    exit 0
  fi
  cat <<'TESTS'
agentacctTests.DashboardVisualRegressionTests/testMinimumAndReferenceAppearances
agentacctTests.SettingsVisualRegressionTests/testCompactDark
agentacctTests.UnitTests/testUnrelatedBehavior
TESTS
  if [[ "${AGENTACCT_FAKE_AMBIGUOUS_SUITE:-}" == "1" ]]; then
    echo 'otherTests.DashboardVisualRegressionTests/testAnotherDashboard'
  fi
elif [[ "$1" == "test" && "$2" == "-c" && "$3" == "release" && "$4" == "--filter" ]]; then
  printf '%s|%s|%s|%s|%s\n' \
    "${AGENTACCT_SNAPSHOT_MODE:-}" \
    "${AGENTACCT_VERIFY_VISUAL_BASELINES:-}" \
    "${AGENTACCT_SNAPSHOT_PLATFORM_ID:-}" \
    "$3" \
    "$5" >> "$AGENTACCT_FAKE_SWIFT_LOG"
else
  echo "unexpected fake swift invocation: $*" >&2
  exit 1
fi
EOF

cat > "$test_dir/bin/uname" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -s) echo Darwin ;;
  -m) echo "${AGENTACCT_FAKE_ARCH:-arm64}" ;;
  *) exit 1 ;;
esac
EOF

cat > "$test_dir/bin/sw_vers" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -productVersion) echo "${AGENTACCT_FAKE_OS_VERSION:-26.6}" ;;
  -buildVersion) echo "${AGENTACCT_FAKE_OS_BUILD:-25G72}" ;;
  *) exit 1 ;;
esac
EOF

cat > "$test_dir/bin/xcodebuild" <<'EOF'
#!/usr/bin/env bash
echo "Xcode ${AGENTACCT_FAKE_XCODE_VERSION:-26.6}"
echo "Build version ${AGENTACCT_FAKE_XCODE_BUILD:-17F113}"
EOF
chmod +x "$test_dir/bin/"*

export PATH="$test_dir/bin:$PATH"
export AGENTACCT_SWIFT_BIN="$test_dir/bin/swift"
export AGENTACCT_FAKE_SWIFT_LOG="$test_dir/swift.log"
platform_id="macos-26-xcode-26.6-arm64-2x"
touch "$AGENTACCT_FAKE_SWIFT_LOG"

fail() {
  echo "visual-snapshots CLI test failed: $*" >&2
  exit 1
}

declared_platform="$(AGENTACCT_FAKE_OS_BUILD=unexpected \
  "$app_dir/Scripts/visual-snapshots" platform-id)"
[[ "$declared_platform" == "$platform_id" ]] \
  || fail "platform-id returned the wrong canonical platform: $declared_platform"
[[ ! -s "$AGENTACCT_FAKE_SWIFT_LOG" ]] \
  || fail "platform-id performed unnecessary test discovery"

detected_platform="$("$app_dir/Scripts/visual-snapshots" check-environment)"
[[ "$detected_platform" == "$platform_id" ]] \
  || fail "environment check returned the wrong platform id: $detected_platform"
[[ ! -s "$AGENTACCT_FAKE_SWIFT_LOG" ]] \
  || fail "environment check performed unnecessary test discovery"

# The pin is macOS MAJOR. These four cases are the whole contract: what the gate
# now tolerates, and what it still refuses. Without them the relaxation is only
# asserted in a comment, and the orphaning it exists to prevent could recur
# silently the next time someone tightens or loosens a constant.
minor_drift="$(AGENTACCT_FAKE_OS_VERSION=26.5.1 AGENTACCT_FAKE_OS_BUILD=25F80 \
  "$app_dir/Scripts/visual-snapshots" check-environment)" \
  || fail "a macOS MINOR difference inside the pinned major must be accepted"
[[ "$minor_drift" == "$platform_id" ]] \
  || fail "a minor drift resolved to a different platform id: $minor_drift"

if AGENTACCT_FAKE_OS_VERSION=27.0 AGENTACCT_FAKE_OS_BUILD=26A428 \
  "$app_dir/Scripts/visual-snapshots" check-environment >/dev/null 2>&1; then
  fail "a macOS MAJOR difference must be refused: nothing has been measured across one"
fi

if AGENTACCT_FAKE_XCODE_VERSION=26.7 \
  "$app_dir/Scripts/visual-snapshots" check-environment >/dev/null 2>&1; then
  fail "an Xcode minor difference must still be refused: that axis is unmeasured"
fi

major_refusal="$(AGENTACCT_FAKE_OS_VERSION=27.0 AGENTACCT_FAKE_OS_BUILD=26A428 \
  "$app_dir/Scripts/visual-snapshots" check-environment 2>&1 || true)"
[[ "$major_refusal" == *"baseline-platform migration"* ]] \
  || fail "the major-version refusal did not say how to migrate: $major_refusal"

resolved="$("$app_dir/Scripts/visual-snapshots" list \
  "$test_dir/TestFiles/DashboardVisualRegressionTests.swift")"
[[ "$resolved" == "agentacctTests.DashboardVisualRegressionTests" ]] \
  || fail "file path did not resolve to its discovered suite: $resolved"

if "$app_dir/Scripts/visual-snapshots" list \
  "$test_dir/TestFiles/MissingVisualRegressionTests.swift" \
  >"$test_dir/missing-path.out" 2>&1; then
  fail "a missing visual test file unexpectedly resolved"
fi
grep -q 'test file does not exist' "$test_dir/missing-path.out" \
  || fail "missing test path did not explain the error"

all_suites="$("$app_dir/Scripts/visual-snapshots" list)"
[[ "$all_suites" == $'agentacctTests.DashboardVisualRegressionTests\nagentacctTests.SettingsVisualRegressionTests' ]] \
  || fail "list did not return only visual regression suites: $all_suites"

empty_list="$(AGENTACCT_FAKE_NO_VISUAL_TESTS=1 \
  "$app_dir/Scripts/visual-snapshots" list)"
[[ -z "$empty_list" ]] || fail "an empty visual test set should list no suites"

if AGENTACCT_FAKE_NO_VISUAL_TESTS=1 \
  "$app_dir/Scripts/visual-snapshots" verify >"$test_dir/empty.out" 2>&1; then
  fail "verify unexpectedly accepted an empty visual test set"
fi
grep -q 'no \*VisualRegressionTests suites' "$test_dir/empty.out" \
  || fail "empty verify did not explain the naming convention"

if AGENTACCT_FAKE_AMBIGUOUS_SUITE=1 \
  "$app_dir/Scripts/visual-snapshots" list DashboardVisualRegressionTests \
  >"$test_dir/ambiguous.out" 2>&1; then
  fail "a short suite name unexpectedly resolved across multiple test modules"
fi
grep -q "target 'DashboardVisualRegressionTests' is ambiguous" \
  "$test_dir/ambiguous.out" \
  || fail "ambiguous suite error did not request a qualified selector"

non_overlapping="$("$app_dir/Scripts/visual-snapshots" list \
  DashboardVisualRegressionTests \
  DashboardVisualRegressionTests/testMinimumAndReferenceAppearances)"
[[ "$non_overlapping" == "agentacctTests.DashboardVisualRegressionTests" ]] \
  || fail "overlapping targets were not collapsed to their suite: $non_overlapping"

"$app_dir/Scripts/visual-snapshots" verify \
  'SettingsVisualRegressionTests/testCompactDark' >/dev/null
expected_verify="verify|1|$platform_id|release|"
expected_verify+='^agentacctTests\.SettingsVisualRegressionTests\/testCompactDark$'
[[ "$(cat "$AGENTACCT_FAKE_SWIFT_LOG")" == "$expected_verify" ]] \
  || fail "verify did not use the exact discovered test selector"

: > "$AGENTACCT_FAKE_SWIFT_LOG"
"$app_dir/Scripts/visual-snapshots" verify >/dev/null
expected_all="verify|1|$platform_id|release|"
expected_all+='^agentacctTests\.DashboardVisualRegressionTests/|'
expected_all+='^agentacctTests\.SettingsVisualRegressionTests/'
[[ "$(cat "$AGENTACCT_FAKE_SWIFT_LOG")" == "$expected_all" ]] \
  || fail "verify did not combine all selected suites into one release test process"

: > "$AGENTACCT_FAKE_SWIFT_LOG"
# `record` writes PNGs and PLATFORM.json into the reference root. Point it at a
# scratch root: writing into the committed references would leave the working
# tree dirty, and packaging refuses to stamp a frozen CLI from a dirty tree, so
# this test would fail a later and entirely unrelated job.
record_root="$test_dir/reference-root"
mkdir -p "$record_root"
CI=false AGENTACCT_REFERENCE_ROOT="$record_root" \
  "$app_dir/Scripts/visual-snapshots" record \
  "$test_dir/TestFiles/DashboardVisualRegressionTests.swift" >/dev/null
[[ -f "$record_root/$platform_id/PLATFORM.json" ]] \
  || fail "record did not write provenance beside the references it recorded"
expected_record="record|1|$platform_id|release|^agentacctTests\\.DashboardVisualRegressionTests/"
expected_record+=$'\n'
expected_record+="verify|1|$platform_id|release|^agentacctTests\\.DashboardVisualRegressionTests/"
[[ "$(cat "$AGENTACCT_FAKE_SWIFT_LOG")" == "$expected_record" ]] \
  || fail "record did not replace and then verify the selected suite"

: > "$AGENTACCT_FAKE_SWIFT_LOG"
if CI=true "$app_dir/Scripts/visual-snapshots" record DashboardVisualRegressionTests \
  >"$test_dir/ci.out" 2>&1; then
  fail "record unexpectedly succeeded in CI"
fi
[[ ! -s "$AGENTACCT_FAKE_SWIFT_LOG" ]] \
  || fail "CI record invoked tests before rejecting the request"
grep -q 'recording is disabled in CI' "$test_dir/ci.out" \
  || fail "CI record error did not explain the safeguard"

# The OS BUILD string is deliberately no longer compared: a full minor+build
# hop was measured at maximum channel delta 1, which the tolerance absorbs, and
# encoding an uncompared fact in the gate is what orphaned the previous
# baseline. A refusal must still come from an axis that IS compared — the
# architecture stands in for the class here, and must still teach the migration.
: > "$AGENTACCT_FAKE_SWIFT_LOG"
AGENTACCT_FAKE_OS_BUILD=unexpected \
  "$app_dir/Scripts/visual-snapshots" check-environment >/dev/null 2>&1 \
  || fail "environment check rejected a build string it no longer compares"

: > "$AGENTACCT_FAKE_SWIFT_LOG"
if AGENTACCT_FAKE_ARCH=x86_64 \
  "$app_dir/Scripts/visual-snapshots" check-environment \
  >"$test_dir/platform.out" 2>&1; then
  fail "environment check unexpectedly accepted a different architecture"
fi
[[ ! -s "$AGENTACCT_FAKE_SWIFT_LOG" ]] \
  || fail "renderer mismatch performed unnecessary test discovery"
grep -q 'explicit baseline-platform migration' "$test_dir/platform.out" \
  || fail "renderer mismatch did not explain the migration workflow"

echo "visual-snapshots CLI tests passed"
