"""The demo tool runs, and every behaviour case in it holds.

`design-plans/data-quality/tools/demo-data-quality.py` asserts the recording loop
end to end against temporary stores: the refusal an agent gets, the retry that
fixes it, what the store then holds, and what the card and the receipt render.
This test is what stops that demo from rotting into a document — it fails the
suite if any of those cases stops holding.

Part 4 of the demo is the falsification run against a real ledger. It is skipped
here with ``--skip-store``: a CI runner has no installed store, and the point of
Part 4 is to report what it finds, not to assert a number.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "design-plans" / "data-quality" / "tools" / "demo-data-quality.py"


def test_the_demo_runs_and_every_behaviour_case_holds() -> None:
    completed = subprocess.run(
        [sys.executable, str(DEMO), "--skip-store"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout
    # The demo must actually have run its cases, not exited early.
    assert "PART 1 —" in output and "PART 2 —" in output and "PART 3 —" in output
    assert "[FAIL]" not in output
    assert "behaviour cases: 0 failed" in output
    # The falsification half is what keeps this honest; it must be named in the
    # summary even when it is skipped.
    assert "Part 4 is the honest half" in output


def test_the_demo_names_its_own_limits() -> None:
    completed = subprocess.run(
        [sys.executable, str(DEMO), "--skip-store"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0
    for limit in (
        "no live Codex session has run through the new hook",
        "the card budget is geometry, not a screen measurement",
        "the rules bind new writes only",
    ):
        assert limit in completed.stdout, limit
