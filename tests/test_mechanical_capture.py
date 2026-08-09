"""The PostToolUse mechanical-check capture pipeline (M2 P2): classify a command,
spool it, drain into a client_hook Evidence-v2 envelope, and link it (option A)
to the step active in its session at the check's time — which lifts that step to
``independently_checked``. Privacy: a command's text/args/output are never
recorded, only a category, a runner name, and a sha256 digest.
"""

from __future__ import annotations

import json

from agentacct.hooks import capture_mechanical_check
from agentacct.mechanical_capture import (
    classify_command,
    command_digest,
    drain_mechanical_check_spool,
    ingest_mechanical_check_spool,
    mechanical_check_spool_path,
    record_mechanical_check_tick,
    _build_envelope,
)
from agentacct.mechanical_checks import build_mechanical_check_events
from agentacct.task_outcome import step_evidence_grade
from agentacct.api import _attach_evidence_to_task_projection, _link_mechanical_checks_by_session_time


def test_classify_command_recognizes_common_runners() -> None:
    assert classify_command("pytest -q") == ("test", "pytest")
    assert classify_command("PYTHONPATH=src python -m pytest tests/") == ("test", "pytest")
    assert classify_command("python3 -m mypy src") == ("typecheck", "mypy")
    assert classify_command("npm run build") == ("build", "npm")
    assert classify_command("yarn lint") == ("lint", "yarn")
    assert classify_command("cargo test --all") == ("test", "cargo")
    assert classify_command("cargo check") == ("typecheck", "cargo")
    assert classify_command("ruff check .") == ("lint", "ruff")
    assert classify_command("tsc --noEmit") == ("typecheck", "tsc")
    assert classify_command("go test ./...") == ("test", "go")
    assert classify_command("swift build") == ("build", "swift")
    assert classify_command("poetry run pytest") == ("test", "pytest")
    assert classify_command("uv run ruff check") == ("lint", "ruff")
    assert classify_command("make test") == ("test", "make")
    # gradle check RUNS the tests -> kind test, not lint.
    assert classify_command("gradle check") == ("test", "gradle")
    # ruff format is a check ONLY as a --check dry run.
    assert classify_command("ruff format --check .") == ("lint", "ruff")
    # A runner reached only after a non-runner &&-chain still determines the exit.
    assert classify_command("cd repo && pytest") == ("test", "pytest")


def test_classify_command_conservative_on_non_checks_and_ambiguity() -> None:
    for command in ["echo hi", "ls -la", "git commit -m x", "cat f | grep x", "prettier --write ."]:
        assert classify_command(command) is None
    # Two DIFFERENT runners in one line is ambiguous — not captured.
    assert classify_command("ruff check && pytest -q") is None
    assert classify_command("") is None
    assert classify_command(None) is None


def test_classify_command_rejects_masked_exit_codes() -> None:
    # The harness observes the LAST command's exit code, so a runner whose
    # failure is masked by ;, ||, |, or a NEWLINE must not be recorded (a red
    # suite could exit 0). && is safe (a failure short-circuits and propagates).
    for command in [
        "pytest || true", "pytest ; echo done", "pytest | tail -20", "npm test | cat",
        # A newline is a statement separator: a runner that is not the LAST line
        # does not determine the script's exit code.
        "pytest tests/\necho done", "pytest\ngit status", "mypy src\r\necho finished",
        "pytest  # run tests\necho ok",  # per-line comment must not hide the trailing echo
    ]:
        assert classify_command(command) is None, repr(command)
    # But a runner that IS the last line still counts.
    assert classify_command("cd repo\npytest -q") == ("test", "pytest")


def test_classify_command_rejects_noop_probes_and_file_mutations() -> None:
    # Nothing is verified by a help/version print, a dry listing, or a formatter
    # that rewrites files — recording their exit 0 as a pass would forge the tier.
    for command in ["pytest --version", "pytest --collect-only", "mypy --help", "tsc --init",
                    "ruff --version", "ruff format .", "eslint --fix"]:
        assert classify_command(command) is None, command


def test_envelope_round_trips_into_a_client_hook_check() -> None:
    tick = {"c": "claude-code", "s": "sess-1", "k": "test", "r": "pytest",
            "d": command_digest("pytest -q"), "x": 0, "t": 1000.0}
    envelope = _build_envelope(tick)
    assert envelope is not None
    rows = build_mechanical_check_events([envelope])
    assert len(rows) == 1
    row = rows[0]
    assert row["source_type"] == "client_hook"
    assert row["result"] == "passed"
    assert row["evidence_type"] == "test"
    assert row["client_session_id"] == "sess-1"
    # A hook check carries no section/work/run id — option A links it by time.
    assert row["section_id"] is None and row["work_id"] is None and row["run_id"] is None
    # The command text never appears anywhere on the wire.
    assert row["command"] is None and row["command_redacted"] is True
    assert "pytest -q" not in json.dumps(row, default=str)
    # A failing exit code projects a failed check.
    fail = build_mechanical_check_events([_build_envelope({**tick, "x": 1, "d": command_digest("pytest x")})])[0]
    assert fail["result"] == "failed"


def test_spool_record_drain_ingest(tmp_path) -> None:
    record_mechanical_check_tick(tmp_path, client="claude-code", session_id="s", check_kind="test",
                                 runner="pytest", digest=command_digest("pytest"), exit_code=0, at=100.0)
    assert mechanical_check_spool_path(tmp_path).exists()
    envelopes = drain_mechanical_check_spool(tmp_path)
    assert len(envelopes) == 1
    # The spool is consumed by the drain (rename-aside), so a second drain is empty.
    assert drain_mechanical_check_spool(tmp_path) == []

    class _FakeEvidence:
        def __init__(self) -> None:
            self.appended: list = []

        def append(self, envelope) -> None:
            self.appended.append(envelope)

    record_mechanical_check_tick(tmp_path, client="claude-code", session_id="s", check_kind="lint",
                                 runner="ruff", digest=command_digest("ruff check"), exit_code=0, at=200.0)
    evidence = _FakeEvidence()
    assert ingest_mechanical_check_spool(tmp_path, evidence=evidence) == 1
    assert len(evidence.appended) == 1


def _post_tool_use(command: str, exit_code, *, tool: str = "Bash") -> str:
    return json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "tool_name": tool,
        "tool_input": {"command": command},
        "tool_output": {"exit_code": exit_code, "stdout": "", "stderr": ""},
    })


def test_capture_hook_spools_a_check_only_for_recognized_commands(tmp_path) -> None:
    capture_mechanical_check(_post_tool_use("pytest -q", 0), store_dir=tmp_path)
    lines = mechanical_check_spool_path(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    tick = json.loads(lines[0])
    assert tick["k"] == "test" and tick["r"] == "pytest" and tick["x"] == 0
    assert tick["d"].startswith("sha256:")
    assert "pytest" == tick["r"] and "pytest -q" not in lines[0]  # command text not spooled

    # Non-check command, non-Bash tool, and a missing exit code each spool nothing.
    capture_mechanical_check(_post_tool_use("echo hi", 0), store_dir=tmp_path)
    capture_mechanical_check(_post_tool_use("pytest", 0, tool="Read"), store_dir=tmp_path)
    capture_mechanical_check(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s",
                                         "tool_name": "Bash", "tool_input": {"command": "pytest"},
                                         "tool_output": {"stdout": ""}}), store_dir=tmp_path)
    assert len(mechanical_check_spool_path(tmp_path).read_text().splitlines()) == 1  # still just the first


def _hook_check(session: str, at: float, result: str = "passed") -> dict:
    return {"event_id": f"evidence:{at}", "source_type": "client_hook", "result": result,
            "client_session_id": session, "created_at": at, "evidence_type": "test",
            "check_identity": f"client-hook:{at}", "check_identity_stable": True}


def test_link_attaches_hook_check_to_the_step_active_at_its_time() -> None:
    task = {
        "task_id": "t1",
        "work_items": [
            {"work_id": "w1", "client_session_id": "S", "kind": "implementation", "latest_status": "completed", "started_at": 100.0, "current_check_events": []},
            {"work_id": "w2", "client_session_id": "S", "kind": "implementation", "latest_status": "completed", "started_at": 200.0, "current_check_events": []},
        ],
        "current_check_events": [_hook_check("S", 250.0)],
    }
    _link_mechanical_checks_by_session_time({"tasks": [task]})
    # Both steps are check-relevant; the check ran at 250, so the most recent one
    # (w2, started 200) is credited, not w1.
    assert len(task["work_items"][1]["current_check_events"]) == 1
    assert task["work_items"][0]["current_check_events"] == []
    # And w2 now grades independently_checked (a hook-observed pass, not the agent's word).
    assert step_evidence_grade(task["work_items"][1])["grade"] == "independently_checked"


def test_link_skips_non_check_relevant_steps() -> None:
    # A docs step is never credited with a test pass — the check goes to the
    # active CHECK-RELEVANT step (the implementation a test actually exercises).
    task = {
        "task_id": "t1",
        "work_items": [
            {"work_id": "w1", "client_session_id": "S", "kind": "implementation", "latest_status": "completed", "started_at": 100.0, "current_check_events": []},
            {"work_id": "w2", "client_session_id": "S", "kind": "docs", "latest_status": "completed", "started_at": 200.0, "current_check_events": []},
        ],
        "current_check_events": [_hook_check("S", 250.0)],
    }
    _link_mechanical_checks_by_session_time({"tasks": [task]})
    assert step_evidence_grade(task["work_items"][0])["grade"] == "independently_checked"  # w1 impl
    assert not task["work_items"][1].get("current_check_events")  # w2 docs untouched


def test_link_does_not_attach_a_failing_hook_check() -> None:
    # A failing hook check is not guessed onto a step — no false demotion; it
    # stays task-level (visible on the decision axis).
    task = {
        "task_id": "t1",
        "work_items": [
            {"work_id": "w1", "client_session_id": "S", "kind": "implementation", "latest_status": "completed", "started_at": 100.0, "current_check_events": []},
        ],
        "current_check_events": [_hook_check("S", 250.0, result="failed")],
    }
    _link_mechanical_checks_by_session_time({"tasks": [task]})
    assert not task["work_items"][0].get("current_check_events")


def test_real_attach_then_link_lights_up_independently_checked() -> None:
    # The full projection flow: _attach routes a hook check to the task level
    # (no section id), then _link places it on the step active at its time — the
    # payoff the whole hook exists for.
    task = {
        "task_id": "t1",
        "session_keys": [{"client": "claude-code", "client_session_id": "S"}],
        "sessions": [{"client": "claude-code", "client_session_id": "S", "namespace_fingerprint": ""}],
        "work_items": [
            {"work_id": "w1", "client": "claude-code", "client_session_id": "S", "latest_status": "completed", "started_at": 100.0, "evidence_events": []},
            {"work_id": "w2", "client": "claude-code", "client_session_id": "S", "latest_status": "completed", "started_at": 200.0, "evidence_events": []},
        ],
    }
    proj = {"tasks": [task], "unresolved_work": []}
    check = {"event_id": "evidence:x", "source_type": "client_hook", "source": "claude-code",
             "client": "claude-code", "result": "passed", "client_session_id": "S", "created_at": 250.0,
             "evidence_type": "test", "check_identity": "client-hook:abc", "check_identity_stable": True}
    _attach_evidence_to_task_projection(proj, [check], require_namespace_for_client_hook=False)
    _link_mechanical_checks_by_session_time(proj)
    assert step_evidence_grade(task["work_items"][1])["grade"] == "independently_checked"
    assert not task["work_items"][0].get("current_check_events")


def test_link_leaves_a_pre_step_check_unattached() -> None:
    task = {
        "task_id": "t1",
        "work_items": [
            {"work_id": "w1", "client_session_id": "S", "latest_status": "completed", "started_at": 100.0, "current_check_events": []},
        ],
        "current_check_events": [_hook_check("S", 50.0)],  # ran before any step began
    }
    _link_mechanical_checks_by_session_time({"tasks": [task]})
    assert task["work_items"][0]["current_check_events"] == []  # honest: stays unattributed
