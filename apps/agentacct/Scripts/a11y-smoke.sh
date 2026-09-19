#!/usr/bin/env bash
#
# a11y-smoke.sh — keyboard / VoiceOver smoke test for the agentacct macOS app.
#
# WHAT IT DOES
#   Given the pid of an ALREADY RUNNING agentacct, it activates that process,
#   Tabs from the Dashboard into the Work table, opens a record with Return and
#   backs out with Escape — all through real key events — dumping the
#   accessibility tree and the focused element after each step. It then judges
#   the recording and exits non-zero when a keyboard-only or VoiceOver user
#   could not do the same thing:
#
#     * a Work table / master row reports role AXUnknown, or exposes no AXPress
#     * Tab focus never lands on a row at all
#     * the opened record's accessibility text is missing the verdict, the
#       coverage tile, the checks tile or the recorded next step
#     * any element that received focus has neither AXDescription nor AXTitle
#
#   It LAUNCHES NOTHING. No app is started, installed, quit or reconfigured; it
#   only talks to the pid you give it.
#
# USAGE
#   Scripts/a11y-smoke.sh --pid <pid> [--dump run.json]
#   Scripts/a11y-smoke.sh --judge run.json      # no GUI at all: re-judge a dump
#   Scripts/a11y-smoke.sh --print-judge-path    # where the judging rules live
#
#   Find the pid with:  pgrep -f 'agentacct' | head -1
#
# PERMISSIONS
#   The terminal running this needs Accessibility permission (System Settings →
#   Privacy & Security → Accessibility). Without it AXUIElement reads return
#   nothing and the tool exits 2 explaining that, rather than reporting a false
#   pass.
#
# WHERE THE RULES LIVE
#   The judging is pure and lives in
#     Tests/agentacctTests/Support/AccessibilitySmokeJudge.swift
#   so it is unit-tested by `swift test --filter AccessibilitySmokeJudgeTests`
#   with fixtures and no running app. This script compiles that same file into
#   the driver, so the tool and the tests can never disagree.
#
# EXIT CODES
#   0  every check passed
#   1  at least one accessibility problem (the report lists each by code)
#   2  the tool could not run (bad arguments, no permission, unreadable dump)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "$here/.." && pwd)"
judge="$app_root/Tests/agentacctTests/Support/AccessibilitySmokeJudge.swift"
driver="$here/a11y-smoke-driver.swift"

if [[ "${1:-}" == "--print-judge-path" ]]; then
  echo "$judge"
  exit 0
fi

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
  exit 2
fi

for required in "$judge" "$driver"; do
  if [[ ! -f "$required" ]]; then
    echo "a11y-smoke: missing $required" >&2
    exit 2
  fi
done

if ! command -v swiftc >/dev/null 2>&1; then
  echo "a11y-smoke: swiftc not found (install the Xcode command line tools)" >&2
  exit 2
fi

build_dir="${TMPDIR:-/tmp}/agentacct-a11y-smoke"
mkdir -p "$build_dir"
binary="$build_dir/a11y-smoke-driver"

# Rebuild only when a source is newer than the binary.
if [[ ! -x "$binary" || "$driver" -nt "$binary" || "$judge" -nt "$binary" ]]; then
  swiftc -O -o "$binary" "$judge" "$driver" \
    -framework ApplicationServices -framework CoreGraphics
fi

exec "$binary" "$@"
