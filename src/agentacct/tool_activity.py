"""Tool-activity capture for the Receipt's Actions dimension.

The Actions question a Receipt must answer is "what did the agent reach for".
This records, per tool call, a COARSE CATEGORY (read/edit/execute/…) AND the
tool's NAME — a builtin like ``Read``/``Bash``, an ``mcp__server__tool`` name, or
a custom command. It never records a tool's ARGUMENTS, its output, or any PATH:
only the name and the category derived from it. The taxonomy is derivable from
the name ALONE — we do not tell ``git commit`` from ``ls`` inside ``execute``,
because that would require reading arguments.

Capturing the NAME (a deliberate move from the earlier category-only line) is
what lets the Receipt answer "which specific tool / connector did the agent
use" — the raw material a manager or a diagnostic pass needs to reason "this
tool caused that problem". The name is captured and stored LOCALLY like every
other field; what to REDACT when a Receipt travels OUTSIDE its origin is a
separate, later presentation concern, not a capture-time one.

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
import re
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
# or payload. ``mcp__*`` tools collapse to ``mcp`` HERE (the coarse category); the
# specific ``mcp__server__tool`` name is preserved separately by
# ``normalize_tool_name`` for the Actions tool-name breakdown.
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
    ``mcp`` by prefix for the CATEGORY only; the full name is kept separately by
    ``normalize_tool_name``.
    """

    name = str(tool_name or "").strip().lower()
    if not name:
        return "other"
    if name.startswith("mcp__"):
        return "mcp"
    return _CATEGORY_BY_TOOL_NAME.get(name, "other")


# A hard bound on the captured NAME. A tool name is short (``Read``,
# ``mcp__server__tool``); this only stops a pathological value from bloating the
# spool. It bounds the name, never its content interpretation.
_TOOL_NAME_MAX = 120


def normalize_tool_name(tool_name: Any) -> str | None:
    """The tiered tool-NAME captured alongside the category.

    Returns the tool's identity verbatim — a builtin (``Read``/``Bash``), an
    ``mcp__server__tool`` name, or a custom command — trimmed and length-bounded.
    It reads ONLY the name: never arguments, never a path, never output. Blank in
    -> ``None`` (nothing to record), so a missing name is an honest gap rather
    than an empty string in the counts.
    """

    name = str(tool_name or "").strip()
    if not name:
        return None
    return name[:_TOOL_NAME_MAX]


_TOUCHED_PATH_MAX = 240
# Hard cap on the number of distinct touched paths carried per drained batch, so a
# pathological session can never bloat one event. "At least this much", never more.
_TOUCHED_FILES_PER_BATCH_MAX = 200


def _normalize_touched_path(path: Any) -> str | None:
    """Defensive normalization of a caller-supplied touched-file path.

    The CALLER (the hook) already relativizes the edited file to the session cwd;
    this is the belt-and-suspenders gate that GUARANTEES the store never holds an
    absolute path, a Windows drive/UNC path, a ``..`` escape, or an over-long
    string — so even a buggy caller can never leak an absolute prefix (home dir,
    username). Blank / unsafe -> ``None`` (nothing recorded)."""

    text = str(path or "").strip()
    if not text or "\x00" in text:
        return None
    if re.match(r"^[A-Za-z]:", text):  # Windows drive (C:\...)
        return None
    # A leading slash OR backslash is absolute — one backslash is a Windows
    # drive-relative absolute (``\Users\bob\...``) that ``os.path.isabs`` misses on
    # POSIX, so it must be caught here before ``replace("\\","/")`` turns it into a
    # ``/…`` path whose empty leading segment would be silently dropped.
    if text.startswith(("\\", "/")):  # single/UNC backslash, or / and //
        return None
    normalized = text.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)[:_TOUCHED_PATH_MAX]


def tool_activity_spool_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / _SPOOL_RELATIVE_PATH


def record_tool_activity_tick(
    store_dir: Path | str,
    *,
    client: str,
    session_id: str,
    category: str,
    name: Any = None,
    path: Any = None,
    at: float,
) -> None:
    """Append ONE tick to the store spool. Best-effort; never raises.

    Writes the small scalars ``{c, s, k, t}`` — client, session id, category, and
    a timestamp — plus ``n``, the tool NAME, when one is given, and ``p``, the
    repo-relative path a file-EDIT tool wrote, when the caller extracted one (the
    Actions dimension's "which artifacts were touched" — the caller relativizes it
    to the session cwd and drops anything outside, so no absolute prefix, home dir,
    or username is ever stored; arguments, content and output are still never
    recorded). The line is a single tiny JSON object written with ``O_APPEND`` so
    concurrent hook processes (many tool calls in flight) never corrupt or
    interleave lines.
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
        payload = {"c": client, "s": session_id, "k": category, "t": float(at)}
        normalized_name = normalize_tool_name(name)
        if normalized_name:
            payload["n"] = normalized_name
        touched = _normalize_touched_path(path)
        if touched:
            payload["p"] = touched
        line = json.dumps(
            payload,
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
    name_counts: dict[tuple[str, str], dict[str, int]] = {}
    touched_paths: dict[tuple[str, str], list[str]] = {}
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
        # Tool NAME (present only on ticks written after name capture shipped —
        # older lines simply have no ``n``, an honest undercount of names, never
        # a wrong one).
        name = normalize_tool_name(row.get("n"))
        if name:
            name_bucket = name_counts.setdefault(key, {})
            name_bucket[name] = name_bucket.get(name, 0) + 1
        # Touched file path (present only on edit-tool ticks written after path
        # capture shipped). Deduped, insertion-ordered, and hard-capped per batch.
        touched = _normalize_touched_path(row.get("p"))
        if touched:
            path_bucket = touched_paths.setdefault(key, [])
            if touched not in path_bucket and len(path_bucket) < _TOUCHED_FILES_PER_BATCH_MAX:
                path_bucket.append(touched)
        stamp = row.get("t")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            latest[key] = max(latest.get(key, 0.0), float(stamp))

    events: list[dict[str, Any]] = []
    for (client, session), bucket in sorted(counts.items()):
        created_at = latest.get((client, session)) or (float(now) if now is not None else 0.0)
        digest = uuid.uuid5(
            uuid.NAMESPACE_URL, f"{batch_token}\0{client}\0{session}"
        ).hex
        metadata: dict[str, Any] = {
            "client": client,
            "client_session_id": session,
            "tool_category_counts": dict(sorted(bucket.items())),
            "capture_basis": TOOL_ACTIVITY_CAPTURE_BASIS,
            "captured_at": created_at,
            "sentinel_semantic_kind": "tool_activity",
        }
        names = name_counts.get((client, session))
        if names:
            # Tool names ride as list VALUES, not dict KEYS. The store's secret
            # redaction blanks the VALUE of any credential-shaped KEY, so a
            # connector like ``mcp__vault__get_token`` used as a key would be
            # silently redacted out. As ``{"name": …, "count": …}`` objects the
            # name is a value under the innocuous key ``name`` and survives.
            metadata["tool_names"] = [
                {"name": name, "count": count} for name, count in sorted(names.items())
            ]
        touched = touched_paths.get((client, session))
        if touched:
            # Paths ride as a plain list of string VALUES (same redaction-safe shape
            # rationale as tool_names): a path is never a dict KEY, so the store's
            # secret redaction can never blank it.
            metadata["touched_files"] = list(touched)
        events.append(
            {
                "event_id": f"toolact:{digest}",
                "created_at": created_at,
                "source": client,
                "event_type": TOOL_ACTIVITY_EVENT_TYPE,
                "run_id": None,
                "metadata": metadata,
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


def build_tool_names_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, int]]:
    """Sum ``tool_activity_observed`` per-NAME counts per (client, session).

    Mirrors ``build_tool_activity_by_session`` for the specific tool names
    (``Read``, ``Bash``, ``mcp__server__tool``, …). Reads the event's
    ``tool_names`` list of ``{"name": …, "count": …}`` objects — the name is a
    VALUE, not a dict key, so a credential-shaped connector name is never
    redacted out by the store's secret redaction. Batches are additive, so the
    per-session total is stable across imports. Names are an OPEN set, so unlike
    categories they are not filtered against a fixed vocabulary — but they are
    normalized (trimmed, length-bounded) and only positive integer counts are
    kept. A session recorded before name capture shipped simply has no names — an
    honest gap, never a fabricated zero.
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
        names = metadata.get("tool_names")
        if not isinstance(names, list):
            continue
        bucket = result.setdefault((client, session), {})
        for entry in names:
            if not isinstance(entry, Mapping):
                continue
            name = normalize_tool_name(entry.get("name"))
            if not name:
                continue
            value = entry.get("count")
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                continue
            bucket[name] = bucket.get(name, 0) + value
    return {key: value for key, value in result.items() if value}


def build_touched_files_by_session(
    events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    """Collect ``tool_activity_observed`` touched-file paths per (client, session).

    Mirrors ``build_tool_names_by_session``: reads each event's ``touched_files``
    list (repo-relative paths a file-edit tool wrote). Batches are additive and the
    union is deduped (insertion-ordered) across imports. A session recorded before
    path capture shipped simply has none — an honest gap, never a fabricated entry.
    Every path is re-run through ``_normalize_touched_path`` here (the third gate,
    after the tick and the drain), which is safety-equivalent to the projection's
    ``_safe_relative_posix_path`` — both reject any absolute/drive/UNC/``..``/NUL path.
    """

    result: dict[tuple[str, str], list[str]] = {}
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
        paths = metadata.get("touched_files")
        if not isinstance(paths, list):
            continue
        bucket = result.setdefault((client, session), [])
        for candidate in paths:
            normalized = _normalize_touched_path(candidate)
            if normalized and normalized not in bucket:
                bucket.append(normalized)
    return {key: value for key, value in result.items() if value}


__all__ = [
    "TOOL_ACTIVITY_CAPTURE_BASIS",
    "TOOL_ACTIVITY_EVENT_TYPE",
    "TOOL_CATEGORIES",
    "build_tool_activity_by_session",
    "build_tool_names_by_session",
    "build_touched_files_by_session",
    "drain_tool_activity_spool",
    "ingest_tool_activity_spool",
    "normalize_tool_name",
    "record_tool_activity_tick",
    "tool_activity_spool_path",
    "tool_category",
]
