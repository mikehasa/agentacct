"""First-run UX: friendly one-line failures instead of stack dumps (Phase 3, item 5).

A user who runs `report`/`judge`/`value`/`pause` right after `init` (empty
store), or mistypes a run id, must get one actionable sentence — never a
Typer/rich traceback — and read-only commands must never create store
directories as a side effect.
"""

import errno
import json
import os

from typer.testing import CliRunner

from agentacct.cli import app

runner = CliRunner()


def _make_run(store_dir, run_id="run_demo", *, owned=True, report_md="demo report", process_group_id=None):
    run_dir = store_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "owned_by_sentinel": owned,
                "process_group_id": process_group_id,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    if report_md is not None:
        (run_dir / "report.md").write_text(report_md, encoding="utf-8")
    return run_dir


def _assert_friendly_failure(result, *fragments):
    assert result.exit_code == 1, result.output
    for fragment in fragments:
        assert fragment in result.output, result.output
    assert "Traceback" not in result.output, result.output


def test_report_latest_on_empty_store_is_one_friendly_line(tmp_path):
    store = tmp_path / "state"
    store.mkdir()

    result = runner.invoke(app, ["report", "latest", "--store-dir", str(store)])

    _assert_friendly_failure(result, "No runs recorded in this store yet", "agentacct demo", "agentacct run --")


def test_report_unknown_run_id_names_it_and_suggests_runs(tmp_path):
    _make_run(tmp_path)

    result = runner.invoke(app, ["report", "run_missing", "--store-dir", str(tmp_path)])

    _assert_friendly_failure(result, "unknown run_id: run_missing", "agentacct runs")


def test_report_json_unknown_run_id_is_friendly_too(tmp_path):
    result = runner.invoke(app, ["report", "run_missing", "--store-dir", str(tmp_path), "--json"])

    _assert_friendly_failure(result, "unknown run_id: run_missing", "agentacct runs")


def test_run_lookup_commands_are_friendly_on_an_empty_store(tmp_path):
    for command in (
        ["judge", "prepare", "latest"],
        ["value", "compute", "latest"],
        ["outcome", "record-machine-check", "latest", "--after-exit-code", "0"],
    ):
        store = tmp_path / ("state-" + command[0])
        store.mkdir()
        result = runner.invoke(app, [*command, "--store-dir", str(store)])
        _assert_friendly_failure(result, "No runs recorded in this store yet")


def test_pause_resume_kill_unknown_run_is_friendly(tmp_path):
    for command in ("pause", "resume", "kill"):
        result = runner.invoke(app, [command, "run_missing", "--store-dir", str(tmp_path)])
        _assert_friendly_failure(result, "unknown run_id: run_missing", "agentacct runs")


def test_pause_keeps_the_ownership_refusal_message_without_a_traceback(tmp_path):
    _make_run(tmp_path, run_id="run_foreign", owned=False)

    result = runner.invoke(app, ["pause", "run_foreign", "--store-dir", str(tmp_path)])

    _assert_friendly_failure(result, "run run_foreign is not owned by agent-chronicle; refusing to control it")


def test_read_only_commands_never_create_store_directories(tmp_path):
    # A typo'd --store-dir on a read command must not leave a stray store.
    for command in (
        ["report", "latest"],
        ["runs"],
        ["judge", "prepare", "latest"],
        ["value", "compute", "latest"],
        ["pause", "run_x"],
        ["resume", "run_x"],
        ["kill", "run_x"],
        ["event", "list"],
        ["event", "summary"],
    ):
        absent = tmp_path / "no-such-store" / command[0]
        result = runner.invoke(app, [*command, "--store-dir", str(absent)])
        assert not absent.exists(), f"{command} created {absent}"
        assert "Traceback" not in result.output, (command, result.output)


def test_runs_command_lists_runs_and_hints_on_empty_store(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    listed = runner.invoke(app, ["runs", "--store-dir", str(empty)])
    assert listed.exit_code == 0, listed.output
    assert "No runs recorded in this store yet" in listed.output

    _make_run(tmp_path)
    result = runner.invoke(app, ["runs", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "run_demo" in result.output
    assert "status=completed" in result.output

    as_json = runner.invoke(app, ["runs", "--store-dir", str(tmp_path), "--json"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["runs"][0]["run_id"] == "run_demo"


def test_usage_import_local_zero_scan_explains_why_and_what_next(tmp_path):
    store = tmp_path / "state"
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()

    result = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store),
            "--codex-home",
            str(empty_home / ".codex"),
            "--claude-home",
            str(empty_home / ".claude"),
            "--opencode-home",
            str(empty_home / ".opencode"),
            "--hermes-home",
            str(empty_home / ".hermes"),
            "--openclaw-home",
            str(empty_home / ".openclaw"),
            "--cursor-home",
            str(empty_home / "Cursor"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No local client session files found" in result.output
    assert "agentacct usage discover-sources" in result.output
    assert "--claude-home" in result.output


def test_usage_import_local_zero_scan_hint_reflects_actual_scanned_homes(tmp_path):
    # With a single --client and a custom home, the hint must name ONLY what was
    # scanned, never the five default homes the run did not touch.
    store = tmp_path / "state"
    custom = tmp_path / "custom-claude"

    result = runner.invoke(
        app,
        ["usage", "import-local", "--store-dir", str(store), "--client", "claude-code", "--claude-home", str(custom)],
    )

    assert result.exit_code == 0, result.output
    assert "No local client session files found" in result.output
    assert str(custom) in result.output
    # The four unscanned default homes must NOT be claimed as checked.
    for unscanned in ("~/.codex", "~/.local/share/opencode", "~/.hermes", "~/.openclaw"):
        assert unscanned not in result.output, (unscanned, result.output)


def test_usage_import_local_symlink_does_not_zero_the_import(tmp_path):
    # issue #84: a directory symlink under ~/.claude/projects (a common shared-
    # memory sync pattern) used to abort the whole Claude import with a
    # misleading "No local client session files found", even though real
    # transcripts sat right beside it. The real session must import, and the
    # skipped symlink must be surfaced rather than silently swallowing the home.
    store = tmp_path / "state"
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-Users-me"
    project.mkdir(parents=True)
    session = project / "real-session.jsonl"
    session.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"type": "ai-title", "sessionId": "real-session", "aiTitle": "Real work"},
                {
                    "type": "assistant",
                    "sessionId": "real-session",
                    "message": {
                        "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 12, "output_tokens": 4},
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    shared_memory = tmp_path / "shared-memory"
    shared_memory.mkdir()
    (project / "memory").symlink_to(shared_memory, target_is_directory=True)

    result = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store),
            "--client",
            "claude-code",
            "--claude-home",
            str(claude_home),
        ],
    )

    assert result.exit_code == 0, result.output
    # The real transcript imports rather than the whole home returning zero.
    assert "Imported 1 local client usage session(s)" in result.output
    assert "No local client session files found" not in result.output
    # The skipped symlink is surfaced, not silently swallowed.
    assert "Skipped 1 symlinked path(s)" in result.output


def test_usage_import_local_all_symlink_home_explains_the_skip_not_just_zero(tmp_path):
    # issue #84, the pure shape: a home whose ONLY project entry is a directory
    # symlink and no real transcript. The scan legitimately finds zero sessions,
    # but the zero-result message must now explain WHY (skipped unsafe symlink)
    # instead of the bare, misleading "No local client session files found" that
    # originally sent the reporter down the wrong path.
    store = tmp_path / "state"
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-Users-me"
    project.mkdir(parents=True)
    shared_memory = tmp_path / "shared-memory"
    shared_memory.mkdir()
    (project / "memory").symlink_to(shared_memory, target_is_directory=True)

    result = runner.invoke(
        app,
        [
            "usage",
            "import-local",
            "--store-dir",
            str(store),
            "--client",
            "claude-code",
            "--claude-home",
            str(claude_home),
        ],
    )

    assert result.exit_code == 0, result.output
    # Zero real sessions in this home — the misleading bare message still appears,
    # but now carries the reason so it is no longer baffling.
    assert "No local client session files found" in result.output
    assert "Skipped 1 symlinked path(s)" in result.output
    assert "skipped as unsafe symlinks" in result.output


def test_runs_command_omits_empty_started_field_for_runner_runs(tmp_path):
    # Runner metadata has no wall-clock started_at, so the human listing must
    # not print a dead empty `started=` column.
    _make_run(tmp_path, run_id="run_no_start")
    result = runner.invoke(app, ["runs", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "run_no_start" in result.output
    assert "started=" not in result.output


def test_cost_read_commands_never_create_store_directories(tmp_path):
    # The read-only cost surface must honor the same no-stray-store invariant as
    # the other read commands (CostLedger/SubscriptionStore no longer mkdir).
    for command in (
        ["cost", "status"],
        ["cost", "subscription-list"],
        ["cost", "subscription-estimate", "--subscription-price-usd", "20"],
    ):
        absent = tmp_path / "no-such-store" / command[1]
        result = runner.invoke(app, [*command, "--store-dir", str(absent)])
        assert not absent.exists(), f"{command} created {absent}"
        assert "Traceback" not in result.output, (command, result.output)


def test_event_list_and_summary_hint_at_next_step_on_empty_store(tmp_path):
    listed = runner.invoke(app, ["event", "list", "--store-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert "No events recorded." in listed.output
    assert "usage import-local" in listed.output

    summary = runner.invoke(app, ["event", "summary", "--store-dir", str(tmp_path)])
    assert summary.exit_code == 0, summary.output
    assert "usage import-local" in summary.output


def test_event_hints_do_not_appear_once_events_exist(tmp_path):
    recorded = runner.invoke(
        app,
        ["event", "record", "--store-dir", str(tmp_path), "--source", "test", "--event-type", "note"],
    )
    assert recorded.exit_code == 0, recorded.output

    listed = runner.invoke(app, ["event", "list", "--store-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert "usage import-local" not in listed.output

    summary = runner.invoke(app, ["event", "summary", "--store-dir", str(tmp_path)])
    assert summary.exit_code == 0, summary.output
    assert "usage import-local" not in summary.output


def test_control_commands_on_an_exited_run_are_friendly_not_a_traceback(tmp_path, monkeypatch):
    # The most likely first-contact control mistake: `demo` (its run exits in
    # ~0.2s) then `pause`/`resume`/`kill` that run -> os.killpg raises
    # ProcessLookupError (ESRCH), which is an OSError but NEITHER
    # FileNotFoundError NOR PermissionError, so it escaped the friendly wrapper
    # into a rich traceback. It must now be one actionable sentence.
    def _dead_pgid(_pgid, _sig):
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(os, "killpg", _dead_pgid)

    for command in ("pause", "resume", "kill"):
        store = tmp_path / ("state-" + command)
        _make_run(store, run_id="run_dead", process_group_id=424242)
        result = runner.invoke(app, [command, "run_dead", "--store-dir", str(store)])
        _assert_friendly_failure(result, "run_dead", "already exited")


def test_control_command_on_a_foreign_process_group_names_the_run(tmp_path, monkeypatch):
    # killpg EPERM (a stale pgid now owned by another process) was swallowed by
    # the ownership-refusal PermissionError arm and printed as a bare
    # "[Errno 1] Operation not permitted" with no run id. It must name the run.
    def _eperm(_pgid, _sig):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", _eperm)
    _make_run(tmp_path, run_id="run_foreign_pgid", process_group_id=424243)

    result = runner.invoke(app, ["kill", "run_foreign_pgid", "--store-dir", str(tmp_path)])

    _assert_friendly_failure(result, "run_foreign_pgid")


def test_malformed_run_id_is_friendly_across_run_commands(tmp_path):
    # A slash, space, or pasted run-dir path is a plausible first-run typo
    # (demo prints full run paths). validate_run_id raises ValueError, which
    # bypassed the wrapper into a traceback; it must be one actionable line.
    _make_run(tmp_path)  # a valid run so `latest` and the store both resolve
    for command in (
        ["report", "../evil"],
        ["value", "compute", "my run"],
        ["judge", "prepare", "a/b"],
        ["pause", "../../etc"],
        ["resume", "with space"],
        ["kill", "a/b/c"],
        ["outcome", "record-machine-check", "bad id", "--after-exit-code", "0"],
    ):
        result = runner.invoke(app, [*command, "--store-dir", str(tmp_path)])
        _assert_friendly_failure(result, "Not a valid run id", "agentacct runs")


def test_typer_app_never_dumps_local_variables():
    # Belt-and-braces: if an exception ever escapes a command, rich must not
    # dump local variables (paths, payloads) at the user.
    assert app.pretty_exceptions_show_locals is False
