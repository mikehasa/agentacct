from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agentacct.canonical.legacy_import import (
    LEGACY_EVENTS_ADAPTER,
    LEGACY_EVENTS_REPRESENTATION,
)
from agentacct.canonical.legacy_recovery import (
    LegacyRecoveryError,
    append_verified_recovery_claims,
    publish_verified_recovery_plan,
    replay_verified_recovery,
)
from agentacct.canonical.migration_archive import (
    VerifiedMigrationArchive,
    VerifiedRecoveryPlan,
    build_migration_archive,
    scan_snapshot_lines,
)
from agentacct.canonical.snapshot import VerifiedSnapshot
from agentacct.canonical.sqlite import CanonicalStore
from agentacct.canonical.types import (
    RECOVERY_FACT_TRANSPORT,
    RECOVERY_LINK_METHOD,
    RECOVERY_LINK_RULE_VERSION,
    SourceInstanceInput,
)
from agentacct.usage_truth import (
    mark_trusted_local_session_observation_event,
    mark_trusted_local_usage_import_event,
)


NAMESPACE = "synthetic-codex-home-a"
OTHER_NAMESPACE = "synthetic-codex-home-b"
MAIN_SESSION = "synthetic-session-main"

CANDIDATE_SECTION = "evt_100001"
NAMESPACE_ONLY = "evt_100002"
CANDIDATE_TASK = "evt_100003"
IDENTITY_CONFLICT = "evt_100004"
AMBIGUOUS = "evt_100005"
NO_PROOF = "evt_100006"
FORGED_MARKERS = "evt_100007"
OBSERVATION_ONLY = "evt_100008"
SCOPED_SECTION = "evt_100009"
SCOPED_NO_EFFECT = "evt_10000a"
UNSUPPORTED = "evt_10000b"


def _manifest_entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _verified_snapshot(tmp_path: Path, payload: bytes, *, name: str) -> VerifiedSnapshot:
    root = tmp_path / f"{name}-snapshot"
    root.mkdir(mode=0o700)
    (root / "events.jsonl").write_bytes(payload)
    manifest = tmp_path / f"{name}-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [_manifest_entry("events.jsonl", payload)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return VerifiedSnapshot.verify(root.resolve(), manifest.resolve())


def _section(
    event_id: str,
    *,
    work_id: str,
    section_id: str,
    status: str = "started",
    namespace: str | None = None,
    session_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "sentinel_semantic_kind": "section",
        "section_status": status,
        "section_title": f"Synthetic section {event_id}",
        "work_id": work_id,
        "section_id": section_id,
    }
    if namespace is not None:
        metadata["source_namespace_fingerprint"] = namespace
    if session_id is not None:
        metadata.update(
            {
                "client": "codex",
                "client_session_id": session_id,
            }
        )
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "event_id": event_id,
        "event_type": f"section_{status}",
        "source": "synthetic-recovery-test",
        "run_id": "synthetic-recovery-run",
        "created_at": 100,
        "metadata": metadata,
    }


def _task(event_id: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "task_started",
        "source": "synthetic-recovery-test",
        "run_id": "synthetic-recovery-run",
        "created_at": 101,
        "metadata": {
            "sentinel_semantic_kind": "task",
            "task_status": "started",
            "task_title": "Synthetic recovered Task",
            "work_id": "recovered-task-work",
            "section_id": "recovered-task-section",
        },
    }


def _usage_donor(
    event_id: str,
    *,
    session_id: str,
    evidenced_event_ids: list[str],
    namespace: str = NAMESPACE,
    created_at: int = 200,
) -> dict[str, Any]:
    event = {
        "event_id": event_id,
        "event_type": "model_usage",
        "source": "codex-local-session-import",
        "provider": "synthetic-provider",
        "model": "synthetic-model",
        "usage_confidence": "client_reported",
        "estimated_input_tokens": 5,
        "estimated_output_tokens": 1,
        "created_at": created_at,
        "metadata": {
            "client": "codex",
            "client_session_id": session_id,
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "started_at": 90,
            "updated_at": created_at,
            "usage_update_semantics": "codex_rollout_token_count_events",
            "usage_additive": True,
            "usage_granularity": "session",
            "precedence_role": "authoritative",
            "evidenced_event_ids": evidenced_event_ids,
            "evidenced_event_id_total": len(evidenced_event_ids),
            "evidenced_outputs_skipped": 0,
            "input_tokens_reported": True,
            "output_tokens_reported": True,
            "cached_input_tokens": 0,
            "cached_input_tokens_reported": True,
            "cache_creation_input_tokens": 0,
            "cache_creation_input_tokens_reported": True,
            "cache_read_input_tokens": 0,
            "cache_read_input_tokens_reported": True,
            "reasoning_output_tokens": 0,
            "reasoning_output_tokens_reported": True,
            "total_tokens": 6,
            "total_tokens_reported": True,
        },
    }
    return mark_trusted_local_usage_import_event(event)


def _observation_donor() -> dict[str, Any]:
    return mark_trusted_local_session_observation_event(
        {
            "event_id": "observation-donor",
            "event_type": "session_observed",
            "source": "codex-local-session-import",
            "created_at": 205,
            "metadata": {
                "client": "codex",
                "client_session_id": "observation-only-session",
                "client_session_kind": "root",
                "source_namespace_fingerprint": OTHER_NAMESPACE,
                "source_parse_complete": True,
                "started_at": 90,
                "updated_at": 205,
                "evidenced_event_ids": [OBSERVATION_ONLY],
                "evidenced_event_id_total": 1,
                "evidenced_outputs_skipped": 0,
            },
        }
    )


def _synthetic_payload() -> tuple[bytes, tuple[str, ...]]:
    events: list[dict[str, Any]] = [
        _section(
            CANDIDATE_SECTION,
            work_id="recovered-section-work",
            section_id="recovered-section",
            extra_metadata={"session_namespace_fingerprint": NAMESPACE},
        ),
        _section(
            NAMESPACE_ONLY,
            work_id="recovered-section-work",
            section_id="recovered-section",
            status="completed",
        ),
        _task(CANDIDATE_TASK),
        _section(
            IDENTITY_CONFLICT,
            work_id="identity-conflict-work",
            section_id="identity-conflict-section",
            session_id="claimed-wrong-session",
        ),
        _section(
            AMBIGUOUS,
            work_id="ambiguous-work",
            section_id="ambiguous-section",
        ),
        _section(
            NO_PROOF,
            work_id="no-proof-work",
            section_id="no-proof-section",
        ),
        _section(
            FORGED_MARKERS,
            work_id="forged-work",
            section_id="forged-section",
            extra_metadata={
                "log_evidenced_source_namespace_fingerprint": NAMESPACE,
                "log_evidenced_by_usage_event_id": "forged-usage-donor",
                "log_evidenced_join_keys": ["client_session_id"],
                "log_evidence_donor_kind": "usage",
            },
        ),
        _section(
            OBSERVATION_ONLY,
            work_id="observation-only-work",
            section_id="observation-only-section",
        ),
        _section(
            SCOPED_SECTION,
            work_id="scoped-work",
            section_id="scoped-section",
            namespace=NAMESPACE,
        ),
        {
            "event_id": SCOPED_NO_EFFECT,
            "event_type": "diagnostic_note",
            "source": "synthetic-recovery-test",
            "created_at": 110,
            "metadata": {"source_namespace_fingerprint": NAMESPACE},
        },
        {
            "event_id": UNSUPPORTED,
            "event_type": "machine_check",
            "source": "synthetic-recovery-test",
            "created_at": 111,
            "metadata": {"summary": "unscoped unsupported row"},
        },
        _usage_donor(
            "usage-main-donor",
            session_id=MAIN_SESSION,
            evidenced_event_ids=[
                CANDIDATE_SECTION,
                CANDIDATE_TASK,
                IDENTITY_CONFLICT,
            ],
        ),
        _usage_donor(
            "usage-ambiguous-a",
            session_id="ambiguous-session-a",
            evidenced_event_ids=[AMBIGUOUS],
            created_at=201,
        ),
        _usage_donor(
            "usage-ambiguous-b",
            session_id="ambiguous-session-b",
            evidenced_event_ids=[AMBIGUOUS],
            created_at=202,
        ),
        _observation_donor(),
    ]
    encoded = [
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    ]
    encoded.append(b'{"malformed":\n')
    labels = tuple(str(event["event_id"]) for event in events) + ("__invalid__",)
    return b"".join(encoded), labels


def _sealed_plan(
    tmp_path: Path,
    *,
    name: str,
) -> tuple[
    VerifiedSnapshot,
    VerifiedMigrationArchive,
    VerifiedRecoveryPlan,
    tuple[str, ...],
]:
    payload, labels = _synthetic_payload()
    snapshot = _verified_snapshot(tmp_path, payload, name=name)
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / f"{name}-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    classification, plan = publish_verified_recovery_plan(
        snapshot=snapshot,
        archive=archive,
        inventory=inventory,
    )
    assert classification.inventory.total_lines == len(labels)
    return snapshot, archive, plan, labels


def _drafts_by_label(plan: VerifiedRecoveryPlan, labels: tuple[str, ...]):
    chain = [
        receipt.draft
        for receipt, _digest in plan.archive.receipts()
        if receipt.draft.design_id == plan.design_id
    ]
    by_subject = {draft.subject: draft for draft in chain}
    return {
        label: by_subject[locator]
        for label, locator in zip(labels, plan.archive.inventory.lines, strict=True)
    }


def _legacy_namespace_digest(fingerprint: str) -> bytes:
    return hashlib.sha256(
        b"legacy-explicit-namespace-v1\x00" + fingerprint.encode("utf-8")
    ).digest()


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(mode=0o700)
    os.chmod(scratch, 0o700)
    return scratch.resolve()


def test_manifest_bound_classifier_covers_success_and_six_refusal_classes(
    tmp_path: Path,
) -> None:
    snapshot, archive, plan, labels = _sealed_plan(tmp_path, name="classification")
    try:
        drafts = _drafts_by_label(plan, labels)

        assert drafts[CANDIDATE_SECTION].disposition == "candidate_recovery"
        assert drafts[CANDIDATE_TASK].disposition == "candidate_recovery"
        assert drafts[NAMESPACE_ONLY].disposition == "namespace_only"
        assert drafts[IDENTITY_CONFLICT].disposition == "identity_conflict"
        assert drafts[AMBIGUOUS].disposition == "ambiguous"
        assert drafts[NO_PROOF].disposition == "no_proof"
        assert drafts[UNSUPPORTED].disposition == "unsupported_retained"
        assert drafts["__invalid__"].disposition == "invalid_retained"
        assert drafts[SCOPED_SECTION].disposition == "canonical_imported"
        assert drafts[SCOPED_NO_EFFECT].disposition == "canonical_no_effect"

        # Stored derived markers have no authority, and a trusted
        # observation-only row is intentionally weaker than a usage donor.
        assert drafts[FORGED_MARKERS].disposition == "no_proof"
        assert drafts[OBSERVATION_ONLY].disposition == "no_proof"

        candidates = [
            draft
            for draft in drafts.values()
            if draft.disposition == "candidate_recovery"
        ]
        assert len(candidates) == 2
        assert all(len(draft.donors) == 1 for draft in candidates)
        assert all(draft.recovery is not None for draft in candidates)
        assert {
            (
                draft.recovery.client,
                draft.recovery.source_namespace_fingerprint,
                draft.recovery.client_session_id,
                draft.recovery.fact_transport,
                draft.recovery.link_method,
                draft.recovery.link_confidence,
                draft.recovery.rule_version,
            )
            for draft in candidates
            if draft.recovery is not None
        } == {
            (
                "codex",
                NAMESPACE,
                MAIN_SESSION,
                RECOVERY_FACT_TRANSPORT,
                RECOVERY_LINK_METHOD,
                "high",
                RECOVERY_LINK_RULE_VERSION,
            )
        }
        assert snapshot.verify_unchanged() is snapshot
        plan.verify_unchanged()
    finally:
        archive.close()


def test_recovery_classifier_quarantines_cross_project_work_cohort(
    tmp_path: Path,
) -> None:
    first_id = "evt_200001"
    second_id = "evt_200002"
    third_id = "evt_200003"
    donor_ids = (
        "usage-project-conflict-donor-a",
        "usage-project-conflict-donor-b",
        "usage-project-conflict-donor-c",
    )
    events = [
        _section(
            first_id,
            work_id="raw-alias-a",
            section_id="shared-recovered-section",
            extra_metadata={"project_dir": "/private/project-a"},
        ),
        _section(
            second_id,
            work_id="raw-alias-b",
            section_id="shared-recovered-section",
            status="completed",
            extra_metadata={"project_dir": "/private/project-b"},
        ),
        _section(
            third_id,
            work_id="raw-alias-c",
            section_id="shared-recovered-section",
            status="checkpoint",
        ),
        _usage_donor(
            donor_ids[0],
            session_id=MAIN_SESSION,
            evidenced_event_ids=[first_id],
            created_at=201,
        ),
        _usage_donor(
            donor_ids[1],
            session_id=MAIN_SESSION,
            evidenced_event_ids=[second_id],
            created_at=202,
        ),
        _usage_donor(
            donor_ids[2],
            session_id=MAIN_SESSION,
            evidenced_event_ids=[third_id],
            created_at=203,
        ),
    ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    snapshot = _verified_snapshot(tmp_path, payload, name="project-conflict")
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / "project-conflict-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    try:
        classification, plan = publish_verified_recovery_plan(
            snapshot=snapshot,
            archive=archive,
            inventory=inventory,
        )
        drafts = _drafts_by_label(
            plan,
            (first_id, second_id, third_id, *donor_ids),
        )

        assert classification.candidate_recovery_rows == 0
        expected_donors = tuple(
            sorted(
                (drafts[donor_id].subject for donor_id in donor_ids),
                key=lambda locator: locator.sort_key(),
            )
        )
        for event_id in (first_id, second_id, third_id):
            assert drafts[event_id].disposition == "ambiguous", (
                drafts[event_id].reason_codes
            )
            assert drafts[event_id].reason_codes == (
                "conflicting_project_identity",
            )
            assert drafts[event_id].donors == expected_donors
            assert drafts[event_id].recovery is None
        assert all(
            drafts[donor_id].disposition == "canonical_imported"
            for donor_id in donor_ids
        )
        plan.verify_unchanged()
    finally:
        archive.close()


def test_project_guard_includes_explicitly_scoped_sibling(
    tmp_path: Path,
) -> None:
    candidate_id = "evt_205001"
    scoped_id = "evt_205002"
    donor_id = "usage-project-scoped-sibling-donor"
    events = [
        _section(
            candidate_id,
            work_id="raw-candidate-work",
            section_id="shared-recovered-section",
            extra_metadata={"project_dir": "/private/project-a"},
        ),
        _section(
            scoped_id,
            work_id="raw-scoped-work",
            section_id="shared-recovered-section",
            namespace=NAMESPACE,
            session_id=MAIN_SESSION,
            status="completed",
            extra_metadata={"project_dir": "/private/project-b"},
        ),
        _usage_donor(
            donor_id,
            session_id=MAIN_SESSION,
            evidenced_event_ids=[candidate_id],
        ),
    ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    snapshot = _verified_snapshot(tmp_path, payload, name="scoped-project-conflict")
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / "scoped-project-conflict-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    try:
        classification, plan = publish_verified_recovery_plan(
            snapshot=snapshot,
            archive=archive,
            inventory=inventory,
        )
        drafts = _drafts_by_label(plan, (candidate_id, scoped_id, donor_id))

        assert classification.candidate_recovery_rows == 0
        assert drafts[candidate_id].disposition == "ambiguous"
        assert drafts[candidate_id].reason_codes == (
            "conflicting_project_identity",
        )
        assert drafts[candidate_id].donors == (drafts[donor_id].subject,)
        assert drafts[scoped_id].disposition == "canonical_imported"
        assert drafts[donor_id].disposition == "canonical_imported"
        plan.verify_unchanged()
    finally:
        archive.close()


def test_namespace_guard_includes_explicitly_scoped_sibling(
    tmp_path: Path,
) -> None:
    candidate_id = "evt_205101"
    scoped_id = "evt_205102"
    donor_id = "usage-namespace-scoped-sibling-donor"
    events = [
        _section(
            candidate_id,
            work_id="raw-candidate-work",
            section_id="shared-recovered-section",
        ),
        _section(
            scoped_id,
            work_id="raw-scoped-work",
            section_id="shared-recovered-section",
            namespace=NAMESPACE,
            session_id=MAIN_SESSION,
            status="completed",
            extra_metadata={
                "session_namespace_fingerprint": OTHER_NAMESPACE,
            },
        ),
        _usage_donor(
            donor_id,
            session_id=MAIN_SESSION,
            evidenced_event_ids=[candidate_id],
        ),
    ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    snapshot = _verified_snapshot(tmp_path, payload, name="scoped-namespace-conflict")
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / "scoped-namespace-conflict-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    try:
        classification, plan = publish_verified_recovery_plan(
            snapshot=snapshot,
            archive=archive,
            inventory=inventory,
        )
        drafts = _drafts_by_label(plan, (candidate_id, scoped_id, donor_id))

        assert classification.candidate_recovery_rows == 0
        assert drafts[candidate_id].disposition == "identity_conflict"
        assert drafts[candidate_id].reason_codes == (
            "conflicting_namespace_identity",
        )
        assert drafts[candidate_id].donors == (drafts[donor_id].subject,)
        assert drafts[candidate_id].recovery is None
        assert drafts[scoped_id].disposition == "canonical_imported"
        assert drafts[donor_id].disposition == "canonical_imported"
        plan.verify_unchanged()
    finally:
        archive.close()


def test_project_guard_uses_canonical_section_id_normalization(
    tmp_path: Path,
) -> None:
    candidate_id = "evt_206001"
    scoped_id = "evt_206002"
    donor_id = "usage-normalized-section-project-donor"
    shared_prefix = "s" * 256
    events = [
        _section(
            candidate_id,
            work_id="raw-candidate-work",
            section_id=f"{shared_prefix}a",
            extra_metadata={"project_dir": "/private/project-a"},
        ),
        _section(
            scoped_id,
            work_id="raw-scoped-work",
            section_id=f"{shared_prefix}b",
            namespace=NAMESPACE,
            session_id=MAIN_SESSION,
            status="completed",
            extra_metadata={"project_dir": "/private/project-b"},
        ),
        _usage_donor(
            donor_id,
            session_id=MAIN_SESSION,
            evidenced_event_ids=[candidate_id],
        ),
    ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    snapshot = _verified_snapshot(tmp_path, payload, name="normalized-section-conflict")
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / "normalized-section-conflict-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    try:
        classification, plan = publish_verified_recovery_plan(
            snapshot=snapshot,
            archive=archive,
            inventory=inventory,
        )
        drafts = _drafts_by_label(plan, (candidate_id, scoped_id, donor_id))

        assert classification.candidate_recovery_rows == 0
        assert drafts[candidate_id].disposition == "ambiguous"
        assert drafts[candidate_id].reason_codes == (
            "conflicting_project_identity",
        )
        assert drafts[scoped_id].disposition == "canonical_imported"
        plan.verify_unchanged()
    finally:
        archive.close()


@pytest.mark.parametrize(
    ("field_name", "field_value", "candidate_id", "expected_disposition"),
    (
        (
            "namespace_fingerprint",
            OTHER_NAMESPACE,
            "evt_207001",
            "identity_conflict",
        ),
        (
            "session_namespace_fingerprint",
            OTHER_NAMESPACE,
            "evt_207002",
            "identity_conflict",
        ),
        ("identity_scope_state", "explicit", "evt_207003", "no_proof"),
    ),
)
def test_recovery_classifier_rejects_claimed_semantic_namespace_conflict(
    tmp_path: Path,
    field_name: str,
    field_value: str,
    candidate_id: str,
    expected_disposition: str,
) -> None:
    donor_id = f"usage-semantic-namespace-{field_name}"
    events = [
        _section(
            candidate_id,
            work_id="semantic-namespace-work",
            section_id="semantic-namespace-section",
            extra_metadata={field_name: field_value},
        ),
        _usage_donor(
            donor_id,
            session_id=MAIN_SESSION,
            evidenced_event_ids=[candidate_id],
        ),
    ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    snapshot = _verified_snapshot(tmp_path, payload, name=field_name)
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / f"{field_name}-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    try:
        classification, plan = publish_verified_recovery_plan(
            snapshot=snapshot,
            archive=archive,
            inventory=inventory,
        )
        drafts = _drafts_by_label(plan, (candidate_id, donor_id))

        assert classification.candidate_recovery_rows == 0
        assert drafts[candidate_id].disposition == expected_disposition
        if expected_disposition == "identity_conflict":
            assert drafts[candidate_id].reason_codes == (
                "claimed_semantic_namespace_conflict",
            )
            assert drafts[candidate_id].donors == (drafts[donor_id].subject,)
        assert drafts[candidate_id].recovery is None
        plan.verify_unchanged()
    finally:
        archive.close()


def test_recovery_publication_requires_fresh_archive(tmp_path: Path) -> None:
    snapshot, archive, plan, _labels = _sealed_plan(tmp_path, name="fresh-only")
    try:
        plan.verify_unchanged()
        with pytest.raises(
            LegacyRecoveryError,
            match="fresh archive with no receipts",
        ):
            publish_verified_recovery_plan(
                snapshot=snapshot,
                archive=archive,
                inventory=archive.inventory,
            )
    finally:
        archive.close()


def test_project_guard_stays_inside_one_recovered_work_boundary(
    tmp_path: Path,
) -> None:
    section_specs = (
        ("evt_210001", "section-a", MAIN_SESSION, "/private/project-a"),
        ("evt_210002", "section-b", MAIN_SESSION, "/private/project-b"),
        ("evt_210003", "shared-section", MAIN_SESSION, "/private/project-a"),
        ("evt_210004", "shared-section", "other-session", "/private/project-b"),
    )
    section_events = [
        _section(
            event_id,
            work_id=f"raw-{event_id}",
            section_id=section_id,
            extra_metadata={"project_dir": project_dir},
        )
        for event_id, section_id, _session_id, project_dir in section_specs
    ]
    task_ids = ("evt_210005", "evt_210006")
    task_events = []
    for event_id, project_dir in zip(
        task_ids,
        ("/private/project-a", "/private/project-b"),
        strict=True,
    ):
        event = _task(event_id)
        event["metadata"] = {
            **event["metadata"],
            "project_dir": project_dir,
        }
        task_events.append(event)

    candidate_ids = tuple(spec[0] for spec in section_specs) + task_ids
    donor_ids = tuple(f"usage-boundary-{index}" for index in range(6))
    donor_sessions = tuple(spec[2] for spec in section_specs) + (
        MAIN_SESSION,
        MAIN_SESSION,
    )
    donors = [
        _usage_donor(
            donor_id,
            session_id=session_id,
            evidenced_event_ids=[candidate_id],
            created_at=220 + index,
        )
        for index, (donor_id, session_id, candidate_id) in enumerate(
            zip(donor_ids, donor_sessions, candidate_ids, strict=True)
        )
    ]
    events = [*section_events, *task_events, *donors]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    snapshot = _verified_snapshot(tmp_path, payload, name="project-boundaries")
    inventory = scan_snapshot_lines(snapshot)
    archive_root = tmp_path / "project-boundaries-archive"
    archive_root.mkdir(mode=0o700)
    os.chmod(archive_root, 0o700)
    archive = build_migration_archive(
        snapshot=snapshot,
        archive_root=archive_root.resolve(),
        inventory=inventory,
    )
    try:
        classification, plan = publish_verified_recovery_plan(
            snapshot=snapshot,
            archive=archive,
            inventory=inventory,
        )
        drafts = _drafts_by_label(plan, (*candidate_ids, *donor_ids))

        assert classification.candidate_recovery_rows == len(candidate_ids)
        assert all(
            drafts[event_id].disposition == "candidate_recovery"
            for event_id in candidate_ids
        )
        assert all(
            drafts[donor_id].disposition == "canonical_imported"
            for donor_id in donor_ids
        )
        plan.verify_unchanged()
    finally:
        archive.close()


def test_replay_imports_scoped_rows_then_recovers_section_and_task_as_high_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _snapshot, archive, plan, _labels = _sealed_plan(tmp_path, name="replay")
    scratch = _scratch(tmp_path, "replay-candidate")
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        read_batches: list[int] = []
        original_read_events = plan.read_events

        def tracked_read_events(decisions):
            read_batches.append(len(decisions))
            return original_read_events(decisions)

        monkeypatch.setattr(plan, "read_events", tracked_read_events)
        first = replay_verified_recovery(
            plan=plan,
            store=store,
            scratch_root=scratch,
        )

        assert read_batches == [2]
        assert first.recovery.candidate_rows == 2
        assert first.recovery.fact_dispositions == {"inserted": 2}
        assert first.recovery.link_dispositions == {"inserted": 2}
        assert first.projection_rebuilt is True
        recovered_rows = store.connection.execute(
            "SELECT fact.source_event_id, fact.transport, claim.event_kind, "
            "link.method, link.confidence, link.rule_version, "
            "source.namespace_scheme, session.client_session_id "
            "FROM facts fact "
            "JOIN work_claims claim USING(fact_id) "
            "JOIN fact_session_links link USING(fact_id) "
            "JOIN source_instances source USING(source_instance_id) "
            "JOIN sessions session USING(session_id) "
            "WHERE fact.transport = ? ORDER BY fact.source_event_id",
            (RECOVERY_FACT_TRANSPORT,),
        ).fetchall()
        assert [tuple(row) for row in recovered_rows] == [
            (
                CANDIDATE_SECTION,
                RECOVERY_FACT_TRANSPORT,
                "section",
                RECOVERY_LINK_METHOD,
                "high",
                RECOVERY_LINK_RULE_VERSION,
                "legacy-explicit-fingerprint-sha256-v1",
                MAIN_SESSION,
            ),
            (
                CANDIDATE_TASK,
                RECOVERY_FACT_TRANSPORT,
                "task",
                RECOVERY_LINK_METHOD,
                "high",
                RECOVERY_LINK_RULE_VERSION,
                "legacy-explicit-fingerprint-sha256-v1",
                MAIN_SESSION,
            ),
        ]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM fact_session_links link "
            "JOIN facts fact USING(fact_id) "
            "WHERE fact.transport = ? AND link.confidence = 'exact'",
            (RECOVERY_FACT_TRANSPORT,),
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM source_instances "
            "WHERE namespace_scheme = 'snapshot-scoped-unresolved-v1'"
        ).fetchone()[0] == 0

        task_ids_before = tuple(
            row[0]
            for row in store.connection.execute(
                "SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id"
            )
        )
        assert task_ids_before
        assert all(
            task_id.startswith("task_") and len(task_id) == 37
            for task_id in task_ids_before
        )
        counts_before = dict(store.repository().table_counts())
        sequence_before = store.repository().canonical_sequence()
        changes_before = store.connection.total_changes

        second = replay_verified_recovery(
            plan=plan,
            store=store,
            scratch_root=scratch,
        )

        assert read_batches == [2, 2]
        assert second.recovery.fact_dispositions == {"noop": 2}
        assert second.recovery.link_dispositions == {"noop": 2}
        assert second.projection_rebuilt is False
        assert second.canonical_sequence_before == sequence_before
        assert second.canonical_sequence_after == sequence_before
        assert second.canonical_writes == 0
        assert store.connection.total_changes == changes_before
        assert dict(store.repository().table_counts()) == counts_before
        assert tuple(
            row[0]
            for row in store.connection.execute(
                "SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id"
            )
        ) == task_ids_before
    finally:
        store.close()
        archive.close()


def test_missing_donor_source_or_session_causes_zero_recovery_writes(
    tmp_path: Path,
) -> None:
    _snapshot, archive, plan, _labels = _sealed_plan(tmp_path, name="lookup-refusal")
    scratch = _scratch(tmp_path, "lookup-refusal-candidate")
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        repository = store.repository()
        sequence_before = repository.canonical_sequence()
        changes_before = store.connection.total_changes
        counts_before = dict(repository.table_counts())

        with pytest.raises(LegacyRecoveryError, match="source was not imported"):
            append_verified_recovery_claims(plan=plan, repository=repository)

        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == changes_before
        assert dict(repository.table_counts()) == counts_before

        repository.get_or_create_source(
            SourceInstanceInput(
                client="codex",
                adapter=LEGACY_EVENTS_ADAPTER,
                representation=LEGACY_EVENTS_REPRESENTATION,
                namespace_digest=_legacy_namespace_digest(NAMESPACE),
                namespace_scheme="legacy-explicit-fingerprint-sha256-v1",
                source_schema_version=None,
                privacy_label="synthetic-source-only",
            )
        )
        sequence_before = repository.canonical_sequence()
        changes_before = store.connection.total_changes
        counts_before = dict(repository.table_counts())

        with pytest.raises(LegacyRecoveryError, match="session was not imported"):
            append_verified_recovery_claims(plan=plan, repository=repository)

        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == changes_before
        assert dict(repository.table_counts()) == counts_before
        assert store.connection.execute(
            "SELECT COUNT(*) FROM facts WHERE transport = ?",
            (RECOVERY_FACT_TRANSPORT,),
        ).fetchone()[0] == 0
    finally:
        store.close()
        archive.close()
