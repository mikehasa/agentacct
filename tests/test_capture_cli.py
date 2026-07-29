from __future__ import annotations

import json

from typer.testing import CliRunner

from agentacct.cli import app


runner = CliRunner()


def test_capture_cli_capabilities_are_explicit_about_usage_and_cost() -> None:
    result = runner.invoke(app, ["capture", "capabilities", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload["capabilities"]) == {"claude-code", "codex", "cursor"}
    assert payload["privacy"]["captures_usage"] is False
    assert all(row["usage"] is False for row in payload["capabilities"].values())
    assert all(row["cost"] is False for row in payload["capabilities"].values())


def test_capture_cli_manifest_is_render_only() -> None:
    result = runner.invoke(app, ["capture", "manifest", "--vendor", "claude-code"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["relative_path"] == ".claude/settings.json"
    assert payload["written"] is False
    assert payload["activation"] == "opt_in"
    assert "agentacct capture hook --vendor claude-code" in result.output


def test_capture_cli_hook_writes_and_never_echoes_bodies(tmp_path) -> None:
    hook_payload = {
        "hook_event_name": "Stop",
        "timestamp": "2026-07-13T02:03:04Z",
        "session_id": "codex-session-1",
        "turn_id": "turn-1",
        "status": "completed",
        "prompt": "PROMPT_CANARY",
        "last_assistant_message": "RESPONSE_CANARY",
    }
    result = runner.invoke(
        app,
        [
            "capture",
            "hook",
            "--vendor",
            "codex",
            "--event",
            "Stop",
            "--store-dir",
            str(tmp_path),
            "--json",
        ],
        input=json.dumps(hook_payload),
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["stored_count"] == 1
    assert receipt["fail_open"] is True
    assert "PROMPT_CANARY" not in result.output
    assert "RESPONSE_CANARY" not in result.output


def test_capture_cli_hook_malformed_payload_still_exits_zero(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "capture",
            "hook",
            "--vendor",
            "cursor",
            "--event",
            "sessionStart",
            "--store-dir",
            str(tmp_path),
            "--json",
        ],
        input="not-json SECRET_CANARY",
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["fail_open"] is True
    assert receipt["ignored_reason"] == "malformed_json"
    assert "SECRET_CANARY" not in result.output


def test_capture_cli_installed_hook_path_is_silent(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "capture",
            "hook",
            "--vendor",
            "codex",
            "--event",
            "Stop",
            "--store-dir",
            str(tmp_path),
        ],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "session-silent"}),
    )

    assert result.exit_code == 0
    assert result.output == ""
