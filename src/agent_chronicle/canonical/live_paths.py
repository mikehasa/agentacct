"""Pure path guards for offline canonical-spike destinations.

The spike must never create a candidate, benchmark scratch tree, or snapshot
manifest in a configured live agentacct/Codex root.  Keep this helper pure: it
resolves caller-declared path names but never opens or scans store contents.
"""

from __future__ import annotations

import os
from pathlib import Path


_LIVE_ROOT_ENV_NAMES = (
    "AGENT_CHRONICLE_STORE_DIR",
    "AGENT_SENTINEL_STORE_DIR",
    "CODEX_HOME",
)

# Project-local live stores are discovered by walking up from cwd, so they can
# never be enumerated as configured roots; reject their component shape
# instead. The bare directory name is the shape: live state also lives beside
# state/ (policy.yaml) and the -global root itself serves as a hooks
# project-dir. Bare .codex stays a configured-root and runner-blocklist
# concern: it is a client home, not a agentacct-owned directory name.
_LIVE_STATE_COMPONENT_SEQUENCES = (
    (".agent-sentinel",),
    (".agent-sentinel-global",),
    (".agent-chronicle",),
    (".agent-chronicle-global",),
)


class LivePathSafetyError(ValueError):
    """A live root or caller path could not be resolved safely."""


def paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either absolute path is the other path or its ancestor."""

    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def configured_live_state_roots() -> tuple[Path, ...]:
    """Return historical/default and env-configured roots without reading them."""

    home = Path.home().expanduser()
    roots = {
        home / ".agent-sentinel",
        home / ".agent-sentinel-global" / "state",
        home / ".agent-chronicle",
        home / ".agent-chronicle-global" / "state",
        home / ".codex",
    }
    for variable in _LIVE_ROOT_ENV_NAMES:
        value = (os.environ.get(variable) or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        roots.add(candidate)

    resolved: set[Path] = set()
    for root in roots:
        try:
            resolved.add(root.resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            raise LivePathSafetyError(
                "configured live state root cannot be resolved safely"
            ) from exc
    return tuple(sorted(resolved, key=str))


def _reject_live_state_component_shape(candidate: Path, *, label: str) -> None:
    """Reject paths shaped like a project-local live store."""

    folded = tuple(part.casefold() for part in candidate.parts)
    for sequence in _LIVE_STATE_COMPONENT_SEQUENCES:
        width = len(sequence)
        if any(
            folded[index : index + width] == sequence
            for index in range(len(folded) - width + 1)
        ):
            raise LivePathSafetyError(
                f"{label} may not be inside {'/'.join(sequence)}"
            )


def reject_live_state_overlap(path: Path, *, label: str) -> None:
    """Reject live-store component shapes and overlap with every live root."""

    if not path.is_absolute():
        raise LivePathSafetyError(f"{label} must be an explicit absolute path")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LivePathSafetyError(f"{label} cannot be resolved safely") from exc
    for candidate in (path, resolved):
        _reject_live_state_component_shape(candidate, label=label)
    if any(
        paths_overlap(resolved, live_root)
        for live_root in configured_live_state_roots()
    ):
        raise LivePathSafetyError(
            f"{label} must be disjoint from every configured live state root"
        )


__all__ = [
    "LivePathSafetyError",
    "configured_live_state_roots",
    "paths_overlap",
    "reject_live_state_overlap",
]
