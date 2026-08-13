"""Ambient session-lifecycle capture for the Receipt's semantic layer.

The semantic layer (a section and its lifecycle: started/checkpoint/completed/
handed_off) is otherwise 100% agent-cooperative — it exists only if the agent
calls ``agentacct_record_section``. That makes the NORMAL end of a session — the
agent just stops without recording a terminal — leave its section open forever,
so the Task reads as perpetually in-progress.

This module adds the one AMBIENT fact needed to close that loop honestly: the
Claude Code ``SessionEnd`` hook fires when a root session terminates, and this
records a single ``session_end_observed`` event carrying only content-free
identity + timing (client, session id, the end ``reason``, a timestamp) — never
any conversation content. The reducer then DERIVES an ``ended_open`` disposition
for a still-open step whose session has ended (see ``task_outcome``); nothing is
fabricated as "completed", and the derived state is marked ``asserted_by:
inferred`` so it never claims the agent's or a check's certainty.

Capture mirrors ``tool_activity``: the fail-open hook appends one tiny line to a
per-store spool, and ``usage import-local`` later drains it into the store — no
daemon required, O(1), best-effort. A lost line is an honest undercount (a
missed auto-close leaves the section open, exactly as today), never an overcount.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


# frozen: stored ``session_end_observed`` events carry this event_type forever.
SESSION_END_EVENT_TYPE = "session_end_observed"
SESSION_END_CAPTURE_BASIS = "client_hook_session_end"
_SPOOL_RELATIVE_PATH = Path("spool") / "claude-session-end.jsonl"

# frozen: stored ``turn_boundary_observed`` events carry this event_type forever.
# Distinct from SESSION_END because a hermes ``on_session_end`` fires per TURN
# (once per user message), not at real session close. It is a pure liveness
# heartbeat: ``build_session_end_by_session`` never treats it as an end (that
# check keys on SESSION_END_EVENT_TYPE), so it contributes to a session's latest
# activity but can NEVER derive an ``ended_open`` disposition. Mapping a per-turn
# end to ``ended_open`` would falsely close a still-live session between turns.
TURN_BOUNDARY_EVENT_TYPE = "turn_boundary_observed"
TURN_BOUNDARY_CAPTURE_BASIS = "client_hook_turn_boundary"
_TURN_BOUNDARY_SPOOL_RELATIVE_PATH = Path("spool") / "hermes-turn-boundary.jsonl"

# A hard bound on the captured reason (a short enum like ``clear``/``logout``).
_REASON_MAX = 64


def _normalize_reason(reason: Any) -> str | None:
    text = str(reason or "").strip()
    if not text:
        return None
    return text[:_REASON_MAX]


def session_end_spool_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / _SPOOL_RELATIVE_PATH


def record_session_end_tick(
    store_dir: Path | str,
    *,
    client: str,
    session_id: str,
    reason: Any = None,
    at: float,
) -> None:
    """Append ONE session-end tick to the store spool. Best-effort; never raises.

    Writes only the scalars ``{c, s, t}`` — client, session id, timestamp — plus
    ``r``, the end reason (a short enum), when given. No conversation content, no
    path, no payload. ``O_APPEND`` so concurrent hook processes never interleave.
    """

    try:
        client = str(client or "").strip()
        session_id = str(session_id or "").strip()
        if not client or not session_id:
            return
        target = session_end_spool_path(store_dir)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload: dict[str, Any] = {"c": client, "s": session_id, "t": float(at)}
        normalized_reason = _normalize_reason(reason)
        if normalized_reason:
            payload["r"] = normalized_reason
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def drain_session_end_spool(
    store_dir: Path | str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Consume the spool and return additive ``session_end_observed`` events.

    The spool is atomically renamed aside before being read, so ticks that land
    during the drain go to a freshly recreated spool and are picked up next time.
    One event per (client, session): the LATEST end timestamp wins (a session can
    legitimately end more than once across resume cycles — the most recent end is
    the one that matters for the disposition). Each event carries a deterministic
    id so re-reducing the event log is idempotent and re-importing never doubles.
    """

    spool = session_end_spool_path(store_dir)
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

    latest: dict[tuple[str, str], float] = {}
    reasons: dict[tuple[str, str], str] = {}
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
        if not client or not session:
            continue
        stamp = row.get("t")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            continue
        key = (client, session)
        stamp = float(stamp)
        if stamp >= latest.get(key, float("-inf")):
            latest[key] = stamp
            reason = _normalize_reason(row.get("r"))
            if reason:
                reasons[key] = reason
            elif key in reasons:
                del reasons[key]

    events: list[dict[str, Any]] = []
    for (client, session), ended_at in sorted(latest.items()):
        digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{batch_token}\0{client}\0{session}").hex
        metadata: dict[str, Any] = {
            "client": client,
            "client_session_id": session,
            "ended_at": ended_at,
            "capture_basis": SESSION_END_CAPTURE_BASIS,
            "sentinel_semantic_kind": "session_lifecycle",
        }
        reason = reasons.get((client, session))
        if reason:
            metadata["reason"] = reason
        events.append(
            {
                "event_id": f"sessend:{digest}",
                "created_at": ended_at,
                "source": client,
                "event_type": SESSION_END_EVENT_TYPE,
                "run_id": None,
                "metadata": metadata,
            }
        )
    return events


def _event_session_key(event: Mapping[str, Any]) -> tuple[str, str] | None:
    meta = event.get("metadata")
    meta = meta if isinstance(meta, Mapping) else {}
    session = str(meta.get("client_session_id") or "").strip()
    if not session:
        return None
    client = str(meta.get("client") or event.get("source") or "").strip()
    if not client:
        return None
    return (client, session)


def build_session_end_by_session(events: Any) -> dict[tuple[str, str], float]:
    """Map ``(client, client_session_id)`` -> the session's end timestamp, but ONLY
    when that end is the session's LAST word.

    A Claude Code session that ends and is later resumed keeps the SAME
    ``client_session_id`` (``claude --resume`` reuses the id), so a stored
    ``session_end_observed`` alone does not mean the session is dead. We therefore
    treat an end as authoritative only when NO other event for that session
    postdates it: the resumed session's own work (tool activity, usage, a fresh
    section) carries a later timestamp and supersedes the stale end, so a live
    resumed session is never inferred ``ended_open``. The end contributes its OWN
    ``ended_at`` (the real session-end time, not the later import/record time) to
    the activity comparison, so a normal end that truly IS the last event still
    counts. The latest end wins across resume cycles.
    """

    ends: dict[tuple[str, str], float] = {}
    latest_activity: dict[tuple[str, str], float] = {}
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        key = _event_session_key(event)
        if key is None:
            continue
        is_end = event.get("event_type") == SESSION_END_EVENT_TYPE
        if is_end:
            meta = event.get("metadata")
            meta = meta if isinstance(meta, Mapping) else {}
            ended = meta.get("ended_at")
            if not isinstance(ended, (int, float)) or isinstance(ended, bool):
                ended = event.get("created_at")
            try:
                ts = float(ended)
            except (TypeError, ValueError):
                continue
            ends[key] = max(ends.get(key, float("-inf")), ts)
        else:
            created = event.get("created_at")
            if not isinstance(created, (int, float)) or isinstance(created, bool):
                continue
            ts = float(created)
        latest_activity[key] = max(latest_activity.get(key, float("-inf")), ts)

    # An end is authoritative only when nothing else for its session is newer.
    return {
        key: ended_at
        for key, ended_at in ends.items()
        if ended_at >= latest_activity.get(key, ended_at)
    }


def ingest_session_end_spool(
    store_dir: Path | str,
    *,
    record: Callable[[dict[str, Any]], Any],
    now: float | None = None,
    token: str | None = None,
) -> int:
    """Drain the spool and hand each event to ``record``. Never raises.

    Mirrors ``tool_activity.ingest_tool_activity_spool`` so the usage importer
    drains session-end the same way it drains tool activity and mechanical checks.
    """

    try:
        events = drain_session_end_spool(store_dir, now=now, token=token)
    except Exception:  # noqa: BLE001 - a spool drain must never fail the import.
        return 0
    written = 0
    for event in events:
        try:
            record(event)
            written += 1
        except Exception:  # noqa: BLE001 - one bad event must not abort the rest.
            continue
    return written


# ---------------------------------------------------------------------------
# Turn-boundary heartbeat (hermes on_session_end — a per-turn liveness fact)
# ---------------------------------------------------------------------------


def turn_boundary_spool_path(store_dir: Path | str) -> Path:
    return Path(store_dir) / _TURN_BOUNDARY_SPOOL_RELATIVE_PATH


def record_turn_boundary_tick(
    store_dir: Path | str,
    *,
    client: str,
    session_id: str,
    at: float,
    completed: bool | None = None,
    interrupted: bool | None = None,
) -> None:
    """Append ONE turn-boundary tick to the store spool. Best-effort; never raises.

    Writes only the scalars ``{c, s, t}`` — client, session id, timestamp — plus
    the terminal ``co`` (completed) / ``ir`` (interrupted) booleans when known. No
    conversation content, no path, no payload. ``O_APPEND`` so concurrent hook
    processes never interleave.
    """

    try:
        client = str(client or "").strip()
        session_id = str(session_id or "").strip()
        if not client or not session_id:
            return
        target = turn_boundary_spool_path(store_dir)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload: dict[str, Any] = {"c": client, "s": session_id, "t": float(at)}
        if isinstance(completed, bool):
            payload["co"] = completed
        if isinstance(interrupted, bool):
            payload["ir"] = interrupted
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - capture must never affect the hook.
        return


def drain_turn_boundary_spool(
    store_dir: Path | str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Consume the spool and return additive ``turn_boundary_observed`` events.

    The spool is atomically renamed aside before being read, so ticks that land
    during the drain go to a freshly recreated spool and are picked up next time.
    One event per (client, session): the LATEST turn-boundary timestamp wins (a
    session fires this every turn — only the most recent boundary matters for
    liveness). Each event carries a deterministic id so re-reducing is idempotent.
    """

    spool = turn_boundary_spool_path(store_dir)
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

    latest: dict[tuple[str, str], float] = {}
    flags: dict[tuple[str, str], dict[str, bool]] = {}
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
        if not client or not session:
            continue
        stamp = row.get("t")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            continue
        key = (client, session)
        stamp = float(stamp)
        if stamp >= latest.get(key, float("-inf")):
            latest[key] = stamp
            row_flags: dict[str, bool] = {}
            if isinstance(row.get("co"), bool):
                row_flags["completed"] = row["co"]
            if isinstance(row.get("ir"), bool):
                row_flags["interrupted"] = row["ir"]
            flags[key] = row_flags

    events: list[dict[str, Any]] = []
    for (client, session), observed_at in sorted(latest.items()):
        digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{batch_token}\0{client}\0{session}").hex
        metadata: dict[str, Any] = {
            "client": client,
            "client_session_id": session,
            "observed_at": observed_at,
            "capture_basis": TURN_BOUNDARY_CAPTURE_BASIS,
            "sentinel_semantic_kind": "session_lifecycle",
        }
        metadata.update(flags.get((client, session), {}))
        events.append(
            {
                "event_id": f"turnboundary:{digest}",
                "created_at": observed_at,
                "source": client,
                "event_type": TURN_BOUNDARY_EVENT_TYPE,
                "run_id": None,
                "metadata": metadata,
            }
        )
    return events


def ingest_turn_boundary_spool(
    store_dir: Path | str,
    *,
    record: Callable[[dict[str, Any]], Any],
    now: float | None = None,
    token: str | None = None,
) -> int:
    """Drain the turn-boundary spool and hand each event to ``record``. Never raises.

    Mirrors ``ingest_session_end_spool`` so the usage importer drains turn
    boundaries the same way it drains session-end and tool activity.
    """

    try:
        events = drain_turn_boundary_spool(store_dir, now=now, token=token)
    except Exception:  # noqa: BLE001 - a spool drain must never fail the import.
        return 0
    written = 0
    for event in events:
        try:
            record(event)
            written += 1
        except Exception:  # noqa: BLE001 - one bad event must not abort the rest.
            continue
    return written
