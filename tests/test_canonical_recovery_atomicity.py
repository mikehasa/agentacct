from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentacct.canonical import (
    CanonicalStore,
    FactInput,
    FactSessionLinkInput,
    RECOVERY_FACT_TRANSPORT,
    RECOVERY_LINK_METHOD,
    RECOVERY_LINK_RULE_VERSION,
    RecoveredWorkClaimInput,
    SessionInput,
    SourceInstanceInput,
    WorkClaimInput,
    canonical_hash,
)


def _source(repository, byte: int = 0x71):
    return repository.get_or_create_source(
        SourceInstanceInput(
            client="codex",
            adapter="synthetic-recovery-test",
            representation="legacy-v1",
            namespace_digest=bytes([byte]) * 32,
            namespace_scheme="legacy-explicit-fingerprint-sha256-v1",
            source_schema_version="synthetic-v1",
            privacy_label="synthetic-recovery",
        )
    )


def _session(repository, source_instance_id: int, name: str = "donor-session") -> int:
    result = repository.reconcile_session(
        SessionInput(
            source_instance_id=source_instance_id,
            client_session_id=name,
            session_kind="root",
            started_at_us=100,
            last_activity_at_us=200,
            observation_order=200,
        )
    )
    assert result.row_id is not None
    return result.row_id


def _claim(
    *,
    source_instance_id: int,
    event_id: str,
    status: str = "started",
    title: str = "Recovered section",
    transport: str = RECOVERY_FACT_TRANSPORT,
    supersedes_fact_id: int | None = None,
) -> WorkClaimInput:
    return WorkClaimInput(
        fact=FactInput(
            source_instance_id=source_instance_id,
            source_event_id=event_id,
            fact_type="work_claim",
            transport=transport,
            strength="recorded_claim",
            occurred_at_us=300,
            source_order=3,
            supersedes_fact_id=supersedes_fact_id,
            content_hash=canonical_hash(
                {
                    "event_id": event_id,
                    "status": status,
                    "title": title,
                    "supersedes_fact_id": supersedes_fact_id,
                }
            ),
        ),
        event_kind="section",
        outcome_axis="outcome" if status == "completed" else "execution",
        status=status,
        title=title,
        work_id="synthetic-recovered-work",
        section_id="synthetic-recovered-section",
    )


def _recovery_link(session_id: int) -> FactSessionLinkInput:
    # append_recovered_work_claim replaces this placeholder with the inserted
    # fact id inside the same transaction. FactSessionLinkInput still requires
    # a positive value at the public type boundary.
    return FactSessionLinkInput(
        fact_id=1,
        session_id=session_id,
        method=RECOVERY_LINK_METHOD,
        confidence="high",
        rule_version=RECOVERY_LINK_RULE_VERSION,
        validation_state="valid",
    )


def _recovered(claim: WorkClaimInput, session_id: int) -> RecoveredWorkClaimInput:
    return RecoveredWorkClaimInput(claim=claim, session_id=session_id)


def test_existing_source_and_session_lookup_is_read_only(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x70)
        session_id = _session(repository, source.source_instance_id, "lookup-session")
        sequence_before = repository.canonical_sequence()
        total_changes_before = store.connection.total_changes

        found_source = repository.get_source(
            source.client,
            source.representation,
            source.namespace_digest,
        )
        found_session = repository.get_session(
            source.source_instance_id,
            "lookup-session",
        )

        assert found_source == source
        assert found_session is not None
        assert found_session.session_id == session_id
        assert repository.get_source(
            source.client,
            source.representation,
            bytes([0xFF]) * 32,
        ) is None
        assert repository.get_session(
            source.source_instance_id,
            "missing-session",
        ) is None
        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == total_changes_before


def test_recovered_claim_and_high_not_exact_link_are_written_atomically(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository)
        session_id = _session(repository, source.source_instance_id)
        claim = _claim(
            source_instance_id=source.source_instance_id,
            event_id="recovered-success",
        )
        sequence_before = repository.canonical_sequence()

        fact_result, link_result = repository.append_recovered_work_claim(
            claim,
            _recovery_link(session_id),
        )

        assert fact_result.disposition == "inserted"
        assert link_result.disposition == "inserted"
        assert fact_result.row_id is not None
        assert repository.canonical_sequence() == sequence_before + 2
        assert tuple(
            store.connection.execute(
                "SELECT fact.transport, link.method, link.confidence, "
                "link.rule_version, link.validation_state, link.veto_reason "
                "FROM facts fact JOIN fact_session_links link USING(fact_id) "
                "WHERE fact.fact_id = ?",
                (fact_result.row_id,),
            ).fetchone()
        ) == (
            RECOVERY_FACT_TRANSPORT,
            RECOVERY_LINK_METHOD,
            "high",
            RECOVERY_LINK_RULE_VERSION,
            "valid",
            None,
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM work_claims WHERE fact_id = ?",
            (fact_result.row_id,),
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM fact_session_links WHERE fact_id = ? "
            "AND confidence = 'exact'",
            (fact_result.row_id,),
        ).fetchone()[0] == 0


def test_same_candidate_recovery_batch_rerun_is_a_physical_noop(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository)
        session_id = _session(repository, source.source_instance_id)
        batch = tuple(
            _recovered(
                _claim(
                    source_instance_id=source.source_instance_id,
                    event_id=f"recovered-rerun-{index}",
                    title=f"Recovered rerun {index}",
                ),
                session_id,
            )
            for index in range(2)
        )
        first = repository.append_recovered_work_claims(batch)
        assert [
            (fact.disposition, link.disposition)
            for fact, link in first
        ] == [("inserted", "inserted"), ("inserted", "inserted")]
        sequence_before = repository.canonical_sequence()
        total_changes_before = store.connection.total_changes
        counts_before = dict(repository.table_counts())

        second = repository.append_recovered_work_claims(batch)

        assert [
            (fact.disposition, link.disposition)
            for fact, link in second
        ] == [("noop", "noop"), ("noop", "noop")]
        assert [
            (fact.row_id, link.row_id)
            for fact, link in second
        ] == [
            (fact.row_id, link.row_id)
            for fact, link in first
        ]
        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == total_changes_before
        assert dict(repository.table_counts()) == counts_before


def test_recovery_batch_idempotency_is_scoped_to_each_source_instance(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        first_source = _source(repository, 0x7A)
        second_source = _source(repository, 0x7B)
        first_session = _session(
            repository,
            first_source.source_instance_id,
            "first-source-session",
        )
        second_session = _session(
            repository,
            second_source.source_instance_id,
            "second-source-session",
        )
        first_claim = _claim(
            source_instance_id=first_source.source_instance_id,
            event_id="shared-recovery-event",
        )
        first_claim = replace(
            first_claim,
            fact=replace(
                first_claim.fact,
                idempotency_scope="shared-recovery-scope",
                idempotency_key="shared-recovery-key",
            ),
        )
        second_claim = replace(
            first_claim,
            fact=replace(
                first_claim.fact,
                source_instance_id=second_source.source_instance_id,
            ),
        )
        batch = (
            _recovered(first_claim, first_session),
            _recovered(second_claim, second_session),
        )

        first = repository.append_recovered_work_claims(batch)
        second = repository.append_recovered_work_claims(batch)

        assert [(fact.disposition, link.disposition) for fact, link in first] == [
            ("inserted", "inserted"),
            ("inserted", "inserted"),
        ]
        assert first[0][0].row_id != first[1][0].row_id
        assert [(fact.disposition, link.disposition) for fact, link in second] == [
            ("noop", "noop"),
            ("noop", "noop"),
        ]
        assert [(fact.row_id, link.row_id) for fact, link in second] == [
            (fact.row_id, link.row_id) for fact, link in first
        ]


def test_incompatible_source_recovery_fails_before_any_canonical_write(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        fact_source = _source(repository, 0x72)
        session_source = _source(repository, 0x73)
        unrelated_session_id = _session(
            repository,
            session_source.source_instance_id,
            "unrelated-session",
        )
        value = _recovered(
            _claim(
                source_instance_id=fact_source.source_instance_id,
                event_id="incompatible-source",
            ),
            unrelated_session_id,
        )
        sequence_before = repository.canonical_sequence()
        total_changes_before = store.connection.total_changes
        counts_before = dict(repository.table_counts())

        with pytest.raises(ValueError, match="exactly match its existing session source"):
            repository.append_recovered_work_claims((value,))

        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == total_changes_before
        assert dict(repository.table_counts()) == counts_before
        assert repository.find_fact_id(
            fact_source.source_instance_id,
            "incompatible-source",
        ) is None


def test_later_invalid_batch_item_leaves_earlier_item_unwritten(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x74)
        session_id = _session(repository, source.source_instance_id)
        first = _recovered(
            _claim(
                source_instance_id=source.source_instance_id,
                event_id="batch-conflict",
                title="First interpretation",
            ),
            session_id,
        )
        conflicting_second = _recovered(
            _claim(
                source_instance_id=source.source_instance_id,
                event_id="batch-conflict",
                title="Conflicting second interpretation",
            ),
            session_id,
        )
        sequence_before = repository.canonical_sequence()
        total_changes_before = store.connection.total_changes
        counts_before = dict(repository.table_counts())

        with pytest.raises(ValueError, match="conflicting stable fact identities"):
            repository.append_recovered_work_claims((first, conflicting_second))

        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == total_changes_before
        assert dict(repository.table_counts()) == counts_before
        assert repository.find_fact_id(
            source.source_instance_id,
            "batch-conflict",
        ) is None


def test_recovered_correction_cannot_inherit_an_exact_predecessor_scope(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository)
        session_id = _session(repository, source.source_instance_id)
        predecessor = repository.append_work_claim(
            _claim(
                source_instance_id=source.source_instance_id,
                event_id="exact-predecessor",
                transport="mcp",
            )
        )
        assert predecessor.row_id is not None
        exact_link = repository.link_fact_to_session(
            FactSessionLinkInput(
                fact_id=predecessor.row_id,
                session_id=session_id,
                method="legacy_explicit_client_session_id",
                confidence="exact",
                rule_version="legacy-import-v1",
                validation_state="valid",
            )
        )
        assert exact_link.disposition == "inserted"
        correction = _claim(
            source_instance_id=source.source_instance_id,
            event_id="recovered-correction-rejected",
            status="completed",
            title="Recovered correction rejected",
            supersedes_fact_id=predecessor.row_id,
        )
        sequence_before = repository.canonical_sequence()
        total_changes_before = store.connection.total_changes

        with pytest.raises(
            ValueError,
            match="same high-confidence recovery scope",
        ):
            repository.append_recovered_work_claim(
                correction,
                _recovery_link(session_id),
            )

        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == total_changes_before
        assert repository.find_fact_id(
            source.source_instance_id,
            "recovered-correction-rejected",
        ) is None


def test_recovered_correction_may_inherit_the_same_high_recovery_scope(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository)
        session_id = _session(repository, source.source_instance_id)
        predecessor_claim = _claim(
            source_instance_id=source.source_instance_id,
            event_id="high-predecessor",
        )
        predecessor, predecessor_link = repository.append_recovered_work_claim(
            predecessor_claim,
            _recovery_link(session_id),
        )
        assert predecessor.disposition == "inserted"
        assert predecessor_link.disposition == "inserted"
        assert predecessor.row_id is not None
        correction = _claim(
            source_instance_id=source.source_instance_id,
            event_id="high-correction",
            status="completed",
            title="Recovered correction accepted",
            supersedes_fact_id=predecessor.row_id,
        )

        correction_result, correction_link = repository.append_recovered_work_claim(
            correction,
            _recovery_link(session_id),
        )

        assert correction_result.disposition == "inserted"
        # _append_fact_in_transaction inherits the predecessor's one valid
        # high recovery scope, so the explicit locked link is already present.
        assert correction_link.disposition == "noop"
        assert correction_result.row_id is not None
        assert [
            tuple(row)
            for row in store.connection.execute(
                "SELECT session_id, method, confidence, rule_version, validation_state "
                "FROM fact_session_links WHERE fact_id = ?",
                (correction_result.row_id,),
            ).fetchall()
        ] == [
            (
                session_id,
                RECOVERY_LINK_METHOD,
                "high",
                RECOVERY_LINK_RULE_VERSION,
                "valid",
            )
        ]


def test_existing_recovery_fact_without_locked_link_fails_without_writes(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository)
        session_id = _session(repository, source.source_instance_id)
        claim = _claim(
            source_instance_id=source.source_instance_id,
            event_id="existing-without-link",
        )
        existing = repository.append_work_claim(claim)
        assert existing.disposition == "inserted"
        assert existing.row_id is not None
        assert store.connection.execute(
            "SELECT COUNT(*) FROM fact_session_links WHERE fact_id = ?",
            (existing.row_id,),
        ).fetchone()[0] == 0
        sequence_before = repository.canonical_sequence()
        total_changes_before = store.connection.total_changes

        with pytest.raises(
            ValueError,
            match="without the identical high-confidence link",
        ):
            repository.append_recovered_work_claim(
                claim,
                _recovery_link(session_id),
            )

        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == total_changes_before
        assert store.connection.execute(
            "SELECT COUNT(*) FROM fact_session_links WHERE fact_id = ?",
            (existing.row_id,),
        ).fetchone()[0] == 0
