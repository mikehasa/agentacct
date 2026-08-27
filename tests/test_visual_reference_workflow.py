"""Structural safety checks for on-demand visual reference candidates."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "visual-reference-candidates.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _combined_steps(job: dict) -> str:
    return "\n".join(str(step) for step in job["steps"])


def test_visual_reference_workflow_is_manual_and_read_only() -> None:
    workflow = _workflow()
    assert set(workflow["on"]) == {"workflow_dispatch"}
    requested_ref = workflow["on"]["workflow_dispatch"]["inputs"]["ref"]
    assert requested_ref["required"] is True
    assert requested_ref["type"] == "string"
    assert workflow["permissions"] == {"contents": "read"}

    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    assert "contents: write" not in text
    assert "secrets." not in text


def test_visual_reference_workflow_uses_two_fresh_canonical_renderers() -> None:
    render = _workflow()["jobs"]["render"]
    assert render["runs-on"] == "macos-26"
    assert render["strategy"]["fail-fast"] is False
    assert render["strategy"]["matrix"]["replica"] == ["a", "b"]
    assert render["env"] == {
        "DEVELOPER_DIR": "/Applications/Xcode_26.6.app/Contents/Developer",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TZ": "UTC",
    }

    steps = render["steps"]
    step_names = [step["name"] for step in steps]
    assert step_names.index("Check canonical renderer") < step_names.index(
        "Run deterministic dashboard render tests"
    )
    combined = _combined_steps(render)
    assert "^[0-9a-f]{40}$" in combined
    assert "visual-snapshots check-environment" in combined
    assert "DashboardSnapshotHarnessTests" in combined
    assert "swift build -c release" in combined


def test_visual_reference_workflow_keeps_trusted_tools_outside_requested_source() -> None:
    render = _workflow()["jobs"]["render"]
    checkout_steps = [step for step in render["steps"] if step.get("uses") == "actions/checkout@v4"]
    assert len(checkout_steps) == 2
    assert checkout_steps[0]["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "path": "trusted",
        "persist-credentials": False,
    }
    assert checkout_steps[1]["with"] == {
        "ref": "${{ inputs.ref }}",
        "path": "source",
        "fetch-depth": 1,
        "persist-credentials": False,
    }

    combined = _combined_steps(render)
    assert "trusted/apps/agentacct/Scripts/visual-snapshots" in combined
    assert "trusted/apps/agentacct/Scripts/package-dashboard-reference-candidate" in combined
    assert "source/apps/agentacct" in combined
    assert "AGENTACCT_SNAPSHOT_MODE=record" not in combined


def test_visual_reference_workflow_packages_traceable_replica_bundles() -> None:
    render = _workflow()["jobs"]["render"]
    combined = _combined_steps(render)
    assert "--source-commit" in combined
    assert "--renderer-id" in combined
    assert "--runner-image" in combined
    assert "--runner-image-version" in combined
    assert "ImageOS" in combined
    assert "ImageVersion" in combined

    upload = next(step for step in render["steps"] if step["name"] == "Upload replica")
    assert upload["with"]["retention-days"] == 1
    assert upload["with"]["if-no-files-found"] == "error"
    assert "matrix.replica" in upload["with"]["name"]


def test_visual_reference_workflow_promotes_only_identical_replicas_to_artifact() -> None:
    verify = _workflow()["jobs"]["verify"]
    assert verify["needs"] == "render"
    assert verify["runs-on"] == "ubuntu-latest"
    combined = _combined_steps(verify)
    assert "dashboard-reference-candidate-${{ inputs.ref }}-a" in combined
    assert "dashboard-reference-candidate-${{ inputs.ref }}-b" in combined
    assert "diff --recursive --brief --no-dereference replicas/a replicas/b" in combined

    final_upload = next(step for step in verify["steps"] if step["name"] == "Upload verified candidate")
    assert final_upload["with"]["name"] == "dashboard-reference-candidate-${{ inputs.ref }}"
    assert final_upload["with"]["path"] == "replicas/a"
    assert final_upload["with"]["retention-days"] == 14
    assert final_upload["with"]["if-no-files-found"] == "error"
