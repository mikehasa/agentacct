"""Glance data layer — the versioned local snapshot behind ``GET /v1/glance``.

This is what a native shell (menu bar app, SwiftBar plugin, widgets) polls to
show "usage · cost · plan · active sessions" at a glance. Design contract:

* **One aggregation, N surfaces** — usage/cost windows and provider limits come
  from :mod:`agentacct.usage_snapshot` (the exact functions ``agentacct now``
  and the TUI render), and plan calibration from :mod:`agentacct.plan_cost`
  with the same calibrated-or-nothing honesty rule. The glance never invents a
  number another surface would not show.
* **Cheap under polling** — the payload is rebuilt only when the event list
  actually changes (``events_fingerprint``); a poll that finds nothing new is a
  dictionary lookup. The expensive work-ledger build is deliberately NOT used
  here: active sessions are derived from the section event stream directly.
* **Additive-only schema** — consumers pin ``schema`` and ignore unknown keys;
  existing keys are never renamed or removed within v1.

The discovery-file helpers let a native shell find and authenticate to the
server without configuration: ``agentacct serve`` binds 127.0.0.1, then writes
``<store>/local-api.json`` (0600) with the actual port and a per-boot bearer
token — the Tailscale "sameuserproof" / Syncthing api-key pattern. Readers
treat the file as the only source of truth and re-read it on auth failure.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

GLANCE_SCHEMA_VERSION = "agentacct.glance.v1"
DISCOVERY_SCHEMA_VERSION = "agentacct.local-api-discovery.v1"
DISCOVERY_FILENAME = "local-api.json"

# A session counts as "recent" in the glance when its latest section/usage
# activity is at most this old. Bounded list, newest first.
ACTIVE_SESSION_WINDOW_SECONDS = 6 * 3600
ACTIVE_SESSION_LIMIT = 8

# Clients whose usage can be expressed as a share of a provider plan. Mirrors
# the TUI's plan column (tui._PLAN_CLIENTS); keep the two in sync.
PLAN_CLIENTS = ("claude-code", "codex")


def events_fingerprint(events: list) -> int:
    """A content-sensitive change key for the loaded event list.

    The event COUNT is NOT a sound change key: the usage importer supersedes rows
    IN PLACE (``service.replace_events`` drops a re-observed row and records a
    fresh one), so a growing single-model session keeps the count fixed while its
    tokens/cost change — the whole point of a live view. Hashing each event's
    identity + observation time makes any append, removal, or in-place supersede
    (which records a new event id) change the key and trigger a rebuild.

    The values are stringified so the key is TOTAL: a corrupted/hand-injected
    ledger row can round-trip ``event_id`` / ``created_at`` as a JSON list or
    object (unhashable), and this function runs on unguarded refresh paths —
    it must never raise. (The TUI aliases this same function.)
    """

    return hash(tuple((str(event.get("event_id")), str(event.get("created_at"))) for event in events))


# ---------------------------------------------------------------------------
# discovery file (native-shell handshake)
# ---------------------------------------------------------------------------


def discovery_file_path(store_dir: Path | str) -> Path:
    return Path(store_dir).expanduser() / DISCOVERY_FILENAME


def write_discovery_file(
    store_dir: Path | str,
    *,
    host: str,
    port: int,
    token: str,
    version: str,
    pid: int | None = None,
) -> Path:
    """Atomically write the 0600 discovery file next to the store.

    Written by ``agentacct serve`` after the bind port is chosen and before the
    server loop starts. Single-slot by design: one dashboard server per store
    owns the file; a restart overwrites it (fresh token every boot). The 0600
    mode is enforced via ``os.fchmod`` (umask-proof) and survives the atomic
    rename, so the token is never world-readable, not even transiently.
    """

    path = discovery_file_path(store_dir)
    # `agentacct serve` may be pointed at a store directory that does not exist
    # yet (the app factory creates it on first use); the discovery file is
    # written first, so it creates the directory the same way.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": DISCOVERY_SCHEMA_VERSION,
        "host": host,
        "port": int(port),
        "token": token,
        "pid": int(pid if pid is not None else os.getpid()),
        "version": version,
        "store_dir": str(Path(store_dir).expanduser()),
        "written_at": time.time(),
    }
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    return path


def read_discovery_file(store_dir: Path | str) -> dict[str, Any] | None:
    """The parsed discovery payload, or ``None`` when absent/unreadable/foreign.

    Never raises: a missing server, a half-written file, or a future schema all
    read as "no discoverable server" — callers fall back to their disconnected
    state exactly as they would for a dead port.
    """

    path = discovery_file_path(store_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != DISCOVERY_SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("port"), int) or not str(payload.get("token") or ""):
        return None
    return payload


def remove_discovery_file(store_dir: Path | str, *, pid: int | None = None) -> bool:
    """Remove the discovery file iff it belongs to ``pid`` (default: this process).

    The pid gate keeps a dying old server from deleting the file a newly
    restarted server just wrote (start-during-shutdown overlap). Returns True
    only when this call actually unlinked the current owner's file.
    """

    owner = int(pid if pid is not None else os.getpid())
    path = discovery_file_path(store_dir)
    payload = read_discovery_file(store_dir)
    if payload is None or payload.get("pid") != owner:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# glance snapshot (pure over events)
# ---------------------------------------------------------------------------


def _section_status_and_title(events: list[dict[str, Any]], *, now: float) -> list[dict[str, Any]]:
    """Recent sessions from the section event stream, newest activity first.

    Deliberately ledger-free: the full work-ledger build is too heavy to run on
    a poll cadence, while the latest section event per (client, session) is a
    single pass over the already-loaded list. A session with sections shows its
    latest section title/status; a session that only imported usage shows just
    activity time. Timestamps are hostile-tolerant (never raise).
    """

    def _safe_time(value: Any) -> float:
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) and number > 0 else 0.0

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        if not event_type.startswith("section_"):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        client = str(metadata.get("client") or event.get("source") or "")
        session_id = str(metadata.get("client_session_id") or "")
        if not client or not session_id:
            continue
        created = _safe_time(event.get("created_at"))
        key = (client, session_id)
        current = latest.get(key)
        if current is None or created >= current["last_activity_at"]:
            latest[key] = {
                "client": client,
                "session_id": session_id,
                "title": str(metadata.get("section_title") or "") or None,
                "status": str(metadata.get("section_status") or "") or None,
                "last_activity_at": created,
            }
    cutoff = now - ACTIVE_SESSION_WINDOW_SECONDS
    rows = [row for row in latest.values() if row["last_activity_at"] >= cutoff]
    rows.sort(key=lambda row: row["last_activity_at"], reverse=True)
    return rows[:ACTIVE_SESSION_LIMIT]


def build_glance_snapshot(
    events: list[dict[str, Any]],
    *,
    store_dir: Path | str,
    version: str,
    now: float | None = None,
) -> dict[str, Any]:
    """The full glance payload from one already-loaded event list.

    Heavy siblings are imported lazily (same pattern as usage_snapshot) so
    importing this module stays cheap for CLI cold starts and tests.
    """

    from .plan_cost import calibrate_plan_weights, session_plan_pcts
    from .usage_snapshot import build_live_snapshot, limit_is_stale, usage_records

    moment = time.time() if now is None else float(now)
    live = build_live_snapshot(events, now=moment)

    usage_windows = [
        {"label": window.label, "days": window.days, "totals": window.totals}
        for window in live.usage.windows
    ]

    limits: list[dict[str, Any]] = []
    for limit in live.limits:
        entry = limit.to_json_entry()
        entry["stale"] = limit_is_stale(limit, moment)
        limits.append(entry)

    # Plan calibration per plan-bearing client — the calibrated-or-nothing
    # honesty rule: per-session percentages exist ONLY when the estimate is
    # grounded in this account's own recorded limit history.
    plan: list[dict[str, Any]] = []
    session_pcts: dict[str, float] = {}
    for client in PLAN_CLIENTS:
        records = usage_records(events, client=client)
        weights = calibrate_plan_weights(events, client=client, records=records)
        plan.append({"client": client, "confidence": weights.confidence})
        if weights.confidence == "calibrated":
            session_pcts.update(session_plan_pcts(records, weights, client=client))

    recent_sessions = _section_status_and_title(events, now=moment)
    for row in recent_sessions:
        pct = session_pcts.get(row["session_id"])
        row["plan_pct"] = round(pct, 2) if pct is not None else None

    return {
        "schema": GLANCE_SCHEMA_VERSION,
        "generated_at": moment,
        "daemon": {"version": version, "pid": os.getpid(), "store_dir": str(Path(store_dir).expanduser())},
        "usage": {
            "as_of": live.usage.as_of,
            "event_count": live.usage.event_count,
            "usage_record_count": live.usage.usage_record_count,
            "windows": usage_windows,
            "by_client": live.usage.by_client,
            "breakdown_window": live.usage.breakdown_window,
        },
        "limits": limits,
        "plan": plan,
        "recent_sessions": recent_sessions,
    }


class GlanceCache:
    """Fingerprint-keyed cache so polling never recomputes an unchanged payload.

    Thread-tolerant under FastAPI's threadpool: the cached value is one atomic
    tuple assignment, so a concurrent reader can never observe a torn pair; two
    racing rebuilds waste one build and the last writer wins (same pattern as
    the TUI's plan cache).
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._cached: tuple[int, dict[str, Any]] | None = None

    def snapshot(
        self,
        events: list[dict[str, Any]],
        *,
        store_dir: Path | str,
        version: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        fingerprint = events_fingerprint(events)
        cached = self._cached
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        with self._lock:
            cached = self._cached
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            payload = build_glance_snapshot(events, store_dir=store_dir, version=version, now=now)
            self._cached = (fingerprint, payload)
            return payload
