"""Kimi Code hook pack: the capture paths, the client-context file, the
config.toml ``[[hooks]]`` writer, the wrapper, and the CLI subcommands.

Why this pack exists: a live Kimi Code session calls the agentacct MCP tools, but
Kimi Code hands NO session id to the model or to MCP servers, so every section it
records arrives with an empty ``client_session_id`` and joins nothing (measured on
this machine). Its hook stdin JSON is the only channel carrying one, and its
``session_id`` shape (``session_<uuid>``) is exactly the value the kimi-code usage
importer records — so a tick or an inherited context attributes with no log
pairing, the same mechanism as the Claude Code / Codex bridges.

Every payload in this file uses the vendor's documented shape
(``hook_event_name`` / ``session_id`` / ``session_title`` / ``client_type`` /
``cwd``, plus ``tool_name`` + ``tool_input`` on PreToolUse). Nothing here touches
the real ``~/.kimi-code`` or a real store: every path is a tmp dir.
"""

from __future__ import annotations

import ast
import json

import time
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentacct import cli
from agentacct.cli import (
    _install_kimi_code_hook,
    _write_kimi_code_hooks_config_at,
    app,
)
from agentacct.hooks import (
    HOOK_CONTEXT_CLIENTS,
    KIMI_CODE_CONFIG_RELATIVE_PATH,
    KIMI_CODE_HOOK_EVENTS,
    KIMI_CODE_HOOK_RELATIVE_PATH,
    capture_claude_code_client_context,
    claude_code_hook_context_dir,
    claude_code_hook_context_path,
    clear_ended_session_hook_context,
    cwd_digest,
    kimi_code_hook_command,
    kimi_code_hook_doctor_checks,
    kimi_code_hooks_toml_block,
    kimi_code_hooks_unsupported_fields,
    kimi_code_session_workdir,
    load_claude_code_hook_contexts,
    process_ancestor_pids,
    render_kimi_code_hook_wrapper,
    select_claude_code_hook_context,
    write_claude_code_hook_context,
)
from agentacct.session_lifecycle import drain_session_end_spool
from agentacct.tool_activity import drain_tool_activity_spool

WRAPPER_BASENAME = KIMI_CODE_HOOK_RELATIVE_PATH.name
_SESSION = "session_abc123"


def _payload(event: str = "PreToolUse", *, session_id: str = _SESSION, **extra: object) -> str:
    """A Kimi Code hook payload in the vendor's documented shape."""
    body: dict[str, object] = {
        "hook_event_name": event,
        "session_id": session_id,
        "session_title": "Fix the login page",
        "client_type": "kimi_code_cli",
        "cwd": "/Users/dev/proj",
    }
    body.update(extra)
    return json.dumps(body)


def _pre_tool(
    tool_name: str = "Bash",
    tool_input: dict[str, object] | None = None,
    *,
    session_id: str = _SESSION,
    cwd: str = "/Users/dev/proj",
) -> str:
    return _payload(
        "PreToolUse",
        session_id=session_id,
        cwd=cwd,
        tool_name=tool_name,
        tool_input=tool_input if tool_input is not None else {"command": "pytest -q"},
    )


# ---------------------------------------------------------------------------
# Capture: one PreToolUse -> one tool tick + one context file
# ---------------------------------------------------------------------------


def test_kimi_code_pre_tool_use_records_a_tool_tick_and_context_file(tmp_path: Path) -> None:
    store = tmp_path / "store"
    result = CliRunner().invoke(
        app,
        ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
        input=_pre_tool("Bash", {"command": "pytest -q"}),
    )
    assert result.exit_code == 0, result.output
    # Observe-only: Kimi Code reads exit 0 + `{}` as allow, so agentacct can never
    # veto a tool call it merely observes.
    assert result.output.strip() == "{}"

    # 1. The tool-activity tick, labelled with the Kimi Code client and the
    #    authoritative session id from the payload (never guessed).
    [event] = drain_tool_activity_spool(store, now=100.0, token="t")
    metadata = event["metadata"]
    assert event["source"] == "kimi-code"
    assert metadata["client"] == "kimi-code"
    assert metadata["client_session_id"] == _SESSION
    assert metadata["tool_category_counts"] == {"execute": 1}
    assert {"name": "Bash", "count": 1} in metadata["tool_names"]

    # 2. The client-context file the MCP server reads join ids from.
    context = json.loads((store / "client-context" / "kimi-code.json").read_text(encoding="utf-8"))
    assert context["client"] == "kimi-code"
    assert context["client_session_id"] == _SESSION
    assert context["schema_version"] == "agent-sentinel.client-context.v1"
    assert context["hook_event_name"] == "PreToolUse"
    assert context["observed_at"] > 0
    # Ancestry of the HOOK process (integers only): the MCP server proves which
    # concurrent session is its own with it.
    ancestors = context["hook_ancestor_pids"]
    assert isinstance(ancestors, list)
    assert all(isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 for pid in ancestors)
    if process_ancestor_pids():
        # `ps` is available here, so the hook must have captured its chain.
        assert ancestors, "hook ancestry was available but not captured"
    # The per-session slot exists too: with two concurrent sessions the single
    # per-client slot cannot say which id belongs to which process.
    per_session = list(claude_code_hook_context_dir(store, "kimi-code").glob("*.json"))
    assert len(per_session) == 1

    # Privacy: tool arguments and output never reach the context file.
    assert "pytest -q" not in json.dumps(context)
    assert "session_title" not in json.dumps(context)  # the user's own words stay out


def test_kimi_code_pre_tool_use_cleans_edit_and_execute_arguments(tmp_path: Path) -> None:
    store = tmp_path / "store"
    runner = CliRunner()
    runner.invoke(
        app,
        ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
        input=_pre_tool(
            "Edit",
            {"file_path": "/Users/dev/proj/src/app.py", "old_string": "SUPER-SECRET", "new_string": "SUPER-SECRET-2"},
            session_id="session_edit",
        ),
    )
    runner.invoke(
        app,
        ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
        input=_pre_tool("Bash", {"command": "curl -H 'Authorization: Bearer tok'"}, session_id="session_exec"),
    )
    events = drain_tool_activity_spool(store, now=100.0, token="t")
    by_session = {event["metadata"]["client_session_id"]: event["metadata"] for event in events}

    # Edit: the destination path, relativized to cwd — never the file content.
    assert by_session["session_edit"]["tool_category_counts"] == {"edit": 1}
    assert by_session["session_edit"]["touched_files"] == ["src/app.py"]
    assert "SUPER-SECRET" not in json.dumps(events)

    # Execute: the command only (single-lined and scrubbed), no other argument.
    assert by_session["session_exec"]["tool_category_counts"] == {"execute": 1}
    [command] = by_session["session_exec"]["commands"]
    assert command.startswith("curl")
    assert "tok" not in command  # the Authorization header value was masked
    assert "‹redacted›" in command


def test_kimi_code_session_start_writes_context_without_a_tick(tmp_path: Path) -> None:
    store = tmp_path / "store"
    result = CliRunner().invoke(
        app,
        ["hooks", "kimi-code", "session-start", "--store-dir", str(store)],
        input=_payload("SessionStart", source="startup", model="kimi-k3"),
    )
    assert result.exit_code == 0
    assert result.output.strip() == "{}"
    context = json.loads((store / "client-context" / "kimi-code.json").read_text(encoding="utf-8"))
    assert context["client_session_id"] == _SESSION
    assert context["hook_event_name"] == "SessionStart"
    assert drain_tool_activity_spool(store, now=100.0, token="t") == []


def test_kimi_code_session_end_records_fact_and_drops_the_context(tmp_path: Path) -> None:
    store = tmp_path / "store"
    runner = CliRunner()
    runner.invoke(
        app, ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)], input=_pre_tool()
    )
    assert (store / "client-context" / "kimi-code.json").exists()

    result = runner.invoke(
        app,
        ["hooks", "kimi-code", "session-end", "--store-dir", str(store)],
        input=_payload("SessionEnd", reason="exit"),
    )
    assert result.exit_code == 0
    assert result.output.strip() == "{}"
    [event] = drain_session_end_spool(store, now=100.0, token="t")
    assert event["source"] == "kimi-code"
    assert event["metadata"]["client"] == "kimi-code"
    assert event["metadata"]["client_session_id"] == _SESSION
    assert event["metadata"]["reason"] == "exit"
    # The ended session's ids are gone: a later session cannot inherit a dead one.
    assert not (store / "client-context" / "kimi-code.json").exists()
    assert not list(claude_code_hook_context_dir(store, "kimi-code").glob("*.json"))


def test_kimi_code_session_end_keeps_another_live_sessions_context(tmp_path: Path) -> None:
    store = tmp_path / "store"
    now = time.time()
    for session_id in ("session_older", "session_newer"):
        write_claude_code_hook_context(
            store,
            {
                "schema_version": "agent-sentinel.client-context.v1",
                "client": "kimi-code",
                "client_session_id": session_id,
                "client_transcript_id": None,
                "project_label": "proj",
                "source": "claude_code_hook",
                "hook_event_name": "PreToolUse",
            },
            now=now,
        )
    # The single slot now holds session_newer; ending session_older must not take
    # another live session's id with it.
    removed = clear_ended_session_hook_context(
        json.dumps({"hook_event_name": "SessionEnd", "session_id": "session_older"}),
        store_dir=store,
        client="kimi-code",
    )
    assert len(removed) == 1
    slot_path = claude_code_hook_context_path(store, "kimi-code")
    assert slot_path.exists()  # another live session's slot is never deleted
    slot = json.loads(slot_path.read_text(encoding="utf-8"))
    assert slot["client_session_id"] == "session_newer"
    remaining = list(claude_code_hook_context_dir(store, "kimi-code").glob("*.json"))
    assert len(remaining) == 1  # session_newer's own file survives


@pytest.mark.parametrize("subcommand", ["pre-tool-use", "session-start", "session-end"])
def test_kimi_code_subcommands_fail_open_on_garbage(subcommand: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["hooks", "kimi-code", subcommand, "--store-dir", str(tmp_path)], input="}{not json"
    )
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


@pytest.mark.parametrize("subcommand", ["pre-tool-use", "session-start", "session-end"])
def test_kimi_code_subcommands_never_guess_a_session_id(subcommand: str, tmp_path: Path) -> None:
    store = tmp_path / "store"
    # A payload with no session id: nothing is spooled and no context is written
    # (a missing id always beats a fabricated one).
    result = CliRunner().invoke(
        app,
        ["hooks", "kimi-code", subcommand, "--store-dir", str(store)],
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/Users/dev/proj"}),
    )
    assert result.exit_code == 0
    assert not (store / "client-context").exists()
    assert drain_tool_activity_spool(store, now=100.0, token="t") == []
    assert drain_session_end_spool(store, now=100.0, token="t") == []


# ---------------------------------------------------------------------------
# MCP-side selection: the context this pack writes is the one pid lineage picks
# ---------------------------------------------------------------------------


def test_kimi_code_hook_context_is_selected_by_pid_lineage(tmp_path: Path) -> None:
    store = tmp_path / "store"
    now = time.time()

    def _write(client: str, session_id: str, ancestors: list[int]) -> None:
        write_claude_code_hook_context(
            store,
            {
                "schema_version": "agent-sentinel.client-context.v1",
                "client": client,
                "client_session_id": session_id,
                "client_transcript_id": None,
                "project_label": "proj",
                "source": "claude_code_hook",
                "hook_event_name": "PreToolUse",
                "hook_ancestor_pids": ancestors,
            },
            now=now,
        )

    # Two live Kimi Code sessions plus a Claude Code session in the same store.
    _write("kimi-code", "session_one", [1111])
    _write("kimi-code", "session_two", [2222])
    _write("claude-code", "session_three", [3333])

    contexts = load_claude_code_hook_contexts(store, now=now + 1, clients=HOOK_CONTEXT_CLIENTS)
    assert {context["client"] for context in contexts} == {"kimi-code", "claude-code"}

    # The MCP server's own ancestry (nearest first) matches only session_two's
    # chain, so that session's id is inherited — not the newest, not the first.
    selection = select_claude_code_hook_context(
        store, now=now + 1, consumer_ancestor_pids=[9999, 2222], clients=HOOK_CONTEXT_CLIENTS
    )
    assert selection.status == "selected"
    assert selection.reason == "pid_lineage_match"
    assert selection.context is not None
    assert selection.context["client"] == "kimi-code"
    assert selection.context["client_session_id"] == "session_two"
    # Selected from its own per-session slot (or the byte-identical single slot
    # copy) — never from another session's file.
    assert selection.context["context_path"].startswith("client-context/kimi-code")
    assert len(list(claude_code_hook_context_dir(store, "kimi-code").glob("*.json"))) == 2

    # A kimi-code-only consumer (the slot the section's client names) sees the
    # same two kimi candidates and still resolves by lineage.
    kimi_only = select_claude_code_hook_context(
        store, now=now + 1, consumer_ancestor_pids=[1111], clients=("kimi-code",)
    )
    assert kimi_only.status == "selected"
    assert kimi_only.context is not None
    assert kimi_only.context["client_session_id"] == "session_one"


def test_kimi_code_hook_context_refuses_when_lineage_cannot_disambiguate(tmp_path: Path) -> None:
    store = tmp_path / "store"
    now = time.time()
    for session_id in ("session_one", "session_two"):
        write_claude_code_hook_context(
            store,
            {
                "schema_version": "agent-sentinel.client-context.v1",
                "client": "kimi-code",
                "client_session_id": session_id,
                "client_transcript_id": None,
                "project_label": "proj",
                "source": "claude_code_hook",
                "hook_event_name": "PreToolUse",
                # Both sessions share the same ancestor (e.g. one terminal window):
                # nothing can prove which is which.
                "hook_ancestor_pids": [4242],
            },
            now=now,
        )
    selection = select_claude_code_hook_context(
        store, now=now + 1, consumer_ancestor_pids=[4242], clients=("kimi-code",)
    )
    assert selection.status == "refused"
    assert selection.reason == "concurrent_contexts_ambiguous"
    assert selection.context is None  # missing beats wrong


# ---------------------------------------------------------------------------
# The Kimi Code DESKTOP shape: one main process, many sessions
# ---------------------------------------------------------------------------


def test_kimi_code_hook_context_is_selected_by_cwd_digest_in_a_shared_ancestry(tmp_path: Path) -> None:
    """Desktop Kimi Code runs every session inside ONE main process.

    Measured on this machine: five live kimi-code contexts carried chains like
    ``[hook_pid_i, 1095]`` while the MCP server (pid 1454) had that same main
    pid as its nearest ancestor — every candidate holds the consumer's ancestor,
    so all of them match at the same lineage rank, pid disambiguation can never
    fire, and inheritance refused for every section. The session's project
    directory still differs, and the server's cwd IS its session's project
    directory, so the cwd digest picks the context that is this session's own.
    """
    store = tmp_path / "store"
    now = time.time()
    shared_main_pid = 1095

    def _write(session_id: str, project: str, hook_pid: int) -> None:
        write_claude_code_hook_context(
            store,
            {
                "schema_version": "agent-sentinel.client-context.v1",
                "client": "kimi-code",
                "client_session_id": session_id,
                "client_transcript_id": None,
                "project_label": Path(project).name,
                "cwd_digest": cwd_digest(project),
                "source": "claude_code_hook",
                "hook_event_name": "PreToolUse",
                # This session's own hook process, then the ONE main process
                # every desktop session lives under.
                "hook_ancestor_pids": [hook_pid, shared_main_pid],
            },
            now=now,
        )

    own_project = "/Users/dev/Projects/agentacct"
    _write("session_tofu", "/Users/dev/Projects/tofu", 1176)
    _write("session_own", own_project, 1454)
    _write("session_golive", "/Users/dev/Projects/golive-skill", 1815)

    consumer_ancestors = [shared_main_pid]
    # Lineage alone cannot tell these apart: that refusal IS the pre-fix outcome
    # (each candidate's chain holds the shared main pid, so every one of them
    # matches the consumer's own ancestry at the same rank).
    refused = select_claude_code_hook_context(
        store, now=now + 1, consumer_ancestor_pids=consumer_ancestors, clients=("kimi-code",)
    )
    assert refused.status == "refused"
    assert refused.reason == "concurrent_contexts_ambiguous"

    selection = select_claude_code_hook_context(
        store,
        now=now + 1,
        consumer_ancestor_pids=consumer_ancestors,
        consumer_cwd_digest=cwd_digest(own_project),
        clients=("kimi-code",),
    )
    assert (selection.status, selection.reason) == ("selected", "cwd_match")
    assert selection.fresh_count == 3
    assert selection.context is not None
    # Its OWN session, never the newest writer (session_golive's hook fired
    # last) and never the first candidate.
    assert selection.context["client_session_id"] == "session_own"
    assert "client-context/kimi-code" in selection.context["context_path"]
    # Every session keeps its own file: the single slot cannot say which id
    # belongs to which process.
    assert len(list(claude_code_hook_context_dir(store, "kimi-code").glob("*.json"))) == 3

    # A consumer digest matching NO candidate must not pick anything by cwd; it
    # falls through to lineage, which ties here -> refusal, not a guess.
    unmatched = select_claude_code_hook_context(
        store,
        now=now + 1,
        consumer_ancestor_pids=consumer_ancestors,
        consumer_cwd_digest=cwd_digest("/Users/dev/Projects/unrelated"),
        clients=("kimi-code",),
    )
    assert unmatched.status == "refused"
    assert unmatched.context is None


def test_kimi_code_cwd_digest_refuses_two_sessions_in_the_same_directory(tmp_path: Path) -> None:
    """Two sessions opened on ONE project share a digest, so cwd cannot decide.

    This is the desktop shape with a second session on the same project: both
    candidates carry the consumer's shared main pid, so a fallback to lineage
    would tie as well — and a fallback to newest would hand this server
    whichever session wrote last. The rule refuses outright instead of trying
    the next discriminator at all (the MCP-side test pins the sharper store
    where lineage WOULD have picked one of them).
    """
    store = tmp_path / "store"
    now = time.time()
    same_project = "/Users/dev/Projects/agentacct"
    for session_id, hook_pid in (("session_one", 1176), ("session_two", 1815)):
        write_claude_code_hook_context(
            store,
            {
                "schema_version": "agent-sentinel.client-context.v1",
                "client": "kimi-code",
                "client_session_id": session_id,
                "client_transcript_id": None,
                "project_label": "agentacct",
                "cwd_digest": cwd_digest(same_project),
                "source": "claude_code_hook",
                "hook_event_name": "PreToolUse",
                "hook_ancestor_pids": [hook_pid, 1095],
            },
            now=now,
        )

    selection = select_claude_code_hook_context(
        store,
        now=now + 1,
        consumer_ancestor_pids=[1095],
        consumer_cwd_digest=cwd_digest(same_project),
        clients=("kimi-code",),
    )
    assert selection.status == "refused"
    assert selection.reason == "cwd_match_ambiguous"
    assert selection.fresh_count == 2
    assert selection.context is None  # never the last writer, never a lineage pick


def test_kimi_code_cwd_digest_never_decides_around_a_candidate_without_one(tmp_path: Path) -> None:
    """A candidate with no digest cannot be ruled out, so cwd matching is OFF.

    Mixed-version stores (contexts written before this feature, or a payload
    that carried no cwd) hold digest-less candidates. Picking by cwd while one
    of them exists could hand this server a DIFFERENT session's id, so the rule
    is skipped entirely and the older discriminators decide — missing beats
    wrong.
    """
    store = tmp_path / "store"
    now = time.time()
    own_project = "/Users/dev/Projects/agentacct"

    def _write(session_id: str, ancestors: list[int], digest: str | None) -> None:
        context: dict[str, object] = {
            "schema_version": "agent-sentinel.client-context.v1",
            "client": "kimi-code",
            "client_session_id": session_id,
            "client_transcript_id": None,
            "project_label": "agentacct",
            "source": "claude_code_hook",
            "hook_event_name": "PreToolUse",
            "hook_ancestor_pids": ancestors,
        }
        if digest is not None:
            context["cwd_digest"] = digest
        write_claude_code_hook_context(store, context, now=now)

    # Unique lineage: a digest-less candidate somewhere else in the store must
    # not disturb a lineage pick that is unambiguous on its own.
    _write("session_own", [4242], cwd_digest(own_project))
    _write("session_legacy", [7777], None)
    selection = select_claude_code_hook_context(
        store,
        now=now + 1,
        consumer_ancestor_pids=[7777],
        consumer_cwd_digest=cwd_digest(own_project),
        clients=("kimi-code",),
    )
    assert (selection.status, selection.reason) == ("selected", "pid_lineage_match")
    assert selection.context is not None
    assert selection.context["client_session_id"] == "session_legacy"

    # Desktop shape (every session under the shared main pid). The control
    # below differs from the blocked store ONLY by that missing digest.
    def _write_desktop(root: Path, *, other_digest: str | None) -> None:
        for session_id, digest in (("session_own", cwd_digest(own_project)), ("session_other", other_digest)):
            write_claude_code_hook_context(
                root,
                {
                    "schema_version": "agent-sentinel.client-context.v1",
                    "client": "kimi-code",
                    "client_session_id": session_id,
                    "client_transcript_id": None,
                    "project_label": "agentacct",
                    "source": "claude_code_hook",
                    "hook_event_name": "PreToolUse",
                    "hook_ancestor_pids": [1454, 1095],
                    **({"cwd_digest": digest} if digest else {}),
                },
                now=now,
            )

    # Control: both candidates carry a digest, so the rule applies and picks
    # this server's own session.
    control = tmp_path / "control"
    _write_desktop(control, other_digest=cwd_digest("/Users/dev/Projects/tofu"))
    selected = select_claude_code_hook_context(
        control,
        now=now + 1,
        consumer_ancestor_pids=[1454, 1095],
        consumer_cwd_digest=cwd_digest(own_project),
        clients=("kimi-code",),
    )
    assert (selected.status, selected.reason) == ("selected", "cwd_match")
    assert selected.context is not None
    assert selected.context["client_session_id"] == "session_own"

    # Same store shape, except the other candidate is digest-less: the rule is
    # skipped, lineage ties (both chains hold the shared main pid), and nothing
    # is inherited — the missing digest alone flips the outcome from cwd_match
    # to refusal.
    blocked = tmp_path / "blocked"
    _write_desktop(blocked, other_digest=None)
    blocked_selection = select_claude_code_hook_context(
        blocked,
        now=now + 1,
        consumer_ancestor_pids=[1454, 1095],
        consumer_cwd_digest=cwd_digest(own_project),
        clients=("kimi-code",),
    )
    assert blocked_selection.status == "refused"
    assert blocked_selection.reason == "concurrent_contexts_ambiguous"
    assert blocked_selection.context is None


def test_kimi_code_context_cwd_digest_is_a_fingerprint_not_a_path(tmp_path: Path) -> None:
    """The cwd discriminator must not smuggle the project path into the store.

    Fixture: the CLI capture path (what a real Kimi Code hook runs) writes the
    same digest for the same directory however it is spelled, a different digest
    for a different directory, and never the directory itself.
    """
    store = tmp_path / "store"
    runner = CliRunner()
    project = "/Users/dev/code/secret-project"
    other_project = "/Users/dev/code/other-project"
    for session_id, cwd in (
        ("session_one", project),
        ("session_two", other_project),
        ("session_three", f"{project}/"),  # same directory, trailing slash
    ):
        result = runner.invoke(
            app,
            ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
            input=_pre_tool(session_id=session_id, cwd=cwd),
        )
        assert result.exit_code == 0, result.output

    digest = cwd_digest(project)
    assert digest is not None
    assert len(digest) == 16 and digest == digest.lower()
    assert cwd_digest(other_project) != digest
    assert cwd_digest(f"{project}/") == digest

    slot = claude_code_hook_context_path(store, "kimi-code")
    sources = [slot, *claude_code_hook_context_dir(store, "kimi-code").glob("*.json")]
    written: dict[str, dict[str, object]] = {}
    for path in sources:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        # No raw path in the file — only the basename display label (the
        # pre-existing rule) and the fingerprint.
        assert project not in text
        assert other_project not in text
        assert "/Users/dev/code" not in text
        assert payload["project_label"] in {"secret-project", "other-project"}
        assert isinstance(payload["cwd_digest"], str)
        written[str(payload["client_session_id"])] = payload

    # Same directory -> same digest (the trailing-slash spelling included);
    # a different directory -> a different one.
    assert written["session_one"]["cwd_digest"] == digest
    assert written["session_three"]["cwd_digest"] == digest
    assert written["session_two"]["cwd_digest"] != digest


def test_every_bridged_client_writes_the_cwd_digest(tmp_path: Path) -> None:
    """Every bridged client's hook payload carries cwd, so every client writes it.

    The digest exists for the Kimi Code desktop shape, but the write rule is
    per-client: Claude Code and Codex hook payloads carry cwd too, and the MCP
    server reads all three clients' slots, so a client that skipped the digest
    would silently lose the discriminator (and, worse, a digest-less candidate
    turns the rule off for every client's candidates in that store).
    """
    store = tmp_path / "store"
    project = "/Users/dev/code/every-client"
    for client in HOOK_CONTEXT_CLIENTS:
        written = capture_claude_code_client_context(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": f"{client}-session",
                    "cwd": project,
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo ok"},
                }
            ),
            store_dir=store,
            client=client,
        )
        assert written is not None
        # The single slot AND the per-session file both carry it.
        slot = json.loads(claude_code_hook_context_path(store, client).read_text(encoding="utf-8"))
        assert slot["client"] == client
        assert slot["cwd_digest"] == cwd_digest(project)
        assert project not in json.dumps(slot)
        [per_session] = list(claude_code_hook_context_dir(store, client).glob("*.json"))
        assert json.loads(per_session.read_text(encoding="utf-8"))["cwd_digest"] == cwd_digest(project)

    # A payload with no usable cwd leaves the digest empty rather than inventing
    # one; an empty digest only disables cwd matching for that candidate.
    assert (
        capture_claude_code_client_context(
            json.dumps({"hook_event_name": "PreToolUse", "session_id": "no-cwd-session"}),
            store_dir=store,
            client="claude-code",
        )
        is not None
    )
    no_cwd = json.loads(claude_code_hook_context_path(store, "claude-code").read_text(encoding="utf-8"))
    assert no_cwd["client_session_id"] == "no-cwd-session"
    assert no_cwd["cwd_digest"] is None


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


def test_kimi_code_wrapper_renders_valid_python_shelling_to_hooks_kimi_code() -> None:
    src = render_kimi_code_hook_wrapper("/abs/agentacct", store_dir="/global/store")
    ast.parse(src)  # must be valid python
    assert '"hooks", "kimi-code"' in src
    assert "/abs/agentacct" in src
    assert "/global/store" in src  # the store binds on the command line
    # Bounded child wait + fail-open `{}`, so a hung agentacct can never hold a
    # Kimi Code event until the vendor's own timeout.
    assert "timeout=5" in src
    assert "TimeoutExpired" in src
    for event, subcommand in (("SessionStart", "session-start"), ("SessionEnd", "session-end")):
        assert f"'{event}': '{subcommand}'" in src


def test_kimi_code_hooks_toml_block_emits_only_the_four_allowed_fields() -> None:
    block = kimi_code_hooks_toml_block('python3 "/Users/a b/hooks/agentacct_kimi_code_hook.py"')
    parsed = tomllib.loads(block)
    assert [entry["event"] for entry in parsed["hooks"]] == list(KIMI_CODE_HOOK_EVENTS)
    for entry in parsed["hooks"]:
        # `[[hooks]]` allows ONLY these; an extra field makes Kimi Code drop the
        # whole hooks section. `matcher` is deliberately absent so every rule
        # matches everything (a bare "*", the Claude/Codex shape, is not a valid
        # regex).
        assert set(entry) == {"event", "command", "timeout"}
    # A path with spaces round-trips exactly through the TOML string.
    assert parsed["hooks"][0]["command"] == 'python3 "/Users/a b/hooks/agentacct_kimi_code_hook.py"'


# ---------------------------------------------------------------------------
# Installer: config.toml merge
# ---------------------------------------------------------------------------

_BASE_CONFIG = (
    "# user config — provider credentials live here\n"
    'model = "kimi-k3"\n'
    "\n"
    "[[providers]]\n"
    'name = "moonshot"\n'
    'api_key = "sk-secret-value"\n'
    "\n"
    "[[hooks]]\n"
    'event = "Notification"\n'
    'matcher = "task\\\\.completed"\n'
    'command = "/usr/local/bin/notify.sh"\n'
    "timeout = 5\n"
    "\n"
    "[[hooks]]\n"
    'event = "PreToolUse"\n'
    'command = "/usr/bin/watchdog --watch /x/hooks/agentacct_kimi_code_hook.py"\n'
    "timeout = 5\n"
)


def test_kimi_code_install_merges_config_toml_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "kimi-home"
    home.mkdir()
    config_path = home / KIMI_CODE_CONFIG_RELATIVE_PATH
    config_path.write_text(_BASE_CONFIG, encoding="utf-8")

    action, wrapper_path = _install_kimi_code_hook(home, tmp_path / "store", "/abs/agentacct")
    assert action == "updated"
    assert wrapper_path == home / KIMI_CODE_HOOK_RELATIVE_PATH
    assert wrapper_path.exists()
    assert wrapper_path.stat().st_mode & 0o111  # executable

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    # Everything the user had is preserved, semantics and order included.
    assert data["model"] == "kimi-k3"
    assert data["providers"] == [{"name": "moonshot", "api_key": "sk-secret-value"}]
    assert data["hooks"][0] == {
        "event": "Notification",
        "matcher": "task\\.completed",
        "command": "/usr/local/bin/notify.sh",
        "timeout": 5,
    }
    # A user command that merely MENTIONS the wrapper as an argument is not ours.
    assert data["hooks"][1]["command"] == "/usr/bin/watchdog --watch /x/hooks/agentacct_kimi_code_hook.py"
    expected_command = kimi_code_hook_command(wrapper_path)
    ours = [entry for entry in data["hooks"] if entry.get("command") == expected_command]
    assert len(ours) == len(KIMI_CODE_HOOK_EVENTS)
    assert {entry["event"] for entry in ours} == set(KIMI_CODE_HOOK_EVENTS)
    for entry in ours:
        assert set(entry) == {"event", "command", "timeout"}
    assert "sk-secret-value" in config_path.read_text(encoding="utf-8")
    assert kimi_code_hooks_unsupported_fields(data) == []

    before = config_path.read_bytes()
    action2, _ = _install_kimi_code_hook(home, tmp_path / "store", "/abs/agentacct")
    assert action2 == "unchanged"
    assert config_path.read_bytes() == before  # byte-identical on re-install


def test_kimi_code_reinstall_replaces_our_rules_in_place(tmp_path: Path) -> None:
    home = tmp_path / "kimi-home"
    home.mkdir()
    config_path = home / KIMI_CODE_CONFIG_RELATIVE_PATH
    config_path.write_text(_BASE_CONFIG, encoding="utf-8")
    _install_kimi_code_hook(home, tmp_path / "store", "/abs/agentacct")
    wrapper_path = home / KIMI_CODE_HOOK_RELATIVE_PATH

    # A re-install that differs only by interpreter must UPDATE our rules, not
    # append a second set (a duplicate would fire every event twice).
    other_command = f"/other/python {wrapper_path}"
    _path, action = _write_kimi_code_hooks_config_at(config_path, other_command, WRAPPER_BASENAME)
    assert action == "updated"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    ours = [entry for entry in data["hooks"] if entry.get("command") == other_command]
    assert {entry["event"] for entry in ours} == set(KIMI_CODE_HOOK_EVENTS)
    assert len(data["hooks"]) == 2 + len(KIMI_CODE_HOOK_EVENTS)
    assert data["hooks"][0]["event"] == "Notification"  # user rules intact
    assert data["hooks"][1]["command"].startswith("/usr/bin/watchdog")


def test_kimi_code_install_refuses_a_config_it_cannot_parse(tmp_path: Path) -> None:
    home = tmp_path / "kimi-home"
    home.mkdir()
    config_path = home / KIMI_CODE_CONFIG_RELATIVE_PATH
    config_path.write_text('[[hooks]]\nevent = \ncommand = "x"\n', encoding="utf-8")
    before = config_path.read_bytes()

    result = CliRunner().invoke(
        app,
        ["hooks", "kimi-code", "install", "--home", str(home), "--store-dir", str(tmp_path / "store")],
    )
    assert result.exit_code == 1
    # Normalize: the rich console soft-wraps at ~80 cols with no TTY (CI), which
    # would split these multi-word phrases across a newline.
    normalized = " ".join(result.output.split())
    assert "LEFT UNCHANGED" in normalized
    assert "No hook was wired" in normalized
    assert config_path.read_bytes() == before  # never clobbered
    # The exact rules are printed so the user can wire them by hand.
    assert 'event = "SessionStart"' in result.output
    assert f'command = "{kimi_code_hook_command(home / KIMI_CODE_HOOK_RELATIVE_PATH)}"' in result.output


def test_kimi_code_install_refuses_a_hooks_key_that_is_not_an_array(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[hooks]\nevent = "PreToolUse"\n', encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")
    _path, action = _write_kimi_code_hooks_config_at(config_path, "/abs/agentacct", WRAPPER_BASENAME)
    assert action == "skipped-unparsed"
    assert config_path.read_text(encoding="utf-8") == before


def test_kimi_code_hooks_writer_never_writes_a_result_it_cannot_prove(tmp_path: Path) -> None:
    # The safety net itself: dropping our rule would orphan a sub-table that had
    # been parsed as a member of it, so re-parsing the edited text is what catches
    # the corruption. A file agentacct cannot prove still parses is left alone.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[[hooks]]\n"
        'event = "PreToolUse"\n'
        f'command = "python3 /x/hooks/{WRAPPER_BASENAME}"\n'
        "timeout = 10\n"
        "[hooks.notes]\n"
        'text = "mine"\n',
        encoding="utf-8",
    )
    before = config_path.read_bytes()
    _path, action = _write_kimi_code_hooks_config_at(config_path, "/abs/agentacct", WRAPPER_BASENAME)
    assert action == "skipped-unparsed"
    assert config_path.read_bytes() == before


def test_kimi_code_hooks_writer_preserves_crlf(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(
        b'model = "kimi-k3"\r\n'
        b"\r\n"
        b"[[hooks]]\r\n"
        b'event = "Notification"\r\n'
        b'command = "/usr/local/bin/notify.sh"\r\n'
        b"timeout = 5\r\n"
    )
    wrapper_path = tmp_path / KIMI_CODE_HOOK_RELATIVE_PATH
    command = kimi_code_hook_command(wrapper_path)
    _path, action = _write_kimi_code_hooks_config_at(config_path, command, WRAPPER_BASENAME)
    assert action == "updated"
    raw = config_path.read_bytes()
    # A reflow to LF would rewrite bytes the user owns; the file stays CRLF.
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8")
    assert 'model = "kimi-k3"' in text
    assert 'command = "/usr/local/bin/notify.sh"' in text
    assert {entry["event"] for entry in tomllib.loads(text)["hooks"]} == {
        "Notification",
        *KIMI_CODE_HOOK_EVENTS,
    }
    before = config_path.read_bytes()
    _path, action2 = _write_kimi_code_hooks_config_at(config_path, command, WRAPPER_BASENAME)
    assert action2 == "unchanged"
    assert config_path.read_bytes() == before


def test_kimi_code_install_cli_respects_kimi_code_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kimi_home = tmp_path / "relocated-kimi-home"
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        app, ["hooks", "kimi-code", "install", "--store-dir", str(tmp_path / "store")]
    )
    assert result.exit_code == 0, result.output
    assert (kimi_home / KIMI_CODE_HOOK_RELATIVE_PATH).exists()
    data = tomllib.loads((kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert {entry["event"] for entry in data["hooks"]} == set(KIMI_CODE_HOOK_EVENTS)
    # The store binds on the command line, so a session anywhere records here.
    assert str(tmp_path / "store") in (kimi_home / KIMI_CODE_HOOK_RELATIVE_PATH).read_text(encoding="utf-8")


def test_kimi_code_doctor_reports_wired_install_and_flags_broken_fields(tmp_path: Path) -> None:
    home = tmp_path / "kimi-home"
    _install_kimi_code_hook(home, tmp_path / "store", "/abs/agentacct")
    checks = {check["name"]: check for check in kimi_code_hook_doctor_checks(home)}
    assert checks["hook wrapper"]["status"] == "ok"
    assert checks["config.toml hooks"]["status"] == "ok"
    assert "hooks entry fields" not in checks

    config_path = home / KIMI_CODE_CONFIG_RELATIVE_PATH
    text = config_path.read_text(encoding="utf-8")
    # A field outside the vendor's four (here hand-added to the first rule) makes
    # Kimi Code drop the WHOLE hooks section, agentacct's rules included.
    config_path.write_text(text.replace("timeout = 10", "timeout = 10\nretries = 3", 1), encoding="utf-8")
    checks = {check["name"]: check for check in kimi_code_hook_doctor_checks(home)}
    assert checks["hooks entry fields"]["status"] == "warn"
    assert "retries" in checks["hooks entry fields"]["details"]
    assert checks["config.toml hooks"]["status"] == "ok"  # our rules are all still there


def test_kimi_code_doctor_flags_a_config_that_does_not_load(tmp_path: Path) -> None:
    home = tmp_path / "kimi-home"
    home.mkdir()
    (home / KIMI_CODE_CONFIG_RELATIVE_PATH).write_text("event = \n", encoding="utf-8")
    checks = {check["name"]: check for check in kimi_code_hook_doctor_checks(home)}
    assert checks["config.toml hooks"]["status"] == "warn"
    assert "not valid TOML" in checks["config.toml hooks"]["details"]


# ---------------------------------------------------------------------------
# Onboarding: the third leg
# ---------------------------------------------------------------------------


def test_onboard_global_kimi_code_installs_the_hook_leg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kimi_home = tmp_path / "kimi-home"
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = tmp_path / "store"

    assert cli._onboard_global_kimi_code(store, "/abs/agentacct") == "wired"

    wrapper_path = kimi_home / KIMI_CODE_HOOK_RELATIVE_PATH
    assert wrapper_path.exists()
    data = tomllib.loads((kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert {entry["event"] for entry in data["hooks"]} == set(KIMI_CODE_HOOK_EVENTS)
    assert json.loads((kimi_home / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["agentacct"]
    assert (kimi_home / "AGENTS.md").exists()
    # Re-running onboarding is idempotent (the resync path runs it on version bumps).
    before = (kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).read_bytes()
    assert cli._onboard_global_kimi_code(store, "/abs/agentacct") == "wired"
    assert (kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).read_bytes() == before


def test_onboard_global_kimi_code_reports_a_refused_config_and_keeps_other_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    (kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).write_text("not = valid = toml\n", encoding="utf-8")
    before = (kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).read_bytes()
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    # The MCP + directive legs still succeed: a config agentacct may not touch is
    # not an onboarding failure, but it must be reported, not swallowed.
    assert cli._onboard_global_kimi_code(tmp_path / "store", "/abs/agentacct") == "wired"
    assert (kimi_home / KIMI_CODE_CONFIG_RELATIVE_PATH).read_bytes() == before
    assert (kimi_home / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# Desktop cwd: the payload carries "/", the session index carries the truth
# ---------------------------------------------------------------------------


def _write_session_index(home: Path, rows: list[object]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    (home / "session_index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_kimi_code_session_workdir_prefers_the_last_live_row(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_session_index(
        home,
        [
            {"sessionId": _SESSION, "workDir": "/Users/dev/first"},
            "not json at all",
            {"sessionId": _SESSION, "workDir": "/Users/dev/second"},
            {"sessionId": "session_gone", "deleted": True},
            {"sessionId": "session_relative", "workDir": "relative/dir"},
        ],
    )
    assert kimi_code_session_workdir(_SESSION, home=home) == "/Users/dev/second"
    assert kimi_code_session_workdir("session_gone", home=home) is None
    assert kimi_code_session_workdir("session_relative", home=home) is None
    assert kimi_code_session_workdir("session_absent", home=home) is None
    assert kimi_code_session_workdir("", home=home) is None
    assert kimi_code_session_workdir(_SESSION, home=tmp_path / "missing") is None


def test_kimi_code_desktop_cwd_digest_comes_from_the_session_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop app dispatches hooks from its OWN directory, so `cwd` is `/`.

    Measured on this machine: five concurrent desktop sessions, 64 of 64
    PreToolUse payloads carrying `cwd="/"` — one shared value, so a digest of
    the payload could never tell two sessions apart. Kimi Code's own
    `session_index.jsonl` maps the payload's `session_id` to the session's
    project directory, and the digest is built from THAT; the unusable payload
    value never reaches the context file.
    """
    kimi_home = tmp_path / "kimi-home"
    project = tmp_path / "Projects" / "agentacct"
    project.mkdir(parents=True)
    _write_session_index(
        kimi_home,
        [{"sessionId": _SESSION, "sessionDir": str(tmp_path / "session-dir"), "workDir": str(project)}],
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    store = tmp_path / "store"

    result = CliRunner().invoke(
        app,
        ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
        input=_payload(
            "PreToolUse",
            session_id=_SESSION,
            client_type="kimi_code_desktop",
            cwd="/",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
        ),
    )
    assert result.exit_code == 0, result.output
    raw_text = (store / "client-context" / "kimi-code.json").read_text(encoding="utf-8")
    context = json.loads(raw_text)
    assert context["cwd_digest"] == cwd_digest(str(project))
    assert context["cwd_digest"] != cwd_digest("/")
    assert context["project_label"] == "agentacct"
    assert str(project) not in raw_text


def test_kimi_code_payload_cwd_is_the_fallback_when_the_index_has_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kimi_home = tmp_path / "kimi-home"
    _write_session_index(kimi_home, [{"sessionId": "session_other", "workDir": "/Users/dev/other"}])
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    store = tmp_path / "store"

    result = CliRunner().invoke(
        app,
        ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
        input=_pre_tool(cwd="/Users/dev/proj"),
    )
    assert result.exit_code == 0, result.output
    context = json.loads((store / "client-context" / "kimi-code.json").read_text(encoding="utf-8"))
    assert context["cwd_digest"] == cwd_digest("/Users/dev/proj")
    assert context["project_label"] == "proj"


def test_kimi_code_desktop_sessions_disambiguate_through_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression the fix exists for: several sessions, one shared payload cwd.

    Three desktop-shaped payloads (`cwd="/"`) from three sessions, each with its
    own project directory in the index, must still let the MCP side pick its
    OWN context by digest — the payload value cannot do that.
    """
    kimi_home = tmp_path / "kimi-home"
    projects = {
        "session_alpha1": tmp_path / "Projects" / "alpha",
        "session_beta22": tmp_path / "Projects" / "beta",
        "session_gamma3": tmp_path / "Projects" / "gamma",
    }
    for project in projects.values():
        project.mkdir(parents=True)
    _write_session_index(
        kimi_home,
        [{"sessionId": sid, "workDir": str(project)} for sid, project in projects.items()],
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    store = tmp_path / "store"

    for sid in projects:
        result = CliRunner().invoke(
            app,
            ["hooks", "kimi-code", "pre-tool-use", "--store-dir", str(store)],
            input=_payload(
                "PreToolUse",
                session_id=sid,
                client_type="kimi_code_desktop",
                cwd="/",
                tool_name="Bash",
                tool_input={"command": "true"},
            ),
        )
        assert result.exit_code == 0, result.output

    selection = select_claude_code_hook_context(
        store,
        consumer_cwd_digest=cwd_digest(str(projects["session_beta22"])),
        clients=("kimi-code",),
    )
    assert (selection.status, selection.reason) == ("selected", "cwd_match")
    assert selection.context is not None
    assert selection.context["client_session_id"] == "session_beta22"
