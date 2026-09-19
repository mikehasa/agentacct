"""One timeline contract for receipts, native detail and paged history."""
from copy import deepcopy

import pytest

from agentacct.task_intelligence import build_task_intelligence
from agentacct.task_timeline import TimelineCursorError, TimelineSnapshotCache, build_timeline_events, valid_time
from tests.test_receipt_api import _app, _auth, _record_usage, _record_section, _record_passing_check
from agentacct.service import SentinelService


def task_with_evidence():
    check = {"event_id": "check-1", "created_at": 15, "result": "failed", "source_type": "mcp",
             "summary": "One failing assertion", "name": "Login tests", "exit_code": 1,
             "artifact_path": "/private/result.txt", "artifact_path_redacted": True,
             "artifact_url": "https://example.test/private", "artifact_url_redacted": True}
    return {"primary_root": {"client": "codex", "client_session_id": "root"},
            "sessions": [{"client": "codex", "client_session_id": "root", "title": "Login"}],
            "work_items": [{"work_id": "login", "client": "codex", "client_session_id": "root",
                            "title": "Implement login", "latest_status": "completed", "started_at": 10,
                            "updated_at": 20, "files": ["login.py"], "evidence_events": [check]}],
            "task_evidence_events": [deepcopy(check)]}


def test_shared_receipt_contract_keeps_detail_and_one_exact_check():
    task = task_with_evidence()
    records = build_timeline_events(task)
    receipt = build_task_intelligence(task, public_task_id="task-login", title="Login")
    assert records == receipt["timeline"]["events"]
    assert len(records) == 2
    work, check = records
    assert check["id"] == "event:check-1"
    assert check["section_record_ids"] == [work["id"]]
    assert check["session_key"] == work["session_key"] == "codex::root"
    assert check["source_label"] == "Agent-reported"
    assert check["status"] == "failed" and check["exit_code"] == 1
    assert check["artifact_path"] is None and check["artifact_url"] is None
    assert check["artifact_path_redacted"] and check["artifact_url_redacted"]
    assert work["updated_at"] == 20
    task["work_items"][0].update(title="Revised title", updated_at=30)
    assert build_timeline_events(task)[0]["id"] == work["id"]


def test_anonymous_checks_stay_separate_and_do_not_invent_section_membership():
    task = task_with_evidence()
    task["task_evidence_events"] = []
    task["work_items"][0]["evidence_events"][0].pop("event_id")
    task["work_items"][0]["evidence_events"] *= 2
    checks = [row for row in build_timeline_events(task) if row["kind"] == "check"]
    assert len(checks) == 2 and len({row["id"] for row in checks}) == 2
    assert all(row["event_id"] is None and row["section_record_ids"] == [] for row in checks)
    assert all(row["session_key"] is None for row in checks)


def test_conflicting_session_never_joins_a_section():
    task = task_with_evidence()
    task["task_evidence_events"][0].update(client="codex", client_session_id="other")
    check = build_timeline_events(task)[1]
    assert check["section_record_ids"] == []
    assert check["session_key"] == "codex::other"
    assert "Conflicting" in check["identity_note"]


def test_multiple_explicit_memberships_are_preserved_without_arbitrary_parent():
    task = task_with_evidence()
    other = deepcopy(task["work_items"][0]); other["work_id"] = "other"
    task["work_items"].append(other)
    records = build_timeline_events(task)
    check = next(row for row in records if row["kind"] == "check")
    assert len(check["section_record_ids"]) == 2
    assert check["section_record_id"] is None


def test_supersession_keeps_historical_result_and_exact_target():
    task = task_with_evidence()
    task["task_evidence_events"][0].update(supersession_state="superseded", superseded_by_event_id="rerun")
    task["task_evidence_events"].append({"event_id": "rerun", "result": "passed", "created_at": 25})
    checks = [row for row in build_timeline_events(task) if row["kind"] == "check"]
    assert checks[0]["superseded"] is True and checks[0]["status"] == "failed"
    assert checks[0]["superseded_by_event_id"] == checks[1]["event_id"]


@pytest.mark.parametrize("value", [None, True, False, -1, 0, float("nan"), float("inf"), 253_402_300_800, "bad"])
def test_invalid_times_cannot_reach_native_date_formatters(value):
    assert valid_time(value) is None


def test_inverted_section_clock_is_a_point_with_a_warning():
    task = task_with_evidence(); task["work_items"][0]["updated_at"] = 5
    work = build_timeline_events(task)[0]
    assert work["started_at"] == 10 and work["updated_at"] is None and work["time_warning"]


def test_history_is_frozen_across_arrivals_and_unchanged_poll_reuses_snapshot():
    cache = TimelineSnapshotCache()
    events = [{"id": str(i)} for i in range(1101)]
    first = cache.page("task-a", limit=500, events=events)
    assert first["total"] == 1101 and first["events"][0]["id"] == "601"
    assert cache.page("task-a", limit=500, events=events)["snapshot_id"] == first["snapshot_id"]
    events[0]["id"] = "mutated"
    events.append({"id": "arrival"})
    assert cache.page("task-a", limit=500, events=events)["snapshot_id"] != first["snapshot_id"]
    second = cache.page("task-a", limit=500, cursor=first["next_cursor"])
    last = cache.page("task-a", limit=500, cursor=second["next_cursor"])
    assert last["total"] == 1101 and last["events"][0]["id"] == "0"
    assert last["next_cursor"] is None and not last["truncated"]
    assert len({row["id"] for page in [first, second, last] for row in page["events"]}) == 1101


def test_cursors_reject_cross_task_expiration_and_eviction():
    now = [0]
    cache = TimelineSnapshotCache(capacity=1, ttl=10, clock=lambda: now[0])
    first = cache.page("a", limit=1, events=[{"id": "1"}, {"id": "2"}])
    for task, cursor in [("b", first["next_cursor"]), ("a", "bad"), ("a", first["snapshot_id"] + ":-1")]:
        with pytest.raises(TimelineCursorError): cache.page(task, limit=1, cursor=cursor)
    cache.page("b", limit=1, events=[])
    with pytest.raises(TimelineCursorError): cache.page("a", limit=1, cursor=first["next_cursor"])
    first = cache.page("a", limit=1, events=[{"id": "1"}, {"id": "2"}])
    now[0] = 10
    with pytest.raises(TimelineCursorError): cache.page("a", limit=1, cursor=first["next_cursor"])


def test_api_auth_identity_receipt_parity_and_frozen_pages(tmp_path):
    service = SentinelService(store_dir=tmp_path)
    _record_usage(service, session_id="root", at=100)
    _record_section(service, session_id="root", section_id="login", status="started", at=101)
    _record_passing_check(service, session_id="root", section_id="login", at=102)
    with _app(tmp_path) as client:
        assert client.get("/v1/task-timeline?task=anything").status_code == 401
        assert client.get("/v1/task-timeline?task=missing", headers=_auth()).status_code == 404
        tasks = client.get("/v1/tasks", headers=_auth()).json()
        task_id = tasks["tasks"][0]["task_id"]
        path = f"/v1/task-timeline?task={task_id}&limit=1"
        first = client.get(path, headers=_auth()).json()
        assert first["total"] == 2 and first["next_cursor"]
        _record_section(service, session_id="root", section_id="later", status="started", at=103)
        second = client.get(path + "&cursor=" + first["next_cursor"], headers=_auth()).json()
        assert second["snapshot_id"] == first["snapshot_id"] and second["total"] == 2
        complete = client.get(f"/v1/task-timeline?task={task_id}", headers=_auth()).json()
        receipt = client.get(f"/v1/receipt?task={task_id}", headers=_auth()).json()
        assert complete["events"] == receipt["timeline"]["events"]
        assert complete["total"] == 3
        assert client.get(path + "&cursor=expired:1", headers=_auth()).status_code == 409


def test_provenance_uses_the_shared_receipt_trust_boundary():
    from agentacct.receipt import _check_source
    from agentacct.task_timeline import check_source
    task = task_with_evidence()
    check = task["task_evidence_events"][0]
    for source_type, source, expected in [(None, "ci", "mcp"), ("mcp_agent_reported", "provider", "mcp"),
                                         ("client_hook", "codex", "hook"), ("provider", "anything", "ci")]:
        check.update(source_type=source_type, source=source)
        row = next(row for row in build_timeline_events(task) if row["kind"] == "check")
        assert row["source"] == _check_source(check) == check_source(check) == expected


def test_disposition_closes_attention_without_changing_recorded_failure():
    from agentacct.finding_disposition import finding_target_digest
    task = task_with_evidence()
    digest = finding_target_digest(task["task_evidence_events"][0])
    assert digest
    task["finding_episodes"] = [{"target_digest": digest, "attention_open": False, "disposition_state": "accepted"}]
    check = next(row for row in build_timeline_events(task) if row["kind"] == "check")
    assert check["disposition"] == "accepted" and check["status"] == "failed"


def test_receipt_and_timeline_share_session_titles_without_cross_client_aliasing():
    from agentacct.task_timeline import session_display_titles
    task = task_with_evidence()
    other = deepcopy(task["work_items"][0]); other.update(client="claude-code", title="Other client's work")
    task["work_items"].append(other)
    assert session_display_titles(task) == {("codex", "root"): "Login", ("claude-code", "root"): "Other client's work"}


def test_receipt_retains_older_important_records_in_canonical_order():
    task = {"work_items": [{"work_id": str(i), "started_at": i, "title": f"Work {i}"} for i in range(20, 80)],
            "task_evidence_events": [{"event_id": str(i), "created_at": i, "result": "failed"} for i in range(1, 10)]}
    timeline = build_task_intelligence(task, public_task_id="task", title="Task", timeline_limit=5)["timeline"]
    assert timeline["total"] == 69 and timeline["shown"] == 10 and timeline["truncated"]
    assert [row["occurred_at"] for row in timeline["events"]] == [5, 6, 7, 8, 9, 75, 76, 77, 78, 79]


def test_work_events_carry_section_kind_next_step_and_evidence_grade():
    task = task_with_evidence()
    item = task["work_items"][0]
    item.update(kind="review", next_step="Re-run the login tests", latest_status="handed_off")
    work = build_timeline_events(task)[0]
    assert work["kind"] == "work" and work["section_kind"] == "review"
    assert work["next_step"] == "Re-run the login tests"
    assert work["evidence_grade"] == "none"
    assert work["evidence_grade_label"] == "not graded"
    # The status label, never the raw key.
    assert work["evidence_grade_reason"] == "Handed off before completion — not graded"

    item.update(latest_status="completed", evidence_events=[
        {"event_id": "pass-1", "created_at": 18, "result": "passed", "name": "Login tests"}])
    task["task_evidence_events"] = []
    work = build_timeline_events(task)[0]
    assert work["evidence_grade"] == "self_checked"
    assert work["evidence_grade_label"] == "self-checked"
    assert "agent reported a check passed" in work["evidence_grade_reason"]

    item.update(evidence_events=[])
    work = build_timeline_events(task)[0]
    assert work["evidence_grade"] == "claimed"
    assert work["evidence_grade_label"] == "not check-relevant"
    item.update(kind="implementation")
    assert build_timeline_events(task)[0]["evidence_grade_label"] == "unchecked"
    item.pop("next_step"); item.pop("kind")
    work = build_timeline_events(task)[0]
    assert work["next_step"] is None and work["section_kind"] is None


def test_terminal_status_time_is_the_latest_update_not_the_section_start():
    """A stop is reported at the section's latest update, and only Python
    decides which statuses are terminal."""
    task = task_with_evidence()
    item = task["work_items"][0]

    item.update(latest_status="handed_off")
    work = build_timeline_events(task)[0]
    assert work["started_at"] == 10 and work["updated_at"] == 20
    assert work["terminal_status_at"] == 20

    for status in ("blocked", "completed"):
        item.update(latest_status=status)
        assert build_timeline_events(task)[0]["terminal_status_at"] == 20

    # Still running: no terminal moment to mark.
    item.update(latest_status="checkpoint")
    assert build_timeline_events(task)[0]["terminal_status_at"] is None

    # A point section stops where it started; an inconsistent clock does not
    # invent a mark before the start.
    item.update(latest_status="handed_off")
    item.pop("updated_at")
    assert build_timeline_events(task)[0]["terminal_status_at"] == 10
    item.update(updated_at=5)
    assert build_timeline_events(task)[0]["terminal_status_at"] == 10


def test_check_events_carry_name_revision_reciprocal_link_and_command_state():
    task = task_with_evidence()
    failed = task["task_evidence_events"][0]
    failed.update(command_redacted=True, command_state="digest_only", git_commit="8a4e0240abcdef",
                  git_branch="main", git_dirty=True, git_revision_basis="server_captured_at_record")
    task["task_evidence_events"].append({"event_id": "rerun", "result": "passed", "created_at": 25,
                                         "name": "Login tests", "summary": "Login tests: passed",
                                         "supersedes_check_event_id": "check-1"})
    checks = {row["event_id"]: row for row in build_timeline_events(task) if row["kind"] == "check"}
    first, rerun = checks["check-1"], checks["rerun"]
    assert first["name"] == "Login tests" and first["title"] == "Login tests"
    # The label states its BASIS. HEAD was read when the record arrived, which
    # is not a claim that this commit ran the check.
    assert first["revision_label"] == "HEAD when recorded: 8a4e024 · main · uncommitted changes"
    assert first["command_state_text"] == (
        "The agent's command argument was not stored; the title is the name the agent recorded."
    )
    assert rerun["supersedes_check_event_id"] == "check-1"
    assert rerun["revision_label"] == "revision not captured"
    assert rerun["command_state_text"] is None
    # A server-synthesized "<name>: <result>" summary is not the agent's prose.
    assert rerun["summary"] is None
    # The placeholder "check" is no name; the title falls back to the summary.
    task["task_evidence_events"][1].update(name="check", summary="Retried the suite")
    rerun = next(row for row in build_timeline_events(task) if row["event_id"] == "rerun")
    assert rerun["name"] is None and rerun["title"] == "Retried the suite"


def test_current_failure_follows_the_attention_open_predicate():
    from agentacct.finding_disposition import finding_target_digest

    task = task_with_evidence()
    failure = [row for row in build_timeline_events(task) if row["kind"] == "check"][0]
    assert failure["is_current_failure"] is True
    digest = finding_target_digest(task["task_evidence_events"][0])
    # Reviewed but still open: still needs you.
    task["finding_episodes"] = [{"target_digest": digest, "disposition_state": "reviewed", "attention_open": True}]
    failure = [row for row in build_timeline_events(task) if row["kind"] == "check"][0]
    assert failure["is_current_failure"] is True and failure["disposition"] is None
    task["finding_episodes"] = [{"target_digest": digest, "disposition_state": "resolved", "attention_open": False}]
    failure = [row for row in build_timeline_events(task) if row["kind"] == "check"][0]
    assert failure["is_current_failure"] is False and failure["status"] == "failed"
    task["finding_episodes"] = []
    task["task_evidence_events"][0].update(supersession_state="superseded")
    failure = [row for row in build_timeline_events(task) if row["kind"] == "check"][0]
    assert failure["is_current_failure"] is False
