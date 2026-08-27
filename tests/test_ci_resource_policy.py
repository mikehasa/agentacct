"""Structural checks for bounded, current GitHub-hosted CI."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
VISUAL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "visual-reference-candidates.yml"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _used_actions(path: Path) -> list[str]:
    workflow = _workflow(path)
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]


def test_branch_ci_cancels_superseded_work_and_bounds_jobs() -> None:
    workflow = _workflow(TESTS_WORKFLOW)
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": True,
    }
    assert workflow["jobs"]["pytest"]["timeout-minutes"] == 20
    assert workflow["jobs"]["macos-app"]["timeout-minutes"] == 20


def test_candidate_generation_is_manual_and_globally_serialized() -> None:
    workflow = _workflow(VISUAL_WORKFLOW)
    assert workflow["concurrency"] == {
        "group": "visual-reference-candidates",
        "cancel-in-progress": False,
    }
    assert workflow["jobs"]["preflight"]["timeout-minutes"] == 5
    assert workflow["jobs"]["render"]["timeout-minutes"] == 20
    assert workflow["jobs"]["verify"]["timeout-minutes"] == 5


def test_touched_workflows_use_supported_hosted_action_majors() -> None:
    actions = _used_actions(TESTS_WORKFLOW) + _used_actions(VISUAL_WORKFLOW)
    expected_majors = {
        "actions/checkout": "v7",
        "actions/setup-python": "v7",
        "actions/upload-artifact": "v7",
        "actions/download-artifact": "v8",
    }
    for action in actions:
        name, version = action.rsplit("@", 1)
        assert version == expected_majors[name], action
