"""Manifest-bound classification and candidate-only legacy recovery.

This module is intentionally narrower than the ordinary legacy importer.  It
classifies every physical line from an explicitly verified offline snapshot,
emits immutable migration-archive receipt drafts, and can replay only the
``candidate_recovery`` decisions from a sealed :class:`VerifiedRecoveryPlan`.

Recovery never discovers a store, creates a source/session/Task identity, or
upgrades log evidence to exact provenance.  Scoped legacy rows are imported by
the existing read-only importer first; recovered rows may only reuse the
already-imported donor source and session.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from agent_chronicle.log_evidence import (
    apply_log_evidence_to_snapshots,
    build_log_evidence_index,
)
from agent_chronicle.usage_truth import (
    is_local_usage_import_event,
    normalized_local_usage_session_id,
)
from agent_chronicle.work_ledger import (
    _project_identity,
    _propagate_log_evidenced_source_constraints,
    build_work_events,
)

from .legacy_import import (
    LEGACY_EVENTS_ADAPTER,
    LEGACY_EVENTS_REPRESENTATION,
    LegacyImportError,
    MigrationReport,
    _EventRecord,
    _Issue,
    _is_usage_event,
    _work_claim_input,
    import_legacy_snapshot,
)
from .v1_events import (
    client_for as _client_for,
    event_metadata as _metadata,
    optional_text as _optional_text,
    semantic_kind as _semantic_kind,
)
from .migration_archive import (
    PhysicalInventory,
    PhysicalLineLocator,
    ReceiptDraft,
    RecoveryIdentity,
    VerifiedMigrationArchive,
    VerifiedRecoveryPlan,
    scan_snapshot_lines,
)
from .snapshot import VerifiedSnapshot
from .sqlite import CanonicalRepository, CanonicalStore
from .types import (
    RECOVERY_FACT_TRANSPORT,
    RecoveredWorkClaimInput,
)


RECOVERY_DESIGN_ID = "unscoped_legacy_recovery_v3"
RECOVERY_CLASSIFIER = "agent-chronicle.legacy-recovery"
RECOVERY_CLASSIFIER_VERSION = "v3"
RECOVERY_RULE_VERSION = "legacy-client-log-recovery-v1"

_RULES = (
    "physical-inventory-must-match-verified-snapshot",
    "recovery-plan-publication-requires-fresh-archive",
    "scoped-rows-use-ordinary-readonly-importer",
    "only-trusted-local-usage-rows-may-donate",
    "candidate-recovery-requires-one-physical-donor",
    "claimed-client-session-transcript-conflicts-veto",
    "claimed-semantic-namespace-conflicts-veto",
    "multi-session-work-cohorts-remain-ambiguous",
    "conflicting-project-identities-remain-ambiguous",
    "scoped-siblings-participate-in-project-conflict-veto",
    "scoped-siblings-participate-in-namespace-conflict-veto",
    "cohort-namespace-inheritance-is-not-row-level-proof",
    "recovered-source-and-session-must-already-exist",
    "recovered-facts-are-high-never-exact",
)
RECOVERY_RULES_DIGEST = hashlib.sha256(
    json.dumps(_RULES, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
).hexdigest()

_EXPLICIT_NAMESPACE_SCHEME = "legacy-explicit-fingerprint-sha256-v1"


class LegacyRecoveryError(LegacyImportError):
    """The sealed recovery evidence or candidate lookup failed closed."""


@dataclass(frozen=True, slots=True)
class RecoveryClassification:
    """Complete one-draft-per-physical-line classification."""

    inventory: PhysicalInventory
    drafts: tuple[ReceiptDraft, ...]
    parsed_object_events: int
    disposition_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        if len(self.drafts) != self.inventory.total_lines:
            raise LegacyRecoveryError(
                "classification must contain one receipt draft per physical line"
            )
        subjects = {draft.subject for draft in self.drafts}
        if subjects != set(self.inventory.lines):
            raise LegacyRecoveryError(
                "classification subjects do not exactly match the physical inventory"
            )
        object.__setattr__(
            self,
            "disposition_counts",
            MappingProxyType(dict(sorted(self.disposition_counts.items()))),
        )

    @property
    def candidate_recovery_rows(self) -> int:
        return int(self.disposition_counts.get("candidate_recovery", 0))


@dataclass(frozen=True, slots=True)
class RecoveryAppendReport:
    candidate_rows: int
    fact_dispositions: Mapping[str, int]
    link_dispositions: Mapping[str, int]
    canonical_sequence_before: int
    canonical_sequence_after: int
    canonical_writes: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "fact_dispositions",
            MappingProxyType(dict(sorted(self.fact_dispositions.items()))),
        )
        object.__setattr__(
            self,
            "link_dispositions",
            MappingProxyType(dict(sorted(self.link_dispositions.items()))),
        )


@dataclass(frozen=True, slots=True)
class RecoveryReplayReport:
    scoped_imports: tuple[MigrationReport, ...]
    recovery: RecoveryAppendReport
    projection_rebuilt: bool
    projection: Mapping[str, int]
    canonical_sequence_before: int
    canonical_sequence_after: int
    canonical_writes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "projection", MappingProxyType(dict(self.projection)))


@dataclass(frozen=True, slots=True)
class _PhysicalEvent:
    locator: PhysicalLineLocator
    event: Mapping[str, Any] | None


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _reject_nonfinite_json_number(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value!r}")


def _read_physical_events(
    snapshot: VerifiedSnapshot,
    inventory: PhysicalInventory,
) -> tuple[_PhysicalEvent, ...]:
    if not isinstance(snapshot, VerifiedSnapshot):
        raise LegacyRecoveryError("classification source must be a VerifiedSnapshot")
    if not isinstance(inventory, PhysicalInventory):
        raise LegacyRecoveryError("classification requires a PhysicalInventory")
    if inventory != scan_snapshot_lines(snapshot):
        raise LegacyRecoveryError(
            "physical inventory does not exactly match the verified snapshot"
        )

    by_file: dict[str, list[PhysicalLineLocator]] = defaultdict(list)
    for locator in inventory.lines:
        by_file[locator.relative_path].append(locator)

    result: list[_PhysicalEvent] = []
    for source in inventory.files:
        expected_offset = 0
        with snapshot.open_binary(source.relative_path) as handle:
            for locator in by_file[source.relative_path]:
                if locator.byte_offset != expected_offset or handle.tell() != expected_offset:
                    raise LegacyRecoveryError(
                        "physical inventory offset changed while reading classification input"
                    )
                raw = handle.read(locator.byte_length)
                if (
                    len(raw) != locator.byte_length
                    or hashlib.sha256(raw).hexdigest() != locator.raw_sha256
                ):
                    raise LegacyRecoveryError(
                        "physical input bytes do not match their verified locator"
                    )
                expected_offset += locator.byte_length
                try:
                    value = json.loads(
                        raw.decode("utf-8", errors="strict"),
                        object_pairs_hook=_reject_duplicate_json_keys,
                        parse_constant=_reject_nonfinite_json_number,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    result.append(_PhysicalEvent(locator, None))
                    continue
                result.append(
                    _PhysicalEvent(
                        locator,
                        MappingProxyType(dict(value))
                        if isinstance(value, Mapping)
                        else None,
                    )
                )
            if expected_offset != source.size_bytes or handle.read(1):
                raise LegacyRecoveryError(
                    "physical classification read did not conserve manifest bytes"
                )
    snapshot.verify_unchanged()
    return tuple(result)


def _event_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("event_id")
    if not isinstance(value, str) or not value:
        return None
    return value


def _source_namespace(event: Mapping[str, Any]) -> str | None:
    value = _metadata(event).get("source_namespace_fingerprint")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _semantic_namespace_claims(event: Mapping[str, Any]) -> frozenset[str]:
    """Return every non-empty semantic session namespace asserted by a row."""

    metadata = _metadata(event)
    return frozenset(
        value.strip()
        for key in ("namespace_fingerprint", "session_namespace_fingerprint")
        if isinstance((value := metadata.get(key)), str) and value.strip()
    )


def _cohort_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    metadata = _metadata(event)
    work_id = metadata.get("work_id")
    if isinstance(work_id, str) and work_id:
        return ("work", work_id)
    semantic = str(metadata.get("sentinel_semantic_kind") or "")
    if semantic == "task" or str(event.get("event_type") or "").startswith("task_"):
        task_id = metadata.get("task_id") or metadata.get("section_id")
        return ("task", str(task_id or _event_id(event) or ""))
    return (
        "section",
        str(event.get("source") or ""),
        str(event.get("run_id") or ""),
        str(metadata.get("section_id") or _event_id(event) or ""),
    )


def _task_snapshot(event: Mapping[str, Any]) -> dict[str, Any]:
    """Project only raw identity claims; stored derived markers are ignored."""

    metadata = _metadata(event)
    return {
        "event_id": _event_id(event) or "",
        "work_id": "\x00".join(_cohort_key(event)),
        "client": metadata.get("client"),
        "client_session_id": metadata.get("client_session_id"),
        "client_transcript_id": metadata.get("client_transcript_id"),
    }


def _recovered_work_key(
    event: Mapping[str, Any],
    recovery: RecoveryIdentity,
) -> tuple[str, str, str] | None:
    """Return the Work identity a candidate recovery would expose.

    Task rows have adapter/membership coverage only and do not enter the
    existing-product Work projection.  Section-like claims use the recovered
    client/session plus the raw section id, matching ``work_key`` without
    importing its rendering concerns into the classifier.
    """

    metadata = _metadata(event)
    semantic_kind = _semantic_kind(event, metadata)
    if (
        str(metadata.get("sentinel_semantic_kind") or "").lower() == "task"
        or str(event.get("event_type") or "").lower().startswith("task_")
        or semantic_kind == "task"
    ):
        return None
    section_id = _optional_text(
        metadata.get("section_id") or event.get("section_id"),
        limit=256,
    )
    if section_id is None:
        return None
    return (
        recovery.client,
        normalized_local_usage_session_id(
            recovery.client,
            recovery.client_session_id,
        ),
        section_id,
    )


def _scoped_work_key(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return the canonical Work key for an explicitly scoped Section row."""

    metadata = _metadata(event)
    semantic_kind = str(metadata.get("sentinel_semantic_kind") or "").lower()
    event_type = str(event.get("event_type") or "").lower()
    if semantic_kind != "section" and not event_type.startswith("section_"):
        return None
    section_id = _optional_text(
        metadata.get("section_id") or event.get("section_id"),
        limit=256,
    )
    session_id = metadata.get("client_session_id") or event.get(
        "client_session_id"
    )
    if (
        section_id is None
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        return None
    client = _client_for(event, metadata)
    return (
        client,
        normalized_local_usage_session_id(client, session_id.strip()[:512]),
        section_id,
    )


def _quarantine_conflicting_work_identity_recoveries(
    drafts: list[ReceiptDraft],
    *,
    events_by_locator: Mapping[PhysicalLineLocator, Mapping[str, Any]],
) -> list[ReceiptDraft]:
    """Fail closed when one recovered Work spans raw identity boundaries.

    Canonical v1 does not persist the existing product's pseudonymous project
    boundary, and a scoped sibling can also assert a semantic namespace that
    differs from its import source.  Recovering either cohort would merge
    snapshots the product intentionally quarantines.  Preserve those safety
    boundaries by rejecting every candidate row in the cohort.  Missing
    project metadata remains compatible with one asserted identity, exactly
    like the existing product guard.
    """

    cohorts: dict[
        tuple[str, str, str],
        list[tuple[int, ReceiptDraft, Mapping[str, Any]]],
    ] = defaultdict(list)
    scoped_project_identities: dict[tuple[str, str, str], set[str]] = defaultdict(
        set
    )
    scoped_namespace_identities: dict[tuple[str, str, str], set[str]] = (
        defaultdict(set)
    )
    for index, draft in enumerate(drafts):
        event = events_by_locator.get(draft.subject)
        if event is None:
            if draft.disposition == "candidate_recovery":
                raise LegacyRecoveryError(
                    "candidate recovery source event disappeared during classification"
                )
            continue
        if draft.disposition == "candidate_recovery" and draft.recovery is not None:
            work_key = _recovered_work_key(event, draft.recovery)
            if work_key is not None:
                cohorts[work_key].append((index, draft, event))
            continue
        if draft.disposition != "canonical_imported":
            continue
        work_key = _scoped_work_key(event)
        project_identity = _project_identity(
            _metadata(event).get("project_dir")
        )
        if work_key is not None and project_identity is not None:
            scoped_project_identities[work_key].add(project_identity)
        if work_key is not None:
            source_namespace = _source_namespace(event)
            if source_namespace is not None:
                scoped_namespace_identities[work_key].add(source_namespace)
            scoped_namespace_identities[work_key].update(
                _semantic_namespace_claims(event)
            )

    result = list(drafts)
    for work_key, cohort in cohorts.items():
        namespace_identities = {
            draft.recovery.source_namespace_fingerprint.strip()
            for _index, draft, _event in cohort
            if draft.recovery is not None
        }
        namespace_identities.update(
            scoped_namespace_identities.get(work_key, set())
        )
        project_identities = {
            identity
            for _index, _draft, event in cohort
            if (
                identity := _project_identity(
                    _metadata(event).get("project_dir")
                )
            )
        }
        project_identities.update(scoped_project_identities.get(work_key, set()))
        namespace_conflict = len(namespace_identities) > 1
        project_conflict = len(project_identities) > 1
        if not namespace_conflict and not project_conflict:
            continue
        cohort_donors = tuple(
            sorted(
                {
                    donor
                    for _index, draft, _event in cohort
                    for donor in draft.donors
                },
                key=PhysicalLineLocator.sort_key,
            )
        )
        for index, draft, _event in cohort:
            disposition = "identity_conflict" if namespace_conflict else "ambiguous"
            reasons = (
                ("conflicting_namespace_identity",)
                if namespace_conflict
                else ("conflicting_project_identity",)
            )
            result[index] = _receipt_draft(
                subject=draft.subject,
                disposition=disposition,
                reasons=reasons,
                donors=cohort_donors,
            )
    return result


def _usage_only_evidence_index(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    full_index = build_log_evidence_index(events)
    result: dict[str, list[dict[str, Any]]] = {}
    for event_id, donors in full_index.items():
        accepted = [
            dict(donor)
            for donor in donors
            if donor.get("donor_kind") == "usage"
            and isinstance(donor.get("donor_event_id"), str)
            and bool(donor.get("donor_event_id"))
            and isinstance(donor.get("client"), str)
            and bool(donor.get("client"))
            and isinstance(donor.get("client_session_id"), str)
            and bool(donor.get("client_session_id"))
            and isinstance(donor.get("source_namespace_fingerprint"), str)
            and bool(str(donor.get("source_namespace_fingerprint")).strip())
        ]
        if accepted:
            result[event_id] = accepted
    return result


def _resolve_donor_locators(
    donors: Sequence[Mapping[str, Any]],
    *,
    locators_by_event_id: Mapping[str, tuple[PhysicalLineLocator, ...]],
    events_by_locator: Mapping[PhysicalLineLocator, Mapping[str, Any]],
) -> tuple[tuple[PhysicalLineLocator, ...], str | None]:
    locators: set[PhysicalLineLocator] = set()
    for donor in donors:
        donor_event_id = donor.get("donor_event_id")
        if not isinstance(donor_event_id, str) or not donor_event_id:
            return (), "donor_locator_missing"
        matches = locators_by_event_id.get(donor_event_id, ())
        if not matches:
            return (), "donor_locator_missing"
        for locator in matches:
            physical_event = events_by_locator.get(locator)
            if physical_event is None or not is_local_usage_import_event(
                dict(physical_event)
            ):
                return (), "donor_locator_not_trusted_usage"
            locators.add(locator)
    ordered = tuple(sorted(locators, key=PhysicalLineLocator.sort_key))
    if len(donors) == 1 and len(ordered) != 1:
        return ordered, "donor_locator_not_unique"
    return ordered, None


def _receipt_draft(
    *,
    subject: PhysicalLineLocator,
    disposition: str,
    reasons: Sequence[str],
    donors: Sequence[PhysicalLineLocator] = (),
    recovery: RecoveryIdentity | None = None,
) -> ReceiptDraft:
    return ReceiptDraft(
        design_id=RECOVERY_DESIGN_ID,
        subject=subject,
        disposition=disposition,  # type: ignore[arg-type]
        classifier=RECOVERY_CLASSIFIER,
        classifier_version=RECOVERY_CLASSIFIER_VERSION,
        rule_version=RECOVERY_RULE_VERSION,
        rules_digest=RECOVERY_RULES_DIGEST,
        reason_codes=tuple(sorted(set(reasons))),
        donors=tuple(sorted(set(donors), key=PhysicalLineLocator.sort_key)),
        recovery=recovery,
    )


def classify_legacy_recovery(
    *,
    snapshot: VerifiedSnapshot,
    inventory: PhysicalInventory,
) -> RecoveryClassification:
    """Classify every physical line without mutating snapshot or candidate.

    ``canonical_imported`` and ``canonical_no_effect`` describe how an
    explicitly scoped *row* is routed through the ordinary importer.  They do
    not claim that the row necessarily causes a physical canonical write (an
    import may legitimately deduplicate, quarantine, or have no effect).
    """

    physical = _read_physical_events(snapshot, inventory)
    parsed = [dict(item.event) for item in physical if item.event is not None]
    events_by_locator = {
        item.locator: item.event for item in physical if item.event is not None
    }
    locators_by_event_id_lists: dict[str, list[PhysicalLineLocator]] = defaultdict(list)
    raw_events_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in physical:
        if item.event is None:
            continue
        event_id = _event_id(item.event)
        if event_id is not None:
            locators_by_event_id_lists[event_id].append(item.locator)
            raw_events_by_id[event_id].append(item.event)
    locators_by_event_id = {
        key: tuple(sorted(value, key=PhysicalLineLocator.sort_key))
        for key, value in locators_by_event_id_lists.items()
    }

    usage_index = _usage_only_evidence_index(parsed)

    # Build sanitized section projections without trusting any stored derived
    # marker, then apply the usage-only evidence index with the physical Work
    # cohort key used by this migration design.
    sections = build_work_events(parsed, log_evidence_index={})
    cohort_by_event_id: dict[str, tuple[str, ...]] = {}
    for event_id, values in raw_events_by_id.items():
        if len(values) == 1:
            cohort_by_event_id[event_id] = _cohort_key(values[0])
    apply_log_evidence_to_snapshots(
        sections,
        usage_index,
        group_key=lambda item: cohort_by_event_id.get(str(item.get("event_id") or "")),
    )
    _propagate_log_evidenced_source_constraints(sections)

    task_snapshots = [
        _task_snapshot(event)
        for event in parsed
        if _semantic_kind(event, _metadata(event)) in {"task", "checkpoint", "completion", "blocker", "event"}
        and (
            str(_metadata(event).get("sentinel_semantic_kind") or "") == "task"
            or str(event.get("event_type") or "").startswith("task_")
        )
    ]
    apply_log_evidence_to_snapshots(
        task_snapshots,
        usage_index,
        group_key=lambda item: item.get("work_id"),
    )

    projections_by_event_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for projection in (*sections, *task_snapshots):
        event_id = str(projection.get("event_id") or "")
        if event_id:
            projections_by_event_id[event_id].append(projection)

    donors_by_cohort: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for event_id, donors in usage_index.items():
        cohort = cohort_by_event_id.get(event_id)
        if cohort is not None:
            donors_by_cohort[cohort].extend(donors)

    drafts: list[ReceiptDraft] = []
    for item in physical:
        event = item.event
        if event is None:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="invalid_retained",
                    reasons=("invalid_json_or_nonobject",),
                )
            )
            continue

        namespace = _source_namespace(event)
        semantic_kind = _semantic_kind(event, _metadata(event))
        if namespace is not None:
            imported = semantic_kind is not None or _is_usage_event(event)
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition=(
                        "canonical_imported" if imported else "canonical_no_effect"
                    ),
                    reasons=(
                        "explicit_source_namespace",
                        "supported_canonical_surface"
                        if imported
                        else "unsupported_canonical_surface",
                    ),
                )
            )
            continue

        if semantic_kind is None:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="unsupported_retained",
                    reasons=("unscoped_unsupported_surface",),
                )
            )
            continue

        event_id = _event_id(event)
        projections = projections_by_event_id.get(event_id or "", [])
        if event_id is None or len(projections) != 1:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="no_proof",
                    reasons=("semantic_projection_not_unique",),
                )
            )
            continue
        projection = projections[0]
        direct_donors = usage_index.get(event_id, [])
        cohort = cohort_by_event_id.get(event_id)
        evidence_donors = list(direct_donors)
        if projection.get("log_evidence_ambiguous") and cohort is not None:
            evidence_donors = donors_by_cohort.get(cohort, evidence_donors)
        donor_locators, locator_problem = _resolve_donor_locators(
            evidence_donors,
            locators_by_event_id=locators_by_event_id,
            events_by_locator=events_by_locator,
        ) if evidence_donors else ((), None)

        if projection.get("log_evidence_conflict"):
            if donor_locators and locator_problem is None:
                drafts.append(
                    _receipt_draft(
                        subject=item.locator,
                        disposition="identity_conflict",
                        reasons=("claimed_identity_conflict",),
                        donors=donor_locators,
                    )
                )
            else:
                drafts.append(
                    _receipt_draft(
                        subject=item.locator,
                        disposition="no_proof",
                        reasons=(locator_problem or "trusted_donor_not_reconstructable",),
                    )
                )
            continue

        if projection.get("log_evidence_ambiguous"):
            if donor_locators:
                drafts.append(
                    _receipt_draft(
                        subject=item.locator,
                        disposition="ambiguous",
                        reasons=(
                            "multiple_candidate_sessions",
                            *(tuple([locator_problem]) if locator_problem else ()),
                        ),
                        donors=donor_locators,
                    )
                )
            else:
                drafts.append(
                    _receipt_draft(
                        subject=item.locator,
                        disposition="no_proof",
                        reasons=(locator_problem or "trusted_donor_not_reconstructable",),
                    )
                )
            continue

        inherited_namespace = projection.get(
            "log_evidenced_source_namespace_fingerprint"
        )
        if not direct_donors and isinstance(inherited_namespace, str) and inherited_namespace:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="namespace_only",
                    reasons=("work_cohort_namespace_only",),
                )
            )
            continue

        if len(direct_donors) != 1:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="no_proof",
                    reasons=("no_unique_trusted_usage_donor",),
                )
            )
            continue

        if locator_problem == "donor_locator_not_unique" and donor_locators:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="ambiguous",
                    reasons=(locator_problem,),
                    donors=donor_locators,
                )
            )
            continue
        if locator_problem is not None or len(donor_locators) != 1:
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="no_proof",
                    reasons=(locator_problem or "trusted_donor_not_reconstructable",),
                )
            )
            continue

        donor = direct_donors[0]
        client = donor.get("client")
        source_namespace = donor.get("source_namespace_fingerprint")
        client_session_id = donor.get("client_session_id")
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (client, source_namespace, client_session_id)
        ):
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="no_proof",
                    reasons=("trusted_donor_identity_incomplete",),
                )
            )
            continue
        semantic_namespaces = _semantic_namespace_claims(event)
        declared_explicit_scope = (
            str(_metadata(event).get("identity_scope_state") or "")
            .strip()
            .lower()
            == "explicit"
        )
        if (
            declared_explicit_scope and not semantic_namespaces
        ) or (
            semantic_namespaces
            and semantic_namespaces != frozenset({source_namespace.strip()})
        ):
            drafts.append(
                _receipt_draft(
                    subject=item.locator,
                    disposition="identity_conflict",
                    reasons=("claimed_semantic_namespace_conflict",),
                    donors=donor_locators,
                )
            )
            continue
        drafts.append(
            _receipt_draft(
                subject=item.locator,
                disposition="candidate_recovery",
                reasons=("unique_trusted_usage_donor",),
                donors=donor_locators,
                recovery=RecoveryIdentity(
                    client=client.strip(),
                    source_namespace_fingerprint=source_namespace.strip(),
                    client_session_id=client_session_id.strip(),
                ),
            )
        )

    drafts = _quarantine_conflicting_work_identity_recoveries(
        drafts,
        events_by_locator=events_by_locator,
    )
    ordered = tuple(sorted(drafts, key=lambda draft: draft.subject.sort_key()))
    counts = Counter(draft.disposition for draft in ordered)
    return RecoveryClassification(
        inventory=inventory,
        drafts=ordered,
        parsed_object_events=len(parsed),
        disposition_counts=counts,
    )


def publish_verified_recovery_plan(
    *,
    snapshot: VerifiedSnapshot,
    archive: VerifiedMigrationArchive,
    inventory: PhysicalInventory | None = None,
) -> tuple[RecoveryClassification, VerifiedRecoveryPlan]:
    """Classify, publish immutable receipts, and seal a complete plan."""

    if not isinstance(archive, VerifiedMigrationArchive):
        raise LegacyRecoveryError("recovery plan requires a VerifiedMigrationArchive")
    inventory = inventory or archive.inventory
    if archive.inventory != inventory or archive.snapshot.manifest_digest != snapshot.manifest_digest:
        raise LegacyRecoveryError("archive, inventory, and snapshot bindings differ")
    if archive.receipts():
        raise LegacyRecoveryError(
            "recovery publication requires a fresh archive with no receipts"
        )
    classification = classify_legacy_recovery(snapshot=snapshot, inventory=inventory)
    archive.publish_receipts(classification.drafts)
    plan = archive.sealed_plan(design_id=RECOVERY_DESIGN_ID)
    _verify_plan_matches_classifier(plan, classification=classification)
    return classification, plan


def _verify_plan_matches_classifier(
    plan: VerifiedRecoveryPlan,
    *,
    classification: RecoveryClassification | None = None,
) -> RecoveryClassification:
    if not isinstance(plan, VerifiedRecoveryPlan):
        raise LegacyRecoveryError("replay requires a VerifiedRecoveryPlan")
    if plan.design_id != RECOVERY_DESIGN_ID:
        raise LegacyRecoveryError("sealed plan uses an unsupported recovery design")
    plan.verify_unchanged()
    expected = classification or classify_legacy_recovery(
        snapshot=plan.archive.snapshot,
        inventory=plan.archive.inventory,
    )
    expected_by_subject = {
        draft.subject: draft.decision_dict() for draft in expected.drafts
    }
    actual_by_subject = {
        receipt.draft.subject: receipt.draft.decision_dict()
        for receipt, _digest in plan.archive.receipts()
        if receipt.draft.design_id == RECOVERY_DESIGN_ID
    }
    if actual_by_subject != expected_by_subject:
        raise LegacyRecoveryError(
            "sealed recovery receipts differ from the frozen classifier output"
        )
    return expected


def _legacy_namespace_digest(fingerprint: str) -> bytes:
    return hashlib.sha256(
        b"legacy-explicit-namespace-v1\x00" + fingerprint.strip().encode("utf-8")
    ).digest()


def _prepare_recovered_claims(
    plan: VerifiedRecoveryPlan,
    repository: CanonicalRepository,
) -> tuple[RecoveredWorkClaimInput, ...]:
    _verify_plan_matches_classifier(plan)
    candidates = []
    for locator, disposition in plan.iter_lines():
        if disposition != "candidate_recovery":
            continue
        decision = plan.recovery_for(locator)
        assert decision is not None
        candidates.append((locator, decision))
    events_by_locator = plan.read_events(
        tuple(decision for _locator, decision in candidates)
    )

    prepared: list[RecoveredWorkClaimInput] = []
    for locator, decision in candidates:
        identity = decision.recovery
        source = repository.get_source(
            identity.client,
            LEGACY_EVENTS_REPRESENTATION,
            _legacy_namespace_digest(identity.source_namespace_fingerprint),
        )
        if source is None:
            raise LegacyRecoveryError(
                "recovery donor source was not imported into the candidate"
            )
        if (
            source.namespace_scheme != _EXPLICIT_NAMESPACE_SCHEME
            or source.adapter != LEGACY_EVENTS_ADAPTER
        ):
            raise LegacyRecoveryError(
                "recovery donor source does not match the ordinary legacy importer"
            )
        normalized_session_id = normalized_local_usage_session_id(
            identity.client,
            identity.client_session_id,
        )
        session = repository.get_session(
            source.source_instance_id,
            normalized_session_id,
        )
        if session is None:
            raise LegacyRecoveryError(
                "recovery donor session was not imported into the candidate"
            )
        event = events_by_locator[locator]
        metadata = _metadata(event)
        issues: list[_Issue] = []
        record = _EventRecord(
            line_number=locator.line_number,
            event=event,
            metadata=metadata,
            source_key=None,  # type: ignore[arg-type]
        )
        claim = _work_claim_input(
            record,
            source_instance_id=source.source_instance_id,
            repository=repository,
            issues=issues,
        )
        if claim is None or issues:
            raise LegacyRecoveryError(
                "sealed candidate row cannot be converted to one recovery claim"
            )
        claim = replace(
            claim,
            fact=replace(claim.fact, transport=RECOVERY_FACT_TRANSPORT),
        )
        prepared.append(
            RecoveredWorkClaimInput(
                claim=claim,
                session_id=session.session_id,
            )
        )
    return tuple(prepared)


def append_verified_recovery_claims(
    *,
    plan: VerifiedRecoveryPlan,
    repository: CanonicalRepository,
) -> RecoveryAppendReport:
    """Append only sealed candidate decisions, after all lookups succeed."""

    prepared = _prepare_recovered_claims(plan, repository)
    sequence_before = repository.canonical_sequence()
    changes_before = repository.connection.total_changes
    results = tuple(repository.append_recovered_work_claims(prepared))
    if len(results) != len(prepared):
        raise LegacyRecoveryError("repository returned an incomplete recovery result set")
    fact_counts: Counter[str] = Counter()
    link_counts: Counter[str] = Counter()
    for fact, link in results:
        if fact.disposition not in {"inserted", "noop"} or link.disposition not in {
            "inserted",
            "noop",
        }:
            raise LegacyRecoveryError("repository did not accept a sealed recovery claim")
        fact_counts[fact.disposition] += 1
        link_counts[link.disposition] += 1
    plan.verify_unchanged()
    return RecoveryAppendReport(
        candidate_rows=len(prepared),
        fact_dispositions=fact_counts,
        link_dispositions=link_counts,
        canonical_sequence_before=sequence_before,
        canonical_sequence_after=repository.canonical_sequence(),
        canonical_writes=repository.connection.total_changes - changes_before,
    )


def replay_verified_recovery(
    *,
    plan: VerifiedRecoveryPlan,
    store: CanonicalStore,
    scratch_root: Path | str,
) -> RecoveryReplayReport:
    """Import scoped rows, then atomically replay sealed high-confidence rows."""

    if not isinstance(store, CanonicalStore) or store.read_only:
        raise LegacyRecoveryError("recovery destination must be a writable CanonicalStore")
    _verify_plan_matches_classifier(plan)
    repository = store.repository()
    sequence_before = repository.canonical_sequence()
    changes_before = store.connection.total_changes
    scoped_reports = tuple(
        import_legacy_snapshot(
            snapshot=plan.archive.snapshot,
            store=store,
            scratch_root=scratch_root,
            source_file=item.relative_path,
        )
        for item in plan.archive.inventory.files
    )
    recovery = append_verified_recovery_claims(
        plan=plan,
        repository=repository,
    )
    projection_rebuilt = recovery.canonical_sequence_after != recovery.canonical_sequence_before
    if projection_rebuilt:
        projection = repository.rebuild_minimal_read_models()
    else:
        projection = {
            "task_count": int(
                repository.connection.execute(
                    "SELECT COUNT(*) FROM rm_task_current"
                ).fetchone()[0]
            ),
            "usage_day_count": int(
                repository.connection.execute(
                    "SELECT COUNT(*) FROM rm_usage_day"
                ).fetchone()[0]
            ),
            "built_through_sequence": repository.canonical_sequence(),
        }
    plan.verify_unchanged()
    return RecoveryReplayReport(
        scoped_imports=scoped_reports,
        recovery=recovery,
        projection_rebuilt=projection_rebuilt,
        projection=projection,
        canonical_sequence_before=sequence_before,
        canonical_sequence_after=repository.canonical_sequence(),
        canonical_writes=store.connection.total_changes - changes_before,
    )


__all__ = [
    "LegacyRecoveryError",
    "RECOVERY_CLASSIFIER",
    "RECOVERY_CLASSIFIER_VERSION",
    "RECOVERY_DESIGN_ID",
    "RECOVERY_RULES_DIGEST",
    "RECOVERY_RULE_VERSION",
    "RecoveryAppendReport",
    "RecoveryClassification",
    "RecoveryReplayReport",
    "append_verified_recovery_claims",
    "classify_legacy_recovery",
    "publish_verified_recovery_plan",
    "replay_verified_recovery",
]
