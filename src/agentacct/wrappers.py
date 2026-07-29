from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .env_compat import read_env_alias
from .platform_support import exit_if_unsupported_platform
from .runner import RunOptions, RunStatus, start_guarded_run
from .store_resolution import StoreResolutionError, resolve_store_dir


@dataclass(frozen=True)
class AgentWrapperSpec:
    agent: str
    default_binary: str
    env_binary: str


# env_binary is the NEW primary name; read_env_alias also accepts the
# pre-rename AGENT_CHRONICLE_* / AGENT_SENTINEL_* aliases silently
# (build_agent_command).
CLAUDE_WRAPPER = AgentWrapperSpec(agent="claude-code", default_binary="claude", env_binary="AGENTACCT_CLAUDE_BINARY")
CODEX_WRAPPER = AgentWrapperSpec(agent="codex", default_binary="codex", env_binary="AGENTACCT_CODEX_BINARY")


@dataclass(frozen=True)
class WrapperOptions:
    store_dir: Path | None
    max_runtime: str | None
    on_timeout: str
    repeated_error_threshold: int | None
    on_repeated_error: str
    cwd: Path | None
    print_report: bool


@dataclass(frozen=True)
class WrapperResult:
    agent: str
    command: list[str]
    run_id: str
    status: str
    exit_code: int | None
    reason: str
    run_dir: Path
    report_md: Path
    stdout_log: Path
    stderr_log: Path


def parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    if value.endswith("s"):
        return float(value[:-1])
    if value.endswith("m"):
        return float(value[:-1]) * 60
    if value.endswith("h"):
        return float(value[:-1]) * 3600
    return float(value)


def _parser(spec: AgentWrapperSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"sentinel-{spec.default_binary}",
        description=f"Run {spec.default_binary} under agentacct ownership and write local run artifacts.",
        epilog=(
            "Everything after agentacct options is passed to the underlying agent binary. "
            "Use -- before agent arguments if an agent flag conflicts with a agentacct flag."
        ),
    )
    # FROZEN: the --sentinel-* flag names are user-facing CLI compatibility —
    # live scripts and docs invoke them, so they keep the pre-rename spelling
    # forever (like the sentinel-claude/sentinel-codex script names). Only
    # help/description PROSE renames.
    parser.add_argument(
        "--sentinel-store-dir",
        type=Path,
        default=None,
        help=(
            "State directory. Defaults to the project store (.agent-sentinel/state found by walking up from the "
            "current directory; Claude worktrees resolve to the owning project). Override here or with "
            "AGENTACCT_STORE_DIR (pre-rename aliases AGENT_CHRONICLE_STORE_DIR / AGENT_SENTINEL_STORE_DIR also accepted). "
            "Fails if no project store exists."
        ),
    )
    parser.add_argument("--sentinel-max-runtime", default=None, help="Max runtime before timeout action, e.g. 90s, 10m, 2h.")
    parser.add_argument("--sentinel-on-timeout", choices=["pause", "kill"], default="pause", help="Action when max runtime is reached.")
    parser.add_argument("--sentinel-repeated-error-threshold", type=int, default=None, help="Pause/kill when the same stderr line repeats N times.")
    parser.add_argument("--sentinel-on-repeated-error", choices=["pause", "kill"], default="pause", help="Action for repeated stderr threshold.")
    parser.add_argument("--sentinel-cwd", type=Path, default=None, help="Working directory for the underlying agent command.")
    parser.add_argument("--sentinel-no-report", action="store_true", help="Do not print agentacct run/report details to stderr after completion.")
    return parser


def split_wrapper_args(spec: AgentWrapperSpec, argv: Sequence[str]) -> tuple[WrapperOptions, list[str]]:
    parser = _parser(spec)
    args, child_args = parser.parse_known_args(list(argv))
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]
    if args.sentinel_repeated_error_threshold is not None and args.sentinel_repeated_error_threshold <= 0:
        parser.error("--sentinel-repeated-error-threshold must be > 0")
    options = WrapperOptions(
        store_dir=args.sentinel_store_dir,
        max_runtime=args.sentinel_max_runtime,
        on_timeout=args.sentinel_on_timeout,
        repeated_error_threshold=args.sentinel_repeated_error_threshold,
        on_repeated_error=args.sentinel_on_repeated_error,
        cwd=args.sentinel_cwd,
        print_report=not args.sentinel_no_report,
    )
    return options, child_args


def build_agent_command(spec: AgentWrapperSpec, child_args: Sequence[str]) -> list[str]:
    binary = read_env_alias(spec.env_binary) or spec.default_binary
    return [binary, *child_args]


def run_agent_wrapper(spec: AgentWrapperSpec, argv: Sequence[str] | None = None) -> WrapperResult:
    options, child_args = split_wrapper_args(spec, sys.argv[1:] if argv is None else argv)
    command = build_agent_command(spec, child_args)
    # One shared resolution rule (flag > AGENTACCT_STORE_DIR (legacy
    # aliases accepted) > project walk-up > explicit failure); raises
    # StoreResolutionError with an
    # actionable message instead of silently writing to ~/.agent-sentinel.
    store_dir = resolve_store_dir(options.store_dir).path
    result = start_guarded_run(
        command,
        RunOptions(
            store_dir=store_dir,
            max_runtime_seconds=parse_duration(options.max_runtime),
            on_timeout=options.on_timeout,  # type: ignore[arg-type]
            repeated_error_threshold=options.repeated_error_threshold,
            on_repeated_error=options.on_repeated_error,  # type: ignore[arg-type]
            cwd=options.cwd,
        ),
    )
    return WrapperResult(
        agent=spec.agent,
        command=command,
        run_id=result.run_id,
        status=result.status.value,
        exit_code=result.exit_code,
        reason=result.reason,
        run_dir=result.run_dir,
        report_md=result.run_dir / "report.md",
        stdout_log=result.run_dir / "stdout.log",
        stderr_log=result.run_dir / "stderr.log",
    )


def _relay_file(path: Path, stream) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if text:
        stream.write(text)
        stream.flush()


def _exit_code_for_result(result: WrapperResult) -> int:
    if result.status == RunStatus.COMPLETED.value:
        return 0
    if result.status == RunStatus.FAILED.value and result.exit_code is not None:
        return int(result.exit_code)
    if result.status == RunStatus.PAUSED.value:
        return 124
    if result.status == RunStatus.KILLED.value:
        return 137
    return 1


def _main(spec: AgentWrapperSpec, argv: Sequence[str] | None = None) -> int:
    # Same fail-fast guard as the agentacct CLI entry point: these wrapper
    # console scripts never import cli, so without this a native-Windows user
    # would hit runner.py's POSIX process-group calls as a raw traceback.
    exit_if_unsupported_platform(sys.platform)
    options, _child_args = split_wrapper_args(spec, sys.argv[1:] if argv is None else argv)
    try:
        result = run_agent_wrapper(spec, sys.argv[1:] if argv is None else argv)
    except StoreResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _relay_file(result.stdout_log, sys.stdout)
    _relay_file(result.stderr_log, sys.stderr)
    if options.print_report:
        print(f"\nagentacct {result.agent} run: {result.run_id} ({result.status})", file=sys.stderr)
        print(f"Reason: {result.reason}", file=sys.stderr)
        print(f"Report: {result.report_md}", file=sys.stderr)
    return _exit_code_for_result(result)


def sentinel_claude_main() -> int:
    return _main(CLAUDE_WRAPPER)


def sentinel_codex_main() -> int:
    return _main(CODEX_WRAPPER)
