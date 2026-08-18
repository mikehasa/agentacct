from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .env_compat import read_env_alias
from .evidence import (
    Completeness,
    EvidenceEnvelope,
    PrivacyMetadata,
    SubjectRefs,
    adapt_v1_event,
)
from .evidence_product import build_evidence_product
from .evidence_store import (
    EvidenceAppendResult,
    EvidenceRecord,
    EvidenceStore,
    RefreshableUsageItem,
)
from .refreshable_usage import (
    refreshable_usage_revision_id,
    refreshable_usage_slot_digest,
    refreshable_usage_slot_identity,
    refreshable_usage_source_order,
    refreshable_usage_truth_digest,
    refreshable_usage_truth_material,
    refreshable_usage_usage_material_digest_from_material,
)
from .usage_truth import (
    LOCAL_USAGE_PROVENANCE,
    LOCAL_USAGE_SOURCE,
    is_legacy_local_usage_import_shape,
    is_local_usage_import_event,
)
from .work_events import WORK_EVENT_TRANSPORTS, WorkEvent


EVIDENCE_V2_ENV = "AGENTACCT_EVIDENCE_V2"
# Retained pre-rename symbol (still exported). Recognition of BOTH pre-rename
# names now flows through read_env_alias (see evidence_v2_enabled).
EVIDENCE_V2_ENV_LEGACY = "AGENT_SENTINEL_EVIDENCE_V2"
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_REFRESHABLE_USAGE_SCALAR_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def evidence_v2_enabled() -> bool:
    # New AGENTACCT_* primary, then both pre-rename aliases
    # (AGENT_CHRONICLE_* / AGENT_SENTINEL_*). Default ENABLED; an explicit
    # falsey value disables.
    value = read_env_alias(EVIDENCE_V2_ENV)
    return str(value if value is not None else "1").strip().lower() not in _FALSE_VALUES


@dataclass(frozen=True)
class ShadowWriteResult:
    enabled: bool
    appended: bool = False
    evidence_id: str | None = None
    disposition: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "appended": self.appended,
            "evidence_id": self.evidence_id,
            "disposition": self.disposition,
            "error": self.error,
        }


@dataclass(frozen=True)
class RefreshableUsageSnapshotResult:
    """Fail-open outcome for one trusted current-usage snapshot.

    ``spool_written`` counts durable logical transitions, not JSONL lines. A
    multi-slot snapshot is persisted as one fsynced batch record so projection
    recovery can replay it atomically.
    """

    enabled: bool
    input_count: int = 0
    eligible: int = 0
    inserted: int = 0
    updated: int = 0
    resurrected: int = 0
    deleted: int = 0
    noop: int = 0
    stale: int = 0
    conflicts: int = 0
    existing_conflicts: int = 0
    excluded: int = 0
    spool_written: int = 0
    complete_requested: bool = False
    complete_applied: bool = False
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "input_count": self.input_count,
            "eligible": self.eligible,
            "inserted": self.inserted,
            "updated": self.updated,
            "resurrected": self.resurrected,
            "deleted": self.deleted,
            "noop": self.noop,
            "stale": self.stale,
            "conflicts": self.conflicts,
            "existing_conflicts": self.existing_conflicts,
            "excluded": self.excluded,
            "spool_written": self.spool_written,
            "complete_requested": self.complete_requested,
            "complete_applied": self.complete_applied,
            "errors": list(self.errors),
        }


class EvidenceRuntime:
    """Small compatibility boundary between v1 transports and Evidence v2.

    Explicit Evidence v2 ingestion raises validation/storage errors. Shadow
    writes from the existing ledger are fail-open: a v2 problem can never make
    a proven v1 MCP, HTTP, CLI, import, or runner write fail.
    """

    def __init__(self, store_dir: Path | str, *, enabled: bool | None = None) -> None:
        self.store_dir = Path(store_dir).expanduser()
        self.enabled = evidence_v2_enabled() if enabled is None else bool(enabled)
        self._store: EvidenceStore | None = None

    @property
    def store(self) -> EvidenceStore:
        if self._store is None:
            self._store = EvidenceStore(self.store_dir)
        return self._store

    @staticmethod
    def _transport(value: str | None) -> str:
        return value if value in WORK_EVENT_TRANSPORTS else "unknown"

    @staticmethod
    def _refreshable_usage_measurement_payload(
        truth: Mapping[str, Any],
        *,
        revision_id: str,
    ) -> dict[str, Any]:
        """Expose only validated importer-observed scalars to Evidence product."""

        payload: dict[str, Any] = {"measurement_id": revision_id}
        token_facts = truth.get("tokens")
        tokens = token_facts if isinstance(token_facts, Mapping) else {}
        for field in _REFRESHABLE_USAGE_SCALAR_KEYS:
            raw_fact = tokens.get(field)
            fact = raw_fact if isinstance(raw_fact, Mapping) else {}
            value = fact.get("value")
            if (
                fact.get("reported") is True
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            ):
                payload[field] = value

        raw_client_cost = truth.get("client_reported_cost")
        client_cost = (
            raw_client_cost if isinstance(raw_client_cost, Mapping) else {}
        )
        raw_cost = client_cost.get("usd")
        if isinstance(raw_cost, str):
            try:
                cost_usd = float(raw_cost)
            except ValueError:
                pass
            else:
                if math.isfinite(cost_usd) and cost_usd >= 0:
                    payload["cost_usd"] = cost_usd
        return payload

    @staticmethod
    def _refreshable_usage_envelope(
        event: Mapping[str, Any],
    ) -> tuple[RefreshableUsageItem, str] | None:
        """Build one deterministic Evidence revision for current usage truth.

        Receipt ids, agentacct arrival time, source polling watermarks, and
        derived price never enter the envelope. The stable slot digest is the
        Evidence ``source_event_id``; changed source truth therefore becomes a
        new version of one logical Evidence event instead of a conflict.
        """

        slot = refreshable_usage_slot_identity(event)
        truth = refreshable_usage_truth_material(event)
        truth_digest = refreshable_usage_truth_digest(event)
        revision_id = refreshable_usage_revision_id(event)
        source_order = refreshable_usage_source_order(event)
        if (
            slot is None
            or truth is None
            or truth_digest is None
            or revision_id is None
        ):
            return None

        slot_key = refreshable_usage_slot_digest(slot)
        client_cost = truth.get("client_reported_cost")
        client_cost_mapping = client_cost if isinstance(client_cost, Mapping) else {}
        cost_value = client_cost_mapping.get("usd")
        dimensions = ("cost", "usage") if cost_value is not None else ("usage",)
        measurement_basis = {"usage": str(truth.get("usage_confidence") or "client_reported")}
        if cost_value is not None:
            measurement_basis["cost"] = str(
                client_cost_mapping.get("confidence") or "client_reported"
            )
        measurement_payload = EvidenceRuntime._refreshable_usage_measurement_payload(
            truth,
            revision_id=revision_id,
        )

        safe_client = "".join(
            character if character.isalnum() or character in {"_", "-"} else "_"
            for character in slot.client
        )
        safe_session = "".join(
            character if character.isalnum() or character in {"_", "-"} else "_"
            for character in slot.client_session_id
        )
        # Keep a readable bounded prefix and add a 20-hex (80-bit) slot-digest
        # prefix so truncation/sanitization of long session ids, namespaces,
        # lanes, or representations cannot accidentally merge distinct slots.
        # This suffix is a disambiguator, not the identity: the COMPLETE slot
        # digest is bound on the envelope (source_event_id/raw_ref).
        run_slot_suffix = slot_key.removeprefix("sha256:")[:20]
        subjects = SubjectRefs.from_dict(
            {
                "run_id": (
                    f"client_{safe_client[:24]}_{safe_session[:48]}_"
                    f"{run_slot_suffix}"
                ),
                "client_session_id": slot.client_session_id,
                "extra": {
                    "source_namespace_fingerprint": slot.source_namespace,
                    "source_namespace_kind": slot.namespace_kind,
                },
            }
        )
        envelope = EvidenceEnvelope.create(
            assertion="observed",
            event_type="model_usage",
            source_type="local_client_log",
            source_system=slot.client,
            source_instance="trusted-v1-current-usage",
            source_schema="agent-chronicle.refreshable-usage-truth.v1",
            adapter="chronicle-refreshable-usage-adapter.v1",
            # A cumulative current fact has a reliable ordering watermark but
            # not necessarily a stable event instant. Keep that absence honest
            # and carry ordering in the dedicated reconcile transition.
            event_timestamp="1970-01-01T00:00:00Z",
            observed_at="1970-01-01T00:00:00Z",
            dimensions=dimensions,
            measurement_basis=measurement_basis,
            completeness=Completeness(
                status="partial",
                missing_dimensions=("event_time",),
                note_codes=("refreshable_current_state",),
            ),
            privacy=PrivacyMetadata(
                classification="internal",
                content_capture="metadata_only",
            ),
            subjects=subjects,
            payload={
                **measurement_payload,
                "refreshable_usage": {
                    "slot": slot.to_dict(),
                    "truth": truth,
                    "truth_digest": truth_digest,
                    "revision_id": revision_id,
                }
            },
            source_event_id=slot_key,
            raw_digest=truth_digest,
            raw_ref=f"trusted-v1-usage-slot:{slot_key}",
            tags=("refreshable_current", "v1_compat"),
        )
        return (
            RefreshableUsageItem(
                slot_key=slot_key,
                slot_identity=slot.to_dict(),
                content_hash=truth_digest,
                source_order=source_order,
                revision_id=revision_id,
                envelope=envelope,
                # Tokens-only digest (evidence links excluded): lets the store
                # tell a real usage change from mere provenance drift at an equal
                # source-order watermark.
                usage_material_hash=refreshable_usage_usage_material_digest_from_material(truth),
            ),
            revision_id,
        )

    @classmethod
    def _refreshable_usage_items(
        cls,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[list[RefreshableUsageItem], int, int, int, int, list[str]]:
        """Normalize and reduce one ledger snapshot without guessing ties."""

        grouped: dict[str, list[RefreshableUsageItem]] = {}
        eligible = excluded = 0
        in_batch_noops = in_batch_stale = 0
        errors: list[str] = []
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise TypeError(f"event[{index}] is not an object")
            metadata_value = event.get("metadata")
            metadata = (
                metadata_value if isinstance(metadata_value, Mapping) else {}
            )
            raw_semantics = metadata.get("usage_update_semantics")
            atomic_increment = (
                str(raw_semantics or "").strip().lower() == "atomic_increment"
            )
            has_reserved_usage_marker = (
                metadata.get("usage_source") == LOCAL_USAGE_SOURCE
                or metadata.get("usage_provenance") == LOCAL_USAGE_PROVENANCE
            )
            if not is_local_usage_import_event(dict(event)):
                if has_reserved_usage_marker:
                    # Pre-provenance rows are a supported stored shape (the v1
                    # planner still accepts them); they never passed the trust
                    # gate, so there is no Evidence row to protect. Excluding
                    # them keeps complete-snapshot authority instead of
                    # erroring every future reconcile on old stores. Anything
                    # else carrying reserved markers stays an error.
                    if is_legacy_local_usage_import_shape(dict(event)):
                        excluded += 1
                    else:
                        errors.append(
                            f"event[{index}] has reserved local usage markers but an incomplete trust or identity gate"
                        )
                continue
            try:
                built = cls._refreshable_usage_envelope(event)
            except Exception as exc:  # noqa: BLE001 - one malformed row withholds complete authority.
                errors.append(
                    f"event[{index}] trusted model_usage is invalid: {type(exc).__name__}: {exc}"[
                        :1000
                    ]
                )
                continue
            if built is None:
                if atomic_increment:
                    excluded += 1
                    continue
                errors.append(
                    f"event[{index}] trusted model_usage has invalid refreshable identity or semantics"
                )
                continue
            item, _revision_id = built
            eligible += 1
            grouped.setdefault(item.slot_key, []).append(item)

        selected: list[RefreshableUsageItem] = []
        for slot_key, slot_items in sorted(grouped.items()):
            by_content: dict[str, list[RefreshableUsageItem]] = {}
            for item in slot_items:
                by_content.setdefault(item.content_hash, []).append(item)
            content_candidates: list[RefreshableUsageItem] = []
            for same_content in by_content.values():
                chosen = max(
                    same_content,
                    key=lambda item: (
                        item.source_order is not None,
                        item.source_order if item.source_order is not None else -1,
                        item.revision_id,
                    ),
                )
                content_candidates.append(chosen)
                in_batch_noops += len(same_content) - 1
            if len(content_candidates) == 1:
                selected.append(content_candidates[0])
                continue
            if all(item.source_order is not None for item in content_candidates):
                ordered = sorted(
                    content_candidates,
                    key=lambda item: (int(item.source_order or 0), item.content_hash),
                    reverse=True,
                )
                if ordered[0].source_order != ordered[1].source_order:
                    selected.append(ordered[0])
                    in_batch_stale += len(ordered) - 1
                    continue
            errors.append(
                f"slot {slot_key} has unordered divergent current usage revisions"
            )

        return (
            selected,
            eligible,
            excluded,
            in_batch_noops,
            in_batch_stale,
            errors,
        )

    def reconcile_refreshable_usage_snapshot(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        complete: bool,
        transport: str | None = "internal",
    ) -> RefreshableUsageSnapshotResult:
        """Reconcile trusted cumulative usage; never fail a proven v1 write."""

        if not isinstance(complete, bool):
            raise TypeError("complete must be a bool")
        # Transport is intentionally not identity material, but normalize it
        # here so callers cannot accidentally assume arbitrary labels persist.
        self._transport(transport)
        if not self.enabled:
            return RefreshableUsageSnapshotResult(
                enabled=False,
                input_count=len(events),
                complete_requested=complete,
            )

        try:
            (
                items,
                eligible,
                excluded,
                reduced_noops,
                reduced_stale,
                errors,
            ) = self._refreshable_usage_items(events)
        except Exception as exc:  # noqa: BLE001 - normalization is fail-open too.
            return RefreshableUsageSnapshotResult(
                enabled=True,
                input_count=len(events),
                complete_requested=complete,
                complete_applied=False,
                errors=(f"{type(exc).__name__}: {exc}"[:1000],),
            )
        complete_applied = complete and not errors
        try:
            result = self.store.reconcile_refreshable_usage_snapshot(
                items,
                complete=complete_applied,
            )
        except Exception as exc:  # noqa: BLE001 - current-state shadow is fail-open by contract.
            return RefreshableUsageSnapshotResult(
                enabled=True,
                input_count=len(events),
                eligible=eligible,
                excluded=excluded,
                noop=reduced_noops,
                stale=reduced_stale,
                complete_requested=complete,
                complete_applied=False,
                errors=tuple([*errors, f"{type(exc).__name__}: {exc}"[:1000]]),
            )

        return RefreshableUsageSnapshotResult(
            enabled=True,
            input_count=len(events),
            eligible=eligible,
            inserted=result.inserted,
            updated=result.updated + result.resurrected,
            resurrected=result.resurrected,
            deleted=result.tombstoned,
            noop=(
                reduced_noops
                + result.unchanged
                + result.watermarked
                + result.existing_conflicts
            ),
            stale=reduced_stale + result.stale,
            conflicts=result.conflicts,
            existing_conflicts=result.existing_conflicts,
            excluded=excluded,
            spool_written=result.transition_count,
            complete_requested=complete,
            complete_applied=complete_applied,
            errors=tuple(errors),
        )

    def shadow_v1_event(self, event: Mapping[str, Any], *, transport: str | None = None) -> ShadowWriteResult:
        if not self.enabled:
            return ShadowWriteResult(enabled=False)
        if is_local_usage_import_event(dict(event)):
            refreshable = self.reconcile_refreshable_usage_snapshot(
                [event],
                complete=False,
                transport=transport,
            )
            if refreshable.eligible:
                if refreshable.errors:
                    return ShadowWriteResult(
                        enabled=True,
                        error="; ".join(refreshable.errors)[:1000],
                    )
                if refreshable.inserted:
                    disposition = "inserted"
                elif refreshable.updated:
                    disposition = "updated"
                elif refreshable.conflicts:
                    disposition = "conflict"
                elif refreshable.stale:
                    disposition = "stale"
                else:
                    disposition = "noop"
                return ShadowWriteResult(
                    enabled=True,
                    appended=bool(refreshable.spool_written),
                    disposition=disposition,
                )
            if refreshable.errors:
                return ShadowWriteResult(
                    enabled=True,
                    error="; ".join(refreshable.errors)[:1000],
                )
            # Atomic increments are intentionally excluded from the mutable
            # current lane and continue through ordinary immutable Evidence.
        normalized_transport = self._transport(transport)
        shadow = dict(event)
        metadata_value = shadow.get("metadata")
        metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
        metadata.setdefault("work_event_transport", normalized_transport)
        if WorkEvent.from_v1_event(shadow, transport=normalized_transport).event_kind != "event":
            metadata.setdefault("work_event_schema_version", "chronicle.work-event.v1")
        shadow["metadata"] = metadata
        try:
            envelope = adapt_v1_event(
                shadow,
                source_instance=f"v1-{normalized_transport}",
                adapter=f"chronicle-v1-{normalized_transport}-adapter",
            )
            result = self.store.append(envelope)
        except Exception as exc:  # noqa: BLE001 - v2 shadowing must never fail a proven v1 write.
            return ShadowWriteResult(enabled=True, error=f"{type(exc).__name__}: {exc}"[:1000])
        return ShadowWriteResult(
            enabled=True,
            appended=result.disposition in {"inserted", "conflict"},
            evidence_id=result.evidence_id,
            disposition=result.disposition,
        )

    def shadow_v1_events(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        transport: str | None = None,
    ) -> tuple[ShadowWriteResult, ...]:
        return tuple(self.shadow_v1_event(event, transport=transport) for event in events)

    def append(self, envelope: EvidenceEnvelope) -> EvidenceAppendResult:
        if not self.enabled:
            raise RuntimeError(f"Evidence v2 is disabled by {EVIDENCE_V2_ENV}=0")
        return self.store.append(envelope)

    def records(self, *, limit: int = 10_000, descending: bool = False, **filters: Any) -> list[EvidenceRecord]:
        if not self.enabled:
            return []
        return self.store.query(limit=limit, descending=descending, **filters)

    def envelopes(self, *, limit: int = 10_000, descending: bool = False, **filters: Any) -> list[EvidenceEnvelope]:
        return [record.envelope for record in self.records(limit=limit, descending=descending, **filters)]

    def recent_records(
        self,
        *,
        source_type: str,
        source_system: str | None = None,
        assertion: str | None = None,
        event_type: str | None = None,
        consumer: str | None = None,
        limit: int = 10_000,
    ) -> list[EvidenceRecord]:
        """Read a bounded newest-first source window for product projections."""

        if not self.enabled:
            return []
        return self.store.query_recent_source(
            source_type=source_type,
            source_system=source_system,
            assertion=assertion,
            event_type=event_type,
            consumer=consumer,
            limit=limit,
        )

    def recent_envelopes(
        self,
        *,
        source_type: str,
        source_system: str | None = None,
        assertion: str | None = None,
        event_type: str | None = None,
        consumer: str | None = None,
        limit: int = 10_000,
    ) -> list[EvidenceEnvelope]:
        return [
            record.envelope
            for record in self.recent_records(
                source_type=source_type,
                source_system=source_system,
                assertion=assertion,
                event_type=event_type,
                consumer=consumer,
                limit=limit,
            )
        ]

    def _conflict_groups(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        grouped: dict[str, list[EvidenceRecord]] = {}
        for record in self.store.conflicts():
            grouped.setdefault(record.envelope.idempotency_key, []).append(record)
        return [
            {
                "idempotency_key": key,
                "version_count": len(records),
                "evidence_ids": sorted(record.evidence_id for record in records),
            }
            for key, records in sorted(grouped.items())
        ]

    def product(self, *, limit: int = 10_000) -> dict[str, Any]:
        if not self.enabled:
            product = build_evidence_product([])
            product["status"] = {"enabled": False, "reason": f"disabled by {EVIDENCE_V2_ENV}"}
            return product
        records = self.store.query(limit=limit, order_by="event_time")
        claimed_links = [
            {
                "link": record.link.to_dict(),
                "validation_state": record.validation_state,
                "is_conflict": record.is_conflict,
                "receipt_count": record.receipt_count,
            }
            for record in self.store.query_claimed_links(limit=limit)
        ]
        stats = self.store.stats()
        product = build_evidence_product(
            [record.envelope for record in records],
            # A conflict scan on a large append-only projection is not free.
            # Exact store stats already prove when there are no versions to
            # return, so keep the empty fast path bounded without weakening a
            # real conflict population.
            store_conflicts=self._conflict_groups() if stats.conflict_versions else (),
            claimed_links=claimed_links,
        )
        product["status"] = {
            "enabled": True,
            "store": "evidence-v2",
            "stats": stats.to_dict(),
            "limit": limit,
            "truncated": stats.evidence_versions > limit,
        }
        return product

    def dashboard_product(self, *, limit: int = 2_000) -> dict[str, Any]:
        """Build a recent bounded projection for the Advanced HTML index.

        The forensic ``product()`` contract intentionally remains unchanged:
        it retains its 10,000-row event-time ordering and exact store stats.
        The HTML hub needs only a truthful preview plus links to those
        forensic surfaces, so it probes one extra newest row to prove whether
        older indexed evidence exists and never scans whole-store counters or
        conflict history on the request path.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit < 10_000:
            raise ValueError("dashboard product limit must be between 1 and 9999")
        if not self.enabled:
            product = build_evidence_product([])
            product["status"] = {
                "enabled": False,
                "reason": f"disabled by {EVIDENCE_V2_ENV}",
                "scope": "advanced_html_preview",
                "limit": limit,
                "truncated": False,
                "store_stats_included": False,
            }
            return product
        records = self.store.query(
            limit=limit + 1,
            order_by="event_time",
            descending=True,
        )
        truncated = len(records) > limit
        product = build_evidence_product([record.envelope for record in records[:limit]])
        product["status"] = {
            "enabled": True,
            "store": "evidence-v2",
            "scope": "advanced_html_preview",
            "selection": "latest_event_time",
            "limit": limit,
            "truncated": truncated,
            "store_stats_included": False,
        }
        return product

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "reason": f"disabled by {EVIDENCE_V2_ENV}"}
        return {
            "enabled": True,
            "store": "evidence-v2",
            "stats": self.store.stats().to_dict(),
            "refreshable_usage": self.store.refreshable_usage_stats().to_dict(),
            "spool_path": str(self.store.spool_path),
            "projection_path": str(self.store.projection_path),
        }

    def replay_v1(self, events: Sequence[Mapping[str, Any]], *, transport: str = "internal") -> dict[str, Any]:
        results = self.shadow_v1_events(events, transport=transport)
        return {
            "enabled": self.enabled,
            "input_count": len(events),
            "inserted_count": sum(result.disposition == "inserted" for result in results),
            "duplicate_count": sum(result.disposition == "duplicate" for result in results),
            "updated_count": sum(result.disposition == "updated" for result in results),
            "noop_count": sum(result.disposition == "noop" for result in results),
            "stale_count": sum(result.disposition == "stale" for result in results),
            "conflict_count": sum(result.disposition == "conflict" for result in results),
            "error_count": sum(result.error is not None for result in results),
            "errors": [result.error for result in results if result.error is not None][:20],
        }


__all__ = [
    "EVIDENCE_V2_ENV",
    "EVIDENCE_V2_ENV_LEGACY",
    "EvidenceRuntime",
    "RefreshableUsageSnapshotResult",
    "ShadowWriteResult",
    "evidence_v2_enabled",
]
