"""Structural pins for .github/workflows/publish.yml.

The publish workflow is the highest-stakes file in the repo (PyPI filenames
can never be reused). These pins encode the Phase 4 hardening decisions:

- a pytest job runs FIRST and everything else depends on it — nothing
  publishes without green tests;
- a tag push rehearses on TestPyPI ONLY; real PyPI requires a published
  GitHub release (the structural gate — environment protection rules are
  owner-configured and NOT guaranteed to exist);
- tag/version consistency is checked before building;
- GITHUB_TOKEN permissions are minimal (empty top-level; contents: read for
  test/build; id-token: write only on the publish jobs);
- publish jobs never run from forks (owner check survives a repo rename).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_publish_workflow_gates_everything_on_a_green_test_job() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert "test" in jobs, "publish.yml must run the test suite before building"
    test_steps = " ".join(str(step.get("run", "")) for step in jobs["test"]["steps"])
    assert "pytest" in test_steps
    assert jobs["build"]["needs"] == "test" or jobs["build"]["needs"] == ["test"]
    # Transitively, both publish jobs sit behind the tests.
    assert jobs["publish-testpypi"]["needs"] in ("build", ["build"])
    assert jobs["publish-pypi"]["needs"] in ("publish-testpypi", ["publish-testpypi"])


def test_publish_workflow_checks_tag_matches_pyproject_version() -> None:
    workflow = _workflow()
    build_steps = workflow["jobs"]["build"]["steps"]
    combined = " ".join(str(step) for step in build_steps)
    assert "pyproject" in combined and "tag" in combined.lower(), (
        "build must fail unless the pushed tag (stripped of 'v') equals the pyproject version"
    )


def test_publish_workflow_real_pypi_requires_a_published_release() -> None:
    # The release-event condition is the STRUCTURAL gate: a bare v* tag push
    # must never reach real PyPI (GitHub auto-creates referenced environments
    # WITHOUT protection rules, so `environment: pypi` alone does not gate).
    workflow = _workflow()
    pypi_condition = workflow["jobs"]["publish-pypi"].get("if", "")
    assert "github.event_name == 'release'" in pypi_condition
    testpypi_condition = workflow["jobs"]["publish-testpypi"].get("if", "")
    assert "github.event_name == 'release'" not in testpypi_condition


def test_publish_workflow_publish_jobs_never_run_from_forks() -> None:
    workflow = _workflow()
    for job in ("publish-testpypi", "publish-pypi"):
        condition = workflow["jobs"][job].get("if", "")
        # repository_owner (not repository) so the repo rename doesn't break it.
        assert "github.repository_owner == 'mikehasa'" in condition, job


def test_publish_workflow_permissions_are_minimal() -> None:
    workflow = _workflow()
    assert workflow.get("permissions") == {}, "top-level permissions must be an empty map"
    jobs = workflow["jobs"]
    for job in ("test", "build"):
        assert jobs[job].get("permissions") == {"contents": "read"}, job
    for job in ("publish-testpypi", "publish-pypi"):
        assert jobs[job].get("permissions") == {"id-token": "write"}, job


def test_publish_workflow_documents_that_environment_alone_does_not_gate() -> None:
    # Comment lines wrap; compare against whitespace-normalized text (the
    # comment prefix "#" collapses into the token stream harmlessly).
    text = " ".join(WORKFLOW_PATH.read_text(encoding="utf-8").split()).replace("# ", "")
    assert "required reviewers" in text
    # The header must not oversell environment protection: without owner-side
    # protection rules the environment does not gate anything.
    assert "does not gate" in text
