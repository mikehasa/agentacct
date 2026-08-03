"""Read a Claude Code subagent's role + task from its local transcript.

Decoupled, read-only, fail-soft — the same "read the local files the agent
already wrote" pattern as :mod:`agentacct.rate_limits`. A child (subagent)
session carries no role in agentacct's event log, but its transcript on disk
does::

    <claude>/projects/<project>/<parentSessionId>/subagents/agent-<agentId>.jsonl

Each such file has an ``attributionAgent`` (the subagent type — e.g. ``Explore``,
``general-purpose``, ``Plan``, ``workflow-subagent``) and its first user message
is the Task prompt. The synthetic child session id agentacct assigns is
``<parentSessionId>:agent-<agentId>``, so a child id maps straight to its file.

Reads are bounded and never raise (a missing/renamed file simply yields no role).
The machine-global scan is gated by ``AGENTACCT_SCAN_SUBAGENT_ROLES`` (pinned off
in tests) unless an explicit ``projects_root`` is passed; the Claude projects root
follows ``CLAUDE_CONFIG_DIR`` (else ``~/.claude``) and is overridable for tests via
``AGENTACCT_CLAUDE_PROJECTS_ROOT``.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SCAN_ENV = "AGENTACCT_SCAN_SUBAGENT_ROLES"
ROOT_ENV = "AGENTACCT_CLAUDE_PROJECTS_ROOT"
_FALSE_VALUES = {"0", "false", "no", "off"}

# Role + task live in the first handful of lines; bound BOTH the line count and
# the byte budget so a huge (or single-giant-line) transcript can never stall the
# UI or blow up memory.
_MAX_LINES = 400
_MAX_BYTES = 512 * 1024


@dataclass(frozen=True)
class SubagentRole:
    """A subagent's type (``attributionAgent``) and its Task prompt, if found."""

    agent_type: str | None
    task: str | None


def scan_enabled() -> bool:
    value = os.environ.get(SCAN_ENV)
    if value is None:
        return True
    return value.strip().lower() not in _FALSE_VALUES


def _claude_config_home() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        parts = [p.strip() for p in configured.split(",") if p.strip()]
        if parts:
            return Path(parts[0]).expanduser()
    return Path.home() / ".claude"


def default_projects_root() -> Path:
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return _claude_config_home() / "projects"


def _resolve_root(projects_root: Path | str | None) -> Path | None:
    """The projects root to scan, or ``None`` when the global scan is disabled and
    no explicit root was given (test isolation)."""

    if projects_root is not None:
        return Path(projects_root).expanduser()
    if not scan_enabled():
        return None
    return default_projects_root()


def _agent_id_from_child_session(client_session_id: str) -> tuple[str, str] | None:
    """``<parent>:agent-<hex>`` → ``(parent, "agent-<hex>")``; else ``None``."""

    if not isinstance(client_session_id, str) or ":" not in client_session_id:
        return None
    parent, _, agent = client_session_id.partition(":")
    if not parent or not agent.startswith("agent-"):
        return None
    return parent, agent


def _extract_role(path: Path) -> SubagentRole | None:
    """Pull ``attributionAgent`` + the first user-text message from a transcript."""

    agent_type: str | None = None
    task: str | None = None
    try:
        # Bound BYTES (not just lines): read a fixed budget with errors="replace"
        # so a corrupted/binary transcript — or a torn multi-byte char in one a
        # subagent is still writing — can never raise UnicodeDecodeError or
        # materialize an unbounded line. The module contract is "never raise".
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            data = handle.read(_MAX_BYTES)
    except OSError:
        return None
    for line in data.splitlines()[:_MAX_LINES]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, Mapping):
            continue
        if agent_type is None:
            attribution = obj.get("attributionAgent")
            if isinstance(attribution, str) and attribution.strip():
                agent_type = attribution.strip()
        if task is None:
            message = obj.get("message")
            is_user = obj.get("type") == "user" or (
                isinstance(message, Mapping) and message.get("role") == "user"
            )
            if is_user and isinstance(message, Mapping):
                task = _first_text(message.get("content"))
        if agent_type is not None and task is not None:
            break
    if agent_type is None and task is None:
        return None
    return SubagentRole(agent_type=agent_type, task=task)


def _first_text(content: object) -> str | None:
    """The first human-readable text in a message ``content`` (str or block list).

    Tool-result blocks (role user, but ``type != "text"``) are skipped, so the
    Task prompt — not a tool echo — is what surfaces.
    """

    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return None


def subagent_files_for_parent(
    parent_client_session_id: str, *, projects_root: Path | str | None = None
) -> dict[str, Path]:
    """One glob → ``{agent_id: transcript_path}`` for every subagent of a parent.

    Batches the filesystem cost: a parent with many children needs a single
    directory scan instead of one glob per child. Returns ``{}`` on any absence
    or error.
    """

    root = _resolve_root(projects_root)
    if root is None or not parent_client_session_id:
        return {}
    # The parent id is a single path segment. Reject anything that could escape
    # the tree, and glob.escape so a metacharacter (e.g. "*") matches literally
    # instead of every parent — a crafted id must never read outside the root.
    if any(ch in parent_client_session_id for ch in ("/", os.sep, "\x00")) or ".." in parent_client_session_id:
        return {}
    pattern = str(root / "*" / glob.escape(parent_client_session_id) / "subagents" / "agent-*.jsonl")
    try:
        matches = glob.glob(pattern)
    except OSError:
        return {}
    try:
        root_resolved = root.resolve()
    except OSError:
        return {}
    files: dict[str, Path] = {}
    for match in matches:
        path = Path(match)
        try:
            if not path.resolve().is_relative_to(root_resolved):  # containment guard
                continue
        except (OSError, ValueError):
            continue
        files.setdefault(path.stem, path)  # stem == "agent-<hex>"
    return files


def read_subagent_role(
    client_session_id: str, *, projects_root: Path | str | None = None
) -> SubagentRole | None:
    """Role + task for one child session id, or ``None`` (not a child / no file)."""

    parts = _agent_id_from_child_session(client_session_id)
    if parts is None:
        return None
    parent, agent_id = parts
    files = subagent_files_for_parent(parent, projects_root=projects_root)
    path = files.get(agent_id)
    if path is None:
        return None
    return _extract_role(path)


def read_roles_for_children(
    parent_client_session_id: str,
    child_session_ids: list[str],
    *,
    projects_root: Path | str | None = None,
) -> dict[str, SubagentRole]:
    """Roles for a set of a parent's children, using a single directory scan.

    Keyed by the full child session id. Children with no locatable/parseable
    transcript are simply absent from the result (fail-soft).
    """

    files = subagent_files_for_parent(parent_client_session_id, projects_root=projects_root)
    if not files:
        return {}
    roles: dict[str, SubagentRole] = {}
    for child_id in child_session_ids:
        parts = _agent_id_from_child_session(child_id)
        if parts is None:
            continue
        path = files.get(parts[1])
        if path is None:
            continue
        role = _extract_role(path)
        if role is not None:
            roles[child_id] = role
    return roles


__all__ = [
    "SCAN_ENV",
    "ROOT_ENV",
    "SubagentRole",
    "scan_enabled",
    "default_projects_root",
    "subagent_files_for_parent",
    "read_subagent_role",
    "read_roles_for_children",
]
