"""Headless adapters for the real agent CLIs.

Every adapter does the same three things: keep the machine's GLOBAL agent
configuration out of the run (a global instruction file would carry the
INSTALLED agentacct block, not the one under test), point the CLI at the
sandbox's MCP server, and run the task with no human in the loop.

Each adapter states how complete its isolation is; the report prints that
beside the numbers rather than letting every agent look equally controlled.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import Sandbox

_MCP_PREFIX = "agentacct"


@dataclass
class AgentRun:
    agent: str
    exit_code: int | None
    seconds: float
    final_text: str = ""
    cost_usd: float | None = None
    #: agentacct tool calls the agent made, and how many came back as errors
    #: (a refusal by the rules, or a malformed call). None when the CLI's
    #: transcript does not expose tool results.
    recording_calls: int | None = None
    refused_calls: int | None = None
    error: str | None = None
    refusals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Adapter:
    name: str
    binary: str
    isolation: str

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run(self, sandbox: Sandbox, task: str, *, model: str | None, timeout: int) -> AgentRun:
        raise NotImplementedError


def _execute(agent: str, argv: list[str], sandbox: Sandbox, env: dict[str, str], timeout: int) -> tuple[AgentRun, str]:
    started = time.monotonic()
    try:
        done = subprocess.run(
            argv, cwd=sandbox.project, env=env, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as expired:
        output = expired.stdout.decode() if isinstance(expired.stdout, bytes) else (expired.stdout or "")
        return AgentRun(agent, None, time.monotonic() - started, error=f"timed out after {timeout}s"), output
    run = AgentRun(agent, done.returncode, time.monotonic() - started)
    if done.returncode != 0:
        run.error = (done.stderr or done.stdout).strip()[-400:] or f"exit {done.returncode}"
    (sandbox.root / f"{agent}.transcript").write_text(done.stdout + "\n--- stderr ---\n" + done.stderr)
    return run, done.stdout


def _json_lines(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


class ClaudeCode(Adapter):
    def __init__(self) -> None:
        super().__init__(
            "claude", "claude",
            "full: --setting-sources project drops global CLAUDE.md, hooks and settings; "
            "--strict-mcp-config drops global MCP servers",
        )

    def run(self, sandbox: Sandbox, task: str, *, model: str | None, timeout: int) -> AgentRun:
        config = sandbox.root / "claude-mcp.json"
        config.write_text(json.dumps({"mcpServers": {"agentacct": {
            "command": sandbox.mcp_command[0], "args": sandbox.mcp_command[1:], "env": sandbox.mcp_env,
        }}}))
        argv = [
            "claude", "-p", task,
            "--mcp-config", str(config), "--strict-mcp-config", "--setting-sources", "project",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
            "Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)", "Bash(git status:*)", "Bash(git diff:*)",
            "mcp__agentacct",
            "--output-format", "stream-json", "--verbose",
        ]
        if model:
            argv += ["--model", model]
        run, stdout = _execute(self.name, argv, sandbox, sandbox.agent_env(), timeout)

        calls: dict[str, str] = {}
        refusals: list[str] = []
        for row in _json_lines(stdout):
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and _MCP_PREFIX in str(block.get("name")):
                    calls[str(block.get("id"))] = str(block.get("name"))
                if block.get("type") == "tool_result" and block.get("is_error") and block.get("tool_use_id") in calls:
                    refusals.append(_text_of(block.get("content"))[:300])
            if row.get("type") == "result":
                run.final_text = str(row.get("result") or "")
                run.cost_usd = row.get("total_cost_usd")
                if row.get("is_error") and not run.error:
                    run.error = run.final_text[:300]
        run.recording_calls, run.refused_calls, run.refusals = len(calls), len(refusals), refusals
        return run


class Codex(Adapter):
    def __init__(self) -> None:
        super().__init__(
            "codex", "codex",
            "full: a private CODEX_HOME holds only a link to the login and this run's MCP server, "
            "so the global AGENTS.md, hooks and MCP servers are absent",
        )

    def run(self, sandbox: Sandbox, task: str, *, model: str | None, timeout: int) -> AgentRun:
        home = sandbox.root / "codex-home"
        home.mkdir()
        login = Path.home() / ".codex" / "auth.json"
        if login.exists():
            # A link, not a copy: the credential stays in one place, and a token
            # refresh during the run lands in the real file.
            (home / "auth.json").symlink_to(login)
        env_toml = ", ".join(f'{key} = {json.dumps(value)}' for key, value in sandbox.mcp_env.items())
        (home / "config.toml").write_text(
            'approval_policy = "never"\nsandbox_mode = "workspace-write"\n\n'
            "[mcp_servers.agentacct]\n"
            f"command = {json.dumps(sandbox.mcp_command[0])}\n"
            f"args = {json.dumps(sandbox.mcp_command[1:])}\n"
            f"env = {{ {env_toml} }}\n"
        )
        argv = ["codex", "exec", "-C", str(sandbox.project), "--skip-git-repo-check", "--json"]
        if model:
            argv += ["-m", model]
        argv.append(task)
        run, stdout = _execute(self.name, argv, sandbox, sandbox.agent_env({"CODEX_HOME": str(home)}), timeout)

        calls = refused = 0
        refusals: list[str] = []
        for row in _json_lines(stdout):
            item = row.get("item") if isinstance(row.get("item"), dict) else {}
            if item.get("type") == "agent_message" and row.get("type") == "item.completed":
                run.final_text = str(item.get("text") or "")
            if item.get("type") == "mcp_tool_call" and row.get("type") == "item.completed":
                if _MCP_PREFIX not in json.dumps(item)[:400]:
                    continue
                calls += 1
                failed = item.get("status") == "failed" or bool(item.get("error")) or (
                    isinstance(item.get("result"), dict) and item["result"].get("is_error")
                )
                if failed:
                    refused += 1
                    refusals.append(_text_of(item.get("error") or item.get("result"))[:300])
        run.recording_calls, run.refused_calls, run.refusals = calls, refused, refusals
        return run


class OpenCode(Adapter):
    def __init__(self) -> None:
        super().__init__(
            "opencode", "opencode",
            "full: XDG_CONFIG_HOME is redirected, so the global AGENTS.md, plugins and MCP servers are "
            "absent; the login lives under XDG_DATA_HOME and is untouched",
        )

    def run(self, sandbox: Sandbox, task: str, *, model: str | None, timeout: int) -> AgentRun:
        config_home = sandbox.root / "opencode-config"
        config_home.mkdir()
        argv = ["opencode", "run"]
        if model:
            argv += ["-m", model]
        argv.append(task)
        run, stdout = _execute(
            self.name, argv, sandbox, sandbox.agent_env({"XDG_CONFIG_HOME": str(config_home)}), timeout,
        )
        run.final_text = stdout.strip()[-1500:]
        return run


class GeminiCli(Adapter):
    def __init__(self) -> None:
        super().__init__(
            "gemini", "gemini",
            "partial: the project's .gemini/settings.json supplies the MCP server, but ~/.gemini "
            "(which also holds the login) is still read, so a global GEMINI.md would leak in",
        )

    def run(self, sandbox: Sandbox, task: str, *, model: str | None, timeout: int) -> AgentRun:
        argv = ["gemini", "-p", task, "--approval-mode", "yolo"]
        if model:
            argv += ["-m", model]
        run, stdout = _execute(self.name, argv, sandbox, sandbox.agent_env(), timeout)
        run.final_text = stdout.strip()[-1500:]
        return run


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text_of(part.get("text") if isinstance(part, dict) else part) for part in content)
    if isinstance(content, dict):
        return _text_of(content.get("text") or content.get("message") or json.dumps(content))
    return "" if content is None else str(content)


ADAPTERS: dict[str, Adapter] = {adapter.name: adapter for adapter in (ClaudeCode(), Codex(), OpenCode(), GeminiCli())}
