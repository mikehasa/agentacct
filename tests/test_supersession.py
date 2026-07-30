"""Read-time supersession gate and the six deciders that layer the new state.

Each test targets behavior that did not exist before the supersession model:
without the fix the failure carries no ``supersession_state`` and the reducers
return ``finding`` / ``verified`` instead of ``finding_superseded``.
"""

from __future__ import annotations

from typing import Any

from agentacct.api import _work_product_state
from agentacct.task_intelligence import build_task_intelligence
from agentacct.task_outcome import reduce_task_outcome
from agentacct.work_ledger import build_evidence_events


# The reference case from the store: same section/project/type, edited command
# and name across the fix. Token-Jaccard(command) ~= 0.80, name ratio ~= 0.833.
_YC_FAIL_CMD = "./build.sh && plutil -lint Info.plist && codesign --verify --deep --strict YCCountdown.app"
_YC_PASS_CMD = (
    "./build.sh && plutil -lint YCCountdown.app/Contents/Info.plist "
    "&& codesign --verify --deep --strict --verbose=2 YCCountdown.app"
)
_YC_FAIL_NAME = "首次应用包构建与严格签名验证"
_YC_PASS_NAME = "应用包构建与签名验证"


def _raw_check(
    *,
    event_id: str,
    created_at: float,
    result: str,
    name: str | None,
    command: str | None,
    exit_code: int | None,
    section: str | None = "yc-build",
    project: str | None = "/repo/yc",
    etype: str = "build",
    source: str = "codex",
    client: str = "codex",
    session: str = "sess-1",
    **extra: Any,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "sentinel_semantic_kind": "evidence",
        "evidence_type": etype,
        "result": result,
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "client": client,
        "client_session_id": session,
    }
    if section is not None:
        metadata["section_id"] = section
    if project is not None:
        metadata["project_dir"] = project
    metadata.update(extra)
    return {
        "event_id": event_id,
        "source": source,
        "event_type": "machine_check",
        "created_at": created_at,
        "metadata": metadata,
    }


def _by_id(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(event.get("event_id")): event for event in events}


def test_edited_command_and_name_supersedes_failure_via_command_shape() -> None:
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name=_YC_FAIL_NAME, command=_YC_FAIL_CMD, exit_code=1),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name=_YC_PASS_NAME, command=_YC_PASS_CMD, exit_code=0),
    ]
    projected = _by_id(build_evidence_events(events))

    failure = projected["fail"]
    assert failure["supersession_state"] == "superseded"
    assert failure["superseded_by_event_id"] == "pass"
    # The edited command still shares enough shape to demote; the name arm was
    # removed as too loose, so command_shape is what carries the reference case.
    assert failure["supersession_basis"] == "command_shape"


def test_name_similarity_alone_never_supersedes() -> None:
    # Sibling checks: names are 0.88 similar but the commands run different tests
    # (Jaccard ~0.67, below the shape gate). A passing "logout" check must not
    # demote a still-failing "login" check out of Needs attention on the name
    # coincidence alone; it stays a standing finding, shown with the later pass.
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="test auth login flow", command="pytest tests/test_login.py", exit_code=1),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="test auth logout flow", command="pytest tests/test_logout.py", exit_code=0),
    ]
    projected = _by_id(build_evidence_events(events))
    failure = projected["fail"]
    assert failure["supersession_state"] == "unconfirmed"
    assert failure["supersession_basis"] is None


def test_cross_project_pass_never_supersedes() -> None:
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="build", command="make", exit_code=1, project="/repo/a"),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="build", command="make", exit_code=0, project="/repo/b"),
    ]
    projected = _by_id(build_evidence_events(events))
    assert projected["fail"]["supersession_state"] is None


def test_cross_section_pass_never_supersedes() -> None:
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="build", command="make", exit_code=1, section="step-a"),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="build", command="make", exit_code=0, section="step-b"),
    ]
    projected = _by_id(build_evidence_events(events))
    assert projected["fail"]["supersession_state"] is None


def test_exit_zero_failure_is_never_demoted() -> None:
    # An exit-0 failure asserts a defect; no later exit-0 command retires it.
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="probe", command="make", exit_code=0),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="probe", command="make", exit_code=0),
    ]
    projected = _by_id(build_evidence_events(events))
    assert projected["fail"]["supersession_state"] is None


def test_unrelated_later_pass_leaves_failure_unconfirmed() -> None:
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="security boundary probe", command="./probe.sh", exit_code=1),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="unrelated lint", command="ruff check", exit_code=0),
    ]
    projected = _by_id(build_evidence_events(events))
    failure = projected["fail"]
    assert failure["supersession_state"] == "unconfirmed"
    assert failure["superseded_by_event_id"] is None


def test_regression_after_the_fixing_pass_keeps_the_finding_standing() -> None:
    # fail -> pass (same command) -> fail again (same command): the most recent
    # gate-matching check is a FAIL, so the first failure is not superseded.
    events = [
        _raw_check(event_id="fail1", created_at=1000.0, result="failed", name="suite", command="pytest", exit_code=1),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="suite", command="pytest", exit_code=0),
        _raw_check(event_id="fail2", created_at=1100.0, result="failed", name="suite", command="pytest", exit_code=1),
    ]
    projected = _by_id(build_evidence_events(events))
    # A later same-scope pass exists, but regression guard fails -> unconfirmed.
    assert projected["fail1"]["supersession_state"] == "unconfirmed"


def test_agent_declared_supersession_is_the_strongest_basis() -> None:
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="alpha", command="cmd-a", exit_code=1),
        _raw_check(
            event_id="pass",
            created_at=1044.0,
            result="passed",
            name="totally different",
            command="cmd-z",
            exit_code=0,
            supersedes_check_event_id="fail",
        ),
    ]
    projected = _by_id(build_evidence_events(events))
    failure = projected["fail"]
    assert failure["supersession_state"] == "superseded"
    assert failure["supersession_basis"] == "agent_declared"


def test_agent_declared_link_is_ignored_across_scope() -> None:
    # A passing check may only supersede an earlier same-scope failure; a
    # cross-project declared link must be structurally ignored.
    events = [
        _raw_check(event_id="fail", created_at=1000.0, result="failed", name="a", command="x", exit_code=1, project="/repo/a"),
        _raw_check(event_id="pass", created_at=1044.0, result="passed", name="b", command="y", exit_code=0, project="/repo/b", supersedes_check_event_id="fail"),
    ]
    projected = _by_id(build_evidence_events(events))
    assert projected["fail"]["supersession_state"] is None


# --- reducer / decider parity -------------------------------------------------


def _check_row(
    *,
    result: str,
    created_at: float,
    event_id: str,
    identity: str,
    supersession_state: str | None = None,
    superseded_by_event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "check_identity": identity,
        "check_identity_stable": True,
        "result": result,
        "created_at": created_at,
        "summary": f"{result} {event_id}",
        "evidence_type": "build",
        "supersession_state": supersession_state,
        "superseded_by_event_id": superseded_by_event_id,
    }


def _task_with_checks(checks: list[dict[str, Any]], *, status: str = "completed") -> dict[str, Any]:
    return {
        "work_items": [
            {
                "latest_status": status,
                "updated_at": 100.0,
                "evidence_status": "strong",
                "evidence_events": list(checks),
            }
        ],
        "task_evidence_events": [],
        "sessions": [],
        "usage": {"rows": 0},
    }


def test_reduce_task_outcome_superseded_failure_is_its_own_state_not_verified() -> None:
    checks = [
        _check_row(result="failed", created_at=100.0, event_id="fail", identity="check:a", supersession_state="superseded", superseded_by_event_id="pass"),
        _check_row(result="passed", created_at=144.0, event_id="pass", identity="check:b"),
    ]
    outcome = reduce_task_outcome(_task_with_checks(checks))
    assert outcome["key"] == "finding_superseded"
    assert outcome["verification"] is None
    # The failed event is never removed from the check set.
    assert any(str(c.get("result")) == "failed" for c in outcome["latest_checks"])


def test_reduce_task_outcome_standing_failure_is_still_a_finding() -> None:
    checks = [
        _check_row(result="failed", created_at=100.0, event_id="fail", identity="check:a"),
        _check_row(result="passed", created_at=144.0, event_id="pass", identity="check:b"),
    ]
    outcome = reduce_task_outcome(_task_with_checks(checks))
    assert outcome["key"] == "finding"


def test_work_product_state_superseded_failure_can_never_become_verified() -> None:
    superseded = _check_row(result="failed", created_at=100.0, event_id="fail", identity="check:a", supersession_state="superseded", superseded_by_event_id="pass")
    passing = _check_row(result="passed", created_at=144.0, event_id="pass", identity="check:b")

    item = {
        "latest_status": "completed",
        "evidence_status": "strong",
        "current_check_events": [superseded, passing],
    }
    state = _work_product_state(item)
    assert state["key"] == "finding_superseded"
    assert state["finding_open"] is False
    assert state["finding_present"] is True
    assert state["action_required"] is False

    # The failure is load-bearing: drop it and the very same item verifies. This
    # is exactly what the present-but-superseded guard must prevent.
    verified_item = {**item, "current_check_events": [passing]}
    assert _work_product_state(verified_item)["key"] == "verified"


def test_strongest_proof_is_never_a_failure() -> None:
    finding_checks = [_check_row(result="failed", created_at=100.0, event_id="fail", identity="check:a")]
    brief = build_task_intelligence(
        _task_with_checks(finding_checks), public_task_id="t", title="t"
    )["decision_brief"]
    assert brief["strongest_proof"] is None
    assert brief["unresolved_finding"] is not None

    superseded_checks = [
        _check_row(result="failed", created_at=100.0, event_id="fail", identity="check:a", supersession_state="superseded", superseded_by_event_id="pass"),
        _check_row(result="passed", created_at=144.0, event_id="pass", identity="check:b"),
    ]
    superseded_brief = build_task_intelligence(
        _task_with_checks(superseded_checks), public_task_id="t2", title="t2"
    )["decision_brief"]
    assert superseded_brief["strongest_proof"] is None
