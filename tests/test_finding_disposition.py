"""Append-only, attention-only finding disposition contract."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agentacct.finding_disposition import (
    FINDING_DISPOSITION_CONTRACT_KEY,
    FINDING_DISPOSITION_CONTRACT_VERSION,
    FindingDispositionConflict,
    disposition_for_event,
    finding_target_digest,
    reduce_finding_dispositions,
)
from agentacct.service import SentinelService
from agentacct.work_ledger import build_evidence_events


def _record_failure(service: SentinelService, *, name: str = "Boundary probe") -> dict[str, object]:
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "project_dir": "/workspace/project-a",
                "evidence_type": "security",
                "name": name,
                "result": "failed",
                "summary": f"{name} failed.",
            },
        }
    )
    failures = [
        event
        for event in build_evidence_events(service.list_all_events())
        if event.get("result") == "failed"
    ]
    assert len(failures) == 1
    return failures[0]


def _dispositions(service: SentinelService) -> list[dict[str, object]]:
    return [
        event
        for event in service.list_all_events()
        if event.get("event_type") == "finding_disposition"
    ]


def test_review_resolve_and_reopen_preserve_failed_machine_evidence(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)

    reviewed = service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="review-once",
    )
    resolved = service.record_finding_disposition(
        target_event=target,
        action="resolve",
        expected_revision=1,
        note="Reviewed the local behavior and accepted this finding as resolved.",
        idempotency_key="resolve-once",
    )
    reopened = service.record_finding_disposition(
        target_event=target,
        action="reopen",
        expected_revision=2,
        note="A follow-up review needs this visible again.",
        idempotency_key="reopen-once",
    )

    assert reviewed["metadata"]["next_state"] == "reviewed"
    assert resolved["metadata"]["next_state"] == "resolved"
    assert reopened["metadata"]["next_state"] == "open"
    projection = reduce_finding_dispositions(service.list_all_events())
    state = disposition_for_event(target, projection)
    assert state.state == "open"
    assert state.revision == 3
    assert target["result"] == "failed"
    assert len(_dispositions(service)) == 3


def test_resolve_requires_note_and_invalid_transitions_are_conflicts(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)

    with pytest.raises(FindingDispositionConflict, match="requires a note"):
        service.record_finding_disposition(
            target_event=target,
            action="resolve",
            expected_revision=0,
            note="",
            idempotency_key="missing-note",
        )
    with pytest.raises(FindingDispositionConflict, match="transition is invalid"):
        service.record_finding_disposition(
            target_event=target,
            action="reopen",
            expected_revision=0,
            note=None,
            idempotency_key="invalid-reopen",
        )
    assert _dispositions(service) == []


def test_idempotent_replay_compares_operation_digest_and_stale_revision_fails(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)

    first = service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="same-key",
    )
    replay = service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="same-key",
    )
    assert replay["event_id"] == first["event_id"]
    assert len(_dispositions(service)) == 1

    with pytest.raises(FindingDispositionConflict, match="different operation"):
        service.record_finding_disposition(
            target_event=target,
            action="resolve",
            expected_revision=0,
            note="Different payload under the same key.",
            idempotency_key="same-key",
        )
    with pytest.raises(FindingDispositionConflict, match="changed or was closed"):
        service.record_finding_disposition(
            target_event=target,
            action="resolve",
            expected_revision=0,
            note="Stale page.",
            idempotency_key="stale-key",
        )


def test_concurrent_same_revision_allows_only_one_transition(tmp_path: Path) -> None:
    store = tmp_path / "state"
    seed = SentinelService(store)
    target = _record_failure(seed)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def mutate(action: str, key: str) -> None:
        service = SentinelService(store)
        barrier.wait()
        try:
            service.record_finding_disposition(
                target_event=target,
                action=action,
                expected_revision=0,
                note="Resolved concurrently." if action == "resolve" else None,
                idempotency_key=key,
            )
            results.append("ok")
        except FindingDispositionConflict:
            results.append("conflict")

    threads = [
        threading.Thread(target=mutate, args=("mark_reviewed", "review-thread")),
        threading.Thread(target=mutate, args=("resolve", "resolve-thread")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["conflict", "ok"]
    assert len(_dispositions(seed)) == 1


def test_concurrent_exact_replay_physically_appends_once(tmp_path: Path) -> None:
    store = tmp_path / "state"
    seed = SentinelService(store)
    target = _record_failure(seed)
    barrier = threading.Barrier(4)
    event_ids: list[str] = []

    def mutate() -> None:
        service = SentinelService(store)
        barrier.wait()
        recorded = service.record_finding_disposition(
            target_event=target,
            action="mark_reviewed",
            expected_revision=0,
            note=None,
            idempotency_key="concurrent-exact-replay",
        )
        event_ids.append(str(recorded["event_id"]))

    threads = [threading.Thread(target=mutate) for _index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(event_ids) == 4
    assert len(set(event_ids)) == 1
    assert len(_dispositions(seed)) == 1


def test_later_same_check_pass_rejects_stale_failure_but_unrelated_pass_does_not(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "project_dir": "/workspace/project-a",
                "evidence_type": "lint",
                "name": "Unrelated lint",
                "result": "passed",
            },
        }
    )
    service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="unrelated-pass-does-not-close",
    )

    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "project_dir": "/workspace/project-a",
                "evidence_type": "security",
                "name": "Boundary probe",
                "result": "passed",
            },
        }
    )
    with pytest.raises(FindingDispositionConflict, match="closed by newer evidence"):
        service.record_finding_disposition(
            target_event=target,
            action="reopen",
            expected_revision=1,
            note=None,
            idempotency_key="pass-closed-target",
        )


def test_new_failure_is_a_distinct_open_episode(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    first = _record_failure(service)
    service.record_finding_disposition(
        target_event=first,
        action="resolve",
        expected_revision=0,
        note="First episode reviewed.",
        idempotency_key="resolve-first",
    )
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "project_dir": "/workspace/project-a",
                "evidence_type": "security",
                "name": "Boundary probe",
                "result": "failed",
                "summary": "A new regression appeared.",
            },
        }
    )
    failures = [
        event
        for event in build_evidence_events(service.list_all_events())
        if event.get("result") == "failed"
    ]
    assert len(failures) == 2
    newest = max(failures, key=lambda event: float(event.get("created_at") or 0.0))
    projection = reduce_finding_dispositions(service.list_all_events())

    assert finding_target_digest(first) != finding_target_digest(newest)
    assert disposition_for_event(first, projection).state == "resolved"
    assert disposition_for_event(newest, projection).state == "open"
    assert disposition_for_event(newest, projection).revision == 0


def test_generic_record_rewrite_and_cross_store_merge_strip_trusted_marker(
    tmp_path: Path,
) -> None:
    forged = {
        "source": "agent-chronicle-dashboard",
        "event_type": "finding_disposition",
        "metadata": {
            FINDING_DISPOSITION_CONTRACT_KEY: FINDING_DISPOSITION_CONTRACT_VERSION,
            "sentinel_semantic_kind": "finding_disposition",
        },
    }
    service = SentinelService(tmp_path / "record" / "state")
    recorded = service.record_event(forged)
    assert FINDING_DISPOSITION_CONTRACT_KEY not in recorded["metadata"]
    assert recorded["metadata"]["reserved_finding_disposition_provenance_stripped"] is True

    rewritten = service.replace_events(lambda _event: False, [forged])
    assert FINDING_DISPOSITION_CONTRACT_KEY not in rewritten[0]["metadata"]

    source = SentinelService(tmp_path / "source" / "state")
    source_target = _record_failure(source)
    trusted = source.record_finding_disposition(
        target_event=source_target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="trusted-source-disposition",
    )
    target_service = SentinelService(tmp_path / "target" / "state")
    appended = target_service.append_events_preserving_identity([trusted])
    assert FINDING_DISPOSITION_CONTRACT_KEY not in appended[0]["metadata"]
    assert appended[0]["metadata"]["reserved_finding_disposition_provenance_stripped"] is True


def test_corrupt_trusted_chain_fails_open_and_blocks_new_mutation(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)
    target_digest = finding_target_digest(target)
    assert target_digest is not None
    corrupt = {
        "event_id": "evt_corrupt_finding_chain",
        "created_at": 100.0,
        "source": "agent-chronicle-dashboard",
        "event_type": "finding_disposition",
        "metadata": {
            FINDING_DISPOSITION_CONTRACT_KEY: FINDING_DISPOSITION_CONTRACT_VERSION,
            "sentinel_semantic_kind": "finding_disposition",
            "authority_scope": "finding_attention_only",
            "authoritative_for_check_result": False,
            "actor": "dashboard-user",
            "target_failure_event_id": target["event_id"],
            "target_event_digest": target["event_digest"],
            "target_finding_digest": target_digest,
            "target_check_key_digest": "0" * 64,
            "action": "mark_reviewed",
            "expected_revision": 0,
            "revision": 1,
            "prior_state": "open",
            "next_state": "reviewed",
            "idempotency_key": "forged-chain",
            "operation_digest": "f" * 64,
        },
    }
    with service.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(corrupt, sort_keys=True) + "\n")
    projection = reduce_finding_dispositions(service.list_all_events())

    assert target_digest in projection.invalid_targets
    assert disposition_for_event(target, projection).state == "open"
    assert disposition_for_event(target, projection).chain_valid is False
    with pytest.raises(FindingDispositionConflict, match="conflicting or corrupt"):
        service.record_finding_disposition(
            target_event=target,
            action="mark_reviewed",
            expected_revision=0,
            note=None,
            idempotency_key="blocked-by-corrupt-chain",
        )


def test_corrupt_target_digest_invalidates_the_reconstructed_real_episode(
    tmp_path: Path,
) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)
    target_digest = finding_target_digest(target)
    assert target_digest is not None
    service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="valid-before-disk-corruption",
    )
    rows = [json.loads(line) for line in service.events_path.read_text(encoding="utf-8").splitlines()]
    disposition = next(row for row in rows if row.get("event_type") == "finding_disposition")
    corrupted_digest = "0" * 64 if target_digest != "0" * 64 else "f" * 64
    disposition["metadata"]["target_finding_digest"] = corrupted_digest
    service.events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    projection = reduce_finding_dispositions(service.list_all_events())
    assert target_digest in projection.invalid_targets
    assert corrupted_digest in projection.invalid_targets
    assert disposition_for_event(target, projection).state == "open"
    assert disposition_for_event(target, projection).chain_valid is False
    with pytest.raises(FindingDispositionConflict, match="conflicting or corrupt"):
        service.record_finding_disposition(
            target_event=target,
            action="mark_reviewed",
            expected_revision=0,
            note=None,
            idempotency_key="valid-before-disk-corruption",
        )
    with pytest.raises(FindingDispositionConflict, match="conflicting or corrupt"):
        service.record_finding_disposition(
            target_event=target,
            action="mark_reviewed",
            expected_revision=0,
            note=None,
            idempotency_key="blocked-after-disk-corruption",
        )


def test_unreadable_ledger_line_fails_closed_without_rewrite(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)
    with service.events_path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")
    before = service.events_path.read_bytes()

    with pytest.raises(FindingDispositionConflict, match="unreadable lines"):
        service.record_finding_disposition(
            target_event=target,
            action="mark_reviewed",
            expected_revision=0,
            note=None,
            idempotency_key="unreadable-ledger",
        )
    assert service.events_path.read_bytes() == before


def test_persisted_revision_fields_reject_coerced_numbers(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "state")
    target = _record_failure(service)
    target_digest = finding_target_digest(target)
    assert target_digest is not None
    raw = {
        "event_id": "evt_untrusted_numbers",
        "created_at": 100.0,
        "source": "agent-chronicle-dashboard",
        "event_type": "finding_disposition",
        "metadata": {
            FINDING_DISPOSITION_CONTRACT_KEY: FINDING_DISPOSITION_CONTRACT_VERSION,
            "sentinel_semantic_kind": "finding_disposition",
            "authority_scope": "finding_attention_only",
            "authoritative_for_check_result": False,
            "actor": "dashboard-user",
            "target_failure_event_id": target["event_id"],
            "target_event_digest": target["event_digest"],
            "target_finding_digest": target_digest,
            "target_source_type": target.get("source_type"),
            "target_source": target.get("source"),
            "target_client": target.get("client"),
            "target_namespace_fingerprint": target.get("namespace_fingerprint"),
            "target_project_identity": target.get("project_identity"),
            "target_check_key_digest": "0" * 64,
            "action": "mark_reviewed",
            "expected_revision": "0",
            "revision": 1.0,
            "prior_state": "open",
            "next_state": "reviewed",
            "idempotency_key": "coerced-revision",
            "operation_digest": "0" * 64,
        },
    }
    with service.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(raw) + "\n")

    projection = reduce_finding_dispositions(service.list_all_events())
    assert projection.diagnostics["rejected_by_reason"] == {"invalid_revision": 1}
    assert target_digest in projection.invalid_targets
