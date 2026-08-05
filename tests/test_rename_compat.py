"""Phase 4 — Agent Sentinel -> Agent Chronicle rename compatibility.

Pins the rename's load-bearing promises:

- DUAL PAIRING FOREVER: historical client logs carry the pre-rename
  ``agent-sentinel`` registration name; log-evidence pairing accepts both the
  new and old server keys (Claude tool_use names and codex namespaces).
- MANY MANAGED MARKERS: pre-rename ``agent-chronicle:begin/end`` AND
  ``agent-sentinel:begin/end`` blocks in user CLAUDE.md/AGENTS.md are
  recognized, migrated on rewrite, and stripped by ``--remove``; writes always
  emit the new ``agentacct:begin/end`` markers.
- ENV ALIASES: every AGENT_CHRONICLE_* read accepts the pre-rename
  AGENT_SENTINEL_* name silently; the new name wins; conflicting store-dir
  values refuse (no silent ledger split).
- DOCTOR: mcp doctor probes BOTH registration keys and says which one it found.
- FROZEN VOCAB TRIPWIRE: stored-data vocabulary (schema strings, event
  sources, provenance values, metadata keys, wire error types, store dir
  names) keeps its pre-rename spelling verbatim — a future rename sweep must
  fail here and read the freeze rule.
- PACKAGING PINS: pyproject ships the new name, the transition alias script,
  and an explicit build-system.
- REPO HYGIENE: no module-level ``agent_sentinel`` usage survives anywhere
  (the dev venv still provides an importable ``agent_sentinel`` from the main
  checkout, so a missed import would silently pass without this guard).
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from pathlib import Path

from typer.testing import CliRunner

import agentacct
from agentacct import install_guide
from agentacct.cli import app
from agentacct.client_usage import discover_claude_code_usage, discover_codex_usage
from agentacct.env_compat import env_alias_names, legacy_env_name, legacy_env_names, read_env_alias
from agentacct.hooks import CLAUDE_CODE_HOOK_CONTEXT_SCHEMA, _AGENT_CHRONICLE_EXECUTABLE_NAMES
from agentacct.log_evidence import (
    ACCEPTED_SERVER_KEYS,
    SENTINEL_CREATION_TOOLS,
    SENTINEL_SERVER_KEY,
    _CODEX_ACCEPTED_NAMESPACES,
    classify_claude_tool_use,
    classify_codex_function_call,
    codex_namespace_matches_sentinel,
)
from agentacct.mcp import TOOLS
from agentacct.store_resolution import (
    ENV_STORE_DIR,
    LEGACY_ENV_STORE_DIR,
    StoreResolutionError,
    resolve_store_dir,
    store_env_dir_value,
)
from agentacct.usage_truth import (
    CLI_INSTRUMENTATION_PROVENANCE,
    DIAGNOSTIC_EVENT_SOURCES,
    LOCAL_USAGE_PROVENANCE,
)
from agentacct.wrappers import CLAUDE_WRAPPER, build_agent_command

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = Path(agentacct.__file__).resolve().parent

runner = CliRunner()


# ---------------------------------------------------------------------------
# 1. Dual server-key pairing (classification level)
# ---------------------------------------------------------------------------


def test_claude_pairing_accepts_both_server_keys() -> None:
    assert classify_claude_tool_use("mcp__agent-chronicle__sentinel_record_section") == "accepted"
    assert classify_claude_tool_use("mcp__agent-sentinel__sentinel_record_section") == "accepted"
    assert classify_claude_tool_use("mcp__agent-sentinel__sentinel_record_event") == "accepted"
    # Any OTHER server segment is rejected (skipped-but-counted), not guessed.
    assert classify_claude_tool_use("mcp__my-sentinel__sentinel_record_event") == "rejected"
    # Non-creation tools never classify.
    assert classify_claude_tool_use("mcp__agent-sentinel__sentinel_list_events") is None


def test_codex_namespaces_accept_all_four_forms() -> None:
    # Additive rename: the published `agentacct` namespace joins BOTH pre-rename
    # generations (agent-chronicle / agent-sentinel, hyphen and underscore).
    assert _CODEX_ACCEPTED_NAMESPACES == frozenset(
        {"agentacct", "agent-chronicle", "agent_chronicle", "agent-sentinel", "agent_sentinel"}
    )
    for namespace in ("agentacct", "agent-chronicle", "agent_chronicle", "agent-sentinel", "agent_sentinel"):
        assert codex_namespace_matches_sentinel(namespace)
        assert codex_namespace_matches_sentinel(f"mcp__{namespace}")
        assert classify_codex_function_call("sentinel_record_section", namespace) == "accepted"
    # Substring smuggles stay rejected for both generations.
    assert not codex_namespace_matches_sentinel("mcp__not_agent_sentinel_fake")
    assert not codex_namespace_matches_sentinel("mcp__not_agentacct_fake")
    assert classify_codex_function_call("sentinel_record_section", "other") == "rejected"


def test_accepted_server_keys_pin_new_and_old() -> None:
    # The published server key is now `agentacct`; BOTH pre-rename keys stay
    # accepted forever so historical client logs keep donating evidence.
    assert SENTINEL_SERVER_KEY == "agentacct"
    assert ACCEPTED_SERVER_KEYS == frozenset({"agentacct", "agent-chronicle", "agent-sentinel"})


# ---------------------------------------------------------------------------
# 1b. Dual pairing end-to-end on synthetic client logs
# ---------------------------------------------------------------------------


def _claude_line(kind: str, session_id: str, **kw) -> dict:
    if kind == "usage":
        return {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": "/work/project",
            "message": {
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5},
            },
        }
    if kind == "tool_use":
        return {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": kw["tool_use_id"], "name": kw["name"], "input": {}}]},
        }
    return {
        "type": "user",
        "sessionId": session_id,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": kw["tool_use_id"], "content": [{"type": "text", "text": kw["text"]}]}]},
    }


def test_old_name_claude_transcript_still_pairs(tmp_path) -> None:
    """A pre-rename transcript (mcp__agent-sentinel__*) keeps donating evidence."""
    claude_home = tmp_path / "claude-home"
    project = claude_home / "projects" / "-work-project"
    project.mkdir(parents=True)
    session_id = "c037bd88-0000-4000-8000-00000000abcd"
    creation_text = json.dumps({"event": {"event_id": "evt_01dabc123456", "event_type": "section_started"}})
    lines = [
        _claude_line("usage", session_id),
        _claude_line("tool_use", session_id, tool_use_id="toolu_old", name="mcp__agent-sentinel__sentinel_record_section"),
        _claude_line("tool_result", session_id, tool_use_id="toolu_old", text=creation_text),
        # New-name call in the same transcript pairs too.
        _claude_line("tool_use", session_id, tool_use_id="toolu_new", name="mcp__agent-chronicle__sentinel_record_event"),
        _claude_line("tool_result", session_id, tool_use_id="toolu_new", text=json.dumps({"event": {"event_id": "evt_ee1dabc12345", "event_type": "task"}})),
    ]
    (project / f"{session_id}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    events = discover_claude_code_usage(claude_home=claude_home, limit_sessions=10)

    assert len(events) == 1
    assert list(events[0].evidenced_event_ids) == ["evt_01dabc123456", "evt_ee1dabc12345"]
    assert events[0].evidenced_outputs_skipped == 0


def test_old_namespace_codex_rollout_still_pairs(tmp_path) -> None:
    """A pre-rename codex rollout (namespace mcp__agent_sentinel) keeps pairing."""
    thread_id = "019f2303-6ae1-7000-8000-00000000cafe"
    codex_home = tmp_path / "codex-home"
    sessions_dir = codex_home / "sessions" / "2026" / "07" / "05"
    sessions_dir.mkdir(parents=True)
    creation_text = json.dumps({"event": {"event_id": "evt_c0de01d5678a", "event_type": "section_started"}})
    output = json.dumps([{"type": "text", "text": creation_text}])
    rollout_lines = [
        {"type": "session_meta", "payload": {"id": thread_id}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "sentinel_record_section",
                "namespace": "mcp__agent_sentinel",
                "call_id": "call_1",
            },
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": output}},
    ]
    rollout = sessions_dir / f"rollout-2026-07-05T10-00-00-{thread_id}.jsonl"
    rollout.write_text("\n".join(json.dumps(line) for line in rollout_lines) + "\n", encoding="utf-8")
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        con.execute(
            "create table threads (id text primary key, rollout_path text not null, created_at integer not null,"
            " updated_at integer not null, cwd text not null, title text not null, tokens_used integer not null,"
            " model text, cli_version text)"
        )
        con.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, str(rollout), 100, 300, "/work/project", "t", 980, "gpt-5.5", "0.test"),
        )
        con.execute("create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)")
        con.commit()
    finally:
        con.close()

    events = discover_codex_usage(codex_home=codex_home, limit_sessions=10)

    assert len(events) == 1
    assert list(events[0].evidenced_event_ids) == ["evt_c0de01d5678a"]


# ---------------------------------------------------------------------------
# 2. Dual managed instruction markers
# ---------------------------------------------------------------------------

_LEGACY_BLOCK = "\n".join(
    (
        install_guide.LEGACY_INSTRUCTIONS_BEGIN_MARKER,
        "## Agent Sentinel — record your work",
        "",
        "- old body line",
        install_guide.LEGACY_INSTRUCTIONS_END_MARKER,
    )
)

# The FORMER-new `agent-chronicle` marker pair is now a recognized-legacy
# generation too (it shipped before the agentacct rename).
_LEGACY_CHRONICLE_BLOCK = "\n".join(
    (
        install_guide.LEGACY_CHRONICLE_INSTRUCTIONS_BEGIN_MARKER,
        "## Agent Chronicle — record your work",
        "",
        "- old chronicle body line",
        install_guide.LEGACY_CHRONICLE_INSTRUCTIONS_END_MARKER,
    )
)


def test_legacy_marker_block_is_recognized_and_migrated_on_rewrite() -> None:
    existing = "# My file\n\n" + _LEGACY_BLOCK + "\n\ntrailing user content\n"
    assert install_guide.instruction_file_has_managed_block(existing)

    rendered = install_guide.render_instruction_file(existing, remove=False)

    # Old block replaced in place by a NEW-marker block; user content intact.
    assert install_guide.LEGACY_INSTRUCTIONS_BEGIN_MARKER not in rendered
    assert install_guide.LEGACY_INSTRUCTIONS_END_MARKER not in rendered
    assert "- old body line" not in rendered
    assert rendered.count(install_guide.INSTRUCTIONS_BEGIN_MARKER) == 1
    assert rendered.count(install_guide.INSTRUCTIONS_END_MARKER) == 1
    assert install_guide.WORKFLOW_INSTRUCTION_HEADING in rendered
    assert "# My file" in rendered
    assert "trailing user content" in rendered


def test_legacy_chronicle_marker_block_is_recognized_and_migrated_on_rewrite() -> None:
    """The former-new `agent-chronicle` block is recognized and migrated to the
    new `agentacct` markers on rewrite (alongside the `agent-sentinel` pair)."""
    existing = "# My file\n\n" + _LEGACY_CHRONICLE_BLOCK + "\n\ntrailing user content\n"
    assert install_guide.instruction_file_has_managed_block(existing)

    rendered = install_guide.render_instruction_file(existing, remove=False)

    # The agent-chronicle block is replaced in place by an agentacct block.
    assert "agent-chronicle:begin" not in rendered
    assert "agent-chronicle:end" not in rendered
    assert "- old chronicle body line" not in rendered
    assert rendered.count(install_guide.INSTRUCTIONS_BEGIN_MARKER) == 1
    assert rendered.count(install_guide.INSTRUCTIONS_END_MARKER) == 1
    assert install_guide.WORKFLOW_INSTRUCTION_HEADING in rendered
    assert "# My file" in rendered
    assert "trailing user content" in rendered

    # --remove strips a chronicle block too.
    stripped = install_guide.render_instruction_file(
        "before\n\n" + _LEGACY_CHRONICLE_BLOCK + "\n\nafter\n", remove=True
    )
    assert "agent-chronicle:begin" not in stripped
    assert "before" in stripped and "after" in stripped


def test_legacy_marker_block_is_stripped_by_remove() -> None:
    existing = "before\n\n" + _LEGACY_BLOCK + "\n\nafter\n"

    rendered = install_guide.render_instruction_file(existing, remove=True)

    assert "agent-sentinel:begin" not in rendered
    assert "agent-chronicle:begin" not in rendered
    assert "before" in rendered and "after" in rendered


def test_mixed_marker_pair_is_recognized_defensively() -> None:
    mixed = "\n".join(
        (
            install_guide.LEGACY_INSTRUCTIONS_BEGIN_MARKER,
            "body",
            install_guide.INSTRUCTIONS_END_MARKER,
        )
    )
    assert install_guide.instruction_file_has_managed_block(mixed)
    assert install_guide.render_instruction_file(mixed, remove=True) == ""


def test_legacy_markers_inside_code_fences_stay_user_content() -> None:
    fenced = "\n".join(
        (
            "docs about the old feature:",
            "```",
            install_guide.LEGACY_INSTRUCTIONS_BEGIN_MARKER,
            install_guide.LEGACY_INSTRUCTIONS_END_MARKER,
            "```",
            "",
        )
    )
    assert not install_guide.instruction_file_has_managed_block(fenced)
    rendered = install_guide.render_instruction_file(fenced, remove=True)
    assert install_guide.LEGACY_INSTRUCTIONS_BEGIN_MARKER in rendered


def test_new_writes_emit_new_markers() -> None:
    block = install_guide.workflow_instruction_block()
    assert block.startswith("<!-- agentacct:begin")
    assert block.endswith("<!-- agentacct:end -->")
    assert install_guide.WORKFLOW_INSTRUCTION_HEADING == "## agentacct — record your work"


# ---------------------------------------------------------------------------
# 3. Env aliases: new wins, old accepted silently
# ---------------------------------------------------------------------------


def test_read_env_alias_new_wins_and_old_accepted() -> None:
    # AGENTACCT_* is the new PRIMARY; the full chain is
    # AGENTACCT_* -> AGENT_CHRONICLE_* -> AGENT_SENTINEL_*, newest wins.
    assert legacy_env_name("AGENTACCT_STORE_DIR") == "AGENT_CHRONICLE_STORE_DIR"
    assert legacy_env_names("AGENTACCT_STORE_DIR") == (
        "AGENT_CHRONICLE_STORE_DIR",
        "AGENT_SENTINEL_STORE_DIR",
    )
    assert env_alias_names("AGENTACCT_OPENAI_API_KEY") == (
        "AGENTACCT_OPENAI_API_KEY",
        "AGENT_CHRONICLE_OPENAI_API_KEY",
        "AGENT_SENTINEL_OPENAI_API_KEY",
    )
    # New primary wins over BOTH pre-rename names.
    all_three = {
        "AGENTACCT_OPENAI_API_KEY": "new",
        "AGENT_CHRONICLE_OPENAI_API_KEY": "mid",
        "AGENT_SENTINEL_OPENAI_API_KEY": "old",
    }
    assert read_env_alias("AGENTACCT_OPENAI_API_KEY", all_three) == "new"
    # AGENT_CHRONICLE_* wins over AGENT_SENTINEL_* when the primary is unset.
    assert (
        read_env_alias(
            "AGENTACCT_OPENAI_API_KEY",
            {"AGENT_CHRONICLE_OPENAI_API_KEY": "mid", "AGENT_SENTINEL_OPENAI_API_KEY": "old"},
        )
        == "mid"
    )
    # BOTH pre-rename names stay accepted on their own, forever.
    assert read_env_alias("AGENTACCT_OPENAI_API_KEY", {"AGENT_CHRONICLE_OPENAI_API_KEY": "mid"}) == "mid"
    assert read_env_alias("AGENTACCT_OPENAI_API_KEY", {"AGENT_SENTINEL_OPENAI_API_KEY": "old"}) == "old"
    assert read_env_alias("AGENTACCT_OPENAI_API_KEY", {}) is None
    # Empty/whitespace values are unset, matching call-site truthiness checks.
    assert read_env_alias("AGENTACCT_OPENAI_API_KEY", {"AGENTACCT_OPENAI_API_KEY": " "}) is None


def test_store_dir_env_alias_resolves_and_conflicts_refuse(tmp_path) -> None:
    # Every recognized store-dir name resolves on its own (source == "env"):
    # the new AGENTACCT_* primary AND BOTH pre-rename aliases.
    for name in ("AGENTACCT_STORE_DIR", "AGENT_CHRONICLE_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"):
        resolution = resolve_store_dir(None, env={name: str(tmp_path / "state")})
        assert resolution.source == "env", name
        assert resolution.path == tmp_path / "state", name

    # No conflict when all names agree; the primary value is returned.
    agree = {
        "AGENTACCT_STORE_DIR": str(tmp_path / "new"),
        "AGENT_CHRONICLE_STORE_DIR": str(tmp_path / "new"),
        "AGENT_SENTINEL_STORE_DIR": str(tmp_path / "new"),
    }
    assert resolve_store_dir(None, env=agree).path == tmp_path / "new"

    # ANY two names set to DIFFERENT paths refuses (never silently split), and
    # the refusal names the conflicting variables.
    for left, right in (
        ("AGENTACCT_STORE_DIR", "AGENT_CHRONICLE_STORE_DIR"),
        ("AGENTACCT_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"),
        ("AGENT_CHRONICLE_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"),
    ):
        conflicting = {left: str(tmp_path / "a"), right: str(tmp_path / "b")}
        try:
            store_env_dir_value(conflicting)
        except StoreResolutionError as exc:
            assert "split the ledger" in str(exc)
            assert left in str(exc) and right in str(exc)
        else:  # pragma: no cover - the refusal is the point
            raise AssertionError(f"conflicting store env values must refuse: {left} vs {right}")


def test_wrapper_binary_env_alias(monkeypatch) -> None:
    assert CLAUDE_WRAPPER.env_binary == "AGENTACCT_CLAUDE_BINARY"
    for name in ("AGENTACCT_CLAUDE_BINARY", "AGENT_CHRONICLE_CLAUDE_BINARY", "AGENT_SENTINEL_CLAUDE_BINARY"):
        monkeypatch.delenv(name, raising=False)
    # Oldest pre-rename alias is still honored on its own.
    monkeypatch.setenv("AGENT_SENTINEL_CLAUDE_BINARY", "/old/claude")
    assert build_agent_command(CLAUDE_WRAPPER, ["-p", "hi"]) == ["/old/claude", "-p", "hi"]
    # AGENT_CHRONICLE_* wins over AGENT_SENTINEL_*.
    monkeypatch.setenv("AGENT_CHRONICLE_CLAUDE_BINARY", "/mid/claude")
    assert build_agent_command(CLAUDE_WRAPPER, [])[0] == "/mid/claude"
    # New AGENTACCT_* primary wins over both.
    monkeypatch.setenv("AGENTACCT_CLAUDE_BINARY", "/new/claude")
    assert build_agent_command(CLAUDE_WRAPPER, [])[0] == "/new/claude"


# ---------------------------------------------------------------------------
# 4. mcp doctor probes BOTH registration keys and reports which is registered
# ---------------------------------------------------------------------------


def _doctor_checks(tmp_path, monkeypatch, mcp_json: dict) -> list[dict]:
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.delenv(LEGACY_ENV_STORE_DIR, raising=False)
    project = tmp_path / "project"
    (project / ".agent-sentinel" / "state").mkdir(parents=True)
    (project / ".mcp.json").write_text(json.dumps(mcp_json), encoding="utf-8")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["mcp", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["checks"]


def test_mcp_doctor_reports_pre_rename_registration(tmp_path, monkeypatch) -> None:
    server = {"command": "agent-sentinel", "args": ["mcp", "serve", "--store-dir", str(tmp_path / "project" / ".agent-sentinel" / "state")]}
    checks = _doctor_checks(tmp_path, monkeypatch, {"mcpServers": {"agent-sentinel": server}})
    named = [check["name"] for check in checks]
    assert "mcp config (.mcp.json [agent-sentinel])" in named
    assert "mcp config (.mcp.json [agent-chronicle])" not in named


def test_mcp_doctor_reports_both_keys_when_both_registered(tmp_path, monkeypatch) -> None:
    store = str(tmp_path / "project" / ".agent-sentinel" / "state")
    server_old = {"command": "agent-sentinel", "args": ["mcp", "serve", "--store-dir", store]}
    server_new = {"command": "agent-chronicle", "args": ["mcp", "serve", "--store-dir", store]}
    checks = _doctor_checks(
        tmp_path, monkeypatch, {"mcpServers": {"agent-chronicle": server_new, "agent-sentinel": server_old}}
    )
    named = [check["name"] for check in checks]
    assert "mcp config (.mcp.json [agent-chronicle])" in named
    assert "mcp config (.mcp.json [agent-sentinel])" in named


# ---------------------------------------------------------------------------
# 5. Frozen stored-data vocabulary tripwire
# ---------------------------------------------------------------------------
# If a rename sweep changes any assertion below, STOP: these strings live in
# historical stores/logs/files on real machines. They are data format, not
# branding. Extend matchers additively; never rename the stored spelling.


def _src(name: str) -> str:
    return (SRC_ROOT / name).read_text(encoding="utf-8")


def test_frozen_tool_names_and_semantic_kind() -> None:
    # HARD CUTOVER: the LIVE MCP tool names are now agentacct_* (new name only).
    assert [tool["name"] for tool in TOOLS] == [
        "agentacct_list_runs",
        "agentacct_get_report",
        "agentacct_record_machine_check",
        "agentacct_record_event",
        "agentacct_attach_client_context",
        "agentacct_record_section",
        "agentacct_record_agent_usage_debug",
        "agentacct_list_events",
        "agentacct_get_event_summary",
        "agentacct_prepare_judge",
        "agentacct_compute_value",
    ]
    # RECOGNIZE-MANY: the log-evidence creation-tool set (a READ path over
    # historical transcripts) accepts BOTH the new agentacct_* names AND the
    # pre-rename sentinel_* names, which historical transcripts carry forever.
    assert SENTINEL_CREATION_TOOLS == frozenset(
        {
            "agentacct_record_event",
            "agentacct_attach_client_context",
            "agentacct_record_section",
            "agentacct_record_agent_usage_debug",
            "agentacct_record_machine_check",
            "sentinel_record_event",
            "sentinel_attach_client_context",
            "sentinel_record_section",
            "sentinel_record_agent_usage_debug",
            "sentinel_record_machine_check",
        }
    )
    # sentinel_semantic_kind is STORED metadata (not a tool name) — still frozen.
    assert '"sentinel_semantic_kind": "section"' in _src("mcp.py")


def test_frozen_provenance_sources_and_schema_versions() -> None:
    assert LOCAL_USAGE_PROVENANCE == "agent_sentinel_local_usage_import"
    assert CLI_INSTRUMENTATION_PROVENANCE == "agent_sentinel_cli_instrumentation_marker"
    assert DIAGNOSTIC_EVENT_SOURCES == frozenset({"agent-sentinel-mcp-doctor", "agent-sentinel-mcp-workflow-smoke"})
    assert CLAUDE_CODE_HOOK_CONTEXT_SCHEMA == "agent-sentinel.client-context.v1"
    from agentacct.cli import INSTRUMENTATION_MARKER_SOURCE
    from agentacct.usage_cube import USAGE_SUMMARY_SCHEMA_VERSION
    from agentacct.work_ledger import SESSION_ROLLUP_SCHEMA_VERSION

    assert INSTRUMENTATION_MARKER_SOURCE == "agent-sentinel-setup"
    assert USAGE_SUMMARY_SCHEMA_VERSION == "agent-sentinel.usage-summary.v1"
    assert SESSION_ROLLUP_SCHEMA_VERSION == "agent-sentinel.session-rollup.v1"
    assert '"schema_version": "agent-sentinel.work-ledger.v2"' in _src("work_ledger.py")
    assert '"schema_version": "agent-sentinel.report.v1"' in _src("reports.py")
    assert '"schema_version": "agent-sentinel.judge-package.v1"' in _src("outcome.py")
    assert '"source": source or "agent-sentinel-mcp"' in _src("mcp.py")


def test_frozen_metadata_keys_wire_vocab_and_store_dirs() -> None:
    assert '"owned_by_sentinel": True' in _src("runner.py")
    assert '"AGENT_SENTINEL_RUN_ID": run_id' in _src("runner.py")
    # BOTH persisted run-metadata env keys are frozen (the child-process env
    # export is covered elsewhere; this pins the on-disk metadata shape).
    assert '"AGENT_SENTINEL_RUN_DIR": str(run_dir)' in _src("runner.py")
    assert 'sanitized["reserved_instrumentation_provenance_stripped"] = True' in _src("usage_truth.py")
    assert 'sanitized["reserved_client_context_provenance_stripped"] = True' in _src("service.py")
    proxy_src = _src("proxy.py")
    for error_type in (
        "agent_sentinel_budget_exceeded",
        "agent_sentinel_missing_budget",
        "agent_sentinel_missing_api_key",
        "agent_sentinel_invalid_api_key_format",
        "agent_sentinel_transport_error",
    ):
        assert error_type in proxy_src
    assert '"agent_sentinel": {' in proxy_src  # response envelope key
    assert 'x_agent_sentinel_run_id' in proxy_src  # X-Agent-Sentinel-Run-Id header
    assert '"agent_sentinel": {' in _src("hooks.py")  # hook decision key
    # /health now returns the agentacct-branded service string; the pre-rename
    # value stays ACCEPTED by the activation recognizer so cross-version
    # upgrades still recognize each other's dashboards. (The read-canary
    # recognizer was retired with the HTML display layer.)
    assert '"agentacct-local-api"' in _src("api.py")  # health service string (renamed)
    assert "agent-sentinel-local-api" in _src("activation.py")  # legacy still accepted
    assert '"agent_sentinel_pricing_catalog"' in _src("client_usage.py")
    assert '"agent_sentinel_builtin"' in _src("pricing_catalog.py")
    # Store dirs: fresh init keeps writing the pre-rename names forever.
    from agentacct.policy import DEFAULT_POLICY_FILE

    assert DEFAULT_POLICY_FILE == Path(".agent-sentinel/policy.yaml")
    assert '".agent-sentinel" / "state"' in _src("store_resolution.py")
    assert '"$HOME/.agent-sentinel-global/state"' in install_guide.GLOBAL_INSTALL_BLOCK
    from agentacct.hooks import (
        CLAUDE_HOOK_RELATIVE_PATH,
        CLAUDE_SETTINGS_RELATIVE_PATH,
        LEGACY_CLAUDE_HOOK_RELATIVE_PATH,
    )

    # F3: the wrapper BASENAME is frozen (doctor + the settings-command matcher
    # recognize an install by this filename), but the directory moved out of the
    # store dir so a store move can't vanish it. The pre-relocation path stays
    # recognized forever via LEGACY_CLAUDE_HOOK_RELATIVE_PATH.
    assert CLAUDE_HOOK_RELATIVE_PATH == Path(".claude/hooks/claude_pre_tool_use.py")
    assert LEGACY_CLAUDE_HOOK_RELATIVE_PATH == Path(".agent-sentinel/hooks/claude_pre_tool_use.py")
    assert CLAUDE_HOOK_RELATIVE_PATH.name == LEGACY_CLAUDE_HOOK_RELATIVE_PATH.name == "claude_pre_tool_use.py"
    assert CLAUDE_SETTINGS_RELATIVE_PATH == Path(".claude/settings.agent-sentinel.example.json")
    # Legacy managed markers stay recognized verbatim.
    assert install_guide.LEGACY_INSTRUCTIONS_BEGIN_MARKER == (
        "<!-- agent-sentinel:begin (managed block — edit via `agent-sentinel setup instructions`) -->"
    )
    assert install_guide.LEGACY_INSTRUCTIONS_END_MARKER == "<!-- agent-sentinel:end -->"
    # Binary discovery keeps the pre-rename names, new-first.
    assert _AGENT_CHRONICLE_EXECUTABLE_NAMES == (
        "agentacct",
        "agentacct.exe",
        "agent-chronicle",
        "agent-chronicle.exe",
        "agent-sentinel",
        "agent-sentinel.exe",
    )


# ---------------------------------------------------------------------------
# 6. Packaging pins
# ---------------------------------------------------------------------------


def test_pyproject_name_alias_scripts_and_build_system() -> None:
    payload = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert payload["project"]["name"] == "agentacct"
    scripts = payload["project"]["scripts"]
    # Public primary console command.
    assert scripts["agentacct"] == "agentacct.cli:app"
    # The published package ships ONLY agentacct-branded scripts. The old
    # "agent-chronicle" / "agent-sentinel" console scripts collide with
    # unrelated PyPI packages, so they are not shipped.
    assert scripts["agentacct-claude"] == "agentacct.wrappers:sentinel_claude_main"
    assert scripts["agentacct-codex"] == "agentacct.wrappers:sentinel_codex_main"
    assert "agent-chronicle" not in scripts
    assert "agent-sentinel" not in scripts
    build_system = payload["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert any(req.startswith("setuptools>=") for req in build_system["requires"])
    assert payload["project"]["license"] == "MIT"
    assert payload["project"]["license-files"] == ["LICENSE"]
    assert "License :: OSI Approved :: MIT License" not in payload["project"]["classifiers"]


def test_packaging_excludes_tests_from_shipped_artifacts() -> None:
    # The repo is the test suite's home. Shipping tests/ in the sdist without
    # conftest.py would ship a suite with the store-safety net stripped (the
    # autouse store pin + real-store tripwire live in conftest.py): stray
    # `pytest` runs from an unpacked sdist could write into a real ledger.
    payload = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # Wheel: explicit src-layout discovery — only src/ packages ship.
    assert payload["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    # sdist: prune the test suite (and examples, which tests import).
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune tests" in manifest
    assert "prune examples" in manifest


# ---------------------------------------------------------------------------
# 7. Repo hygiene: the old module name must be gone
# ---------------------------------------------------------------------------
# The dev venv installs the MAIN checkout editable, so `import agent_sentinel`
# still resolves there — a missed import in this repo would silently pass its
# tests against the OLD code. Drive module-level usage to zero, and require
# every remaining old-name occurrence to be one of the frozen-vocabulary
# spellings pinned above.

_MODULE_USAGE = re.compile(r"(?:\bfrom\s+agent_sentinel\b|\bimport\s+agent_sentinel\b|\bagent_sentinel\.)")

# Longer-first tokens whose presence legitimizes an old-name occurrence.
_ALLOWED_OLD_NAME_TOKENS = (
    # underscore vocabulary (stored/wire)
    "agent_sentinel_local_usage_import",
    "agent_sentinel_cli_instrumentation_marker",
    "agent_sentinel_pricing_catalog",
    "agent_sentinel_builtin",
    "agent_sentinel_catalog",
    "agent_sentinel_budget_exceeded",
    "agent_sentinel_missing_budget",
    "agent_sentinel_missing_api_key",
    "agent_sentinel_invalid_api_key_format",
    "agent_sentinel_transport_error",
    "x_agent_sentinel_run_id",
    "mcp__not_agent_sentinel_fake",
    "mcp__agent_sentinel",
    '"agent_sentinel"',
    "AGENT_SENTINEL_",
    # hyphen vocabulary (store dirs, sources, schemas, markers, binaries)
    ".agent-sentinel",  # store dir, policy file, -global, -cli, example filename
    "agent-sentinel-setup",
    "agent-sentinel-mcp",
    "agent-sentinel-workflow-smoke",
    "agent-sentinel-doctor-probe",
    "agent-sentinel-smoke",
    "agent-sentinel-local-api",
    "agent-sentinel.work-ledger",
    "agent-sentinel.usage-summary",
    "agent-sentinel.client-context",
    "agent-sentinel.report",
    "agent-sentinel.session-rollup",
    "agent-sentinel.judge-package",
    # NOTE: the pre-rename GitHub repo URLs (github.com and raw.githubusercontent.com
    # paths ending in the old repo name) are deliberately NOT in this freeze list:
    # the public release ships from a fresh agent-chronicle repo with no redirects,
    # so any reintroduction of an old repo URL would 404 for public users and must
    # fail this tripwire.
    "agent-sentinel:begin",
    "agent-sentinel:end",
    "mcp__agent-sentinel__",
    "[mcp_servers.agent-sentinel]",
    '"agent-sentinel"',  # quoted binary/server-key literals in dual-name code
    "'agent-sentinel'",
    "`agent-sentinel`",
    '"agent-sentinel.exe"',
    "`agent-sentinel setup instructions`",  # part of the frozen legacy marker text
    '"agent_sentinel_*"',  # freeze-comment shorthand for the proxy error family
    '\\"agent-sentinel\\"',  # escaped-quote form inside test TOML/JSON fixtures
    "bin/agent-sentinel",  # embedded legacy binary paths in wrapper fixtures
    # Preserved dated-evidence command shape: docs/live-smoke-results.md keeps
    # the pre-rename smoke commands its 2026-06-27 run actually used, and the
    # doc-gate test quotes them (dated evidence keeps the observed name).
    "agent-sentinel smoke",
    # Stale-registration remediation (fix/opencode-connection): the MCP preview
    # and install notes deliberately name the old server so the user can REMOVE
    # a leftover pre-rename registration (its old binary no longer exists — the
    # ENOENT that reads as a crash). These two forms carry that guidance.
    "mcp remove agent-sentinel",  # `opencode mcp remove agent-sentinel` remediation command
    "agent-sentinel/agent-chronicle",  # prose pairing in the "remove any stale ..." guidance
    # Frozen refusal wire text: codex wraps every failed MCP call as
    # "tool call failed for `<server key>/<tool>`", and historical rollouts
    # carry the pre-rename key in that slot forever. log_evidence's refusal
    # classifier must keep reading those, so the fixture pinning that behaviour
    # keeps the observed string.
    "`agent-sentinel/",
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in (REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "examples"):
        files.extend(
            path
            for path in root.rglob("*.py")
            # This file defines the freeze list, so it may spell the frozen
            # vocabulary (and the scanning regexes) without tripping itself.
            if "__pycache__" not in path.parts and path.name != "test_rename_compat.py"
        )
    return files


def test_old_module_name_never_imported_anywhere() -> None:
    assert not (REPO_ROOT / "src" / "agent_sentinel").exists()
    offenders: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if _MODULE_USAGE.search(line) and "mcp__agent_sentinel" not in line and "not_agent_sentinel" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], "old module usage would silently run the venv's MAIN-checkout install:\n" + "\n".join(offenders)


def test_remaining_old_name_occurrences_are_frozen_vocabulary_only() -> None:
    offenders: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if "agent-sentinel" not in line and "agent_sentinel" not in line:
                continue
            residual = line
            for token in sorted(_ALLOWED_OLD_NAME_TOKENS, key=len, reverse=True):
                residual = residual.replace(token, "")
            if "agent-sentinel" in residual or "agent_sentinel" in residual:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "old-name occurrence outside the frozen-vocabulary allowlist "
        "(rename it, or add it to the freeze list WITH a pin comment):\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 8. Bare-brand sweep: "Sentinel" as a product name is retired
# ---------------------------------------------------------------------------
# The word-bounded scan misses CamelCase identifiers (SentinelService),
# SCREAMING_CASE constants, and lowercase frozen vocabulary on purpose —
# those are code identifiers and stored/wire spellings, not brand prose.

_BARE_BRAND = re.compile(r"\bSentinel\b")

# Deliberate retentions of the capitalized brand word. Everything else is a
# product reference and must say Chronicle.
_ALLOWED_BARE_BRAND_TOKENS = (
    "X-Agent-Sentinel-Run-Id",  # frozen wire header (proxy contract)
    "# Agent Sentinel MCP",  # frozen codex config comment marker (upsert)
    "## Agent Sentinel",  # legacy instruction-section markers, recognized forever
    '"# Agent Sentinel"',
    '"Agent Sentinel"',  # comments naming the legacy section spelling
    "Agent Sentinel to Agent Chronicle",  # the rename note itself (env_compat)
    # Quoted preserved dated evidence (docs/public-alpha-checklist.md keeps
    # the observed wording of the pre-rename MCP client probe).
    "successfully called Sentinel MCP tools",
)


def test_bare_brand_word_is_gone_from_python_sources() -> None:
    offenders: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not _BARE_BRAND.search(line):
                continue
            residual = line
            for token in sorted(_ALLOWED_BARE_BRAND_TOKENS, key=len, reverse=True):
                residual = residual.replace(token, "")
            if _BARE_BRAND.search(residual):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "bare 'Sentinel' used as the product name (rename the prose to Chronicle, "
        "or add the frozen spelling to the allowlist WITH a pin comment):\n" + "\n".join(offenders)
    )


# Public docs scanned for the bare brand word. The four dated-evidence pages
# are excluded on purpose: dated maintainer evidence keeps the name under
# which it was observed, forever (see test_dated_evidence_* below), and
# PROGRESS.md is preserved history.
_DATED_EVIDENCE_DOCS = {
    "docs/live-smoke-results.md",
    "docs/live-mcp-client-smoke-results.md",
    "docs/coding-agent-integrations.md",
    "docs/public-alpha-checklist.md",
}

_ALLOWED_DOC_BRAND_TOKENS = (
    "formerly Agent Sentinel",  # migration note naming the old brand once
)


def _public_docs() -> list[Path]:
    docs = [REPO_ROOT / name for name in ("README.md", "INSTALL.md", "SECURITY.md", "CONTRIBUTING.md")]
    docs.extend(sorted((REPO_ROOT / "docs").glob("*.md")))
    docs.extend(sorted((REPO_ROOT / ".github").rglob("*.yml")))
    docs.extend(sorted((REPO_ROOT / "integrations").rglob("*.md")))
    return [
        path
        for path in docs
        if path.exists() and str(path.relative_to(REPO_ROOT)) not in _DATED_EVIDENCE_DOCS
    ]


def test_bare_brand_word_is_gone_from_public_docs() -> None:
    offenders: list[str] = []
    for path in _public_docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not _BARE_BRAND.search(line):
                continue
            residual = line
            for token in sorted(_ALLOWED_DOC_BRAND_TOKENS, key=len, reverse=True):
                residual = residual.replace(token, "")
            if _BARE_BRAND.search(residual):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "bare 'Sentinel' used as the product name in public docs "
        "(rename it to Chronicle, or add a pinned allowlist token):\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 9. Dated-evidence honesty: observations keep the observed name, forever
# ---------------------------------------------------------------------------


def test_dated_evidence_keeps_the_observed_pre_rename_names() -> None:
    """The AGENT_CHRONICLE_* markers and the agent-chronicle registration key
    did not exist when the recorded release-gate runs happened, so the
    preserved evidence can only truthfully name their AGENT_SENTINEL_* /
    agent-sentinel forms. A sweep that modernizes a dated observation
    falsifies the release-gate audit trail and must fail here."""
    smoke = (REPO_ROOT / "docs" / "live-smoke-results.md").read_text(encoding="utf-8")
    assert "AGENT_SENTINEL_CLAUDE_WRAP_OK" in smoke
    assert "AGENT_SENTINEL_CODEX_WRAP_OK" in smoke
    assert "AGENT_CHRONICLE_CLAUDE_WRAP_OK" not in smoke
    assert "AGENT_CHRONICLE_CODEX_WRAP_OK" not in smoke

    mcp_page = (REPO_ROOT / "docs" / "live-mcp-client-smoke-results.md").read_text(encoding="utf-8")
    assert "agent-sentinel-sentinel_get_event_summary" in mcp_page  # Claude tool name
    assert "agent-sentinel.sentinel_get_event_summary" in mcp_page  # Codex tool name
    assert "[mcp_servers.agent-sentinel]" in mcp_page  # the codex config used
    assert '"agent-sentinel"' in mcp_page  # the .mcp.json key used
    assert "agent-chronicle-sentinel_get_event_summary" not in mcp_page

    integrations = (REPO_ROOT / "docs" / "coding-agent-integrations.md").read_text(encoding="utf-8")
    assert "`agent-sentinel mcp serve`" in integrations  # Hermes probe record
    assert "agent-sentinel_sentinel_record_event" in integrations  # OpenCode call
    assert "agent-sentinel__sentinel_record_event" in integrations  # OpenClaw call

    # Each restored page says the preservation is deliberate.
    for text in (smoke, mcp_page, integrations):
        assert "pre-rename" in text
