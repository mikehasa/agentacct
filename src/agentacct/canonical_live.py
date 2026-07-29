"""Flag-gated canonical live shadow writer (migration phase 3.1).

Mirrors the Evidence-v2 runtime contract exactly: the shadow write happens
after — and never under the lock of — a proven v1 ``events.jsonl`` write, and
it is fail-open (a canonical problem can never fail the v1 write). With the
flag off this module opens nothing, creates nothing, and imports none of the
canonical machinery, so live behavior is byte-identical to before.

Phase 3.1 scope: only the generic ``record_event`` append lane is shadowed,
and only events the canonical model represents as work claims (the same
subset the legacy importer persists). Session lineage edges, task anchors,
usage, and read-model maintenance land in later increments; the shadow store
is disposable evidence, not the cutover store.

Known deferred divergence (phase 3.2): the importer ABSORBS all of a
session's events before one reconcile (min started_at, max last_activity,
last-wins kind/parent), while this writer reconciles per event with each
event's own fields. For sessions whose started_at or parent varies across
events the final rows differ, and a tail re-import against a live store
would quarantine them as visible source_conflicts rather than merging. The
session-observation lane must replicate the absorb semantics (which needs
the incumbent parent identity — today only representable in the observation
hash, not the sessions row) before any import may target a live store.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .env_compat import read_env_alias


CANONICAL_LIVE_ENV = "AGENTACCT_CANONICAL_LIVE_WRITE"

# Historical truthy set: every one of these has always meant "shadow" (canonical
# writes on, v1 stays the record). Kept exact so no existing deployment's value
# silently changes meaning under the tri-state parse.
_SHADOW_VALUES = {"1", "true", "yes", "on", "shadow"}
_AUTHORITATIVE_VALUES = {"authoritative"}
# Retained for any external importer of the old name; still the "canonical
# writes on" set (shadow OR authoritative both count as on).
_TRUE_VALUES = _SHADOW_VALUES | _AUTHORITATIVE_VALUES


class CanonicalWriteMode(str, Enum):
    """Tri-state canonical write posture (see
    docs/canonical-authoritative-writes-design.md §2). ``off``/``shadow`` behave
    exactly as the historical bool did; ``authoritative`` is the P1-second-cut
    target where canonical becomes the primary write target — unwired until the
    precondition register is closed, so requesting it today fails loud."""

    OFF = "off"
    SHADOW = "shadow"
    AUTHORITATIVE = "authoritative"


# Authoritative writes are not wired until migration increments I1–I6 close the
# precondition risk register (docs/canonical-authoritative-writes-design.md §3).
# Until then, requesting authoritative mode is a hard, loud configuration error
# rather than a silent fall-back to shadow — silently treating v1 as no longer
# the record would be exactly the dishonest failure the migration must avoid.
# A later increment flips this to True once the cutover gate (I6) certifies
# operational readiness; the actual production flip stays owner-gated (I7).
_AUTHORITATIVE_WRITE_SUPPORTED = False


class CanonicalAuthoritativeNotReady(RuntimeError):
    """Raised when the write flag requests authoritative mode before the
    migration preconditions are closed. Fail-loud by design: v1 must not be
    silently demoted from the record while the authoritative path is unwired."""


def canonical_live_write_mode() -> CanonicalWriteMode:
    """Resolve the tri-state write mode from the env alias pair. Unrecognized
    values fail closed to ``off`` (canonical writes nothing, v1-only), matching
    the historical bool's treatment of unknown tokens."""

    raw = str(read_env_alias(CANONICAL_LIVE_ENV) or "").strip().lower()
    if raw in _AUTHORITATIVE_VALUES:
        return CanonicalWriteMode.AUTHORITATIVE
    if raw in _SHADOW_VALUES:
        return CanonicalWriteMode.SHADOW
    return CanonicalWriteMode.OFF


def require_supported_write_mode(mode: CanonicalWriteMode) -> CanonicalWriteMode:
    """Gate the unwired authoritative mode. Returns the mode unchanged for
    ``off``/``shadow``; raises for ``authoritative`` until the migration lands."""

    if mode is CanonicalWriteMode.AUTHORITATIVE and not _AUTHORITATIVE_WRITE_SUPPORTED:
        raise CanonicalAuthoritativeNotReady(
            "AGENTACCT_CANONICAL_LIVE_WRITE=authoritative is not supported "
            "yet: canonical-authoritative writes land across migration increments "
            "I1–I6 (docs/canonical-authoritative-writes-design.md) and the flip is "
            "owner-gated after a soak. Use 'shadow' (the current production mode) "
            "or 'off'."
        )
    return mode


def _resolve_write_mode(
    *, enabled: bool | None, mode: CanonicalWriteMode | None
) -> CanonicalWriteMode:
    """Normalize the runtime's construction inputs to a single mode. An explicit
    ``mode`` wins; otherwise a bool ``enabled`` maps to shadow/off for backward
    compatibility (a bool can never select authoritative); ``None`` reads the
    env. The authoritative gate applies to every non-bool source."""

    if mode is not None:
        return require_supported_write_mode(mode)
    if enabled is None:
        return require_supported_write_mode(canonical_live_write_mode())
    return CanonicalWriteMode.SHADOW if enabled else CanonicalWriteMode.OFF

LIVE_EVENTS_ADAPTER = "chronicle-v1-live-writer"
# The representation names the data shape (a v1 ledger event), which is the
# same shape the legacy importer reads, so a resolved source carries the same
# semantic identity in both worlds. NOTE: identity continuation across an
# import and live writes in ONE store is deliberately still impossible —
# get_or_create_source hard-fails on an adapter mismatch for the same
# (client, digest, representation) identity, and LegacyImporter refuses
# live-role destinations outright. Resolving that seam (normalize adapters at
# promotion, or relax the adapter assertion) is a phase-3.5 cutover decision.
LIVE_EVENTS_REPRESENTATION = "legacy-v1"
LIVE_UNRESOLVED_NAMESPACE_SCHEME = "live-unresolved-store-namespace-v1"
LIVE_FACT_LINK_METHOD = "live_explicit_client_session_id"
LIVE_FACT_LINK_RULE_VERSION = "live-writer-v1"


def canonical_live_write_enabled() -> bool:
    """True when canonical writes are active (shadow OR authoritative). This is
    the historical accessor, now derived from the tri-state mode so every prior
    flag value keeps its exact meaning. Note it does NOT apply the authoritative
    readiness gate — that gate belongs at runtime construction; a bare enabled
    check must stay side-effect-free."""

    return canonical_live_write_mode() is not CanonicalWriteMode.OFF


@dataclass(frozen=True)
class CanonicalShadowResult:
    """Outcome of one shadow attempt; ``error`` is diagnostic, never raised."""

    enabled: bool
    written: bool = False
    dispositions: dict[str, str] = field(default_factory=dict)
    skipped_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "written": self.written,
            "dispositions": dict(self.dispositions),
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


class CanonicalLiveRuntime:
    """Shadow v1 ledger events into ``<store>/chronicle.sqlite3``.

    Connection lifecycle copies ``EvidenceStore``: open per shadow call and
    close before returning, so the runtime is safe under Starlette's
    threadpool and across the several live processes that share one store
    (SQLite WAL + BEGIN IMMEDIATE + busy timeout serialize the writers).
    """

    def __init__(
        self,
        store_root: Path | str,
        *,
        enabled: bool | None = None,
        mode: CanonicalWriteMode | None = None,
    ) -> None:
        self.store_root = Path(store_root).expanduser()
        # Resolve the tri-state posture once, up front, so an unsupported
        # authoritative request fails loudly here (before anything is opened)
        # instead of somewhere inside the fail-open shadow path where the error
        # would be swallowed into a counter. ``self.enabled`` stays the boolean
        # every existing gate reads (shadow and authoritative are both "on").
        self.mode = _resolve_write_mode(enabled=enabled, mode=mode)
        self.enabled = self.mode is not CanonicalWriteMode.OFF
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "attempts": 0,
            "written": 0,
            "noop": 0,
            "skipped": 0,
            "deferred": 0,
            "conflicts": 0,
            "errors": 0,
            "last_error": None,
        }

    def status(self) -> dict[str, Any]:
        """In-memory shadow counters for health surfaces and tests.

        This is the minimum honesty floor until phase 3.4 wires the shadow
        into ingestion health: a persistently failing lane must at least be
        discoverable from the running process, never silently absorbed.
        """

        with self._status_lock:
            snapshot = dict(self._status)
        snapshot["enabled"] = self.enabled
        snapshot["mode"] = self.mode.value
        return snapshot

    def _account(self, result: CanonicalShadowResult) -> CanonicalShadowResult:
        deferred = any(
            str(value).endswith("_deferred") for value in result.dispositions.values()
        )
        conflicted = any(
            str(value) == "conflict" for value in result.dispositions.values()
        )
        with self._status_lock:
            self._status["attempts"] += 1
            if result.error is not None:
                self._status["errors"] += 1
                self._status["last_error"] = result.error
                return result
            # These are independent facts about one shadow attempt, not a
            # single bucket: a session write can coexist with a skipped claim
            # lane, and a deferred observation or a genuine conflict must
            # never masquerade as a clean noop.
            if result.written:
                self._status["written"] += 1
            if result.skipped_reason is not None:
                self._status["skipped"] += 1
            if deferred:
                self._status["deferred"] += 1
            if conflicted:
                self._status["conflicts"] += 1
            if (
                not result.written
                and result.skipped_reason is None
                and not deferred
                and not conflicted
            ):
                self._status["noop"] += 1
        return result

    def shadow_v1_event(
        self,
        event: Mapping[str, Any],
        *,
        transport: str | None = None,
    ) -> CanonicalShadowResult:
        if not self.enabled:
            return CanonicalShadowResult(enabled=False)
        try:
            return self._account(self._shadow(event, transport=transport))
        except Exception as exc:  # noqa: BLE001 - shadowing must never fail a proven v1 write.
            return self._account(
                CanonicalShadowResult(
                    enabled=True, error=f"{type(exc).__name__}: {exc}"[:1000]
                )
            )

    @staticmethod
    def _reconcile_lineage_products(
        *,
        store: Any,
        repository: Any,
        dispositions: dict[str, str],
        session_id: int,
        child_digest: bytes,
        child_client_session_id: str,
        merged_kind: str,
        merged_parent: tuple[str, str, bytes] | None,
    ) -> None:
        """Mirror the importer's edge and task-anchor passes for one session.

        The importer resolves parents against the sessions the snapshot
        itself observed and derives exactly one edge from the FINAL absorbed
        state; the live writer resolves against what the store has observed
        SO FAR and must therefore converge its own earlier derivations
        (reconcile_observed_parent_edge supersedes them) as the absorbed
        parent, relation, or kind moves. A parent that has not been seen yet
        leaves the edge pending until a later event retries; the cutover
        import converges any residual gap. Anchors mirror the importer: only
        parentless root/unknown sessions anchor a Task, an anchor whose basis
        a newer observation withdraws is retired, and add_session_edge
        retires anchors a validated parent invalidates.
        """

        from .canonical.sqlite import canonical_hash
        from .canonical.v1_events import EDGE_BASIS_EXPLICIT_PARENT, session_edge_hash_material

        if merged_parent is not None:
            parent_client, parent_client_session_id, parent_digest = merged_parent
            # The importer's parent identity is the full source key — client
            # AND namespace digest — so a same-named session under another
            # client sharing the fingerprint must not be linked.
            parent_row = store.connection.execute(
                "SELECT s.session_id FROM sessions s "
                "JOIN source_instances i ON i.source_instance_id = s.source_instance_id "
                "WHERE i.client = ? AND i.namespace_digest = ? AND s.client_session_id = ?",
                (parent_client, parent_digest, parent_client_session_id),
            ).fetchone()
            if parent_row is None:
                dispositions["edge"] = "parent_session_unobserved"
                repository.retire_task_anchor(session_id)
                return
            parent_session_id = int(parent_row["session_id"])
            if parent_session_id == session_id:
                # The importer quarantines self-parent rows as issues; the
                # live shadow ignores the claim the same way (no edge) and
                # the session cannot anchor a Task while it claims a parent.
                dispositions["edge"] = "self_parent_ignored"
                repository.retire_task_anchor(session_id)
                return
            edge_hash = canonical_hash(
                session_edge_hash_material(
                    child_namespace_digest=child_digest,
                    child_client_session_id=child_client_session_id,
                    parent_namespace_digest=parent_digest,
                    parent_client_session_id=parent_client_session_id,
                    relation_kind=merged_kind,
                )
            )
            relation = merged_kind if merged_kind in {"continuation", "retry"} else "parent"
            edge_result = repository.reconcile_observed_parent_edge(
                child_session_id=session_id,
                parent_session_id=parent_session_id,
                relation=relation,
                basis=EDGE_BASIS_EXPLICIT_PARENT,
                content_hash=edge_hash,
            )
            dispositions["edge"] = edge_result.disposition
            return
        if merged_kind in {"root", "unknown"}:
            anchor = repository.get_or_create_task_anchor(session_id)
            dispositions["task_anchor"] = (
                "present" if anchor is not None else "unavailable"
            )
        elif repository.retire_task_anchor(session_id):
            # The session's newest absorbed kind no longer anchors a Task
            # (the importer would never have anchored it); converge.
            dispositions["task_anchor"] = "retired"

    @staticmethod
    def _shadow_usage(
        *,
        store: Any,
        repository: Any,
        dispositions: dict[str, str],
        event: Mapping[str, Any],
        metadata: Mapping[str, Any],
        client: str,
        session_id: int | None,
    ) -> None:
        """Mirror the importer's usage pass for one event.

        The importer collapses a whole file to one last-wins row per
        (session, lane, representation) before a single reconcile; the live
        writer reconciles per event, so — exactly like the session lane — a
        non-newer arrival whose content differs is deferred visibly instead
        of being pushed into reconcile_usage's stale-divergence conflict
        lane. In-order streams (every watcher scan) reconcile cleanly, and
        an unchanged re-observation stays a physical no-op through the
        repository's content hash (the pre-write coalescing contract).
        """

        from .canonical.legacy_import import (
            _canonical_usage_semantics,
            _is_usage_event,
            _raw_usage_semantics,
            _usage_input,
        )

        if not _is_usage_event(event):
            return
        if session_id is None:
            # The importer quarantines these as usage_missing_session_identity.
            dispositions["usage"] = "usage_missing_session_identity"
            return
        raw_semantics = _raw_usage_semantics(event, metadata)
        if raw_semantics == "atomic_increment":
            # Refused by design until the immutable atomic-event lane exists.
            dispositions["usage"] = "atomic_usage_requires_immutable_event_lane"
            return
        if _canonical_usage_semantics(raw_semantics) is None:
            dispositions["usage"] = "invalid_usage_update_semantics"
            return
        issues: list[Any] = []
        usage = _usage_input(
            event,
            metadata,
            client=client,
            line_number=0,
            session_id=session_id,
            issues=issues,
        )
        if issues:
            reasons = sorted({str(issue.reason) for issue in issues})
            dispositions["usage_issues"] = ",".join(reasons)[:128]
        incoming_hash = repository.usage_content_hash(usage)
        incumbent = store.connection.execute(
            "SELECT source_order, content_hash FROM usage_measurements "
            "WHERE session_id = ? AND lane = ? AND representation = ?",
            (usage.session_id, usage.lane, usage.representation),
        ).fetchone()
        if incumbent is not None and bytes(incumbent["content_hash"]) != incoming_hash:
            incumbent_order = incumbent["source_order"]
            incoming_order = usage.source_order
            newer = incoming_order is not None and (
                incumbent_order is None or int(incoming_order) > int(incumbent_order)
            )
            if not newer:
                tie = (
                    incoming_order is not None
                    and incumbent_order is not None
                    and int(incoming_order) == int(incumbent_order)
                )
                dispositions["usage"] = (
                    "usage_order_tie_deferred" if tie else "usage_stale_deferred"
                )
                return
        result = repository.reconcile_usage(usage)
        dispositions["usage"] = result.disposition

    def _shadow(
        self,
        event: Mapping[str, Any],
        *,
        transport: str | None,
    ) -> CanonicalShadowResult:
        # Canonical imports stay inside the enabled path so a disabled flag
        # keeps the runtime import graph (and behavior) untouched.
        from .canonical.sqlite import CanonicalStore, canonical_hash
        from .canonical.types import FactInput, FactSessionLinkInput, SessionInput, SourceInstanceInput, WorkClaimInput
        from .canonical.v1_events import (
            EXPLICIT_NAMESPACE_SCHEME,
            absorb_observed_session,
            bounded_text,
            client_for,
            event_metadata,
            explicit_namespace_digest,
            normalized_work_claim_fields,
            observed_session_fields,
            optional_text,
            session_observation_hash_material,
            timestamp_to_us,
            work_claim_occurred_raw,
        )

        from .canonical.legacy_import import _is_usage_event

        metadata = event_metadata(event)
        normalized = normalized_work_claim_fields(event, metadata)
        client = client_for(event, metadata)
        fields = observed_session_fields(event, metadata, client=client)
        if normalized is None and fields is None and not _is_usage_event(event):
            # Same subset the importer persists: everything else is not yet
            # represented in the canonical model and is skipped, not lost —
            # the v1 ledger row it shadows remains the record.
            return CanonicalShadowResult(
                enabled=True, skipped_reason="not_represented_in_canonical_model"
            )
        source_event_id = optional_text(event.get("event_id"), limit=512)
        occurred = timestamp_to_us(work_claim_occurred_raw(event, metadata))

        dispositions: dict[str, str] = {}
        skipped_reason: str | None = None
        with closing(CanonicalStore.open_or_create_live(self.store_root)) as store:
            repository = store.repository()
            with store.transaction(write=True):
                store_uuid = str(
                    store.connection.execute(
                        "SELECT store_uuid FROM store_metadata WHERE singleton = 1"
                    ).fetchone()[0]
                )
                fingerprint = metadata.get("source_namespace_fingerprint")
                if isinstance(fingerprint, str) and fingerprint.strip():
                    digest = explicit_namespace_digest(fingerprint)
                    scheme = EXPLICIT_NAMESPACE_SCHEME
                else:
                    digest = hashlib.sha256(
                        b"live-unresolved-store-namespace-v1\x00"
                        + store_uuid.encode("utf-8")
                        + b"\x00"
                        + client.encode("utf-8")
                    ).digest()
                    scheme = LIVE_UNRESOLVED_NAMESPACE_SCHEME
                source = repository.get_or_create_source(
                    SourceInstanceInput(
                        client=client,
                        adapter=LIVE_EVENTS_ADAPTER,
                        representation=LIVE_EVENTS_REPRESENTATION,
                        namespace_digest=digest,
                        namespace_scheme=scheme,
                        source_schema_version=optional_text(
                            event.get("schema_version"), limit=128
                        ),
                    )
                )

                session_id: int | None = None
                merged_parent: tuple[str, str, bytes] | None = None
                merged_kind: str | None = None
                if fields is not None:
                    if fields.parent_client_session_id is None:
                        incoming_parent = None
                    else:
                        parent_fingerprint = metadata.get("parent_source_namespace_fingerprint")
                        # Parity with the importer's parent source key: an
                        # explicit parent fingerprint names its namespace and
                        # may name its client; otherwise the parent is
                        # presumed to share the event's own source.
                        if isinstance(parent_fingerprint, str) and parent_fingerprint.strip():
                            incoming_parent_digest = explicit_namespace_digest(parent_fingerprint)
                            incoming_parent_client = bounded_text(
                                metadata.get("parent_client"), default=client, limit=64
                            )
                        else:
                            incoming_parent_digest = digest
                            incoming_parent_client = client
                        incoming_parent = (
                            incoming_parent_client,
                            fields.parent_client_session_id,
                            incoming_parent_digest,
                        )
                    # Absorb the observation the way the importer's in-memory
                    # merge does: min started / max activity are commutative
                    # and order-free; kind and parent follow the newest
                    # observation. The incumbent's raw parent claim comes from
                    # session_observed_lineage (schema v4). The reads are
                    # race-free under this BEGIN IMMEDIATE transaction.
                    incumbent = store.connection.execute(
                        "SELECT s.session_id, s.session_kind, s.started_at_us, "
                        "s.last_activity_at_us, s.observation_order, s.observation_hash, "
                        "l.parent_client, l.parent_client_session_id, l.parent_namespace_digest "
                        "FROM sessions s LEFT JOIN session_observed_lineage l USING (session_id) "
                        "WHERE s.source_instance_id = ? AND s.client_session_id = ?",
                        (source.source_instance_id, fields.client_session_id),
                    ).fetchone()
                    incoming_order = fields.observation_order
                    if incumbent is None:
                        newer = True
                        tie = False
                        merged_kind, merged_started, merged_last, merged_parent = (
                            fields.session_kind,
                            fields.started_at_us,
                            fields.last_activity_at_us,
                            incoming_parent,
                        )
                        merged_order = incoming_order
                    else:
                        incumbent_order = incumbent["observation_order"]
                        newer = incoming_order is not None and (
                            incumbent_order is None
                            or int(incoming_order) > int(incumbent_order)
                        )
                        tie = (
                            incoming_order is not None
                            and incumbent_order is not None
                            and int(incoming_order) == int(incumbent_order)
                        )
                        incumbent_parent = (
                            (
                                str(incumbent["parent_client"]),
                                str(incumbent["parent_client_session_id"]),
                                bytes(incumbent["parent_namespace_digest"]),
                            )
                            if incumbent["parent_client_session_id"] is not None
                            else None
                        )
                        merged_kind, merged_started, merged_last, merged_parent = (
                            absorb_observed_session(
                                incumbent_session_kind=str(incumbent["session_kind"]),
                                incumbent_started_at_us=incumbent["started_at_us"],
                                incumbent_last_activity_at_us=incumbent["last_activity_at_us"],
                                incumbent_parent=incumbent_parent,
                                incoming_session_kind=fields.session_kind,
                                incoming_started_at_us=fields.started_at_us,
                                incoming_last_activity_at_us=fields.last_activity_at_us,
                                incoming_parent=incoming_parent,
                                incoming_wins_last_writes=newer,
                            )
                        )
                        merged_order = (
                            incoming_order if newer else incumbent_order
                        )
                    merged_hash = canonical_hash(
                        session_observation_hash_material(
                            client_session_id=fields.client_session_id,
                            session_kind=merged_kind,
                            started_at_us=merged_started,
                            last_activity_at_us=merged_last,
                            parent_client_session_id=(
                                merged_parent[1] if merged_parent is not None else None
                            ),
                            parent_namespace_digest=(
                                merged_parent[2] if merged_parent is not None else None
                            ),
                        )
                    )
                    # A non-newer observation whose last-wins fields disagree
                    # is a fork the merge deliberately did NOT take (the
                    # importer breaks order ties — and orders order-less rows
                    # — by line number; the live writer has no line).
                    orderless = incumbent is not None and incoming_order is None
                    last_wins_fork = (
                        incumbent is not None
                        and (tie or orderless)
                        and (
                            fields.session_kind != merged_kind
                            or (
                                incoming_parent is not None
                                and incoming_parent != merged_parent
                            )
                        )
                    )
                    if incumbent is not None and not newer:
                        # I1 absorb parity: fold in the ORDER-FREE fields
                        # (min started_at, max last_activity) that the importer
                        # applies unconditionally. min()/max() are commutative
                        # and idempotent, so this converges on the importer's
                        # exact row regardless of arrival order — no fabricated
                        # order, no spurious conflict. The incumbent is the
                        # newest observation, so its last-wins kind/parent are
                        # also what the importer keeps; they are left untouched.
                        absorb_result = repository.absorb_session_observation(
                            SessionInput(
                                source_instance_id=source.source_instance_id,
                                client_session_id=fields.client_session_id,
                                session_kind=str(incumbent["session_kind"]),  # type: ignore[arg-type]
                                started_at_us=fields.started_at_us,
                                last_activity_at_us=fields.last_activity_at_us,
                            )
                        )
                        session_id = int(incumbent["session_id"])
                        merged_kind = str(incumbent["session_kind"])
                        merged_parent = incumbent_parent
                        if last_wins_fork:
                            # Only the last-wins WINNER stays ambiguous: the
                            # importer's line-number tiebreak might pick the
                            # incoming kind/parent, this writer keeps the
                            # incumbent's. Flag it so the residual divergence
                            # stays visible; the cutover import resolves it.
                            dispositions["session"] = (
                                "session_order_tie_deferred"
                                if tie
                                else "session_orderless_deferred"
                            )
                        else:
                            # Fully converged: started_at/last_activity now
                            # equal the importer's, kind/parent already did.
                            dispositions["session"] = absorb_result.disposition
                    else:
                        session_result = repository.reconcile_session(
                            SessionInput(
                                source_instance_id=source.source_instance_id,
                                client_session_id=fields.client_session_id,
                                session_kind=merged_kind,  # type: ignore[arg-type]
                                started_at_us=merged_started,
                                last_activity_at_us=merged_last,
                                observation_order=merged_order,
                                observation_hash=merged_hash,
                                parent_client=(
                                    merged_parent[0] if merged_parent is not None else None
                                ),
                                parent_client_session_id=(
                                    merged_parent[1] if merged_parent is not None else None
                                ),
                                parent_namespace_digest=(
                                    merged_parent[2] if merged_parent is not None else None
                                ),
                            )
                        )
                        dispositions["session"] = session_result.disposition
                        if session_result.disposition in {"inserted", "updated", "noop"}:
                            session_id = session_result.row_id

                if session_id is not None and merged_kind is not None:
                    self._reconcile_lineage_products(
                        store=store,
                        repository=repository,
                        dispositions=dispositions,
                        session_id=session_id,
                        child_digest=digest,
                        child_client_session_id=fields.client_session_id,  # type: ignore[union-attr]
                        merged_kind=merged_kind,
                        merged_parent=merged_parent,
                    )

                self._shadow_usage(
                    store=store,
                    repository=repository,
                    dispositions=dispositions,
                    event=event,
                    metadata=metadata,
                    client=client,
                    session_id=session_id,
                )

                if normalized is not None:
                    if source_event_id is None:
                        skipped_reason = "missing_event_id"
                    else:
                        supersedes_id = None
                        supersedes_event = normalized["supersedes_event_id"]
                        unknown_supersede = False
                        if supersedes_event is not None:
                            supersedes_id = repository.find_fact_id(
                                source.source_instance_id, supersedes_event
                            )
                            if supersedes_id is None:
                                # The importer quarantines this as
                                # requires_choice; the live shadow skips the
                                # claim visibly and leaves the v1 row as the
                                # record (the session write above stands).
                                skipped_reason = "unknown_superseded_event"
                                unknown_supersede = True
                        if not unknown_supersede:
                            if occurred is None:
                                # Same sentinel the importer stamps (with an
                                # issue row); surfaced here through the result
                                # so a bad clock cannot silently pass as 1970
                                # truth.
                                dispositions["occurred_fallback"] = "epoch_zero"
                            fact = FactInput(
                                source_instance_id=source.source_instance_id,
                                fact_type="work_claim",
                                strength="recorded_claim",
                                transport=f"live-{transport}"[:64] if transport else "live-unknown",
                                occurred_at_us=occurred if occurred is not None else 0,
                                source_event_id=source_event_id,
                                source_order=occurred,
                                content_hash=canonical_hash(normalized),
                                supersedes_fact_id=supersedes_id,
                            )
                            claim_result = repository.append_work_claim(
                                WorkClaimInput(
                                    fact=fact,
                                    **{
                                        key: value
                                        for key, value in normalized.items()
                                        if key != "supersedes_event_id"
                                    },  # type: ignore[arg-type]
                                )
                            )
                            dispositions["fact"] = claim_result.disposition

                            if (
                                session_id is not None
                                and claim_result.row_id is not None
                                and claim_result.disposition in {"inserted", "noop"}
                            ):
                                link_result = repository.link_fact_to_session(
                                    FactSessionLinkInput(
                                        fact_id=claim_result.row_id,
                                        session_id=session_id,
                                        method=LIVE_FACT_LINK_METHOD,
                                        confidence="exact",
                                        rule_version=LIVE_FACT_LINK_RULE_VERSION,
                                        validation_state="valid",
                                    )
                                )
                                dispositions["link"] = link_result.disposition

        written = any(
            value in {"inserted", "updated", "absorbed"} for value in dispositions.values()
        )
        return CanonicalShadowResult(
            enabled=True,
            written=written,
            dispositions=dispositions,
            skipped_reason=skipped_reason,
        )


# Minimum seconds between rebuild ATTEMPTS (rebuilds and failures both
# consume it). The watcher ticks every ~60s and every rebuild rewrites the
# whole rm_* tables even when nothing changed underneath — the cooldown keeps
# a busy session from turning the tick cadence into pointless full rewrites
# (the churn discipline), while staleness stays visible on /health via
# built_through_sequence vs canonical_sequence meanwhile.
REBUILD_COOLDOWN_SECONDS = 300.0

class CanonicalRebuildPolicy:
    """Stale-only, cooldown-bounded rebuild of the rm_* read models.

    The migration's rebuild policy (phase 4.3): rebuilds NEVER run in a read
    request path — this policy is ticked by the WATCHER loop only, after each
    successful scan, gated by the live-write flag (canonical maintenance
    belongs to the writer side; the read flag only gates product reads).
    Ticks outside the cooldown window make a cheap read-only staleness
    check; ticks inside it return immediately without touching the store. A
    writable open + full rebuild happens only when a projection is missing,
    non-current, or behind ``canonical_sequence``, and at most once per
    cooldown window (attempts and failures consume the window; healthy
    "current" checks do not, so a persistently broken store costs one
    attempt per window while a healthy one keeps getting checked every
    tick). Fail-open like every canonical lane: a rebuild problem never
    breaks the watcher.

    The cooldown lives in process memory only. A ``usage watch --once``
    cron deployment constructs a fresh policy per invocation, so there the
    churn bound is the cron cadence itself — acceptable because an idle
    scan does not advance ``canonical_sequence``, so a rebuild only happens
    on ticks that actually imported new data. Concurrent rebuilds (e.g. a
    cron tick racing a persistent watcher) serialize in SQLite and converge
    on identical rows.
    """

    def __init__(
        self,
        store_root: Path | str,
        *,
        enabled: bool | None = None,
        cooldown_seconds: float = REBUILD_COOLDOWN_SECONDS,
        clock: Any = None,
    ) -> None:
        import time

        self.store_root = Path(store_root).expanduser()
        self.enabled = canonical_live_write_enabled() if enabled is None else bool(enabled)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock if clock is not None else time.monotonic
        self._last_attempt_at: float | None = None

    def tick(self) -> dict[str, Any]:
        """One maintenance opportunity. Returns an outcome dict whose
        ``action`` is one of: disabled, no_store, cooldown, current,
        rebuilt, failed. Never raises."""

        if not self.enabled:
            return {"action": "disabled"}
        from .canonical.sqlite import (
            LIVE_STORE_FILENAME,
            MINIMAL_READ_MODEL_NAMES,
            CanonicalStore,
        )

        # Only rebuilds and FAILURES stamp _last_attempt_at, so the cooldown
        # rate-limits attempts (incl. probe failures on a broken store or
        # mount, which would otherwise hot-loop every tick) while a healthy
        # store keeps getting its cheap read-only staleness check each tick.
        # The gate runs before the presence lstat so a failing lstat is
        # cooldown-bounded too.
        now = float(self._clock())
        if (
            self._last_attempt_at is not None
            and now - self._last_attempt_at < self.cooldown_seconds
        ):
            return {"action": "cooldown"}
        path = self.store_root / LIVE_STORE_FILENAME
        try:
            path.lstat()
        except FileNotFoundError:
            # No store, nothing to maintain — and deliberately no creation:
            # the shadow writer creates the store on its first write.
            return {"action": "no_store"}
        except OSError as exc:
            # A presence probe that fails for any reason other than plain
            # absence (a path component that is a file, lost directory
            # permissions, a failing disk or network mount) is a FAILURE,
            # not a crash: fail-open like everything else, and consume the
            # cooldown so a broken mount costs one attempt per window.
            self._last_attempt_at = now
            return {
                "action": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        try:
            with closing(CanonicalStore.open_live(self.store_root, read_only=True)) as store:
                health = store.repository().health_summary()
            metadata = health.get("store") or {}
            canonical_sequence = int(metadata.get("canonical_sequence") or 0)
            generations = {
                str(row.get("projection_name")): row
                for row in health.get("projections", ())
            }
            stale = False
            for name in MINIMAL_READ_MODEL_NAMES:
                row = generations.get(name)
                if (
                    row is None
                    or str(row.get("state") or "") != "current"
                    or int(row.get("built_through_sequence") or 0) < canonical_sequence
                ):
                    stale = True
                    break
            if not stale:
                return {"action": "current", "canonical_sequence": canonical_sequence}
            self._last_attempt_at = now
            with closing(CanonicalStore.open_live(self.store_root)) as store:
                result = store.repository().rebuild_minimal_read_models()
            return {"action": "rebuilt", **result}
        except Exception as exc:  # noqa: BLE001 - maintenance is fail-open, never breaks the watcher.
            self._last_attempt_at = float(self._clock())
            return {
                "action": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }


__all__ = [
    "CANONICAL_LIVE_ENV",
    "CanonicalAuthoritativeNotReady",
    "CanonicalLiveRuntime",
    "CanonicalRebuildPolicy",
    "CanonicalShadowResult",
    "CanonicalWriteMode",
    "LIVE_EVENTS_ADAPTER",
    "LIVE_EVENTS_REPRESENTATION",
    "LIVE_FACT_LINK_METHOD",
    "LIVE_FACT_LINK_RULE_VERSION",
    "LIVE_UNRESOLVED_NAMESPACE_SCHEME",
    "REBUILD_COOLDOWN_SECONDS",
    "canonical_live_write_enabled",
    "canonical_live_write_mode",
    "require_supported_write_mode",
]
