from __future__ import annotations

import json
import sys
from pathlib import Path

from agentacct.wrappers import CLAUDE_WRAPPER, CODEX_WRAPPER, _main, build_agent_command, run_agent_wrapper, split_wrapper_args


def test_sentinel_claude_wrapper_runs_child_under_sentinel_and_preserves_args(tmp_path, monkeypatch) -> None:
    marker = "WRAPPER_CLAUDE_OK"
    monkeypatch.setenv("AGENT_CHRONICLE_CLAUDE_BINARY", sys.executable)

    result = run_agent_wrapper(
        CLAUDE_WRAPPER,
        ["--sentinel-store-dir", str(tmp_path / "state"), "--", "-c", f"print({marker!r})"],
    )

    assert result.agent == "claude-code"
    assert result.status == "completed"
    assert result.exit_code == 0
    assert Path(result.stdout_log).read_text(encoding="utf-8").strip() == marker
    metadata = json.loads((result.run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["owned_by_sentinel"] is True
    assert metadata["command"] == [sys.executable, "-c", f"print({marker!r})"]
    # stored run metadata keeps the FROZEN pre-rename key name forever
    assert metadata["env"]["AGENT_SENTINEL_RUN_ID"] == result.run_id
    assert result.report_md.exists()


def test_sentinel_codex_wrapper_preserves_child_exit_code_and_stderr(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CHRONICLE_CODEX_BINARY", sys.executable)

    result = run_agent_wrapper(
        CODEX_WRAPPER,
        [
            "--sentinel-store-dir",
            str(tmp_path / "state"),
            "--",
            "-c",
            "import sys; print('bad', file=sys.stderr); raise SystemExit(7)",
        ],
    )

    assert result.agent == "codex"
    assert result.status == "failed"
    assert result.exit_code == 7
    assert "bad" in Path(result.stderr_log).read_text(encoding="utf-8")
    metadata = json.loads((result.run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["owned_by_sentinel"] is True
    assert metadata["exit_code"] == 7


def test_wrapper_sentinel_options_do_not_leak_into_child_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CHRONICLE_CLAUDE_BINARY", sys.executable)

    result = run_agent_wrapper(
        CLAUDE_WRAPPER,
        [
            "--sentinel-store-dir",
            str(tmp_path / "state"),
            "--sentinel-max-runtime",
            "30s",
            "--sentinel-on-timeout",
            "kill",
            "--sentinel-no-report",
            "--",
            "-c",
            "print('clean')",
        ],
    )

    metadata = json.loads((result.run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["command"] == [sys.executable, "-c", "print('clean')"]
    assert "--sentinel-store-dir" not in metadata["command"]
    assert "--sentinel-max-runtime" not in metadata["command"]


def test_wrapper_builds_default_agent_commands_without_global_config_changes(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_CHRONICLE_CLAUDE_BINARY", raising=False)
    monkeypatch.delenv("AGENT_CHRONICLE_CODEX_BINARY", raising=False)

    assert build_agent_command(CLAUDE_WRAPPER, ["-p", "hello"]) == ["claude", "-p", "hello"]
    assert build_agent_command(CODEX_WRAPPER, ["exec", "hello"]) == ["codex", "exec", "hello"]


def test_wrapper_arg_parser_accepts_agent_flags_after_separator(tmp_path) -> None:
    options, child_args = split_wrapper_args(
        CLAUDE_WRAPPER,
        ["--sentinel-store-dir", str(tmp_path), "--", "--sentinel-max-runtime", "agent-owned-flag"],
    )

    assert options.store_dir == tmp_path
    assert child_args == ["--sentinel-max-runtime", "agent-owned-flag"]


def test_wrapper_main_relays_child_output_and_returns_child_failure_code(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_CHRONICLE_CODEX_BINARY", sys.executable)

    code = _main(
        CODEX_WRAPPER,
        [
            "--sentinel-store-dir",
            str(tmp_path / "state"),
            "--",
            "-c",
            "import sys; print('child stdout'); print('child stderr', file=sys.stderr); raise SystemExit(9)",
        ],
    )

    captured = capsys.readouterr()
    assert code == 9
    assert "child stdout" in captured.out
    assert "child stderr" in captured.err
    assert "agentacct codex run:" in captured.err
    assert "Report:" in captured.err


def test_package_declares_public_wrapper_scripts() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'agentacct-claude = "agentacct.wrappers:sentinel_claude_main"' in text
    assert 'agentacct-codex = "agentacct.wrappers:sentinel_codex_main"' in text
