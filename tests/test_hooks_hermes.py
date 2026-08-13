"""Hermes observe-only shell hooks: the three capture paths, the turn-boundary
heartbeat, the config.yaml ``hooks:`` writer, the wrapper, and the CLI subcommands.

Hermes' shell-hook STDIN is a single serialization point whose top-level keys
``hook_event_name`` / ``tool_name`` / ``session_id`` are the SAME snake_case as
Claude Code + Codex, and its ``session_id`` equals the ``state.db`` sessions.id
the usage importer keys on — verified on the real machine — so tool ticks and
checks attribute straight to the Hermes session with no log pairing. Unlike a
Claude Code / Codex ``SessionEnd`` (once, at real close), a hermes
``on_session_end`` fires per TURN, so it is captured as a distinct
``turn_boundary_observed`` liveness heartbeat that is NEVER consumed as a session
close (no false ``ended_open`` between turns).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentacct.cli import (
    _hermes_hook_command,
    _install_hermes_hook,
    _write_hermes_hooks_config_at,
    app,
)
from agentacct.hooks import (
    HERMES_CONFIG_RELATIVE_PATH,
    HERMES_HOOK_EVENT_SUBCOMMANDS,
    HERMES_HOOK_RELATIVE_PATH,
    capture_hermes_tool_check,
    capture_hermes_turn_boundary,
    capture_tool_activity,
    render_hermes_hook_wrapper,
)
from agentacct.mechanical_capture import drain_mechanical_check_spool
from agentacct.session_lifecycle import (
    TURN_BOUNDARY_EVENT_TYPE,
    build_session_end_by_session,
    drain_turn_boundary_spool,
    record_turn_boundary_tick,
)
from agentacct.tool_activity import drain_tool_activity_spool

WRAPPER_BASENAME = HERMES_HOOK_RELATIVE_PATH.name
_COMMAND = f"/usr/bin/python3 /home/u/.hermes/hooks/{WRAPPER_BASENAME}"


def _pre(session_id: str, tool_name: str = "terminal", command: str = "echo hi") -> str:
    return json.dumps(
        {
            "hook_event_name": "pre_tool_call",
            "tool_name": tool_name,
            "tool_input": {"command": command},
            "session_id": session_id,
            "cwd": "/x",
            "extra": {"task_id": "t1", "tool_call_id": "c1", "turn_id": "tt"},
        }
    )


def _post(session_id: str, command: str, *, exit_code: int = 0, result_as_string: bool = False, tool_name: str = "terminal") -> str:
    result: object = {"output": "x", "exit_code": exit_code, "error": None}
    if result_as_string:
        result = json.dumps(result)
    return json.dumps(
        {
            "hook_event_name": "post_tool_call",
            "tool_name": tool_name,
            "tool_input": {"command": command},
            "session_id": session_id,
            "cwd": "/x",
            "extra": {"result": result, "status": "success", "duration_ms": 42},
        }
    )


def _end(session_id: str, *, completed: bool = True, interrupted: bool = False) -> str:
    return json.dumps(
        {
            "hook_event_name": "on_session_end",
            "session_id": session_id,
            "cwd": "/x",
            "extra": {"completed": completed, "interrupted": interrupted, "turn_id": "tt"},
        }
    )


# ---------------------------------------------------------------------------
# #1 tool activity
# ---------------------------------------------------------------------------


def test_capture_tool_activity_labels_hermes_client(tmp_path: Path) -> None:
    capture_tool_activity(_pre("20260813_031424_2b0e74", "terminal"), store_dir=tmp_path, client="hermes")
    events = drain_tool_activity_spool(tmp_path)
    assert len(events) == 1
    meta = events[0]["metadata"]
    assert events[0]["source"] == "hermes"
    assert meta["client_session_id"] == "20260813_031424_2b0e74"
    assert {"name": "terminal", "count": 1} in meta["tool_names"]


# ---------------------------------------------------------------------------
# #3 exit-code mechanical check
# ---------------------------------------------------------------------------


def _drain_checks(tmp_path: Path) -> list:
    envelopes = drain_mechanical_check_spool(tmp_path)
    return envelopes


def test_hermes_tool_check_records_recognized_check(tmp_path: Path) -> None:
    capture_hermes_tool_check(_post("s1", "pytest -q", exit_code=0), store_dir=tmp_path)
    envelopes = _drain_checks(tmp_path)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.source_system == "hermes"
    # Provenance names the hermes wire, not Claude Code's PostToolUse.
    assert env.source_schema == "hermes_post_tool_call.v1"
    assert env.adapter == "hermes_post_tool_call"


def test_hermes_tool_check_reads_exit_from_string_result(tmp_path: Path) -> None:
    # Some hermes tools carry extra.result as a JSON STRING, not a dict.
    capture_hermes_tool_check(_post("s1", "npm run build", exit_code=2, result_as_string=True), store_dir=tmp_path)
    envelopes = _drain_checks(tmp_path)
    assert len(envelopes) == 1
    attrs = envelopes[0].payload["attributes"]
    assert attrs["exit_code"] == 2
    assert attrs["passed"] is False


def test_hermes_tool_check_skips_non_check_command(tmp_path: Path) -> None:
    capture_hermes_tool_check(_post("s1", "echo hi", exit_code=0), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_hermes_tool_check_skips_non_terminal_tool(tmp_path: Path) -> None:
    capture_hermes_tool_check(_post("s1", "pytest -q", tool_name="web_search"), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_hermes_tool_check_skips_missing_exit_code(tmp_path: Path) -> None:
    event = {
        "hook_event_name": "post_tool_call",
        "tool_name": "terminal",
        "tool_input": {"command": "pytest -q"},
        "session_id": "s1",
        "extra": {"result": {"output": "x", "error": None}},  # no exit_code
    }
    capture_hermes_tool_check(json.dumps(event), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_hermes_tool_check_never_raises_on_garbage(tmp_path: Path) -> None:
    capture_hermes_tool_check("not json", store_dir=tmp_path)
    capture_hermes_tool_check(json.dumps([1, 2, 3]), store_dir=tmp_path)
    assert _drain_checks(tmp_path) == []


def test_hermes_tool_check_attributes_input_session(tmp_path: Path) -> None:
    # Invariant 6: the check must key to the session it came from, unmodified.
    capture_hermes_tool_check(_post("20260813_031424_2b0e74", "pytest -q"), store_dir=tmp_path)
    env = _drain_checks(tmp_path)[0]
    assert env.source_instance == "20260813_031424_2b0e74"
    assert getattr(env.subjects, "client_session_id", None) == "20260813_031424_2b0e74"


def test_hermes_captures_reject_overlong_session_id(tmp_path: Path) -> None:
    # Invariant 6: a session id past the bound (241+ chars) is dropped, never spooled.
    overlong = "x" * 5000
    capture_hermes_tool_check(_post(overlong, "pytest -q"), store_dir=tmp_path)
    capture_hermes_turn_boundary(_end(overlong), store_dir=tmp_path)
    capture_tool_activity(_pre(overlong), store_dir=tmp_path, client="hermes")
    assert _drain_checks(tmp_path) == []
    assert drain_turn_boundary_spool(tmp_path) == []
    assert drain_tool_activity_spool(tmp_path) == []


# ---------------------------------------------------------------------------
# #4 turn-boundary heartbeat (NEVER ended_open)
# ---------------------------------------------------------------------------


def test_turn_boundary_records_liveness_with_flags(tmp_path: Path) -> None:
    capture_hermes_turn_boundary(_end("s1", completed=True, interrupted=False), store_dir=tmp_path)
    events = drain_turn_boundary_spool(tmp_path)
    assert len(events) == 1
    assert events[0]["event_type"] == TURN_BOUNDARY_EVENT_TYPE
    meta = events[0]["metadata"]
    assert meta["client"] == "hermes"
    assert meta["client_session_id"] == "s1"
    assert meta["completed"] is True
    assert meta["interrupted"] is False


def test_turn_boundary_latest_per_session_wins(tmp_path: Path) -> None:
    record_turn_boundary_tick(tmp_path, client="hermes", session_id="s1", at=100.0, completed=False)
    record_turn_boundary_tick(tmp_path, client="hermes", session_id="s1", at=200.0, completed=True)
    events = drain_turn_boundary_spool(tmp_path)
    assert len(events) == 1
    assert events[0]["created_at"] == 200.0
    assert events[0]["metadata"]["completed"] is True


def test_turn_boundary_never_derives_ended_open(tmp_path: Path) -> None:
    capture_hermes_turn_boundary(_end("s1"), store_dir=tmp_path)
    events = drain_turn_boundary_spool(tmp_path)
    # A turn-boundary event is pure liveness: build_session_end_by_session (which
    # keys ended_open on SESSION_END_EVENT_TYPE) must return NO end for it.
    assert build_session_end_by_session(events) == {}


# ---------------------------------------------------------------------------
# wrapper
# ---------------------------------------------------------------------------


def test_hermes_wrapper_renders_valid_python_with_event_map(tmp_path: Path) -> None:
    source = render_hermes_hook_wrapper("/abs/agentacct", store_dir=tmp_path)
    ast.parse(source)  # valid python
    assert "hooks" in source and "hermes" in source
    # dispatch map is embedded so the wrapper routes on hook_event_name
    for event, subcommand in HERMES_HOOK_EVENT_SUBCOMMANDS.items():
        assert event in source
        assert subcommand in source
    assert "--store-dir" in source and str(tmp_path) in source
    assert "timeout=5" in source


# ---------------------------------------------------------------------------
# config.yaml hooks: writer
# ---------------------------------------------------------------------------

_BASE_CONFIG = (
    "model:\n"
    "  default: gpt-5.5\n"
    "hooks:\n"
    "  post_llm_call:\n"
    "  - command: /home/u/.hermes/scripts/lark.py\n"
    "    timeout: 30\n"
    "hooks_auto_accept: false\n"
    "mcp_servers:\n"
    "  agentacct:\n"
    "    command: agentacct\n"
)


def _load(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_bytes().decode("utf-8"))


def test_hooks_writer_fresh_insert_preserves_everything(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE_CONFIG, encoding="utf-8")
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "updated"
    data = _load(cfg)
    for event in HERMES_HOOK_EVENT_SUBCOMMANDS:
        entries = data["hooks"][event]
        assert entries[0]["command"] == _COMMAND
        assert entries[0]["timeout"] == 10
    # the user's lark hook and every non-hooks section survive untouched
    assert data["hooks"]["post_llm_call"] == [{"command": "/home/u/.hermes/scripts/lark.py", "timeout": 30}]
    assert data["hooks_auto_accept"] is False
    assert data["mcp_servers"] == {"agentacct": {"command": "agentacct"}}


def test_hooks_writer_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE_CONFIG, encoding="utf-8")
    _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    first = cfg.read_bytes()
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "unchanged"
    assert cfg.read_bytes() == first  # byte-identical on re-run


def test_hooks_writer_reinstall_replaces_in_place_no_duplicate(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE_CONFIG, encoding="utf-8")
    _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    other = f"/opt/py/python3 /home/u/.hermes/hooks/{WRAPPER_BASENAME}"
    _, action = _write_hermes_hooks_config_at(cfg, other, WRAPPER_BASENAME)
    assert action == "updated"
    data = _load(cfg)
    for event in HERMES_HOOK_EVENT_SUBCOMMANDS:
        ours = [e for e in data["hooks"][event] if WRAPPER_BASENAME in (e.get("command") or "")]
        assert len(ours) == 1  # the stale interpreter entry was replaced, not duplicated
        assert ours[0]["command"] == other


def test_hooks_writer_preserves_bytes_outside_hooks_region(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE_CONFIG, encoding="utf-8")
    _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    text = cfg.read_bytes().decode("utf-8")
    # Everything before the hooks: header and after the hooks region is verbatim.
    assert text.startswith("model:\n  default: gpt-5.5\n")
    assert "hooks_auto_accept: false\nmcp_servers:\n  agentacct:\n    command: agentacct\n" in text


def test_hooks_writer_preserves_user_entry_under_shared_event(tmp_path: Path) -> None:
    config = (
        "hooks:\n"
        "  pre_tool_call:\n"
        "  - command: /home/u/.hermes/scripts/user_gate.py\n"
        "    timeout: 15\n"
        "other: 1\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    data = _load(cfg)
    commands = [e["command"] for e in data["hooks"]["pre_tool_call"]]
    assert _COMMAND in commands
    assert "/home/u/.hermes/scripts/user_gate.py" in commands  # user's own entry kept
    assert data["other"] == 1


def test_hooks_writer_appends_block_when_no_hooks_key(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: gpt-5.5\n", encoding="utf-8")
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "updated"
    data = _load(cfg)
    assert set(HERMES_HOOK_EVENT_SUBCOMMANDS).issubset(data["hooks"].keys())


def test_hooks_writer_creates_fresh_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "wrote"
    data = _load(cfg)
    assert set(HERMES_HOOK_EVENT_SUBCOMMANDS).issubset(data["hooks"].keys())


def test_hooks_writer_refuses_inline_hooks_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("hooks: {}\nother: 1\n", encoding="utf-8")
    before = cfg.read_bytes()
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "skipped-unparsed"
    assert cfg.read_bytes() == before  # left untouched


def test_hooks_writer_refuses_tab_indented_children(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("hooks:\n\tpost_llm_call:\n\t- command: x\n", encoding="utf-8")
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "skipped-unparsed"


def test_hooks_writer_preserves_crlf(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(_BASE_CONFIG.replace("\n", "\r\n").encode("utf-8"))
    _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    raw = cfg.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare LF introduced


def test_hooks_writer_handles_deeper_indented_sequence_style(tmp_path: Path) -> None:
    # A user config whose list items are indented one level DEEPER than the key
    # (equally valid YAML). Our entry must be written at the SAME indent so the
    # sequence stays consistent (mixed indents would make PyYAML reject the file).
    config = (
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - command: /home/u/user_gate.py\n"
        "      timeout: 15\n"
        "other: 1\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "updated"
    data = _load(cfg)  # must not raise — the sequence indent stayed consistent
    commands = [e["command"] for e in data["hooks"]["pre_tool_call"]]
    assert _COMMAND in commands
    assert "/home/u/user_gate.py" in commands  # user's deeper-indented entry kept
    assert data["other"] == 1
    # re-run is idempotent even in the deeper style (no duplicate, no corruption)
    _, action2 = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action2 == "unchanged"


def test_hooks_writer_refuses_space_before_colon_hooks_key(tmp_path: Path) -> None:
    # `hooks :` parses as the hooks mapping but header_re does not match it;
    # appending a second hooks: block would clobber the user's hooks, so refuse.
    config = "hooks :\n  pre_tool_call:\n  - command: /home/u/user_gate.py\n    timeout: 15\n"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    before = cfg.read_bytes()
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "skipped-unparsed"
    assert cfg.read_bytes() == before  # user's hooks untouched, not duplicated


def test_hooks_writer_refuses_inline_event_value(tmp_path: Path) -> None:
    # An event present as an inline value (`pre_tool_call: []`) cannot be edited
    # textually without duplicating the key, so the writer refuses rather than corrupt.
    config = "hooks:\n  pre_tool_call: []\nother: 1\n"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    before = cfg.read_bytes()
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "skipped-unparsed"
    assert cfg.read_bytes() == before


def test_hooks_writer_refuses_block_mapping_event(tmp_path: Path) -> None:
    # An event whose value is a block MAPPING (not a sequence) must be refused, not
    # rewritten as a list — otherwise its children corrupt into our list item.
    config = (
        "hooks:\n"
        "  pre_tool_call:\n"
        "    command: /bin/user-hook\n"
        "    timeout: 5\n"
        "other:\n"
        "  keep: yes\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    before = cfg.read_bytes()
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "skipped-unparsed"
    assert cfg.read_bytes() == before  # user's mapping + data untouched


def test_hooks_writer_keeps_user_entry_naming_wrapper_as_argument(tmp_path: Path) -> None:
    # A user hook that merely NAMES the wrapper file as an argument (running a
    # different executable) is not ours and must be preserved on reinstall.
    user_cmd = f"/usr/bin/watchdog --watch {WRAPPER_BASENAME} --restart"
    config = f'hooks:\n  pre_tool_call:\n  - command: "{user_cmd}"\n    timeout: 30\n'
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    data = _load(cfg)
    commands = [e["command"] for e in data["hooks"]["pre_tool_call"]]
    assert _COMMAND in commands
    assert user_cmd in commands  # the watchdog entry was NOT misclassified as ours


def test_hooks_writer_dedupes_folded_scalar_prior_entry(tmp_path: Path) -> None:
    # A prior agentacct entry a formatter rewrote as a folded scalar must still be
    # recognized and replaced on reinstall (no duplicate wrapper entry). Our command
    # is always <python-interpreter> <wrapper>, so the prior interpreter is python-named.
    old = f"/old/python3 /home/u/.hermes/hooks/{WRAPPER_BASENAME}"
    config = (
        "hooks:\n"
        "  pre_tool_call:\n"
        "  - command: >-\n"
        f"      {old}\n"
        "    timeout: 10\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    _, action = _write_hermes_hooks_config_at(cfg, _COMMAND, WRAPPER_BASENAME)
    assert action == "updated"
    data = _load(cfg)
    ours = [e for e in data["hooks"]["pre_tool_call"] if WRAPPER_BASENAME in (e.get("command") or "")]
    assert len(ours) == 1  # the folded prior entry was replaced, not duplicated
    assert ours[0]["command"] == _COMMAND


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------


def test_install_hermes_hook_writes_wrapper_and_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".hermes").mkdir()
    (home / ".hermes" / "config.yaml").write_text(_BASE_CONFIG, encoding="utf-8")
    store = tmp_path / "store"
    action, wrapper_path = _install_hermes_hook(home, store, "/abs/agentacct")
    assert action == "updated"
    assert wrapper_path == home / HERMES_HOOK_RELATIVE_PATH
    assert wrapper_path.exists()
    # wrapper is executable
    assert wrapper_path.stat().st_mode & 0o111
    data = _load(home / HERMES_CONFIG_RELATIVE_PATH)
    expected = _hermes_hook_command(wrapper_path)
    for event in HERMES_HOOK_EVENT_SUBCOMMANDS:
        assert data["hooks"][event][0]["command"] == expected


def test_install_hermes_hook_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(_BASE_CONFIG, encoding="utf-8")
    store = tmp_path / "store"
    _install_hermes_hook(home, store, "/abs/agentacct")
    config_bytes = (home / HERMES_CONFIG_RELATIVE_PATH).read_bytes()
    action, _ = _install_hermes_hook(home, store, "/abs/agentacct")
    assert action == "unchanged"
    assert (home / HERMES_CONFIG_RELATIVE_PATH).read_bytes() == config_bytes


# ---------------------------------------------------------------------------
# CLI subcommands (fail-open, spool)
# ---------------------------------------------------------------------------


def test_pre_tool_call_subcommand_spools_tick(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "hermes", "pre-tool-call", "--store-dir", str(tmp_path)],
        input=_pre("s1", "terminal"),
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
    assert len(drain_tool_activity_spool(tmp_path)) == 1


def test_post_tool_call_subcommand_spools_check(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "hermes", "post-tool-call", "--store-dir", str(tmp_path)],
        input=_post("s1", "pytest -q"),
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
    assert len(drain_mechanical_check_spool(tmp_path)) == 1


def test_session_end_subcommand_spools_turn_boundary(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "hermes", "session-end", "--store-dir", str(tmp_path)],
        input=_end("s1"),
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
    assert len(drain_turn_boundary_spool(tmp_path)) == 1


@pytest.mark.parametrize("subcommand", ["pre-tool-call", "post-tool-call", "session-end"])
def test_subcommands_fail_open_on_garbage(subcommand: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["hooks", "hermes", subcommand, "--store-dir", str(tmp_path)],
        input="}{not json",
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "{}"
