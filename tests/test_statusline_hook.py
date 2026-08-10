"""Tests for the terminal-CLI Claude statusLine rate-limit path."""

from __future__ import annotations

import io
import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct import rate_limits as rl
from agentacct import statusline_hook
from agentacct.cli import app
from agentacct.service import SentinelService


def _statusline_payload(five=18, seven=63):
    return {
        "model": {"display_name": "Opus 4.8", "id": "claude-opus-4-8"},
        "context_window": {"used_percentage": 42},
        "cost": {"total_cost_usd": 1.27},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": 1785600000},
            "seven_day": {"used_percentage": seven, "resets_at": 1785900000},
        },
    }


# --------------------------------------------------------------------------- #
# normalization + stream identity
# --------------------------------------------------------------------------- #


def test_normalize_statusline_has_windows_resets_and_origin():
    snap = rl.normalize_claude_statusline(_statusline_payload(), captured_at=1000.0)
    assert snap is not None
    assert snap.client == "claude-code"
    assert snap.origin == rl.ORIGIN_CLAUDE_STATUSLINE
    windows = {w.kind: w for w in snap.windows}
    assert windows["5h"].used_percent == 18.0 and windows["5h"].resets_at == 1785600000
    assert windows["7d"].used_percent == 63.0 and windows["7d"].resets_at == 1785900000


def test_normalize_statusline_rejects_missing_and_empty():
    assert rl.normalize_claude_statusline(None) is None
    assert rl.normalize_claude_statusline({}) is None  # no rate_limits
    assert rl.normalize_claude_statusline({"rate_limits": {}}) is None  # no windows
    # A window missing used_percentage is skipped, keeping the other.
    snap = rl.normalize_claude_statusline(
        {"rate_limits": {"five_hour": {"resets_at": 1}, "seven_day": {"used_percentage": 5}}}
    )
    assert [w.kind for w in snap.windows] == ["7d"]


def test_statusline_is_a_distinct_stream_from_plan_usage():
    sl = rl.normalize_claude_statusline(_statusline_payload())
    pu = rl.normalize_claude_plan_usage_sample({"t": 1, "org": "o", "u": {"fh": 1, "sd": 2}})
    sl_event = rl.snapshot_to_event(sl)
    pu_event = rl.snapshot_to_event(pu)
    assert sl_event["source"] == rl.CLAUDE_STATUSLINE_SOURCE
    assert sl_event["run_id"] == rl.CLAUDE_STATUSLINE_RUN_ID
    assert sl_event["metadata"]["origin"] == rl.ORIGIN_CLAUDE_STATUSLINE
    # distinct stream keys so a hybrid user's two feeds never collide
    assert (sl_event["source"], sl_event["run_id"]) != (pu_event["source"], pu_event["run_id"])


# --------------------------------------------------------------------------- #
# spool round-trip + path resolution
# --------------------------------------------------------------------------- #


def test_spool_write_read_roundtrip(tmp_path):
    spool = tmp_path / "statusline-latest.json"
    ok = rl.write_claude_statusline_spool(
        _statusline_payload()["rate_limits"], captured_at=1234.5, path=spool
    )
    assert ok and spool.exists()
    snap = rl.read_claude_statusline_latest(spool)
    assert snap is not None and snap.captured_at == 1234.5
    assert {w.kind for w in snap.windows} == {"5h", "7d"}
    # missing / malformed → None
    assert rl.read_claude_statusline_latest(tmp_path / "nope.json") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert rl.read_claude_statusline_latest(tmp_path / "bad.json") is None


def test_spool_path_follows_env_and_claude_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(rl.STATUSLINE_SPOOL_ENV, str(tmp_path / "explicit.json"))
    assert rl.default_claude_statusline_spool_path() == tmp_path / "explicit.json"
    monkeypatch.delenv(rl.STATUSLINE_SPOOL_ENV, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert rl.default_claude_statusline_spool_path() == tmp_path / "cfg" / "agentacct" / "statusline-latest.json"


# --------------------------------------------------------------------------- #
# the hook itself: fast, fail-open, spools + prints
# --------------------------------------------------------------------------- #


def test_hook_writes_spool_and_prints(tmp_path, monkeypatch, capsys):
    spool = tmp_path / "sl.json"
    monkeypatch.setenv(rl.STATUSLINE_SPOOL_ENV, str(spool))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_statusline_payload())))
    rc = statusline_hook.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "5h 18%" in out and "7d 63%" in out and "Opus 4.8" in out
    assert spool.exists()
    snap = rl.read_claude_statusline_latest(spool)
    assert snap is not None


def test_hook_is_fail_open_on_bad_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rl.STATUSLINE_SPOOL_ENV, str(tmp_path / "sl.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{ this is not json"))
    rc = statusline_hook.main()
    assert rc == 0  # never breaks the status bar
    capsys.readouterr()  # a line was printed (possibly empty); no exception


def test_hook_without_rate_limits_still_prints_no_spool(tmp_path, monkeypatch, capsys):
    spool = tmp_path / "sl.json"
    monkeypatch.setenv(rl.STATUSLINE_SPOOL_ENV, str(spool))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"model": {"display_name": "X"}})))
    assert statusline_hook.main() == 0
    assert "X" in capsys.readouterr().out
    assert not spool.exists()  # nothing to spool when the payload has no rate_limits


# --------------------------------------------------------------------------- #
# import ingests the spool (gated); install merges statusLine (never clobbers)
# --------------------------------------------------------------------------- #


def test_import_ingests_statusline_spool(tmp_path, monkeypatch):
    from agentacct.cli import _local_usage_import_payload

    spool = tmp_path / "sl.json"
    rl.write_claude_statusline_spool(_statusline_payload()["rate_limits"], captured_at=5000.0, path=spool)
    monkeypatch.setenv(rl.STATUSLINE_SPOOL_ENV, str(spool))
    monkeypatch.setenv("AGENTACCT_SCAN_GLOBAL_LIMITS", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))  # hermetic usage discovery
    store_dir = tmp_path / "state"

    _local_usage_import_payload(store_dir=store_dir, client="claude-code", claude_home=None)
    events = [
        e
        for e in SentinelService(store_dir).list_all_events()
        if e.get("event_type") == "rate_limit_observed"
    ]
    assert len(events) == 1
    assert events[0]["source"] == rl.CLAUDE_STATUSLINE_SOURCE


def test_import_does_not_ingest_spool_when_global_scan_off(tmp_path, monkeypatch):
    from agentacct.cli import _local_usage_import_payload

    spool = tmp_path / "sl.json"
    rl.write_claude_statusline_spool(_statusline_payload()["rate_limits"], captured_at=5000.0, path=spool)
    monkeypatch.setenv(rl.STATUSLINE_SPOOL_ENV, str(spool))
    # conftest already pins AGENTACCT_SCAN_GLOBAL_LIMITS off; be explicit.
    monkeypatch.setenv("AGENTACCT_SCAN_GLOBAL_LIMITS", "0")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    store_dir = tmp_path / "state"

    _local_usage_import_payload(store_dir=store_dir, client="claude-code", claude_home=None)
    events = [
        e
        for e in SentinelService(store_dir).list_all_events()
        if e.get("event_type") == "rate_limit_observed"
    ]
    assert events == []


def test_install_example_includes_statusline_and_never_blocks_core_merge(tmp_path):
    from agentacct.hooks import claude_code_settings_example
    from agentacct.cli import _merge_claude_settings_from_example

    example = claude_code_settings_example(python_executable="/usr/bin/python3")
    assert "statusLine" in example
    assert "agentacct.statusline_hook" in example["statusLine"]["command"]

    example_path = tmp_path / "example.json"
    example_path.write_text(json.dumps(example), encoding="utf-8")

    # fresh target → statusLine + env + hooks all installed
    target = tmp_path / "settings.json"
    _merge_claude_settings_from_example(example_path, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["statusLine"]["command"] == example["statusLine"]["command"]
    assert merged["env"]["ENABLE_TOOL_SEARCH"] == "auto"
    assert "PreToolUse" in merged["hooks"]


def test_user_statusline_is_preserved_and_never_blocks_core_recording(tmp_path):
    """The regression the adversarial review caught: a user's own statusLine must
    be left untouched AND must NOT abort the essential env+hooks merge."""
    from agentacct.hooks import claude_code_settings_example
    from agentacct.cli import _merge_claude_settings_from_example

    example_path = tmp_path / "example.json"
    example_path.write_text(
        json.dumps(claude_code_settings_example(python_executable="/usr/bin/python3")),
        encoding="utf-8",
    )
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "my-own-bar"}}),
        encoding="utf-8",
    )
    _merge_claude_settings_from_example(example_path, target)  # must NOT raise
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["statusLine"]["command"] == "my-own-bar"  # theirs kept
    assert merged["env"]["ENABLE_TOOL_SEARCH"] == "auto"  # core env still merged
    assert "PreToolUse" in merged["hooks"]  # core recording hooks still merged


def _example_with(interp: str, wrap):
    from agentacct.hooks import claude_code_settings_example

    return claude_code_settings_example(python_executable=interp, hook_path=wrap)


def _rows(merged, event):
    return merged["hooks"][event]


def _cmds(merged, event):
    return [h["command"] for r in merged["hooks"][event] for h in r.get("hooks", [])]


def test_merge_collapses_a_preexisting_duplicate_agentacct_hook(tmp_path):
    """The dogfood double-count bug: a machine onboarded twice (same wrapper file,
    two different python interpreters) has two hook rows and double-fires. The
    next onboard must COLLAPSE them to one, not leave the duplicate."""
    from agentacct.cli import _merge_claude_settings_from_example

    wrap = tmp_path / "hooks" / "claude_pre_tool_use.py"
    ex_a = _example_with("/interp-a/bin/python", wrap)
    ex_b = _example_with("/interp-b/bin/python3", wrap)
    # existing = BOTH interpreters wired (the double-count state)
    existing = {"hooks": {}}
    for ex in (ex_a, ex_b):
        for event, rows in ex["hooks"].items():
            existing["hooks"].setdefault(event, []).extend(rows)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(existing), encoding="utf-8")
    assert len(existing["hooks"]["PreToolUse"]) == 2  # precondition: doubled

    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(ex_a), encoding="utf-8")
    _merge_claude_settings_from_example(example_path, target)

    merged = json.loads(target.read_text(encoding="utf-8"))
    for event in ex_a["hooks"]:
        assert len(_rows(merged, event)) == 1, (event, _rows(merged, event))
        assert _cmds(merged, event) == _cmds({"hooks": ex_a["hooks"]}, event)


def test_merge_is_idempotent_never_doubles_the_hook(tmp_path):
    from agentacct.cli import _merge_claude_settings_from_example

    wrap = tmp_path / "hooks" / "claude_pre_tool_use.py"
    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(_example_with("/interp/bin/python", wrap)), encoding="utf-8")
    target = tmp_path / "settings.json"
    _merge_claude_settings_from_example(example_path, target)
    _, action = _merge_claude_settings_from_example(example_path, target)  # second run
    assert action == "unchanged"
    merged = json.loads(target.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "SessionStart", "PostToolUse"):
        assert len(_rows(merged, event)) == 1  # never doubled on repeat


def test_merge_replaces_hook_in_place_when_interpreter_changes(tmp_path):
    from agentacct.cli import _merge_claude_settings_from_example

    wrap = tmp_path / "hooks" / "claude_pre_tool_use.py"
    target = tmp_path / "settings.json"
    old = tmp_path / "old.json"
    old.write_text(json.dumps(_example_with("/old/bin/python", wrap)), encoding="utf-8")
    _merge_claude_settings_from_example(old, target)
    new = tmp_path / "new.json"
    new.write_text(json.dumps(_example_with("/new/bin/python", wrap)), encoding="utf-8")
    _merge_claude_settings_from_example(new, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "SessionStart", "PostToolUse"):
        assert len(_rows(merged, event)) == 1  # replaced, not appended
        assert all("/new/bin/python" in c and "/old/bin/python" not in c for c in _cmds(merged, event))


def test_merge_preserves_a_users_own_hook_while_collapsing_a_real_duplicate(tmp_path):
    from agentacct.cli import _merge_claude_settings_from_example

    wrap = tmp_path / "hooks" / "claude_pre_tool_use.py"
    user_hook = {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/my-linter.sh"}]}
    ex_x = _example_with("/interp-x/bin/python", wrap)
    ex_y = _example_with("/interp-y/bin/python3", wrap)
    # existing: the user's own hook, then TWO of ours (a genuine duplicate — this
    # is what forces the collapse branch; a single copy would pass under the old
    # append code too).
    existing = {
        "hooks": {
            "PreToolUse": [user_hook]
            + list(ex_x["hooks"]["PreToolUse"])
            + list(ex_y["hooks"]["PreToolUse"])
        }
    }
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(existing), encoding="utf-8")
    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(ex_x), encoding="utf-8")
    _merge_claude_settings_from_example(example_path, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    rows = _rows(merged, "PreToolUse")
    cmds = _cmds(merged, "PreToolUse")
    assert cmds[0] == "/usr/bin/my-linter.sh"  # user's own hook untouched, at front
    assert sum(1 for c in cmds if "claude_pre_tool_use.py" in c) == 1  # collapsed to one
    assert len(rows) == 2  # [user_hook, single our row], not three


def test_merge_preserves_a_user_hook_sharing_our_basename_at_a_different_path(tmp_path):
    """The dedup matches by wrapper FILE (full path), NOT basename — so a user's
    own unrelated hook that merely shares the filename claude_pre_tool_use.py at a
    different location must never be mistaken for ours and deleted. Locks the
    full-path invariant against a future 'compare basenames' simplification."""
    from agentacct.cli import _merge_claude_settings_from_example

    our_wrap = tmp_path / "hooks" / "claude_pre_tool_use.py"
    ex = _example_with("/interp/bin/python", our_wrap)
    user_same_name = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "python /home/me/tools/claude_pre_tool_use.py"}],
    }
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps({"hooks": {"PreToolUse": [user_same_name] + list(ex["hooks"]["PreToolUse"])}}),
        encoding="utf-8",
    )
    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(ex), encoding="utf-8")
    _merge_claude_settings_from_example(example_path, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    cmds = _cmds(merged, "PreToolUse")
    assert "python /home/me/tools/claude_pre_tool_use.py" in cmds  # user's same-named hook preserved
    assert sum(1 for c in cmds if str(our_wrap) in c) == 1  # ours present exactly once


def test_merge_dedups_project_form_hook_across_interpreters(tmp_path):
    """The project-install command double-quotes the wrapper for $CLAUDE_PROJECT_DIR
    expansion; shell-aware tokenizing must still recognize it so a re-onboard from a
    different interpreter collapses instead of double-firing."""
    from agentacct.cli import _merge_claude_settings_from_example
    from agentacct.hooks import claude_code_settings_example

    ex_a = claude_code_settings_example(python_executable="/venvA/bin/python", hook_path=None)
    ex_b = claude_code_settings_example(python_executable="/venvB/bin/python3", hook_path=None)
    existing = {"hooks": {}}
    for ex in (ex_a, ex_b):
        for event, rows in ex["hooks"].items():
            existing["hooks"].setdefault(event, []).extend(rows)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(existing), encoding="utf-8")
    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(ex_a), encoding="utf-8")
    _merge_claude_settings_from_example(example_path, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "SessionStart", "PostToolUse"):
        assert len(_rows(merged, event)) == 1, (event, _cmds(merged, event))


def test_merge_dedups_when_the_wrapper_path_contains_a_space(tmp_path):
    """A home dir with a space (common on macOS/Windows) makes the command
    shlex-quote the wrapper path; the dedup must still collapse across interpreters."""
    from agentacct.cli import _merge_claude_settings_from_example

    wrap = Path("/Users/John Smith") / ".claude" / "hooks" / "claude_pre_tool_use.py"
    ex_a = _example_with("/pyA/bin/python", wrap)
    ex_b = _example_with("/pyB/bin/python3", wrap)
    existing = {"hooks": {}}
    for ex in (ex_a, ex_b):
        for event, rows in ex["hooks"].items():
            existing["hooks"].setdefault(event, []).extend(rows)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(existing), encoding="utf-8")
    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(ex_a), encoding="utf-8")
    _merge_claude_settings_from_example(example_path, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert len(_rows(merged, "PreToolUse")) == 1, _cmds(merged, "PreToolUse")


def test_merge_keeps_a_user_command_colocated_in_our_matcher_row(tmp_path):
    """If a user adds their own command to the SAME matcher entry as our wrapper,
    the merge must drop only OUR command from that row, never the user's."""
    from agentacct.cli import _merge_claude_settings_from_example

    wrap = tmp_path / "hooks" / "claude_pre_tool_use.py"
    ex = _example_with("/interp/bin/python", wrap)
    our_cmd = ex["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    mixed = {
        "matcher": "*",
        "hooks": [
            {"type": "command", "command": our_cmd},
            {"type": "command", "command": "/usr/bin/my-audit.sh"},
        ],
    }
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"hooks": {"PreToolUse": [mixed]}}), encoding="utf-8")
    example_path = tmp_path / "ex.json"
    example_path.write_text(json.dumps(ex), encoding="utf-8")
    _merge_claude_settings_from_example(example_path, target)
    merged = json.loads(target.read_text(encoding="utf-8"))
    cmds = _cmds(merged, "PreToolUse")
    assert "/usr/bin/my-audit.sh" in cmds  # user's co-located command NEVER dropped
    assert sum(1 for c in cmds if "claude_pre_tool_use.py" in c) == 1  # ours deduped
