from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


CoreUsageClient = Literal["codex", "claude-code"]
PathPrecedence = Literal["explicit", "environment", "default"]
MAX_USAGE_SOURCE_HOMES = 32


@dataclass(frozen=True)
class UsageSourcePathPlan:
    """Filesystem-only plan shared by local source readers and status views.

    Resolving a plan never opens a database or transcript.  Callers receive the
    same ordered, deduplicated homes whether they are about to discover,
    import, report status, or run a watch tick.
    """

    client: CoreUsageClient
    homes: tuple[Path, ...]
    precedence: PathPrecedence
    environment_variable: str
    omitted_home_count: int = 0

    def display_paths(self) -> list[str]:
        return [str(path) for path in self.homes]


def resolve_usage_source_paths(
    client: CoreUsageClient,
    *,
    explicit_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
) -> UsageSourcePathPlan:
    """Resolve explicit > client environment > defaults for one client.

    The client environment values retain the historical comma-separated-path
    behavior. Claude's default plan intentionally contains both its XDG and
    legacy homes, in that order, because installations can legitimately have
    sessions in both during a migration.
    """

    if client not in {"codex", "claude-code"}:
        raise ValueError(f"unsupported core usage client: {client}")
    env = os.environ if environ is None else environ
    raw_home = user_home or Path.home()
    try:
        home = raw_home.expanduser()
    except RuntimeError:
        # An invalid ``~user`` must not make an otherwise healthy multi-home
        # plan unusable.  Keep it as an absolute lexical path; the per-home
        # reader will report it like any other unavailable source.
        home = Path(os.path.abspath(os.fspath(raw_home)))
    environment_variable = "CODEX_HOME" if client == "codex" else "CLAUDE_CONFIG_DIR"

    if explicit_home is not None:
        candidates = [explicit_home]
        precedence: PathPrecedence = "explicit"
    else:
        configured = env.get(environment_variable, "")
        configured_paths = [part.strip() for part in configured.split(",") if part.strip()]
        if configured_paths:
            candidates = [Path(part) for part in configured_paths]
            precedence = "environment"
        elif client == "codex":
            candidates = [home / ".codex"]
            precedence = "default"
        else:
            xdg_value = env.get("XDG_CONFIG_HOME", "").strip()
            xdg_home = Path(xdg_value) if xdg_value else home / ".config"
            candidates = [xdg_home / "claude", home / ".claude"]
            precedence = "default"

    unique_homes = _ordered_unique_paths(candidates, user_home=home)
    return UsageSourcePathPlan(
        client=client,
        homes=unique_homes[:MAX_USAGE_SOURCE_HOMES],
        precedence=precedence,
        environment_variable=environment_variable,
        omitted_home_count=max(0, len(unique_homes) - MAX_USAGE_SOURCE_HOMES),
    )


def resolve_core_usage_source_plans(
    *,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
) -> dict[CoreUsageClient, UsageSourcePathPlan]:
    """Build the shared Codex/Claude plan once for a scan or status request."""

    return {
        "codex": resolve_usage_source_paths(
            "codex",
            explicit_home=codex_home,
            environ=environ,
            user_home=user_home,
        ),
        "claude-code": resolve_usage_source_paths(
            "claude-code",
            explicit_home=claude_home,
            environ=environ,
            user_home=user_home,
        ),
    }


def _ordered_unique_paths(paths: list[Path], *, user_home: Path) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in paths:
        expanded = _expand_path(candidate, user_home=user_home)
        try:
            normalized = expanded.resolve()
        except (OSError, RuntimeError):
            normalized = Path(os.path.abspath(os.fspath(expanded)))
        key = os.path.normcase(os.fspath(normalized))
        if key in seen:
            continue
        seen.add(key)
        unique.append(expanded)
    return tuple(unique)


def _expand_path(path: Path, *, user_home: Path) -> Path:
    text = os.fspath(path)
    if text == "~":
        return user_home
    if text.startswith(f"~{os.sep}"):
        return user_home / text[2:]
    try:
        return path.expanduser()
    except RuntimeError:
        return Path(os.path.abspath(text))
