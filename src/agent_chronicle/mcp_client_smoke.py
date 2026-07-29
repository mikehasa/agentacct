from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal

from agent_chronicle.mcp import SentinelMCPServer

MCPClient = Literal["hermes", "opencode", "openclaw"]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

# Standard env var each provider's key is read from (API-key providers).
_PROVIDER_KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
# Default opencode model per provider (override with --model).
_OPENCODE_DEFAULT_MODEL = {
    "deepseek": "deepseek/deepseek-chat",
    "openai": "openai/gpt-5.4-mini-fast",
}


def _default_opencode_auth_path() -> Path:
    """Real opencode credential store (~/.local/share/opencode/auth.json)."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "opencode" / "auth.json"


@dataclass(frozen=True)
class MCPClientSmokeResult:
    client: str
    provider: str
    model: str
    status: str
    marker_found: bool
    event_count: int
    by_source: dict[str, int]
    events: list[str]
    token_cost_observed: bool
    store_dir: str
    isolated_home: str | None = None
    isolated_profile: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "marker_found": self.marker_found,
            "event_count": self.event_count,
            "by_source": self.by_source,
            "events": self.events,
            "token_cost_observed": self.token_cost_observed,
            "store_dir": self.store_dir,
            "isolated_home": self.isolated_home,
            "isolated_profile": self.isolated_profile,
            "error": self.error,
        }


class MCPClientSmokeError(RuntimeError):
    pass


def _default_runner(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(*args, **kwargs)


def _sentinel_module_command() -> list[str]:
    return [sys.executable, "-m", "agent_chronicle.cli"]


def _prompt(client: str, source: str, run_id: str, marker: str, provider: str = "deepseek") -> str:
    return (
        f"You are doing a tiny real agentacct {provider} MCP smoke test. "
        "Use the agentacct MCP tool sentinel_record_section twice: "
        f"first record section_id=mcp-client-smoke section_status=started source={source} run_id={run_id} summary={provider}-mcp-client-smoke; "
        f"then record section_id=mcp-client-smoke section_status=completed source={source} run_id={run_id} summary=MCP workflow ledger works for {client}. "
        f"Finally reply exactly {marker}."
    )


def _mcp_text(response: dict[str, object]) -> str:
    result = response.get("result")
    if not isinstance(result, dict):
        return "{}"
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return "{}"
    first = content[0]
    if not isinstance(first, dict):
        return "{}"
    text = first.get("text")
    return text if isinstance(text, str) else "{}"


def _read_event_summary(store_dir: Path) -> tuple[int, dict[str, int], list[str]]:
    server = SentinelMCPServer(store_dir=store_dir)
    response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sentinel_get_event_summary", "arguments": {"limit": 200}}})
    parsed = json.loads(_mcp_text(response or {}))
    summary = parsed.get("summary", parsed)
    events_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "sentinel_list_events", "arguments": {"limit": 200}}})
    events = json.loads(_mcp_text(events_response or {})).get("events", [])
    return int(summary.get("event_count", 0)), dict(summary.get("by_source", {})), [str(event.get("event_type")) for event in events]


def _run_checked(runner: CommandRunner, cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return runner(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, input=input_text)


def _opencode_token_cost_observed(stdout: str) -> bool:
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = obj.get("part") or {}
        if part.get("type") == "step-finish" and (part.get("tokens") or part.get("cost") is not None):
            return True
    return False


def run_mcp_client_smoke(
    client: MCPClient,
    *,
    provider: str = "deepseek",
    model: str | None = None,
    key_env: str | None = None,
    reuse_client_auth: bool = False,
    client_auth_src: Path | None = None,
    repo_dir: Path | None = None,
    store_dir: Path | None = None,
    acknowledge_real_api: bool = False,
    runner: CommandRunner = _default_runner,
    timeout_seconds: int = 420,
) -> MCPClientSmokeResult:
    """Run a tiny real provider-backed MCP client smoke.

    Disabled unless acknowledge_real_api=True (it can spend paid credits). Tests
    inject a fake runner. ``reuse_client_auth`` copies the client's OWN stored
    auth (e.g. opencode's OAuth ``auth.json``) into the isolated home instead of
    requiring an API key in ``key_env`` — needed for OAuth providers that have no
    plain API key. Non-deepseek providers are currently opencode-only.
    """
    if not acknowledge_real_api:
        raise MCPClientSmokeError("real client smoke is disabled; pass --i-understand-this-uses-real-api")
    if client not in {"hermes", "opencode", "openclaw"}:
        raise MCPClientSmokeError(f"unsupported client: {client}")
    if provider != "deepseek" and client != "opencode":
        raise MCPClientSmokeError(f"provider {provider!r} is currently only supported for opencode")

    key_env = key_env or _PROVIDER_KEY_ENV.get(provider)
    key: str | None = None
    if not reuse_client_auth:
        if not key_env:
            raise MCPClientSmokeError(
                f"no key env known for provider {provider!r}; pass key_env or use reuse_client_auth"
            )
        key = os.environ.get(key_env)
        if not key:
            raise MCPClientSmokeError(f"missing required environment variable: {key_env}")

    repo = (repo_dir or Path.cwd()).resolve()
    state = store_dir or Path(tempfile.mkdtemp(prefix=f"as-{client}-{provider}-smoke-store-"))
    state.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if key and key_env:
        env[key_env] = key
    source = f"{client}-{provider}-smoke"
    run_id = f"{client}_{provider}_smoke_{int(time.time())}"
    marker = f"AGENT_CHRONICLE_{client.upper()}_{provider.upper()}_MCP_OK".replace("-", "_")
    prompt = _prompt(client, source, run_id, marker, provider)
    sentinel_cmd = _sentinel_module_command()

    isolated_home: str | None = None
    isolated_profile: str | None = None
    stdout = ""
    stderr = ""
    model = model or ""

    try:
        if client == "hermes":
            home = Path(tempfile.mkdtemp(prefix="as-hermes-deepseek-smoke-home-"))
            isolated_home = str(home)
            env["HERMES_HOME"] = str(home)
            # Let Hermes load the key from the process env, not the user's real profile.
            add = _run_checked(
                runner,
                ["hermes", "mcp", "add", "agent-chronicle", "--command", sentinel_cmd[0], "--args", *sentinel_cmd[1:], "mcp", "serve", "--store-dir", str(state)],
                cwd=repo,
                env=env,
                timeout=120,
                input_text="Y\n",
            )
            if add.returncode != 0:
                raise MCPClientSmokeError((add.stderr or add.stdout)[-1000:])
            model = model or "deepseek-v4-flash"
            run = _run_checked(
                runner,
                # "agent-sentinel-smoke" is a frozen stored source string (pre-rename).
                ["hermes", "chat", "-q", prompt, "--provider", "deepseek", "-m", model, "--max-turns", "8", "--ignore-rules", "--source", "agent-sentinel-smoke"],
                cwd=repo,
                env=env,
                timeout=timeout_seconds,
            )
        elif client == "opencode":
            home = Path(tempfile.mkdtemp(prefix=f"as-opencode-{provider}-smoke-home-"))
            isolated_home = str(home)
            env["HOME"] = str(home)
            if reuse_client_auth:
                # Copy opencode's OWN stored auth (e.g. OAuth) so the isolated run
                # authenticates without any API key. Opaque file copy — the token
                # is never read into this process.
                src = Path(client_auth_src) if client_auth_src is not None else _default_opencode_auth_path()
                if not src.exists():
                    raise MCPClientSmokeError(f"opencode auth not found at {src}; cannot reuse client auth")
                dst = home / ".local" / "share" / "opencode" / "auth.json"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            model = model or _OPENCODE_DEFAULT_MODEL.get(provider, "deepseek/deepseek-chat")
            add = _run_checked(
                runner,
                ["opencode", "mcp", "add", "agent-chronicle", "--", *sentinel_cmd, "mcp", "serve", "--store-dir", str(state)],
                cwd=repo,
                env=env,
                timeout=120,
            )
            if add.returncode != 0:
                raise MCPClientSmokeError((add.stderr or add.stdout)[-1000:])
            run = _run_checked(
                runner,
                ["opencode", "run", prompt, "--model", model, "--format", "json", "--dangerously-skip-permissions"],
                cwd=repo,
                env=env,
                timeout=timeout_seconds,
            )
        else:
            profile = f"agent-chronicle-deepseek-smoke-{int(time.time())}"
            isolated_profile = profile
            model = model or "deepseek/deepseek-chat"
            add = _run_checked(
                runner,
                ["openclaw", "--profile", profile, "mcp", "add", "agent-chronicle", "--command", sentinel_cmd[0], "--arg", sentinel_cmd[1], "--arg", sentinel_cmd[2], "--arg", "mcp", "--arg", "serve", "--arg", "--store-dir", "--arg", str(state), "--no-probe"],
                cwd=repo,
                env=env,
                timeout=120,
            )
            if add.returncode != 0:
                raise MCPClientSmokeError((add.stderr or add.stdout)[-1000:])
            provider = {
                "baseUrl": "https://api.deepseek.com/v1",
                "apiKey": {"source": "env", "provider": "default", "id": key_env},
                "auth": "api-key",
                "api": "openai-completions",
                "contextWindow": 64000,
                "maxTokens": 512,
            }
            config = _run_checked(runner, ["openclaw", "--profile", profile, "config", "set", "models.providers.deepseek", json.dumps(provider), "--strict-json"], cwd=repo, env=env, timeout=120)
            if config.returncode != 0:
                raise MCPClientSmokeError((config.stderr or config.stdout)[-1000:])
            run = _run_checked(
                runner,
                ["openclaw", "--profile", profile, "agent", "--local", "--model", model, "--message", prompt, "--json", "--timeout", str(timeout_seconds), "--session-key", "agent:main:sentinel-deepseek-smoke"],
                cwd=repo,
                env=env,
                timeout=timeout_seconds + 30,
            )
        stdout = run.stdout or ""
        stderr = run.stderr or ""
        if run.returncode != 0:
            raise MCPClientSmokeError((stderr or stdout)[-1000:])
        event_count, by_source, events = _read_event_summary(state)
        status = "passed" if marker in (stdout + stderr) and event_count >= 2 else "failed"
        return MCPClientSmokeResult(
            client=client,
            provider=provider,
            model=model,
            status=status,
            marker_found=marker in (stdout + stderr),
            event_count=event_count,
            by_source=by_source,
            events=events,
            token_cost_observed=_opencode_token_cost_observed(stdout) if client == "opencode" else False,
            store_dir=str(state),
            isolated_home=isolated_home,
            isolated_profile=isolated_profile,
            error=None if status == "passed" else "marker or agentacct events missing",
        )
    except MCPClientSmokeError as exc:
        return MCPClientSmokeResult(
            client=client,
            provider=provider,
            model=model,
            status="failed",
            marker_found=marker in (stdout + stderr),
            event_count=0,
            by_source={},
            events=[],
            token_cost_observed=False,
            store_dir=str(state),
            isolated_home=isolated_home,
            isolated_profile=isolated_profile,
            error=str(exc),
        )


def run_deepseek_mcp_client_smoke(
    client: MCPClient,
    *,
    repo_dir: Path | None = None,
    store_dir: Path | None = None,
    deepseek_key_env: str = "DEEPSEEK_API_KEY",
    acknowledge_real_api: bool = False,
    runner: CommandRunner = _default_runner,
    timeout_seconds: int = 420,
) -> MCPClientSmokeResult:
    """Back-compat wrapper: DeepSeek-backed smoke (see run_mcp_client_smoke)."""
    return run_mcp_client_smoke(
        client,
        provider="deepseek",
        key_env=deepseek_key_env,
        repo_dir=repo_dir,
        store_dir=store_dir,
        acknowledge_real_api=acknowledge_real_api,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


def assert_mcp_client_smoke_passed(result: MCPClientSmokeResult) -> None:
    if result.status != "passed":
        raise MCPClientSmokeError(result.error or f"{result.client} smoke failed")


# Back-compat alias.
assert_deepseek_mcp_client_smoke_passed = assert_mcp_client_smoke_passed
