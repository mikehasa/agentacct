"""Per-agent connection view.

Joins two independent facts into one honest row per supported agent:
- "is it set up" — whether agentacct wrote its instrumentation (the activation
  record's ``clients`` list), and
- "is it recording" — that source's live ingestion-health state.

and classifies each agent by HOW agentacct instruments it, which decides what a
row can offer. Everything here is a pure read: a row never claims "connected" or
"recording" without evidence from those two stores.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CONNECTIONS_SCHEMA_VERSION = "agentacct.connections.v1"

# How agentacct instruments each agent — decides the row's available actions.
#   active  : agentacct writes hooks/MCP/instructions; `onboard --agent X` both
#             connects AND idempotently re-syncs (the point-to-point fix).
#   passive : observation-only — agentacct just reads its logs; nothing to
#             install, so the row is purely its import health.
#   semi    : imported, but its MCP registration is manual (the user runs a
#             command); agentacct has no writer, so the row offers guidance, not
#             a one-click connect.
_ACTIVE_CLIENTS = frozenset({"claude-code", "codex", "opencode", "hermes", "dsh"})
_PASSIVE_CLIENTS = frozenset({"cursor"})

_KIND_RANK = {"active": 0, "semi": 1, "passive": 2}

_DISPLAY_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "hermes": "Hermes",
    "dsh": "DeepSeek Harness",
    "openclaw": "OpenClaw",
    "cursor": "Cursor",
}


def agent_kind(client: str) -> str:
    """active | semi | passive — from how agentacct instruments the agent."""
    if client in _PASSIVE_CLIENTS:
        return "passive"
    if client in _ACTIVE_CLIENTS:
        return "active"
    return "semi"


def _issues_by_source(issues: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        source = issue.get("source")
        if isinstance(source, str) and source:
            grouped.setdefault(source, []).append(dict(issue))
    return grouped


def _derive_status(
    kind: str, configured: bool, recording_state: Any, live: bool, has_error_issue: bool
) -> tuple[str, str | None]:
    """(status, primary_action). Honest: status reflects only the two stores.

    ``live`` is the load-bearing liveness gate: a healthy source only counts as
    actively recording/reading when the CURRENT watcher is running AND covers it
    (``watcher.state == "running"`` and the source's scope is ``"watched"``). A
    stopped/stale watcher, or a source known only from a historical/manual
    import, leaves ``recording_state == "healthy"`` in the snapshot off a stale
    success — so without this gate the view would claim green "Recording" while
    nothing is being captured. When not live, a healthy source is only
    ``connected_idle`` (active) / ``read_only`` (passive), never green.

    Statuses: recording | connected_idle | not_connected | needs_attention |
    reading | read_only. Actions: connect | connect_manual | resync | resolve
    | None.
    """
    degraded = recording_state == "degraded" or has_error_issue
    recording = recording_state == "healthy" and live

    if kind == "passive":
        # Nothing to install; the row is purely its import health.
        if degraded:
            return "needs_attention", "resolve"
        if recording:
            return "reading", None
        return "read_only", None
    if kind == "semi":
        # No onboarding writer ever records a semi agent (openclaw), so its
        # activation flag is never set — honesty must come from ingestion
        # evidence, exactly like a passive agent, not from a flag we never
        # write. The manual MCP-registration step rides along as standing
        # guidance (connect_manual, rendered as a note, never a one-click
        # button and never a false "it's set up" claim).
        if degraded:
            return "needs_attention", "resolve"
        if recording:
            return "reading", "connect_manual"
        return "read_only", "connect_manual"
    # active
    if not configured:
        return "not_connected", "connect"
    if degraded:
        # Set up, but its data/config needs a fix — the point-to-point re-sync.
        return "needs_attention", "resync"
    if recording:
        return "recording", None
    # Set up, but the current watcher hasn't confirmed live data yet.
    return "connected_idle", None


def build_connections(
    *,
    supported_clients: Iterable[str],
    configured_clients: Iterable[str],
    ingestion_snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """One row per supported agent, active agents first, then semi, then passive."""
    configured = {str(c) for c in configured_clients if c}
    rows = {
        str(row.get("source")): row
        for row in (ingestion_snapshot.get("sources") or [])
        if isinstance(row, Mapping) and row.get("source")
    }
    issues = _issues_by_source(ingestion_snapshot.get("issues") or [])
    watcher = ingestion_snapshot.get("watcher")
    # A live watcher: its snapshot projection reports state=="running" only when
    # the heartbeat is fresh (ingestion_health downgrades a stale/stopped watcher
    # to "stale"/"stopped"). Per-source scope=="watched" then means that live
    # watcher actually covers this source.
    watcher_running = isinstance(watcher, Mapping) and watcher.get("state") == "running"

    out: list[dict[str, Any]] = []
    for client in supported_clients:
        client = str(client)
        kind = agent_kind(client)
        row = rows.get(client) or {}
        recording_state = row.get("state")
        scope = row.get("scope")
        live = watcher_running and scope == "watched"
        client_issues = issues.get(client, [])
        has_error_issue = any(
            (issue.get("severity") or "error") == "error" for issue in client_issues
        )
        status, action = _derive_status(
            kind, client in configured, recording_state, live, has_error_issue
        )
        out.append(
            {
                "id": client,
                "display_name": _DISPLAY_NAMES.get(client, client),
                "kind": kind,
                "configured": client in configured,
                "recording_state": recording_state,
                "scope": row.get("scope"),
                "last_success_at": row.get("last_success_at"),
                "issues": client_issues,
                "status": status,
                "primary_action": action,
            }
        )

    out.sort(key=lambda conn: (_KIND_RANK.get(conn["kind"], 9), conn["id"]))
    return out
