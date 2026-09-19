"""Display-quality and completeness rules for agent-recorded data.

Design: ``design-plans/data-quality/RULES.md``. Every rule here is enforced at
record time, because a record the UI cannot render is worse than a refusal the
agent can fix in one retry. The tests below pin both directions: the refusal for
an incomplete report, and the acceptance of every shape the real ledger actually
contains.

Measured basis (installed store, 8,369 events): 0 titles empty or over the field
cap, 5 of 536 terminal sections (0.9%) without a summary, 64 of 335 checks with
no command, 272 of 335 naming no files. The rules close those gaps without
refusing anything an agent legitimately records.
"""

from __future__ import annotations

import json
from typing import Any

from agentacct.mcp import SentinelMCPServer


def _call(server: SentinelMCPServer, name: str, arguments: dict[str, Any], msg_id: int = 1) -> dict[str, Any]:
    return server.handle_message(
        {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )


def _error(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    return None if error is None else str(error.get("message"))


def _stored(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, response.get("error")
    return json.loads(response["result"]["content"][0]["text"])


def _section(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "codex",
        "section_id": "rules-section",
        "section_status": "started",
        "section_title": "Enforce display rules",
        # An opening section now states the TASK's goal (what the work is FOR).
        # It lives in the shared helper because a section without one is no
        # longer a fully-formed opening record -- every test that is not about
        # the goal should send a complete one.
        "task_goal": "Display rules hold on every surface a reviewer reads.",
    }
    arguments.update(overrides)
    return _call(server, "agentacct_record_section", arguments)


def _check(server: SentinelMCPServer, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "source": "codex",
        "name": "pytest tests/test_mcp.py",
        "result": "passed",
        # evidence_type is present in 335 of 335 stored checks, and it is what
        # keeps the call on the evidence lane rather than the run-scoped
        # before/after outcome lane.
        "evidence_type": "test",
        "command": "pytest tests/test_mcp.py",
    }
    arguments.update(overrides)
    return _call(server, "agentacct_record_machine_check", arguments)


# --- R1: a displayed title must be real text --------------------------------


def test_whitespace_only_title_is_refused_with_the_reason(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title="   "))
    assert message is not None
    assert "readable text" in message and "whitespace" in message


def test_punctuation_only_title_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title="--- ..."))
    assert message is not None
    assert "at least 2 letters or digits" in message


def test_title_alias_is_held_to_the_same_rule(tmp_path) -> None:
    """`title` is the HTTP lane's name for the same field and must not be a
    loophole around the display rule."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title=None, title="  "))
    assert message is not None
    assert "readable text" in message or "at least 2 letters" in message


# --- R2: control characters never reach display -----------------------------


def test_newline_and_tab_in_a_title_become_one_line(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_section(server, section_title="Fix\nUI\tspacing"))
    assert stored["event"]["metadata"]["section_title"] == "Fix UI spacing"


def test_collapsing_happens_before_the_length_check(tmp_path) -> None:
    """160 characters of title plus stray whitespace is 160 characters of title."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    padded = "  " + ("a" * 79) + "\t \n" + ("b" * 79) + "  "
    stored = _stored(_section(server, section_title=padded))
    collapsed = stored["event"]["metadata"]["section_title"]
    assert collapsed == ("a" * 79) + " " + ("b" * 79)
    assert len(collapsed) == 159


def test_a_genuinely_long_title_is_still_refused_with_both_numbers(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_title="a" * 200))
    assert message is not None
    assert "160" in message and "200" in message


def test_narrative_newlines_are_preserved(tmp_path) -> None:
    """Summaries keep real line structure: it is how a mangled tool call is
    detected, and how a reader sees a list."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    summary = "Ran the focused suite after the change.\n\n- 3 passed\n- 0 failed"
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary=summary,
            files=["src/agentacct/mcp.py"],
        )
    )
    assert stored["event"]["metadata"]["summary"] == summary


# --- R4: a terminal section must carry its outcome --------------------------


def test_completed_section_without_a_summary_is_refused_with_a_fix(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="completed"))
    assert message is not None
    assert "summary" in message
    # The refusal must be actionable: it names the field and shows a corrected call.
    assert "agentacct_record_section(" in message
    assert "section_id=\"rules-section\"" in message


def test_completed_section_with_a_whitespace_only_summary_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="completed", summary=" \n\t ", files=["src/agentacct/api.py"]))
    assert message is not None
    assert "requires `summary`" in message


def test_completed_section_with_a_one_line_outcome_is_accepted(tmp_path) -> None:
    """Presence is the rule, not a character count: a real outcome can be short."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    outcome = "Bumped requests to 2.32.3; CI green."
    stored = _stored(_section(server, section_status="completed", summary=outcome, files=["pyproject.toml"]))
    assert stored["event"]["metadata"]["summary"] == outcome


def test_completed_section_with_a_real_summary_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _section(
            server,
            section_status="completed",
            summary="Added the rate limiter to the login endpoint and covered it with three tests.",
            files=["src/agentacct/api.py"],
        )
    )
    assert stored["event"]["metadata"]["section_status"] == "completed"


def test_blocked_section_requires_a_blocker_not_a_summary(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_section(server, section_status="blocked"))
    assert message is not None
    assert "blocker" in message
    stored = _stored(
        _section(
            server,
            section_status="blocked",
            blocker="The staging database rejects the migration without an owner role.",
            next_step="Ask the platform team to grant the owner role.",
            files=["migrations/0007_add_owner.sql"],
        )
    )
    assert stored["event"]["metadata"]["blocker"].startswith("The staging database")


def test_started_section_needs_no_summary(tmp_path) -> None:
    """The rule must not make the normal opening call harder."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_section(server, section_status="started"))
    assert stored["event"]["metadata"]["section_status"] == "started"


# --- R5/R6: a machine check must be reproducible and identifiable -----------


def test_check_without_command_files_or_artifact_is_refused(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(
        _call(
            server,
            "agentacct_record_machine_check",
            {"source": "codex", "result": "passed", "evidence_type": "test", "name": "login smoke test"},
        )
    )
    assert message is not None
    assert "command" in message and "files" in message
    # And it names the honest alternative for a manual observation.
    assert "agentacct_record_event" in message


def test_check_with_only_a_command_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, command="pytest -q", files=None, name="pytest -q"))
    assert stored["event"]["metadata"]["command"] == "pytest -q"


def test_check_with_only_files_is_accepted(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, command=None, files=["tests/test_mcp.py"], name="test_mcp suite"))
    assert stored["event"]["metadata"]["files"] == ["tests/test_mcp.py"]


def test_files_naming_only_the_project_root_do_not_satisfy_the_rule(tmp_path) -> None:
    """'files: ["."]' survives validation but stores nothing, so it cannot be the
    evidence a check rests on."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_check(server, command=None, files=["."], name="root files check"))
    assert message is not None
    assert "command" in message


def test_before_after_outcome_lane_stays_supported(tmp_path) -> None:
    """A repair recorded as before/after exit codes is evidence and must not be
    refused for naming no command.

    The outcome lane additionally requires an existing run (the server resolves
    run_id="latest" against the store), so this call also carries the files the
    smoke check covered -- which is the shape the CLI writes when it knows them.
    """
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(
        _call(
            server,
            "agentacct_record_machine_check",
            {
                "source": "codex",
                "name": "smoke",
                "evidence_type": "smoke",
                "result": "passed",
                "files": ["scripts/smoke.sh"],
                "summary": "Recorded the before/after exit codes for the smoke script.",
            },
        )
    )
    assert "error" not in stored, stored.get("error")
    assert stored["event"]["metadata"]["files"] == ["scripts/smoke.sh"]


def test_check_with_no_name_command_or_files_is_refused_with_an_example(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    message = _error(_call(server, "agentacct_record_machine_check", {"source": "codex", "result": "passed"}))
    assert message is not None
    # The refusal must show a LABEL alongside a command, never a command as a
    # name: the old example ('name="pytest tests/test_mcp.py"') is exactly what
    # taught agents to put the command in the name field.
    assert "no `name`" in message
    assert 'name="percentage() rounds half-up"' in message
    assert 'command="python -m pytest tests/test_percent.py"' in message


def test_a_short_check_name_is_accepted_as_the_agent_wrote_it(tmp_path) -> None:
    """The label is the agent's to choose; the rule is only that one exists."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    stored = _stored(_check(server, name="e2e", command=None, exit_code=0))
    assert stored["event"]["metadata"]["name"] == "e2e"
    stored = _stored(_check(server, name="check", command="pytest -q"))
    assert stored["event"]["metadata"]["name"] == "check"


# --- every shape the real store contains must still be accepted -------------


def test_real_ledger_shapes_are_accepted(tmp_path) -> None:
    """Regression guard against over-strictness: these mirror the measured
    records (median title 40 characters, median summary 207, checks named
    "pytest", "pnpm build:web", artifact checks with paths)."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    accepted = [
        _section(
            server,
            section_id="real-1",
            section_status="completed",
            section_title="Coordinate 100 native macOS design reviews",
            summary="Ran the ten review rounds and recorded each disposition with its source hash.",
            # A review step changes no files, and the contract says so by name
            # rather than making the agent invent a path.
            kind="review",
        ),
        _section(
            server,
            section_id="real-2",
            section_status="completed",
            section_title="Evaluate masked native information candidates",
            summary="Compared the masked candidates against the measured ledger and kept the two that agreed.",
            files=["src/agentacct/mcp.py"],
        ),
        _check(server, name="pytest tests/test_mcp.py", command="pytest tests/test_mcp.py", section_id="real-1"),
        _check(
            server,
            name="pnpm build:web",
            command="pnpm build:web",
            evidence_type="build",
            section_id="real-1",
            files=["apps/web/src/main.tsx"],
        ),
    ]
    for response in accepted:
        assert "error" not in response, response.get("error")


def _server_with_a_run(tmp_path) -> tuple[SentinelMCPServer, str]:
    """A store with one real run.

    The before/after summaries put a call on the run-scoped OUTCOME lane, which
    resolves run_id="latest" against the store. That lane is not what these two
    tests are about -- they are about whether a blank summary counts as evidence
    -- so the run exists purely to let the call reach the rule.
    """
    from agentacct.runner import RunOptions, start_guarded_run

    dummy = tmp_path / "mcp_dummy.py"
    dummy.write_text("print('ok')\n", encoding="utf-8")
    store_root = tmp_path / "state"
    result = start_guarded_run(["python", str(dummy)], RunOptions(store_dir=store_root, poll_interval=0.05))
    return SentinelMCPServer(store_dir=store_root), result.run_id


def test_a_blank_outcome_summary_is_not_evidence(tmp_path) -> None:
    """An empty before/after summary must not satisfy the evidence rule.

    A client sending `before_summary=""` means "nothing here". Treating the key's
    mere presence as evidence would let a check pass reproducibility on a blank
    field -- the silent hole this rule exists to close.
    """
    server, run_id = _server_with_a_run(tmp_path)
    message = _error(_call(server, "agentacct_record_machine_check", {
        "source": "codex", "run_id": run_id, "name": "smoke", "result": "passed",
        "evidence_type": "smoke", "section_id": "blank-outcome",
        "before_summary": "", "after_summary": "   ",
    }))
    assert message is not None, "a blank outcome pair must not be accepted as evidence"
    assert "command" in message and "files" in message


def test_a_real_outcome_summary_is_evidence(tmp_path) -> None:
    """The other direction: a repair recorded with real summaries still lands."""
    server, run_id = _server_with_a_run(tmp_path)
    stored = _stored(_call(server, "agentacct_record_machine_check", {
        "source": "codex", "run_id": run_id, "name": "smoke", "result": "passed",
        "evidence_type": "smoke", "section_id": "real-outcome",
        "before_summary": "failed before", "after_summary": "passed after",
    }))
    assert stored["event"]["metadata"]["result"] == "passed"


# --- Write-time advisories (non-blocking, never a refusal) --------------------

from agentacct.display_budget import CARD_TITLE_CHARACTERS  # noqa: E402
from agentacct.semantic_rules import check_advisories, section_advisories  # noqa: E402


def test_failed_with_exit_code_zero_and_no_artifact_is_advised_but_stored(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(
        _check(
            server,
            result="failed",
            exit_code=0,
            name="reproduce the release build failure",
            summary="Ran the release build 3 times; it succeeded every time and the reported error never appeared.",
            # Declared, so the exit-code advisory is the only one under test:
            # `rest_of_work` outranks it, and the response is capped at two.
            rest_of_work="usable",
        )
    )
    # Stored exactly as sent: an advisory never rewrites or refuses a record.
    assert payload["event"]["metadata"]["result"] == "failed"
    codes = [advisory["code"] for advisory in payload["advisories"]]
    assert codes == ["failed_with_exit_code_zero"]
    assert any("exit_code=0" in warning for warning in payload["warnings"])


def test_failed_with_exit_code_zero_is_not_advised_when_an_artifact_shows_the_defect() -> None:
    assert check_advisories(name="the release build reproduces the failure", result="failed", exit_code=0, artifact_path="out/report.txt", rest_of_work="usable") == []
    assert check_advisories(name="the release build reproduces the failure", result="failed", exit_code=1, command="pnpm build", rest_of_work="usable") == []
    assert check_advisories(name="the release build reproduces the failure", result="unknown", exit_code=0, command="pnpm build") == []
    assert check_advisories(name="the release build reproduces the failure", result="failed", exit_code=None, command="pnpm build", rest_of_work="usable") == []


def test_over_budget_check_name_and_section_title_are_advised_but_stored(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    long_name = "pytest " + "tests/test_mcp.py " * 5
    assert len(long_name.strip()) > CARD_TITLE_CHARACTERS
    check_payload = _stored(_check(server, name=long_name))
    assert check_payload["event"]["metadata"]["name"] == long_name.strip()
    assert [a["code"] for a in check_payload["advisories"]] == ["title_over_card_budget"]
    assert check_payload["advisories"][0]["field"] == "name"
    assert "about 54 characters; lead with the distinguishing words" in check_payload["advisories"][0]["hint"]

    long_title = "Implementation of the login rate limiting work across every route"
    assert len(long_title) > CARD_TITLE_CHARACTERS
    section_payload = _stored(_section(server, section_title=long_title))
    assert section_payload["event"]["metadata"]["section_title"] == long_title
    assert [a["field"] for a in section_payload["advisories"]] == ["section_title"]
    assert any("lead with the distinguishing words" in w for w in section_payload["warnings"])


def test_titles_within_the_card_budget_are_not_advised(tmp_path) -> None:
    assert section_advisories(section_title="x" * CARD_TITLE_CHARACTERS) == []
    assert check_advisories(name="y" * CARD_TITLE_CHARACTERS, result="passed", exit_code=0, command="pytest -q") == []
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    assert "advisories" not in _stored(_section(server))
    assert "advisories" not in _stored(_check(server))


def test_a_check_without_a_summary_gets_no_synthesized_summary(tmp_path) -> None:
    """The server no longer invents '<name>: <result>': a missing summary stays
    absent, and the result stays in its own field."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    metadata = _stored(_check(server))["event"]["metadata"]
    assert metadata.get("summary") in (None, "")
    assert metadata["result"] == "passed"


def test_check_schema_describes_result_evidence_type_and_section_kind() -> None:
    from agentacct.mcp import TOOLS

    by_name = {tool["name"]: tool["inputSchema"]["properties"] for tool in TOOLS}
    check = by_name["agentacct_record_machine_check"]
    assert check["result"]["description"].startswith(
        "Verdict on the work: passed = the work does what was claimed; failed = the check shows a defect "
        "in the work; not_reproduced = the probe RAN and the reported problem did not appear; error = "
        "the check could not run"
    )
    assert "typecheck" in check["evidence_type"]["description"]
    kind = by_name["agentacct_record_section"]["kind"]["description"]
    assert "review/research/planning/docs steps are not check-relevant" in kind


def test_work_status_does_not_ask_a_review_step_for_a_check(tmp_path) -> None:
    """C68: work_status uses the receipt's step_is_checkable rule, so a
    completed review step with no check is not listed as owing one, while an
    implementation step still is."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    outcome = "Fixed the login redirect in auth/login.py and covered it with a regression test"
    _stored(_section(server, section_id="review-step", section_status="completed", kind="review",
                     client_session_id="sess-c68", summary=outcome))
    _stored(_section(server, section_id="impl-step", section_status="completed", kind="implementation",
                     client_session_id="sess-c68", summary=outcome, files=["auth/login.py"]))
    status = _stored(_call(server, "agentacct_work_status", {"client_session_id": "sess-c68"}))
    owing = [item["section_id"] for item in status["completed_without_evidence"]]
    assert owing == ["impl-step"]
    assert status["counts"]["completed_without_evidence"] == 1


def test_work_status_shows_the_reviewer_headline_and_standing_checks(tmp_path) -> None:
    """K62: before finishing, work_status projects the session's Task through
    the receipt reducer, so an agent sees the headline the reviewer will see
    and every standing check — and is never told to record a pass."""
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    session, ns = "sess-k62", "sha256:k62-ns"
    scope = {
        "client": "claude-code",
        "client_session_id": session,
        "session_namespace_fingerprint": ns,
        "identity_scope_state": "explicit",
        "project_dir": "/tmp/project",
    }
    server.service.record_event(
        {
            "event_id": "evt_usage_k62",
            "created_at": 100.0,
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "provider": "claude-code",
            "model": "claude-opus-4-8",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "usage_confidence": "client_reported",
            "metadata": {
                **scope,
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "started_at": 100.0,
                "updated_at": 100.0,
                "source_namespace_fingerprint": ns,
            },
        },
        trusted_usage_import=True,
    )
    server.service.record_event(
        {
            "event_id": "evt_section_k62_completed",
            "created_at": 101.0,
            "source": "claude-code",
            "event_type": "section_completed",
            "metadata": {
                **scope,
                "sentinel_semantic_kind": "section",
                "client_context_keys_authored": ["client_session_id"],
                "section_id": "impl",
                "section_status": "completed",
                "section_title": "Fix sign placement",
                "kind": "implementation",
                "summary": "Moved the sign before the currency symbol in format_amount.",
                "files": ["moneyutil/format.py"],
            },
        }
    )
    server.service.record_event(
        {
            "event_id": "evt_check_k62_ruff",
            "created_at": 102.0,
            "source": "claude-code",
            "event_type": "machine_check",
            "metadata": {
                **scope,
                "sentinel_semantic_kind": "evidence",
                "result": "error",
                "evidence_type": "lint",
                "name": "moneyutil lints clean",
                "summary": "ruff aborted: unknown rule selector 'RUF200' in pyproject.toml, so nothing was linted.",
                "exit_code": 1,
                "section_id": "impl",
            },
        }
    )
    status = _stored(_call(server, "agentacct_work_status", {"client_session_id": session}))
    assert status["tasks"], status
    task = status["tasks"][0]
    assert task["task_headline"]
    assert "Finding" not in task["task_headline"]
    assert task["standing_attention"] == [
        {"reason_label": "Check could not run", "label": "Lint check · moneyutil lints clean · exit 1"}
    ]
    advice = " ".join(status["what_to_do_next"])
    assert "re-run the same check" in advice
    assert "Never record it as passed" in advice
    assert "record it as passed" not in advice.replace("Never record it as passed", "")


# --- Mechanical git-revision capture (never a self-reported SHA) --------------
import subprocess  # noqa: E402


def _git_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(path)}
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True, env={**env})
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    run("add", "f.txt")
    run("commit", "-q", "-m", "init")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def test_machine_check_stamps_the_server_captured_git_revision(tmp_path) -> None:
    repo = tmp_path / "repo"
    head = _git_repo(repo)
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(_check(server, project_dir=str(repo)))
    meta = payload["event"]["metadata"]
    assert meta["git_commit"] == head
    assert meta["git_revision_basis"] == "server_captured_at_record"
    assert meta["git_dirty"] is False  # clean tree right after commit


def test_machine_check_never_stamps_git_for_a_non_repo_project_dir(tmp_path) -> None:
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(_check(server, project_dir=str(tmp_path / "not-a-repo")))
    meta = payload["event"]["metadata"]
    # No repo -> no revision noise at all (receipt reads that as "not captured").
    assert "git_revision_basis" not in meta
    assert "git_commit" not in meta


def test_a_caller_supplied_git_commit_is_stripped_not_trusted(tmp_path) -> None:
    # record_section accepts free-form metadata, so it is the lane where a model
    # could try to smuggle a SHA. The server-read revision must win and the
    # smuggled value must be stripped and recorded as stripped.
    repo = tmp_path / "repo"
    head = _git_repo(repo)
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(
        _section(
            server,
            section_status="completed",
            summary="Added the rate limiter to the login endpoint and covered it with tests.",
            project_dir=str(repo),
            kind="review",
            metadata={"git_commit": "deadbeefdeadbeef", "git_revision_basis": "host_hook"},
        )
    )
    meta = payload["event"]["metadata"]
    # The server value wins; the smuggled SHA and basis never survive.
    assert meta["git_commit"] == head
    assert meta["git_commit"] != "deadbeefdeadbeef"
    assert meta["git_revision_basis"] == "server_captured_at_record"
    assert "git_commit" in (meta.get("reserved_context_keys_stripped") or [])


def test_machine_check_tool_does_not_accept_free_form_metadata(tmp_path) -> None:
    # Defense in depth: the check tool has no metadata argument at all, so there
    # is no free-form lane to smuggle a revision through in the first place.
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    assert _error(_check(server, metadata={"git_commit": "deadbeef"})) is not None


def test_declared_files_absent_at_the_stamped_revision_are_recorded(tmp_path) -> None:
    """The revision basis is ``server_captured_at_record``: HEAD is read when the
    call ARRIVES. An agent that records a check before it commits therefore
    stamps the commit BEFORE its own work. When the check declares files, one
    ``git ls-tree`` proves the stamp cannot be what ran — so the contradiction is
    recorded instead of the receipt quietly asserting the revision.
    """

    repo = tmp_path / "repo"
    _git_repo(repo)
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_subtract.py").write_text("def test(): ...\n", encoding="utf-8")
    payload = _stored(
        _check(server, project_dir=str(repo), files=["f.txt", "tests/test_subtract.py"])
    )
    meta = payload["event"]["metadata"]
    # f.txt is in the stamped commit; the brand-new test file is not.
    assert meta["git_declared_files_absent"] == ["tests/test_subtract.py"]


def test_a_check_whose_declared_files_all_exist_records_no_contradiction(tmp_path) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(_check(server, project_dir=str(repo), files=["f.txt"]))
    meta = payload["event"]["metadata"]
    # Nothing contradicted: the key is simply absent (the store drops nulls),
    # never a fabricated list and never an empty-list "verified" claim.
    assert meta.get("git_declared_files_absent") is None


def test_a_caller_cannot_smuggle_its_own_absent_file_verdict(tmp_path) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)
    server = SentinelMCPServer(store_dir=tmp_path / "state")
    payload = _stored(
        _section(
            server,
            section_status="completed",
            summary="Added the rate limiter to the login endpoint and covered it with tests.",
            project_dir=str(repo),
            kind="review",
            metadata={"git_declared_files_absent": ["invented.py"]},
        )
    )
    meta = payload["event"]["metadata"]
    assert meta.get("git_declared_files_absent") != ["invented.py"]
    assert "git_declared_files_absent" in (meta.get("reserved_context_keys_stripped") or [])
