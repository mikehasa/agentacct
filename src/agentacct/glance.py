"""Glance data layer — the versioned local snapshot behind ``GET /v1/glance``.

This is what a native shell (menu bar app, SwiftBar plugin, widgets) polls to
show "usage · cost · plan · active sessions" at a glance. Design contract:

* **One aggregation, N surfaces** — usage/cost windows and provider limits come
  from :mod:`agentacct.usage_snapshot` (the exact functions ``agentacct now``
  and the TUI render), and plan calibration from :mod:`agentacct.plan_cost`
  with the same calibrated-or-nothing honesty rule. The glance never invents a
  number another surface would not show.
* **Cheap under polling** — the payload is rebuilt when the event list changes
  (``events_fingerprint``) or when the cached build is older than the cache
  TTL; a poll that hits the cache skips the aggregation rebuild (each poll
  still loads the event list once to compute the change key). The TTL exists
  because the payload is calendar/time-dependent (the "today" window, the
  recency cutoff, ``limits[].stale``) — an unchanged event list must still
  refresh across midnight. The expensive work-ledger build is deliberately NOT
  used here: recent sessions derive from the section + usage event streams in
  one pass.
* **Additive-only schema** — consumers pin ``schema`` and ignore unknown keys;
  existing keys are never renamed or removed within v1. Freshness must be
  judged from the HTTP response itself: ``generated_at`` is the BUILD time and
  may lag wall clock by up to the cache TTL.

The discovery-file helpers let a native shell find and authenticate to the
server without configuration: ``agentacct serve`` binds 127.0.0.1, then claims
``<store>/local-api.json`` (0600) with the actual port and a per-boot bearer
token — the Tailscale "sameuserproof" / Syncthing api-key pattern.

Reader contract for the discovery file: first-alive-writer-wins (a second
server against the same store leaves a live owner's file alone and simply
stays unpublished); a crash/SIGKILL/closed terminal can leave a stale file
whose ``pid`` is dead — readers treat a failed connect exactly like a missing
file (disconnected state). Every server runs a re-claim heartbeat
(:func:`run_discovery_heartbeat`), so a freed or stale slot — including the
restart-drain window where the new server started unpublished — is taken over
within one interval (~30s). The token is per-boot, so a stale token is
worthless. Re-read the file whenever auth fails (401): the server restarted
with a fresh token.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform: no flock available
    fcntl = None  # type: ignore[assignment]

GLANCE_SCHEMA_VERSION = "agentacct.glance.v1"
DISCOVERY_SCHEMA_VERSION = "agentacct.local-api-discovery.v1"
DISCOVERY_FILENAME = "local-api.json"
DISCOVERY_LOCK_FILENAME = "local-api.lock"

# A session counts as "recent" in the glance when its latest section/usage
# activity is at most this old. Bounded list, newest first.
ACTIVE_SESSION_WINDOW_SECONDS = 6 * 3600
ACTIVE_SESSION_LIMIT = 8

# Rebuild a cached payload after this many seconds even when the event list is
# unchanged: the "today" window, the recency cutoff, and limits[].stale are all
# clock-derived, so a fingerprint-only cache would serve yesterday's "today"
# after midnight on an idle store (adversarial-review HIGH finding).
GLANCE_CACHE_MAX_AGE_SECONDS = 60.0

# Clients whose usage can be expressed as a share of a provider plan. Mirrors
# the TUI's plan column (tui._PLAN_CLIENTS); keep the two in sync.
PLAN_CLIENTS = ("claude-code", "codex")

# Section statuses that mean "someone is (or should be) still working".
_OPEN_SECTION_STATUSES = frozenset({"started", "checkpoint"})


def events_fingerprint(events: list) -> int:
    """A content-sensitive change key for the loaded event list.

    The event COUNT is NOT a sound change key: the usage importer supersedes rows
    IN PLACE (``service.replace_events`` drops a re-observed row and records a
    fresh one), so a growing single-model session keeps the count fixed while its
    tokens/cost change — the whole point of a live view. Hashing each event's
    identity + observation time makes any append, removal, or in-place supersede
    (which records a new event id) change the key and trigger a rebuild.

    One write path rewrites events WITHOUT minting a new id or timestamp:
    ``service.bind_local_usage_source_namespaces`` (the TOFU bind) mutates only
    the source-namespace metadata fields in place — and those fields flip
    ``local_usage_additivity``, i.e. the totals. The key therefore folds those
    fields in as well; a fingerprint blind to them kept serving pre-bind totals
    until an unrelated event landed (adversarial-review MEDIUM finding). Any
    NEW in-place rewrite path must extend this key the same way.

    The values are stringified so the key is TOTAL: a corrupted/hand-injected
    ledger row can round-trip any of these fields as a JSON list or object
    (unhashable), and this function runs on unguarded refresh paths — it must
    never raise. (The TUI aliases this same function for all its caches.)
    """

    parts = []
    for event in events:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        parts.append(
            (
                str(event.get("event_id")),
                str(event.get("created_at")),
                str(metadata.get("source_namespace_fingerprint")),
                str(metadata.get("parent_source_namespace_fingerprint")),
                str(metadata.get("source_namespace_binding")),
            )
        )
    return hash(tuple(parts))


def _safe_timestamp(value: Any) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


# ---------------------------------------------------------------------------
# discovery file (native-shell handshake)
# ---------------------------------------------------------------------------


def discovery_file_path(store_dir: Path | str) -> Path:
    return Path(store_dir).expanduser() / DISCOVERY_FILENAME


@contextmanager
def _discovery_lock(store_dir: Path | str) -> Iterator[None]:
    """Serialize claim/remove against each other (closes the read-then-unlink
    TOCTOU a bare pid gate leaves open). flock on a sibling lock file; on a
    platform without fcntl the helpers degrade to the unlocked pid gate."""

    lock_path = Path(store_dir).expanduser() / DISCOVERY_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _pid_alive(pid: Any) -> bool:
    """Best-effort liveness: signal-0 probe. PermissionError means the pid
    exists (another user's process) and counts as alive."""

    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def write_discovery_file(
    store_dir: Path | str,
    *,
    host: str,
    port: int,
    token: str,
    version: str,
    pid: int | None = None,
) -> Path:
    """Atomically write the 0600 discovery file next to the store (unconditional).

    Prefer :func:`claim_discovery_file`, which refuses to clobber a LIVE
    owner's file. This raw writer stays lock-free so the claim path can call
    it while holding the discovery lock. The 0600 mode is enforced via
    ``os.fchmod`` (umask-proof) and survives the atomic rename, so the token
    is never world-readable, not even transiently.
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
    # O_EXCL after clearing any stale leftover: a crash between open and
    # replace can strand a 0600 tmp with a (per-boot, soon-worthless) token,
    # and O_EXCL refuses to follow a pre-planted symlink at the tmp name.
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def claim_discovery_file(
    store_dir: Path | str,
    *,
    host: str,
    port: int,
    token: str,
    version: str,
    pid: int | None = None,
) -> Path | None:
    """Publish the discovery file unless another LIVE server already owns it.

    First-alive-writer-wins: a second server against the same store must not
    clobber the managed daemon's slot and then delete it on exit, stranding a
    healthy daemon undiscoverable (adversarial-review MEDIUM finding). A file
    whose owner pid is dead (crash, SIGKILL, closed terminal) is stale and is
    taken over. Returns the path when this process now owns the slot, or
    ``None`` when a different live pid holds it — the caller keeps serving
    /v1 with its own token, just unpublished.
    """

    owner = int(pid if pid is not None else os.getpid())
    with _discovery_lock(store_dir):
        existing = read_discovery_file(store_dir)
        if existing is not None and existing.get("pid") != owner and _pid_alive(existing.get("pid")):
            return None
        return write_discovery_file(store_dir, host=host, port=port, token=token, version=version, pid=owner)


def run_discovery_heartbeat(
    store_dir: Path | str,
    *,
    host: str,
    port: int,
    token: str,
    version: str,
    stop: Any,
    interval_seconds: float = 30.0,
) -> None:
    """Periodically re-claim the discovery slot until ``stop`` is set.

    Why a heartbeat: claim-once leaves one stranding window — a new server
    that starts while the OLD one is still draining sees a live owner, stays
    unpublished, and when the old server exits (removing its own file) the
    healthy survivor is undiscoverable forever. The heartbeat closes that and
    every other freed-slot state (SIGKILL-stale files included): within one
    interval of the slot freeing up, the surviving server owns it.

    Each tick is one flock + one small read (plus a rewrite only when the slot
    is free or already ours — claim semantics). ``stop`` is a
    ``threading.Event``; the loop exits promptly when it is set. Errors are
    swallowed per-tick: discovery is best-effort and must never take the
    server down.
    """

    while not stop.wait(interval_seconds):
        try:
            claim_discovery_file(store_dir, host=host, port=port, token=token, version=version)
        except OSError:
            continue


def read_discovery_file(store_dir: Path | str) -> dict[str, Any] | None:
    """The parsed discovery payload, or ``None`` when absent/unreadable/foreign.

    Never raises: a missing server, a half-written file, or a future schema all
    read as "no discoverable server" — callers fall back to their disconnected
    state exactly as they would for a dead port. Liveness is deliberately NOT
    checked here (see the module docstring's reader contract): a stale file and
    a dead port produce the same disconnected UX.
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
    restarted server just wrote; the read-and-unlink runs under the discovery
    lock so a concurrent claim cannot land between the check and the unlink.
    Returns True only when this call actually unlinked the owner's file.
    """

    owner = int(pid if pid is not None else os.getpid())
    path = discovery_file_path(store_dir)
    with _discovery_lock(store_dir):
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


def fold_namespace_compatible(
    child_ns: str | None, parent_ns: str | None, expected_parent_ns: str | None
) -> bool:
    """The same source-home compatibility rule the ledger's parent join uses
    (``work_ledger._session_source_parent_compatible``): a child folds into a
    parent only when both live in the same recorded source home. A mismatch is
    a cross-home session-id collision — folding would hand one identity's
    consumption to another identity's row (adversarial-review finding; the
    ledger refuses the join, and every fold surface must agree with it)."""

    if child_ns is None and parent_ns is None:
        return expected_parent_ns is None
    if child_ns is None or parent_ns is None or child_ns != parent_ns:
        return False
    return expected_parent_ns is None or expected_parent_ns == parent_ns


def resolve_fold_top(
    step: dict[tuple[str, str], tuple[str, str] | None], key: tuple[str, str]
) -> tuple[str, str]:
    """The topmost fold target for ``key`` over a single-step fold map.

    Rows normally nest one level; the walk guards hostile chains rather than
    trusting that. A parent CYCLE (corrupt/hostile client metadata — the
    ledger guards direct self-parents but not mutual loops) is broken
    deterministically: every member resolves to the cycle's minimum key, so
    exactly one member is a root and every share still lands on a visible row
    (round-2 adversarial finding: an unbroken cycle dropped all members and
    their shares from the roots page).
    """

    seen: list[tuple[str, str]] = [key]
    seen_set = {key}
    current = key
    while True:
        parent = step.get(current)
        if parent is None:
            return current
        if parent in seen_set:
            return min(seen[seen.index(parent):])
        seen.append(parent)
        seen_set.add(parent)
        current = parent


# Marker value: a session whose own rows disagree about their source home. Folds
# into or out of such a session are always refused — the ledger quarantines
# mixed-source rows the same way (unique-or-quarantine), and any "pick one"
# rule would make the fold decision depend on event ORDER (round-3
# adversarial finding: last-non-null-wins flipped folds under reordering).
_NS_CONFLICT = object()


def child_root_plan_fold(
    events: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Single-step fold map ``(client, child_session_id) -> (client, parent)``.

    The key space matches :func:`agentacct.plan_cost.session_plan_pcts` (the
    usage view's normalized session ids), so a per-session plan share can be
    re-attributed from a child to the root whose row actually shows it. Roots
    and unfoldable rows are absent — a lookup falls through to identity;
    resolve chains with :func:`resolve_fold_top`.

    This is the events-only mirror of the ledger's parent join, matched rule
    by rule so the two /v1 surfaces agree (round-3 adversarial findings):

    * parent ids join RAW (no ``':'`` splitting of a metadata parent id — the
      rollup joins raw ids; a ``'R:lane'`` parent that is itself a legacy
      child lane reaches ``R`` through its OWN fold step, gates applied at
      every hop);
    * the parent must EXIST on this surface — visible via its own usage or
      section events. A ghost parent refuses the fold (the child stands as
      its own row); a sections-only orchestrator counts as existing, exactly
      as it holds a rollup entry on the sessions lane;
    * namespaces are unique-or-refuse per session (never "last one wins"),
      then gated by :func:`fold_namespace_compatible` — same-home evidence or
      no fold. A session whose rows mix source homes refuses every fold
      touching it, deterministically, in any event order;
    * conflicting parent CLAIMS for one child (different rows naming
      different parents) refuse the fold rather than pick one.

    The ``':'`` suffix fallback applies only to a session with no parent
    metadata at all (legacy child lanes; not client-gated — acceptable while
    only claude-code carries plan shares). Residual known divergences, all
    conservative and sum-preserving: a parent visible ONLY through mechanical
    observations (no usage, no sections) exists on the sessions lane but not
    here, and a parent POINTER carried only by observation rows is invisible
    to this events-only pass.
    """

    from .client_usage import is_local_usage_import_event
    from .usage_truth import normalized_local_usage_session_id

    ns_values: dict[tuple[str, str], set[str]] = {}
    # Sessions with at least one usage row carrying NO fingerprint. Explicit
    # alongside missing is identity ambiguity, same as two different values —
    # the ledger quarantines "source_namespace_missing_vs_explicit" sessions
    # (the upgrade-boundary store state), and treating the explicit value as
    # the session's home here folded what the sessions lane quarantines
    # (round-4 adversarial finding).
    ns_missing: set[tuple[str, str]] = set()
    universe: set[tuple[str, str]] = set()
    # child_key -> {(parent_key, expected_parent_ns)}
    claims: dict[tuple[str, str], set[tuple[tuple[str, str], str | None]]] = {}
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event_type = str(event.get("event_type") or "")
        if event_type.startswith("section_"):
            client = str(metadata.get("client") or event.get("source") or "")
            session_id = str(metadata.get("client_session_id") or "")
            if client and session_id:
                universe.add((client, normalized_local_usage_session_id(metadata.get("client"), session_id)))
            continue
        if not is_local_usage_import_event(event):
            continue
        client = str(metadata.get("client") or event.get("source") or "")
        raw_session = str(metadata.get("client_session_id") or event.get("run_id") or "")
        if not client or not raw_session:
            continue
        child_id = normalized_local_usage_session_id(metadata.get("client"), raw_session)
        child_key = (client, child_id)
        universe.add(child_key)
        own_ns = metadata.get("source_namespace_fingerprint")
        if own_ns:
            ns_values.setdefault(child_key, set()).add(str(own_ns))
        else:
            ns_missing.add(child_key)

        kind = str(metadata.get("client_session_kind") or "root")
        parent = str(metadata.get("parent_client_session_id") or "")
        expected_parent_ns = metadata.get("parent_source_namespace_fingerprint")
        expected_parent_ns = str(expected_parent_ns) if expected_parent_ns else None
        if kind != "root" and parent:
            folded_raw = parent
        elif ":" in raw_session:
            folded_raw = raw_session.split(":", 1)[0]
            expected_parent_ns = None  # legacy lanes carry no parent-home claim
        else:
            continue
        parent_key = (client, normalized_local_usage_session_id(metadata.get("client"), folded_raw))
        if parent_key != child_key:
            claims.setdefault(child_key, set()).add((parent_key, expected_parent_ns))

    def _session_ns(key: tuple[str, str]) -> Any:
        values = ns_values.get(key)
        if not values:
            return None  # no fingerprints at all: a pre-fingerprint session
        if len(values) > 1 or key in ns_missing:
            return _NS_CONFLICT  # mixed homes, or explicit-vs-missing ambiguity
        return next(iter(values))

    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    for child_key, child_claims in claims.items():
        parent_keys = {parent_key for parent_key, _expected in child_claims}
        if len(parent_keys) != 1:
            continue  # conflicting lineage claims — refuse rather than pick one
        parent_key = next(iter(parent_keys))
        if parent_key not in universe:
            continue  # ghost parent — the child stands as its own row
        child_ns = _session_ns(child_key)
        parent_ns = _session_ns(parent_key)
        if child_ns is _NS_CONFLICT or parent_ns is _NS_CONFLICT:
            continue
        if all(
            fold_namespace_compatible(child_ns, parent_ns, expected)
            for _parent, expected in child_claims
        ):
            mapping[child_key] = parent_key
    return mapping


def fold_plan_pcts_to_roots(
    session_pcts: dict[tuple[str, str], float],
    fold_map: dict[tuple[str, str], tuple[str, str]],
) -> dict[tuple[str, str], float]:
    """Re-attribute descendant plan shares onto their topmost roots (sum-preserving).

    A surface that HIDES child sessions must carry their share on the root row
    it does show — otherwise a workflow-heavy session understates exactly when
    it matters most. The total across keys is unchanged; a descendant key
    vanishes into its root's sum (chains and cycles via
    :func:`resolve_fold_top`).
    """

    folded: dict[tuple[str, str], float] = {}
    for key, pct in session_pcts.items():
        root_key = resolve_fold_top(fold_map, key)
        folded[root_key] = folded.get(root_key, 0.0) + pct
    return folded


def plan_status_and_session_pcts(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], float]]:
    """Per-client plan payload entries + calibrated per-session plan shares.

    The calibrated-or-nothing honesty rule is baked in: ``session_pcts`` holds
    ONLY clients whose estimate is grounded in this account's own recorded
    limit history, keyed ``(client, session_id)`` so one client's number can
    never attach to another client's row. Keys are the usage view's normalized
    session ids, UNFOLDED — apply :func:`fold_plan_pcts_to_roots` on a surface
    that hides child sessions. Shared by the glance and the /v1 sessions lane
    so the two can never disagree.
    """

    from .plan_cost import calibrate_plan_weights, plan_status_entry, session_plan_pcts

    from .usage_snapshot import usage_records

    plan: list[dict[str, Any]] = []
    session_pcts: dict[tuple[str, str], float] = {}
    for client in PLAN_CLIENTS:
        records = usage_records(events, client=client)
        weights = calibrate_plan_weights(events, client=client, records=records)
        plan.append(plan_status_entry(weights))
        if weights.confidence == "calibrated":
            for session_id, pct in session_plan_pcts(records, weights, client=client).items():
                session_pcts[(client, session_id)] = pct
    return plan, session_pcts


def _recent_sessions(
    events: list[dict[str, Any]],
    *,
    now: float,
    fold_map: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Recent sessions from the section + usage event streams, newest first.

    ``status`` is the TUI-parity reduction over each session's PER-SECTION
    latest status — blocked > handed_off > in-progress > completed — because a
    real session commonly leaves one stray open section, and a "latest event
    wins" rule would let a later completed section erase a still-open or
    blocked one. A finished-looking label on unfinished work is exactly the
    dishonesty class this product exists to fix, so the menu bar must agree
    with the TUI badge here.

    Usage imports advance ``last_activity_at`` (a session burning tokens right
    now stays "recent" even when its last section is hours old) and create
    status-less rows for clients that never record sections. Child usage
    activity folds into the root row through the SAME fold map the plan
    shares use (``fold_map``, built by :func:`child_root_plan_fold` when not
    passed) — one fold decision, every use, so a hidden child's activity and
    its plan share can never land on different rows. Deliberately ledger-free;
    timestamps are hostile-tolerant (never raise).
    """

    from .client_usage import is_local_usage_import_event
    from .usage_truth import normalized_local_usage_session_id
    from .usage_view import _session_activity_time

    fold = child_root_plan_fold(events) if fold_map is None else fold_map

    # (client, session) -> {section_id: (created_at, status)} — latest per section.
    sections: dict[tuple[str, str], dict[str, tuple[float, str]]] = {}
    # (client, session) -> (created_at, title) — latest titled section event.
    titles: dict[tuple[str, str], tuple[float, str]] = {}
    # (client, session) -> newest activity timestamp across sections + usage.
    activity: dict[tuple[str, str], float] = {}

    def _touch(key: tuple[str, str], timestamp: float) -> None:
        if timestamp > activity.get(key, 0.0):
            activity[key] = timestamp

    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event_type = str(event.get("event_type") or "")
        if event_type.startswith("section_"):
            client = str(metadata.get("client") or event.get("source") or "")
            session_id = str(metadata.get("client_session_id") or "")
            if not client or not session_id:
                continue
            created = _safe_timestamp(event.get("created_at"))
            key = (client, session_id)
            status = str(metadata.get("section_status") or "") or None
            if status:
                per_section = sections.setdefault(key, {})
                section_id = str(metadata.get("section_id") or "")
                prior = per_section.get(section_id)
                if prior is None or created >= prior[0]:
                    per_section[section_id] = (created, status)
            title = str(metadata.get("section_title") or "")
            if title:
                prior_title = titles.get(key)
                if prior_title is None or created >= prior_title[0]:
                    titles[key] = (created, title)
            _touch(key, created)
        elif is_local_usage_import_event(event):
            client = str(metadata.get("client") or event.get("source") or "")
            raw_session = str(metadata.get("client_session_id") or event.get("run_id") or "")
            if not client or not raw_session:
                continue
            # Child sessions (subagents, workflow agents, replay lanes) FOLD
            # into their root through the shared fold map — the same decision
            # that re-attributes their plan share (fold_plan_pcts_to_roots),
            # including its refusals (cross-home mismatch, ghost parent).
            # Normalization first: the map's key space is the usage view's
            # normalized ids, which is also what joins section session ids
            # and plan_pct session ids.
            session_id = normalized_local_usage_session_id(metadata.get("client"), raw_session)
            key = resolve_fold_top(fold, (client, session_id))
            _touch(key, _session_activity_time(event))

    def _reduce_status(statuses: set[str]) -> str | None:
        if "blocked" in statuses:
            return "blocked"
        if "handed_off" in statuses:
            return "handed_off"
        if statuses & _OPEN_SECTION_STATUSES:
            return "in_progress"
        if "completed" in statuses:
            return "completed"
        return None

    cutoff = now - ACTIVE_SESSION_WINDOW_SECONDS
    rows: list[dict[str, Any]] = []
    for key, last_activity_at in activity.items():
        if last_activity_at < cutoff:
            continue
        client, session_id = key
        per_section = sections.get(key) or {}
        statuses = {status for (_created, status) in per_section.values()}
        title_entry = titles.get(key)
        rows.append(
            {
                "client": client,
                "session_id": session_id,
                "title": title_entry[1] if title_entry else None,
                "status": _reduce_status(statuses),
                "last_activity_at": last_activity_at,
            }
        )
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

    from .usage_snapshot import build_live_snapshot, limit_is_stale

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

    # Plan calibration per plan-bearing client (calibrated-or-nothing; see
    # plan_status_and_session_pcts). The recents list HIDES child sessions
    # (they fold into their root row), so each root's plan_pct must carry its
    # children's share too — an unfolded join understates a workflow-heavy
    # root exactly when it matters most. A child that still appears as its own
    # row (it records sections but its usage folded away) shows None rather
    # than a share its root's row already includes. ONE fold map drives both
    # the activity fold and the share fold, so they can never disagree.
    plan, session_pcts = plan_status_and_session_pcts(events)
    fold_map = child_root_plan_fold(events)
    folded_pcts = fold_plan_pcts_to_roots(session_pcts, fold_map)

    recent_sessions = _recent_sessions(events, now=moment, fold_map=fold_map)
    for row in recent_sessions:
        # The raw estimate, never rounded here: round(pct, 2) can claim an
        # exact 0 for a nonzero share. Shells format like the TUI does —
        # "≈{pct:.1f}%" with a "<0.1%" band — the payload carries the float.
        row["plan_pct"] = folded_pcts.get((row["client"], row["session_id"]))

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
    """Fingerprint + TTL cache so polling stays cheap without going stale.

    Two invalidation triggers, both required: the fingerprint catches every
    event-list change, and ``max_age_seconds`` catches CLOCK drift — the
    "today" window crossing midnight, the recency cutoff, and limits[].stale
    all move with wall time on an unchanged event list, so a fingerprint-only
    cache served yesterday's "today" every morning (adversarial-review HIGH
    finding).

    Thread-tolerant under FastAPI's threadpool: the cached value is one atomic
    tuple assignment, so a concurrent reader can never observe a torn triple;
    two racing rebuilds waste one build and the last writer wins (same pattern
    as the TUI's plan cache). A rebuild racing an import can briefly win with a
    slightly older event list — one poll may regress and self-heals on the
    next; it never fabricates a number.
    """

    def __init__(self, max_age_seconds: float = GLANCE_CACHE_MAX_AGE_SECONDS) -> None:
        self._lock = Lock()
        # (fingerprint, built_at, payload) — always assigned as one tuple.
        self._cached: tuple[int, float, dict[str, Any]] | None = None
        self.max_age_seconds = float(max_age_seconds)

    def _fresh(self, cached: tuple[int, float, dict[str, Any]] | None, fingerprint: int, moment: float) -> bool:
        return (
            cached is not None
            and cached[0] == fingerprint
            and (moment - cached[1]) < self.max_age_seconds
        )

    def snapshot(
        self,
        events: list[dict[str, Any]],
        *,
        store_dir: Path | str,
        version: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        moment = time.time() if now is None else float(now)
        fingerprint = events_fingerprint(events)
        cached = self._cached
        if self._fresh(cached, fingerprint, moment):
            return cached[2]  # type: ignore[index]
        with self._lock:
            cached = self._cached
            if self._fresh(cached, fingerprint, moment):
                return cached[2]  # type: ignore[index]
            payload = build_glance_snapshot(events, store_dir=store_dir, version=version, now=now)
            self._cached = (fingerprint, moment, payload)
            return payload
