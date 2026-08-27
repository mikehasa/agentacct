"""Structural resource and safety policy for the visual renderer canary."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CANARY_PATH = REPO_ROOT / ".github" / "workflows" / "visual-renderer-canary.yml"
CANDIDATE_PATH = REPO_ROOT / ".github" / "workflows" / "visual-reference-candidates.yml"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_canary_runs_weekly_or_manually_with_read_only_access() -> None:
    workflow = _workflow(CANARY_PATH)
    assert workflow["on"] == {
        "schedule": [{"cron": "17 5 * * 1"}],
        "workflow_dispatch": None,
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "visual-renderer-canary",
        "cancel-in-progress": True,
    }


def test_canary_uses_one_short_canonical_runner() -> None:
    canary = _workflow(CANARY_PATH)
    candidate = _workflow(CANDIDATE_PATH)
    assert set(canary["jobs"]) == {"check"}

    check = canary["jobs"]["check"]
    canonical_render = candidate["jobs"]["render"]
    assert check["runs-on"] == canonical_render["runs-on"] == "macos-26"
    assert check["env"] == canonical_render["env"]
    assert check["timeout-minutes"] == 3


def test_canary_only_checks_identity() -> None:
    check = _workflow(CANARY_PATH)["jobs"]["check"]
    assert check["defaults"] == {
        "run": {"working-directory": "apps/agentacct"}
    }
    assert check["steps"] == [
        {
            "name": "Check out renderer definition",
            "uses": "actions/checkout@v7",
            "with": {"persist-credentials": False},
        },
        {
            "name": "Check canonical renderer",
            "run": "./Scripts/visual-snapshots check-environment",
        },
    ]

    text = CANARY_PATH.read_text(encoding="utf-8")
    for prohibited in (
        "swift build",
        "swift test",
        "snapshot-dashboard",
        "upload-artifact",
        "download-artifact",
        "secrets.",
        "contents: write",
    ):
        assert prohibited not in text
