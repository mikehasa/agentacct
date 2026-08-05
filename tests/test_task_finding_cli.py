"""The local write lanes that replace the retired HTML form handlers.

`agentacct task link|unlink|rename` drives ContinuationTaskStore; `agentacct
finding list|dispose` resolves ONLY against the scope-quarantined projection
index (the exact set GET /tasks surfaces), restoring the foreign-project
refusal the retired form resolver enforced.
"""

from __future__ import annotations

import json
import time

from typer.testing import CliRunner

from agentacct.api import build_store_task_projection
from agentacct.cli import app as cli_app
from agentacct.service import SentinelService
from agentacct.task_continuations import ContinuationTaskStore


def _record_root_session(service: SentinelService, *, client: str, session_id: str, title: str) -> None:
    service.record_event({
        "source": client,
        "event_type": "section_started",
        "metadata": {
            "client": client,
            "client_session_id": session_id,
            "client_session_kind": "root",
            "client_session_title": title,
            "section_id": f"sec-{session_id}",
            "section_status": "started",
            "section_title": title,
        },
    })


def _record_failed_check(service: SentinelService, *, client: str, session_id: str, name: str) -> None:
    service.record_event({
        "source": client,
        "event_type": "machine_check",
        "metadata": {
            "client": client,
            "client_session_id": session_id,
            "name": name,
            "evidence_type": "test",
            "result": "failed",
            "summary": f"{name} failed",
            "command": "pytest -q",
            "exit_code": 1,
        },
    })


def test_task_link_rename_unlink_round_trip(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)
    _record_root_session(service, client="claude-code", session_id="sess-a", title="start work")
    _record_root_session(service, client="codex", session_id="sess-b", title="continue work")
    runner = CliRunner()

    linked = runner.invoke(cli_app, [
        "task", "link", "--client", "claude-code", "--session", "sess-a",
        "--to-client", "codex", "--to-session", "sess-b",
        "--title", "one task", "--store-dir", str(store),
    ])
    assert linked.exit_code == 0, linked.output
    assert "linked" in linked.output

    projection = ContinuationTaskStore(store).project().to_dict()
    tasks = projection.get("tasks") or []
    assert len(tasks) == 1
    task_id = tasks[0]["task_id"]
    members = {
        (m["session"]["client"], m["session"]["client_session_id"])
        for m in tasks[0]["memberships"]
    }
    assert members == {("claude-code", "sess-a"), ("codex", "sess-b")}

    # Idempotent relink reports already linked and changes nothing.
    relinked = runner.invoke(cli_app, [
        "task", "link", "--client", "claude-code", "--session", "sess-a",
        "--to-client", "codex", "--to-session", "sess-b", "--store-dir", str(store),
    ])
    assert relinked.exit_code == 0 and "already linked" in relinked.output

    renamed = runner.invoke(cli_app, [
        "task", "rename", "--task", task_id, "--title", "grand plan", "--store-dir", str(store),
    ])
    assert renamed.exit_code == 0, renamed.output
    assert ContinuationTaskStore(store).project().to_dict()["tasks"][0]["title"] == "grand plan"

    unlinked = runner.invoke(cli_app, [
        "task", "unlink", "--task", task_id, "--client", "codex", "--session", "sess-b",
        "--store-dir", str(store),
    ])
    assert unlinked.exit_code == 0, unlinked.output
    # Unlinking down to one member dissolves the task — a single-session
    # task is not a continuation, so the projection lists nothing.
    assert ContinuationTaskStore(store).project().to_dict()["tasks"] == []


def test_task_rename_accepts_the_public_task_id(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)
    _record_root_session(service, client="claude-code", session_id="sess-a", title="a")
    _record_root_session(service, client="codex", session_id="sess-b", title="b")
    runner = CliRunner()
    assert runner.invoke(cli_app, [
        "task", "link", "--client", "claude-code", "--session", "sess-a",
        "--to-client", "codex", "--to-session", "sess-b", "--store-dir", str(store),
    ]).exit_code == 0

    projection = build_store_task_projection(store)
    public_ids = [t.get("public_task_id") for t in projection.get("tasks", []) if t.get("public_task_id")]
    assert public_ids, "expected a decorated public task id"
    result = runner.invoke(cli_app, [
        "task", "rename", "--task", public_ids[0], "--title", "via public id", "--store-dir", str(store),
    ])
    assert result.exit_code == 0, result.output
    assert ContinuationTaskStore(store).project().to_dict()["tasks"][0]["title"] == "via public id"


def test_finding_list_and_dispose_round_trip(tmp_path):
    store = tmp_path / "state"
    service = SentinelService(store)
    _record_root_session(service, client="claude-code", session_id="sess-a", title="work")
    _record_failed_check(service, client="claude-code", session_id="sess-a", name="unit tests")
    runner = CliRunner()

    listed = runner.invoke(cli_app, ["finding", "list", "--json", "--store-dir", str(store)])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["findings"]
    assert len(rows) == 1 and rows[0]["state"] == "open" and rows[0]["revision"] == 0
    digest = rows[0]["digest"]

    disposed = runner.invoke(cli_app, [
        "finding", "dispose", "--digest", digest[:16], "--action", "resolve",
        "--note", "fixed by hand", "--store-dir", str(store),
    ])
    assert disposed.exit_code == 0, disposed.output
    assert "resolve recorded" in disposed.output

    after = json.loads(runner.invoke(cli_app, ["finding", "list", "--json", "--store-dir", str(store)]).output)
    assert after["findings"][0]["state"] == "resolved"
    assert after["findings"][0]["revision"] == 1
    assert after["findings"][0]["attention_open"] is False

    # Repeating the action once it is already in the requested state is a
    # friendly no-op, never a conflict.
    repeat = runner.invoke(cli_app, [
        "finding", "dispose", "--digest", digest[:16], "--action", "resolve",
        "--note", "fixed by hand", "--store-dir", str(store),
    ])
    assert repeat.exit_code == 0, repeat.output
    assert "already" in repeat.output


def test_finding_dispose_refuses_a_hidden_foreign_project_finding(tmp_path):
    """The restored scope-quarantine negative: a validly crafted digest for a
    finding this store's projection does NOT surface must be refused, and no
    disposition row may be written."""

    store = tmp_path / "repo" / ".agent-sentinel" / "state"
    store.parent.mkdir(parents=True)
    service = SentinelService(store)
    # A check whose project scope is a DIFFERENT repo: the projection
    # quarantines it (assignment skipped), so it is never surfaced.
    service.record_event({
        "source": "claude-code",
        "event_type": "machine_check",
        "metadata": {
            "client": "claude-code",
            "client_session_id": "sess-foreign",
            "name": "foreign check",
            "evidence_type": "test",
            "result": "failed",
            "summary": "foreign failure",
            "project_dir": "/somewhere/else/entirely",
        },
    })
    projection = build_store_task_projection(store)
    from agentacct.api import surfaced_finding_episodes
    from agentacct.finding_disposition import finding_target_digest

    surfaced = surfaced_finding_episodes(projection)
    stored = service.list_all_events()
    target = next(e for e in stored if e.get("event_type") == "machine_check")
    digest = finding_target_digest(target)
    surfaced_digests = {
        str(finding_target_digest(ep["failure_event"]))
        for ep in surfaced
        if isinstance(ep.get("failure_event"), dict)
    }
    if digest is not None and str(digest) in surfaced_digests:
        # The scenario depends on scope quarantine actually hiding the row; if
        # the projection surfaces it, this test's premise is broken and must
        # fail loudly rather than pass vacuously.
        raise AssertionError("expected the foreign-project finding to be quarantined")

    runner = CliRunner()
    refused = runner.invoke(cli_app, [
        "finding", "dispose", "--digest", str(digest), "--action", "resolve",
        "--note", "attempt", "--store-dir", str(store),
    ])
    assert refused.exit_code == 1
    assert "not found" in refused.output.lower()
    # And nothing was written.
    from agentacct.finding_disposition import is_trusted_finding_disposition_event

    assert not any(is_trusted_finding_disposition_event(e) for e in service.list_all_events())


def test_finding_dispose_unknown_digest_is_refused(tmp_path):
    store = tmp_path / "state"
    SentinelService(store)
    result = CliRunner().invoke(cli_app, [
        "finding", "dispose", "--digest", "deadbeef", "--action", "resolve",
        "--note", "attempt", "--store-dir", str(store),
    ])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_write_lanes_refuse_a_nonexistent_store(tmp_path):
    """A typo'd --store-dir must be a loud error, never a silently fabricated
    empty store (real findings would appear to vanish)."""

    missing = tmp_path / "not" / "a" / "store"
    runner = CliRunner()
    for args in (
        ["finding", "list", "--store-dir", str(missing)],
        ["finding", "dispose", "--digest", "abc", "--action", "reopen", "--store-dir", str(missing)],
        ["task", "rename", "--task", "x", "--title", "t", "--store-dir", str(missing)],
    ):
        result = runner.invoke(cli_app, args)
        assert result.exit_code == 1, args
        assert "No agentacct store at" in result.output
    assert not missing.exists()  # and nothing was created


def test_task_cli_input_errors_are_one_line_not_tracebacks(tmp_path):
    store = tmp_path / "state"
    SentinelService(store)
    runner = CliRunner()
    bad_id = runner.invoke(cli_app, [
        "task", "rename", "--task", "garbage id", "--title", "t", "--store-dir", str(store),
    ])
    assert bad_id.exit_code == 1
    assert "Rename failed:" in bad_id.output
    assert "Traceback" not in bad_id.output
    empty_client = runner.invoke(cli_app, [
        "task", "link", "--client", "", "--session", "s",
        "--to-client", "codex", "--to-session", "t", "--store-dir", str(store),
    ])
    assert empty_client.exit_code == 1
    assert "Link failed:" in empty_client.output
    assert "Traceback" not in empty_client.output


def test_finding_dispose_validates_action_and_note_up_front(tmp_path):
    store = tmp_path / "state"
    SentinelService(store)
    runner = CliRunner()
    bad_action = runner.invoke(cli_app, [
        "finding", "dispose", "--digest", "abc", "--action", "resolvee", "--store-dir", str(store),
    ])
    assert bad_action.exit_code == 1
    assert "Unknown action" in bad_action.output and "mark_reviewed" in bad_action.output
    no_note = runner.invoke(cli_app, [
        "finding", "dispose", "--digest", "abc", "--action", "resolve", "--store-dir", str(store),
    ])
    assert no_note.exit_code == 1
    assert "requires --note" in no_note.output


def test_finding_reinstate_is_idempotent_when_attention_is_open(tmp_path):
    """Repeating reinstate on an open-and-attended finding must be a no-op,
    not another appended ledger row per retry."""

    from agentacct.finding_disposition import is_trusted_finding_disposition_event

    store = tmp_path / "state"
    service = SentinelService(store)
    _record_root_session(service, client="claude-code", session_id="sess-a", title="work")
    _record_failed_check(service, client="claude-code", session_id="sess-a", name="unit tests")
    runner = CliRunner()
    for attempt in range(3):
        result = runner.invoke(cli_app, [
            "finding", "dispose", "--digest", _only_digest(runner, store), "--action", "reinstate",
            "--store-dir", str(store),
        ])
        assert result.exit_code == 0, result.output
        assert "already under attention" in result.output
    rows = [e for e in service.list_all_events() if is_trusted_finding_disposition_event(e)]
    assert rows == []  # open+attended from the start: nothing to write, ever


def _only_digest(runner: CliRunner, store) -> str:
    listed = runner.invoke(cli_app, ["finding", "list", "--json", "--store-dir", str(store)])
    return json.loads(listed.output)["findings"][0]["digest"]
