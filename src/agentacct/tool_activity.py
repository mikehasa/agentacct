"""Privacy-safe tool-CATEGORY capture for the Receipt's Actions dimension.

The Actions question a Receipt must answer is "what kinds of things did the
agent do". WorkEvent (``work_events.py``) deliberately excludes tool names,
arguments, and results, and this module holds that same line: it records only a
COARSE CATEGORY derived from a tool's NAME (never the name itself, never its
arguments, never a path). The taxonomy is intentionally derivable from the tool
name ALONE — we do not try to tell ``git commit`` from ``ls`` inside ``execute``,
because that would require reading arguments.

The capture path mirrors the terminal-CLI statusLine spool
(``rate_limits.write_claude_statusline_spool``): the installed Claude Code
PreToolUse hook already sees every tool name and fails open, so it appends one
tiny category tick to a per-store spool (no daemon required, O(1), best-effort).
``agentacct usage import-local`` later drains the spool into additive
``tool_activity_observed`` events, which the work ledger reduces into a
per-session ``tool_category_counts`` and the Task projection sums into
``task["actions"]``. A session with no captured ticks honestly carries no
counts (the Receipt shows a Gap), never a fabricated zero.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable


# frozen: stored ``tool_activity_observed`` events carry this event_type forever.
TOOL_ACTIVITY_EVENT_TYPE = "tool_activity_observed"
TOOL_ACTIVITY_CAPTURE_BASIS = "client_hook_tool_category"
_SPOOL_RELATIVE_PATH = Path("spool") / "claude-tool-activity.jsonl"

# The complete, closed category set. Every tool name maps to exactly one of
# these; anything unrecognised is ``other`` (honest, never dropped silently).
TOOL_CATEGORIES: frozenset[str] = frozenset(
    {"read", "edit", "execute", "search", "network", "agent", "plan", "mcp", "other"}
)

# Name-only taxonomy. Keys are lower-cased tool names. Only the tool NAME is
# consulted — never its arguments — so this map can never leak a command, path,
# or payload. ``mcp__*`` tools are collapsed to ``mcp`` by prefix below so no
# specific MCP tool name is ever recorded.
_CATEGORY_BY_TOOL_NAME: dict[str, str] = {
    "read": "read",
    "notebookread": "read",
    "edit": "edit",
    "write": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "bash": "execute",
    "bashoutput": "execute",
    "killshell": "execute",
    "killbash": "execute",
    "grep": "search",
    "glob": "search",
    "ls": "search",
    "webfetch": "network",
    "websearch": "network",
    "task": "agent",
    "agent": "agent",
    "todowrite": "plan",
    "exitplanmode": "plan",
    "enterplanmode": "plan",
}


def tool_category(tool_name: Any) -> str:
    """Map a tool NAME to a coarse category. Never inspects arguments.

    Unknown names return ``other`` rather than being dropped, so the count of
    what the agent did stays honest. ``mcp__<server>__<tool>`` collapses to
    ``mcp`` by prefix, so a specific MCP tool name is never recorded.
    """

    name = str(tool_name or "").strip().lower()
    if not name:
        return "other"
    if name.startswith("mcp__"):
        return "mcp"
    return _CATEGORY_BY_TOOL_NAME.get(name, "other")


def tool_activity_spool_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / _SPOOL_RELATIVE_PATH


def record_tool_activity_tick(
    store_dir: Path | str,
    *,
    client: str,
    session_id: str,
    category: str,
    at: float,
) -> None:
    """Append ONE category tick to the store spool. Best-effort; never raises.

    Only the four small scalars ``{c, s, k, t}`` are written — client, session
    id, category, and a timestamp. No tool name, no arguments, no path. The line
    is a single tiny JSON object written with ``O_APPEND`` so concurrent hook
    processes (many tool calls in flight) never corrupt or interleave lines.
    """

    try:
        if category not in TOOL_CATEGORIES:
            category = "other"
        client = str(client or "").strip()
        session_id = str(session_id or "").strip()
        if not client or not session_id:
            return
        target = tool_activity_spool_path(store_dir)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(
            {"c": client, "s": session_id, "k": category, "t": float(at)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def drain_tool_activity_spool(
    store_dir: Path | str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Consume the spool and return additive ``tool_activity_observed`` events.

    The spool is atomically renamed aside before being read, so hook ticks that
    land during the drain go to a freshly recreated spool and are picked up by
    the NEXT drain. Each returned event is one batch of per-session category
    counts; because batches are additive and carry a unique id, re-reducing the
    event log is idempotent and re-importing never double counts. A few ticks
    appended between the read and the rename can be lost — an acceptable, honest
    undercount (Actions means "at least this much"), never an overcount.
    """

    spool = tool_activity_spool_path(store_dir)
    if not spool.exists():
        return []
    batch_token = token or uuid.uuid4().hex
    consuming = spool.with_name(f".{spool.name}.consuming-{os.getpid()}-{batch_token}")
    try:
        os.replace(spool, consuming)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        raw = consuming.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    finally:
        try:
            consuming.unlink()
        except OSError:
            pass

    counts: dict[tuple[str, str], dict[str, int]] = {}
    latest: dict[tuple[str, str], float] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, Mapping):
            continue
        client = str(row.get("c") or "").strip()
        session = str(row.get("s") or "").strip()
        category = str(row.get("k") or "").strip()
        if not client or not session:
            continue
        if category not in TOOL_CATEGORIES:
            category = "other"
        key = (client, session)
        bucket = counts.setdefault(key, {})
        bucket[category] = bucket.get(category, 0) + 1
        stamp = row.get("t")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            latest[key] = max(latest.get(key, 0.0), float(stamp))

    events: list[dict[str, Any]] = []
    for (client, session), bucket in sorted(counts.items()):
        created_at = latest.get((client, session)) or (float(now) if now is not None else 0.0)
        digest = uuid.uuid5(
            uuid.NAMESPACE_URL, f"{batch_token}\0{client}\0{session}"
        ).hex
        events.append(
            {
                "event_id": f"toolact:{digest}",
                "created_at": created_at,
                "source": client,
                "event_type": TOOL_ACTIVITY_EVENT_TYPE,
                "run_id": None,
                "metadata": {
                    "client": client,
                    "client_session_id": session,
                    "tool_category_counts": dict(sorted(bucket.items())),
                    "capture_basis": TOOL_ACTIVITY_CAPTURE_BASIS,
                    "captured_at": created_at,
                    "sentinel_semantic_kind": "tool_activity",
                },
            }
        )
    return events


def ingest_tool_activity_spool(
    store_dir: Path | str,
    *,
    record: Callable[[dict[str, Any]], Any],
    now: float | None = None,
    token: str | None = None,
) -> int:
    """Drain the spool and hand each batch event to ``record``. Never raises.

    Mirrors ``rate_limits.record_snapshots_transitionally``'s
    ``record=service.record_event`` contract so the usage importer can call it
    the same way it records rate-limit snapshots.
    """

    try:
        events = drain_tool_activity_spool(store_dir, now=now, token=token)
    except Exception:  # noqa: BLE001 - a spool drain must never fail the import.
        return 0
    written = 0
    for event in events:
        try:
            record(event)
            written += 1
        except Exception:  # noqa: BLE001 - one bad record must not abort the rest.
            continue
    return written


def build_tool_activity_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, int]]:
    """Sum ``tool_activity_observed`` batch counts per (client, session).

    Batches are additive: reducing the whole event log sums every recorded
    batch, so the per-session total is stable no matter how many import runs
    produced it. Only known categories and positive integer counts are kept.
    """

    result: dict[tuple[str, str], dict[str, int]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != TOOL_ACTIVITY_EVENT_TYPE:
            continue
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        client = str(metadata.get("client") or "").strip()
        session = str(metadata.get("client_session_id") or "").strip()
        if not client or not session:
            continue
        counts = metadata.get("tool_category_counts")
        if not isinstance(counts, Mapping):
            continue
        bucket = result.setdefault((client, session), {})
        for category, value in counts.items():
            if category not in TOOL_CATEGORIES:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                continue
            bucket[category] = bucket.get(category, 0) + value
    return {key: value for key, value in result.items() if value}


__all__ = [
    "TOOL_ACTIVITY_CAPTURE_BASIS",
    "TOOL_ACTIVITY_EVENT_TYPE",
    "TOOL_CATEGORIES",
    "build_tool_activity_by_session",
    "drain_tool_activity_spool",
    "ingest_tool_activity_spool",
    "record_tool_activity_tick",
    "tool_activity_spool_path",
    "tool_category",
]
