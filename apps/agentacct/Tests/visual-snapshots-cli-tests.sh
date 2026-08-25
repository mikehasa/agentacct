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
if [[ "$1" == "test" && "$2" == "list" ]]; then
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
elif [[ "$1" == "test" && "$2" == "--filter" ]]; then
  printf '%s|%s|%s|%s\n' \
    "${AGENTACCT_SNAPSHOT_MODE:-}" \
    "${AGENTACCT_VERIFY_VISUAL_BASELINES:-}" \
    "${AGENTACCT_SNAPSHOT_PLATFORM_ID:-}" \
    "$3" >> "$AGENTACCT_FAKE_SWIFT_LOG"
else
  echo "unexpected fake swift invocation: $*" >&2
  exit 1
fi
EOF

cat > "$test_dir/bin/uname" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -s) echo Darwin ;;
  -m) echo arm64 ;;
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
platform_id="macos-26.6-25G72-xcode-26.6-17F113-arm64-2x"
touch "$AGENTACCT_FAKE_SWIFT_LOG"

fail() {
  echo "visual-snapshots CLI test failed: $*" >&2
  exit 1
}

resolved="$("$app_dir/Scripts/visual-snapshots" list \
  "$test_dir/TestFiles/DashboardVisualRegressionTests.swift")"
[[ "$resolved" == "agentacctTests.DashboardVisualRegressionTests" ]] \
  || fail "file path did not resolve to its discovered suite: $resolved"

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
expected_verify="verify|1|$platform_id|"
expected_verify+='^agentacctTests\.SettingsVisualRegressionTests\/testCompactDark$'
[[ "$(cat "$AGENTACCT_FAKE_SWIFT_LOG")" == "$expected_verify" ]] \
  || fail "verify did not use the exact discovered test selector"

: > "$AGENTACCT_FAKE_SWIFT_LOG"
"$app_dir/Scripts/visual-snapshots" record \
  "$test_dir/TestFiles/DashboardVisualRegressionTests.swift" >/dev/null
expected_record="record|1|$platform_id|^agentacctTests\\.DashboardVisualRegressionTests/"
expected_record+=$'\n'
expected_record+="verify|1|$platform_id|^agentacctTests\\.DashboardVisualRegressionTests/"
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

: > "$AGENTACCT_FAKE_SWIFT_LOG"
if AGENTACCT_FAKE_OS_BUILD=unexpected \
  "$app_dir/Scripts/visual-snapshots" verify DashboardVisualRegressionTests \
  >"$test_dir/platform.out" 2>&1; then
  fail "verification unexpectedly accepted a different renderer build"
fi
[[ ! -s "$AGENTACCT_FAKE_SWIFT_LOG" ]] \
  || fail "renderer mismatch invoked a visual test before rejecting the environment"
grep -q 'explicit baseline-platform migration' "$test_dir/platform.out" \
  || fail "renderer mismatch did not explain the migration workflow"

echo "visual-snapshots CLI tests passed"
