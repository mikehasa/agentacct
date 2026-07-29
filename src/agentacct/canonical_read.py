"""Flag-gated canonical read path (migration phase 4.1).

The READ-side mirror of :mod:`canonical_live`'s flag contract: default OFF,
independently gated from the write flag, and with the flag off every PRODUCT
read surface stays byte-identical to the proven v1 path (no store opens, no
canonical imports on that path). One sanctioned exception (phase 4.3):
:meth:`CanonicalReadRuntime.health_probe` is deliberately flag-independent —
operator diagnostics on /health and the ``canonical health`` CLI open the
store read-only regardless of the flag, because cutover-readiness evidence
is needed precisely while production reads still serve v1.

Unlike the shadow writer (fail-open, because a canonical problem must never
fail a proven v1 write), the reader fails VISIBLE: when the flag is on but
the canonical store cannot honestly serve — absent, refused by the
fail-closed open, or its read model was never built — the caller receives a
typed :class:`CanonicalReadUnavailable` and is expected to fall back to the
v1 surface WITH a label saying so. Silent wrong data is the one failure mode
this module is not allowed to have.

Connection lifecycle differs from the writer on purpose: the writer opens
per shadow call because write volume is low, but ``CanonicalStore.open_live``
re-verifies the packaged schema inventory on every open, which is too heavy
for request-path reads. Readers therefore cache ONE read-only store per
thread (sqlite3 connections are same-thread; Starlette sync endpoints run in
a bounded threadpool) and revalidate the cached handle against the store
file's device/inode identity on every acquisition, so a promotion that
replaces ``chronicle.sqlite3`` is picked up on the next read instead of
serving rows from a deleted inode.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .env_compat import read_env_alias


CANONICAL_READ_ENV = "AGENTACCT_CANONICAL_READ"
_TRUE_VALUES = {"1", "true", "yes", "on"}

# rm_usage_day pins usage whose timestamp was absent/unparseable to the
# epoch-zero sentinel day. Readers must present those rows as an explicit
# "undated" bucket — never as a real 1970 date, never silently dropped.
UNDATED_SENTINEL_DAY = "1970-01-01"
# First representable dated day: everything on the sentinel day is undated
# by construction (no genuine pre-1970-01-02 usage can exist).
FIRST_DATED_DAY = "1970-01-02"
LAST_DATED_DAY = "9999-12-31"

# usage_days() hard limit (mirrors CanonicalRepository's bound). Hitting it
# means the result MAY be incomplete, which the read result labels.
USAGE_DAYS_QUERY_LIMIT = 2000
# list_tasks() / list_sessions() hard limits (mirror CanonicalRepository's
# bounds). Hitting one means the newest-first page MAY be incomplete, which
# the read result labels.
TASK_LIST_QUERY_LIMIT = 500
SESSION_LIST_QUERY_LIMIT = 2000


def canonical_read_enabled() -> bool:
    value = read_env_alias(CANONICAL_READ_ENV)
    return str(value or "").strip().lower() in _TRUE_VALUES


class CanonicalReadUnavailable(Exception):
    """The canonical store cannot honestly serve this read.

    ``reason`` is a stable machine token for labels/counters; ``detail`` is
    the bounded human diagnostic (e.g. the fail-closed open's refusal text).
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail[:500]
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class CanonicalUsageDayRead:
    """One honest usage-days read: rows plus everything a surface must label."""

    rows: Sequence[Mapping[str, Any]]
    undated_rows: Sequence[Mapping[str, Any]]
    truncated: bool
    undated_truncated: bool
    store: Mapping[str, Any]
    projection: Mapping[str, Any]
    model_matches_store: bool | None
    day_basis: str = "utc"


@dataclass(frozen=True)
class CanonicalTaskListRead:
    """One honest rm_task_current page plus the labeling lookups it needs.

    ``sessions``/``sources`` resolve each task's primary session identity; a
    missing entry means the row could not be resolved and the surface must
    say so, never invent one.
    """

    tasks: Sequence[Any]
    sessions: Mapping[int, Any]
    sources: Mapping[int, Any]
    truncated: bool
    store: Mapping[str, Any]
    projection: Mapping[str, Any]


@dataclass(frozen=True)
class CanonicalTaskDetailRead:
    """One rm_task_current row (or an honest miss: ``task is None``)."""

    task: Any | None
    sessions: Mapping[int, Any]
    sources: Mapping[int, Any]
    store: Mapping[str, Any]
    projection: Mapping[str, Any]


@dataclass(frozen=True)
class CanonicalSessionListRead:
    """One honest sessions page. ``sessions`` is a base fact table (always
    current) so no projection block rides along — store labels only.

    ``lineage_edges`` maps a page session id to its single VALIDATED lineage
    edge ``(parent_session_id, relation)`` — relation is 'parent',
    'continuation', or 'retry', mirroring the child's session kind;
    ``parent_sessions`` carries referenced parents that fell outside the
    page. Raw observed-lineage claims are pre-validation state and
    deliberately absent.
    """

    sessions: Sequence[Any]
    sources: Mapping[int, Any]
    lineage_edges: Mapping[int, tuple[int, str]]
    parent_sessions: Mapping[int, Any]
    truncated: bool
    store: Mapping[str, Any]


@dataclass
class _ThreadStoreCache:
    store: Any
    identity: tuple[int, int]


class CanonicalReadRuntime:
    """Read product surfaces from ``<store>/chronicle.sqlite3``, fail visible."""

    def __init__(self, store_root: Path | str, *, enabled: bool | None = None) -> None:
        self.store_root = Path(store_root).expanduser()
        self.enabled = canonical_read_enabled() if enabled is None else bool(enabled)
        self._thread_local = threading.local()
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "attempts": 0,
            "served": 0,
            "unavailable": 0,
            "errors": 0,
            "last_error": None,
            "last_unavailable_reason": None,
            # Per-surface lanes (phase 4.3): the aggregates above answer "is
            # the read path healthy at all"; these answer "WHICH surface is
            # falling back" without correlating logs.
            "surfaces": {},
        }

    def status(self) -> dict[str, Any]:
        """In-memory read-path counters for health surfaces and tests.

        A canonical read path that quietly falls back to v1 forever would be
        invisible wrong-ish behavior; these counters make every fallback
        discoverable from the running process.
        """

        with self._status_lock:
            snapshot = dict(self._status)
            snapshot["surfaces"] = {
                name: dict(lane) for name, lane in self._status["surfaces"].items()
            }
        snapshot["enabled"] = self.enabled
        return snapshot

    def _surface_lane(self, surface: str) -> dict[str, Any]:
        """Callers hold ``_status_lock``."""

        return self._status["surfaces"].setdefault(
            surface,
            {
                "attempts": 0,
                "served": 0,
                "unavailable": 0,
                "errors": 0,
                "last_error": None,
                "last_unavailable_reason": None,
            },
        )

    # -- store acquisition ---------------------------------------------------

    def _drop_cached_store(self) -> None:
        cache: _ThreadStoreCache | None = getattr(self._thread_local, "cache", None)
        if cache is not None:
            try:
                cache.store.close()
            except Exception:  # noqa: BLE001 - closing a dead handle must not mask the read outcome.
                pass
            self._thread_local.cache = None

    def _acquire_store(self) -> Any:
        """The per-thread cached read-only store, revalidated by file identity."""

        from .canonical.sqlite import LIVE_STORE_FILENAME, CanonicalStore

        path = self.store_root / LIVE_STORE_FILENAME
        try:
            observed = path.lstat()
        except FileNotFoundError as exc:
            self._drop_cached_store()
            raise CanonicalReadUnavailable("store_absent", str(path)) from exc
        identity = (observed.st_dev, observed.st_ino)
        cache: _ThreadStoreCache | None = getattr(self._thread_local, "cache", None)
        if cache is not None and cache.identity == identity:
            # Same inode does not prove same store: an in-place migration by
            # a newer build or a role demotion mutates the file without
            # changing its identity, and every fail-closed check lives in
            # open_live. Re-prove the two cheap load-bearing facts per
            # acquisition; any doubt drops the handle and takes the full
            # fail-closed reopen below.
            from .canonical.sqlite import SCHEMA_VERSION

            try:
                version = int(
                    cache.store.connection.execute("PRAGMA user_version").fetchone()[0]
                )
                role_row = cache.store.connection.execute(
                    "SELECT store_role FROM store_metadata WHERE singleton = 1"
                ).fetchone()
                if version == SCHEMA_VERSION and role_row is not None and str(role_row[0]) == "live":
                    return cache.store
            except sqlite3.Error:
                pass
        self._drop_cached_store()
        try:
            store = CanonicalStore.open_live(self.store_root, read_only=True)
        except FileNotFoundError as exc:
            raise CanonicalReadUnavailable("store_absent", str(path)) from exc
        except PermissionError as exc:
            raise CanonicalReadUnavailable("store_permissions", str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            # The fail-closed open's refusal (role/schema/inventory/identity);
            # the detail carries the precise reason (e.g. "open writable to
            # upgrade" for an older-schema store).
            raise CanonicalReadUnavailable("store_refused", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - sqlite3 open errors and the like: visible, not a crash.
            raise CanonicalReadUnavailable(
                "store_unreadable", f"{type(exc).__name__}: {exc}"
            ) from exc
        # open_live re-stats and proves the identity itself; cache the proven
        # value, not the pre-open observation.
        self._thread_local.cache = _ThreadStoreCache(store, store.file_identity)
        return store

    # -- diagnostics ---------------------------------------------------------

    def health_probe(self) -> dict[str, Any]:
        """Store/projection diagnostics for health surfaces — deliberately
        INDEPENDENT of the read flag (the flag gates PRODUCT reads; an
        operator needs cutover-readiness evidence — store present, role,
        schema, projection staleness — while production reads still serve
        v1). Never raises, and never counts into the product-surface
        counters: a health poll must not look like read-path traffic.
        """

        probe: dict[str, Any] = {"available": False, "reason": None, "detail": None}
        # The result processing lives INSIDE the guard too: a malformed value
        # out of a byte-corrupted store must become the honest error shape,
        # not a raise — the same discipline the rebuild policy's tick applies
        # to the identical data.
        try:
            from .canonical.sqlite import MINIMAL_READ_MODEL_NAMES

            store = self._acquire_store()
            health = store.repository().health_summary()
            metadata = health.get("store") or {}
            canonical_sequence = int(metadata.get("canonical_sequence") or 0)
            projections: dict[str, Any] = {}
            for row in health.get("projections", ()):
                entry = dict(row)
                built_through = int(entry.get("built_through_sequence") or 0)
                pending_writes = max(0, canonical_sequence - built_through)
                entry["stale"] = pending_writes > 0
                entry["pending_writes"] = pending_writes
                projections[str(entry.get("projection_name"))] = entry
            for name in MINIMAL_READ_MODEL_NAMES:
                # Absence of a generation row IS the never-built signal — say
                # it instead of omitting the projection.
                projections.setdefault(name, {"projection_name": name, "state": "never_built"})
            return {
                "available": True,
                "reason": None,
                "detail": None,
                "store": self._store_block(metadata, canonical_sequence),
                "projections": projections,
                "sources": list(health.get("sources", ())),
            }
        except CanonicalReadUnavailable as unavailable:
            probe["reason"] = unavailable.reason
            probe["detail"] = unavailable.detail or None
            return probe
        except Exception as exc:  # noqa: BLE001 - a health poll must never crash the surface.
            self._drop_cached_store()
            probe["reason"] = "error"
            probe["detail"] = f"{type(exc).__name__}: {exc}"[:500]
            return probe

    # -- reads ---------------------------------------------------------------

    def _account(
        self,
        *,
        served: bool,
        reason: str | None,
        error: str | None,
        surface: str | None = None,
    ) -> None:
        with self._status_lock:
            lanes = [self._status]
            if surface is not None:
                lanes.append(self._surface_lane(surface))
            for lane in lanes:
                lane["attempts"] += 1
                if error is not None:
                    lane["errors"] += 1
                    lane["last_error"] = error[:1000]
                if served:
                    lane["served"] += 1
                elif reason is not None:
                    lane["unavailable"] += 1
                    lane["last_unavailable_reason"] = reason

    def surface_failure(
        self, detail: str, *, surface: str | None = None
    ) -> CanonicalReadUnavailable:
        """Account a failure that happened AFTER a successful read, while the
        surface was consuming it (e.g. the cube build), and hand back the
        typed failure for the labeled v1 fallback. The read itself stays
        counted as served — errors/last_error make the crash visible on
        /health instead of letting a served-then-crashed request look
        healthy."""

        bounded = detail[:1000]
        with self._status_lock:
            lanes = [self._status]
            if surface is not None:
                lanes.append(self._surface_lane(surface))
            for lane in lanes:
                lane["errors"] += 1
                lane["last_error"] = bounded
        return CanonicalReadUnavailable("error", bounded)

    def _guarded_read(self, worker: Any, *, surface: str) -> Any:
        """The shared read contract: flag gate, typed unavailability
        accounting (aggregate + per-surface lane), and conversion of anything
        unexpected into the typed ``"error"`` failure (with the possibly
        poisoned per-thread handle dropped) so a surface can always fall back
        with a label instead of crashing."""

        if not self.enabled:
            raise CanonicalReadUnavailable("read_flag_disabled")
        try:
            result = worker()
        except CanonicalReadUnavailable as unavailable:
            self._account(
                served=False, reason=unavailable.reason, error=None, surface=surface
            )
            raise
        except Exception as exc:  # noqa: BLE001 - reads fail visible, never crash a surface.
            self._drop_cached_store()
            detail = f"{type(exc).__name__}: {exc}"
            self._account(served=False, reason="error", error=detail, surface=surface)
            raise CanonicalReadUnavailable("error", detail) from exc
        self._account(served=True, reason=None, error=None, surface=surface)
        return result

    @staticmethod
    def _store_block(metadata: Mapping[str, Any], canonical_sequence: int) -> dict[str, Any]:
        return {
            "store_uuid": metadata.get("store_uuid"),
            "store_role": metadata.get("store_role"),
            "schema_version": metadata.get("schema_version"),
            "canonical_sequence": canonical_sequence,
        }

    @staticmethod
    def _gated_projection(
        health: Mapping[str, Any], projection_name: str, canonical_sequence: int
    ) -> dict[str, Any]:
        """The rebuild-only read-model gate, shared by every projection-backed
        surface: never built or not current = refuse (an empty projection must
        not impersonate empty data — the live shadow store is exactly that
        shape); stale-but-current serves WITH the staleness labels."""

        projection_row = next(
            (
                dict(row)
                for row in health.get("projections", ())
                if row.get("projection_name") == projection_name
            ),
            None,
        )
        if projection_row is None:
            raise CanonicalReadUnavailable(
                "projection_never_built",
                f"{projection_name} has no projection_generations row",
            )
        state = str(projection_row.get("state") or "")
        if state != "current":
            raise CanonicalReadUnavailable(
                "projection_not_current",
                f"{projection_name} projection state is {state!r}",
            )
        built_through = int(projection_row.get("built_through_sequence") or 0)
        pending_writes = max(0, canonical_sequence - built_through)
        return {
            **projection_row,
            "canonical_sequence": canonical_sequence,
            "stale": pending_writes > 0,
            "pending_writes": pending_writes,
        }

    def usage_day_read(
        self,
        *,
        start_day: str,
        end_day: str,
        client: str | None = None,
        model: str | None = None,
    ) -> CanonicalUsageDayRead:
        """Read rm_usage_day for a dated range plus the undated sentinel bucket.

        Raises :class:`CanonicalReadUnavailable` when the store cannot
        honestly serve; anything unexpected is converted to the same typed
        failure (reason ``"error"``) so a surface can always fall back with a
        label instead of crashing.
        """

        return self._guarded_read(
            lambda: self._usage_day_read(
                start_day=start_day, end_day=end_day, client=client, model=model
            ),
            surface="usage_days",
        )

    def _usage_day_read(
        self,
        *,
        start_day: str,
        end_day: str,
        client: str | None,
        model: str | None,
    ) -> CanonicalUsageDayRead:
        from .canonical.types import UsageDayQuery

        store = self._acquire_store()
        repository = store.repository()
        # One WAL read snapshot for the gate AND the data statements: 4.3's
        # watcher/CLI rebuild is the first writer that rewrites a live
        # store's rm_* tables concurrently with request reads, and a rebuild
        # committing between the gate and a SELECT must not produce a
        # response whose staleness labels describe a different projection
        # generation than the rows it serves.
        with store.transaction():
            health = repository.health_summary()
            metadata = health.get("store") or {}
            canonical_sequence = int(metadata.get("canonical_sequence") or 0)
            projection = self._gated_projection(health, "rm_usage_day", canonical_sequence)

            # The sentinel day never enters the dated range: rows on it are
            # undated by construction and are fetched as their own bucket.
            dated_start = max(start_day, FIRST_DATED_DAY)
            dated_end = min(end_day, LAST_DATED_DAY)
            rows: Sequence[Mapping[str, Any]] = ()
            if dated_start <= dated_end:
                rows = repository.usage_days(
                    UsageDayQuery(
                        start_day=dated_start,
                        end_day=dated_end,
                        client=client,
                        model=model,
                        limit=USAGE_DAYS_QUERY_LIMIT,
                    )
                )
            # The DDL only pins day's LENGTH; a value the surface cannot parse
            # as a real ISO date (drifted writer, hand-repaired store) must
            # become a typed refusal here, not a crash downstream in the cube.
            # Rows on the sentinel day are pinned by the equality query and
            # need no parse.
            for row in rows:
                day_text = str(row.get("day") or "")
                try:
                    date.fromisoformat(day_text)
                except ValueError as exc:
                    raise CanonicalReadUnavailable(
                        "store_invalid_data",
                        f"rm_usage_day.day is not a valid ISO date: {day_text!r}",
                    ) from exc
            undated_rows = repository.usage_days(
                UsageDayQuery(
                    start_day=UNDATED_SENTINEL_DAY,
                    end_day=UNDATED_SENTINEL_DAY,
                    client=client,
                    model=model,
                    limit=USAGE_DAYS_QUERY_LIMIT,
                )
            )
            model_matches: bool | None = None
            if model is not None:
                # v1 distinguishes "unknown model" from "known model, empty
                # range" against ALL saved rows; the canonical analog probes
                # the whole table through the model index, one row is enough.
                probe = repository.usage_days(
                    UsageDayQuery(
                        start_day=UNDATED_SENTINEL_DAY,
                        end_day=LAST_DATED_DAY,
                        model=model,
                        limit=1,
                    )
                )
                model_matches = bool(probe)
        return CanonicalUsageDayRead(
            rows=rows,
            undated_rows=undated_rows,
            truncated=len(rows) >= USAGE_DAYS_QUERY_LIMIT,
            undated_truncated=len(undated_rows) >= USAGE_DAYS_QUERY_LIMIT,
            store=self._store_block(metadata, canonical_sequence),
            projection=projection,
            model_matches_store=model_matches,
        )

    # -- work/task/sessions surfaces (migration phase 4.2) -------------------

    def task_list_read(self, *, limit: int = TASK_LIST_QUERY_LIMIT) -> CanonicalTaskListRead:
        """Read the newest-first rm_task_current page with identity labels.

        The rebuild-only projection gate applies exactly as for usage days:
        a never-built or non-current rm_task_current refuses (an EMPTY task
        list from a store that never built the projection must not
        impersonate "no tasks"), stale-but-current serves with labels.
        """

        return self._guarded_read(
            lambda: self._task_list_read(limit=limit), surface="task_list"
        )

    def _task_list_read(self, *, limit: int) -> CanonicalTaskListRead:
        store = self._acquire_store()
        repository = store.repository()
        # One read snapshot: labels and rows from the same committed
        # generation (see _usage_day_read).
        with store.transaction():
            health = repository.health_summary()
            metadata = health.get("store") or {}
            canonical_sequence = int(metadata.get("canonical_sequence") or 0)
            projection = self._gated_projection(health, "rm_task_current", canonical_sequence)
            tasks = repository.list_tasks(limit=limit)
            session_ids = sorted({task.primary_session_id for task in tasks})
            sessions = repository.get_sessions_by_ids(session_ids)
            source_ids = sorted(
                {session.source_instance_id for session in sessions.values()}
            )
            sources = repository.get_source_instances_by_ids(source_ids)
        return CanonicalTaskListRead(
            tasks=tasks,
            sessions=sessions,
            sources=sources,
            truncated=len(tasks) >= limit,
            store=self._store_block(metadata, canonical_sequence),
            projection=projection,
        )

    def task_detail_read(self, public_task_id: str) -> CanonicalTaskDetailRead:
        """Read one task by its canonical public id.

        ``task is None`` is an HONEST miss (the projection gate has already
        proven the read model current-and-built) — canonical public ids are
        importer-minted and live in a different namespace than the v1 HMAC
        task ids, so a v1-era id landing here misses by construction.
        """

        return self._guarded_read(
            lambda: self._task_detail_read(public_task_id), surface="task_detail"
        )

    def _task_detail_read(self, public_task_id: str) -> CanonicalTaskDetailRead:
        store = self._acquire_store()
        repository = store.repository()
        # One read snapshot: labels and rows from the same committed
        # generation (see _usage_day_read).
        with store.transaction():
            health = repository.health_summary()
            metadata = health.get("store") or {}
            canonical_sequence = int(metadata.get("canonical_sequence") or 0)
            projection = self._gated_projection(health, "rm_task_current", canonical_sequence)
            task = repository.get_task(str(public_task_id))
            sessions: Mapping[int, Any] = {}
            sources: Mapping[int, Any] = {}
            if task is not None:
                sessions = repository.get_sessions_by_ids([task.primary_session_id])
                sources = repository.get_source_instances_by_ids(
                    sorted({session.source_instance_id for session in sessions.values()})
                )
        return CanonicalTaskDetailRead(
            task=task,
            sessions=sessions,
            sources=sources,
            store=self._store_block(metadata, canonical_sequence),
            projection=projection,
        )

    def session_list_read(
        self, *, limit: int = SESSION_LIST_QUERY_LIMIT
    ) -> CanonicalSessionListRead:
        """Read the newest-first sessions page with source and lineage labels.

        ``sessions`` is a base fact table maintained transactionally by every
        writer — NO projection gate applies, and a store whose read models
        were never built (the live shadow shape) must still serve its
        sessions. Store-level gates (role/schema/identity) apply unchanged.
        """

        return self._guarded_read(
            lambda: self._session_list_read(limit=limit), surface="session_list"
        )

    def _session_list_read(self, *, limit: int) -> CanonicalSessionListRead:
        store = self._acquire_store()
        repository = store.repository()
        # One read snapshot: labels and rows from the same committed
        # generation (see _usage_day_read).
        with store.transaction():
            health = repository.health_summary()
            metadata = health.get("store") or {}
            canonical_sequence = int(metadata.get("canonical_sequence") or 0)
            rows = repository.list_sessions(limit=limit)
            page_ids = [session.session_id for session in rows]
            lineage_edges = repository.valid_lineage_edges(page_ids)
            in_page = set(page_ids)
            parent_sessions = repository.get_sessions_by_ids(
                sorted(
                    {
                        parent_id
                        for parent_id, _relation in lineage_edges.values()
                        if parent_id not in in_page
                    }
                )
            )
            source_ids = sorted(
                {session.source_instance_id for session in rows}
                | {session.source_instance_id for session in parent_sessions.values()}
            )
            sources = repository.get_source_instances_by_ids(source_ids)
        return CanonicalSessionListRead(
            sessions=rows,
            sources=sources,
            lineage_edges=lineage_edges,
            parent_sessions=parent_sessions,
            truncated=len(rows) >= limit,
            store=self._store_block(metadata, canonical_sequence),
        )


__all__ = [
    "CANONICAL_READ_ENV",
    "CanonicalReadRuntime",
    "CanonicalReadUnavailable",
    "CanonicalSessionListRead",
    "CanonicalTaskDetailRead",
    "CanonicalTaskListRead",
    "CanonicalUsageDayRead",
    "FIRST_DATED_DAY",
    "LAST_DATED_DAY",
    "SESSION_LIST_QUERY_LIMIT",
    "TASK_LIST_QUERY_LIMIT",
    "UNDATED_SENTINEL_DAY",
    "USAGE_DAYS_QUERY_LIMIT",
    "canonical_read_enabled",
]
