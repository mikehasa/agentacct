"""Tests for the terminal-CLI Claude statusLine rate-limit path."""

from __future__ import annotations

import io
import json

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
