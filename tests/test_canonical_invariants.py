from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentacct.canonical import (
    CanonicalStore,
    CostCalculationInput,
    FactInput,
    FactSessionLinkInput,
    SessionEdgeInput,
    SessionInput,
    SourceInstanceInput,
    UsageMeasurementInput,
    WorkClaimInput,
    canonical_hash,
)
from agentacct.canonical.types import SQLITE_INT64_MAX, TOKEN_FIELDS


FIXTURE = Path(__file__).parent / "fixtures" / "canonical" / "v1" / "spike.json"


def _source(repository, byte: int, *, representation: str = "sqlite"):
    return repository.get_or_create_source(
        SourceInstanceInput(
            client="codex",
            adapter="synthetic-fixture",
            representation=representation,
            namespace_digest=bytes([byte]) * 32,
            source_schema_version="synthetic-v1",
            privacy_label=f"synthetic-{byte}",
        )
    )


def _session(
    repository,
    source_instance_id: int,
    client_session_id: str,
    *,
    kind: str = "root",
    order: int = 1,
    activity: int = 1_784_455_200_000_000,
):
    result = repository.reconcile_session(
        SessionInput(
            source_instance_id=source_instance_id,
            client_session_id=client_session_id,
            session_kind=kind,
            started_at_us=activity - 100,
            last_activity_at_us=activity,
            observation_order=order,
        )
    )
    assert result.row_id is not None
    return result


def _usage(
    session_id: int,
    *,
    representation: str = "sqlite",
    precedence_role: str = "authoritative",
    totals_eligible: bool = True,
    held_reason: str | None = None,
    source_order: int = 1,
    input_tokens: int | None = 1000,
    input_reported: bool = True,
    output_tokens: int | None = 0,
    output_reported: bool = True,
    cached_tokens: int | None = None,
    cached_reported: bool = False,
    total_tokens: int | None = 1000,
    total_reported: bool = True,
):
    return UsageMeasurementInput(
        session_id=session_id,
        lane="session_total",
        representation=representation,
        update_semantics="cumulative_snapshot",
        precedence_role=precedence_role,
        granularity="session",
        totals_eligible=totals_eligible,
        provider="openai",
        model="synthetic-model",
        usage_confidence="observed" if totals_eligible else "partial",
        held_reason=held_reason,
        input_tokens=input_tokens,
        input_tokens_reported=input_reported,
        output_tokens=output_tokens,
        output_tokens_reported=output_reported,
        cached_input_tokens=cached_tokens,
        cached_input_tokens_reported=cached_reported,
        total_tokens=total_tokens,
        total_tokens_reported=total_reported,
        source_order=source_order,
        observed_at_us=1_784_455_200_000_000,
        updated_at_us=1_784_455_200_000_000 + source_order,
    )


def test_usage_accepts_sqlite_int64_max_before_and_at_the_bind_boundary(
    tmp_path: Path,
) -> None:
    max_tokens = {
        name: SQLITE_INT64_MAX
        for name in TOKEN_FIELDS
    }
    max_presence = {
        f"{name}_reported": True
        for name in TOKEN_FIELDS
    }
    value = replace(
        _usage(1),
        **max_tokens,
        **max_presence,
        observed_at_us=SQLITE_INT64_MAX,
        updated_at_us=SQLITE_INT64_MAX,
        client_reported_cost_microusd=SQLITE_INT64_MAX,
        source_order=SQLITE_INT64_MAX,
        source_revision_id=SQLITE_INT64_MAX,
    )

    assert value.token_values() == max_tokens
    assert value.observed_at_us == SQLITE_INT64_MAX
    assert value.updated_at_us == SQLITE_INT64_MAX
    assert value.client_reported_cost_microusd == SQLITE_INT64_MAX
    assert value.source_order == SQLITE_INT64_MAX
    assert value.source_revision_id == SQLITE_INT64_MAX

    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x7F)
        session = _session(repository, source.source_instance_id, "int64-max-session")
        assert session.row_id is not None
        bindable = replace(value, session_id=session.row_id, source_revision_id=None)

        result = repository.reconcile_usage(bindable)

        assert result.disposition == "inserted"
        stored = store.connection.execute(
            "SELECT input_tokens, total_tokens, client_reported_cost_microusd, "
            "source_order, observed_at_us, updated_at_us "
            "FROM usage_measurements WHERE usage_measurement_id = ?",
            (result.row_id,),
        ).fetchone()
        assert tuple(stored) == (SQLITE_INT64_MAX,) * 6


@pytest.mark.parametrize(
    "field_name",
    [
        *TOKEN_FIELDS,
        "observed_at_us",
        "updated_at_us",
        "client_reported_cost_microusd",
        "source_order",
        "source_revision_id",
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(1 << 63, id="sqlite-overflow"),
        pytest.param(1.0, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(-1, id="negative"),
    ],
)
def test_usage_rejects_non_sqlite_integers_before_binding(
    field_name: str,
    invalid_value: object,
) -> None:
    changes = {field_name: invalid_value}
    if field_name in TOKEN_FIELDS:
        changes[f"{field_name}_reported"] = True

    with pytest.raises(ValueError, match=field_name):
        replace(_usage(1), **changes)


@pytest.mark.parametrize("field_name", TOKEN_FIELDS)
def test_usage_unreported_token_values_must_remain_null(field_name: str) -> None:
    with pytest.raises(ValueError, match=rf"{field_name} must be None"):
        replace(
            _usage(1),
            **{
                field_name: 0,
                f"{field_name}_reported": False,
            },
        )


def test_synthetic_fixture_is_private_and_covers_the_approved_cases() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert fixture["privacy"] == {
        "synthetic_only": True,
        "contains_live_store_data": False,
        "contains_source_paths": False,
        "contains_prompt_or_transcript_content": False,
    }
    assert fixture["churn_collapse_workload"]["equivalent_attempt_count"] == 1_100_000
    assert fixture["normal_corpus"]["generation"]["task_anchor_count"] == 24
    assert all(
        item["public_task_id"].startswith("task_")
        for item in fixture["task_anchors"]
    )
    assert {item["expected"]["disposition"] for item in fixture["semantic_ingest_attempts"]} == {
        "inserted",
        "exact_replay_noop",
        "quarantined_conflict",
        "inserted_revision",
    }


def test_namespace_identity_edges_and_persistent_opaque_task_ids(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        alpha = _source(repository, 0xA1)
        beta = _source(repository, 0xB2)
        with pytest.raises(ValueError, match="different adapter or namespace scheme"):
            repository.get_or_create_source(
                SourceInstanceInput(
                    client="codex",
                    adapter="different-adapter",
                    representation="sqlite",
                    namespace_digest=bytes([0xA1]) * 32,
                )
            )

        alpha_root = _session(repository, alpha.source_instance_id, "shared-session")
        beta_root = _session(repository, beta.source_instance_id, "shared-session")
        alpha_child = _session(
            repository,
            alpha.source_instance_id,
            "alpha-child",
            kind="continuation",
            order=2,
        )
        alpha_other_root = _session(
            repository,
            alpha.source_instance_id,
            "alpha-other-root",
            order=1,
        )

        assert alpha_root.row_id != beta_root.row_id
        assert repository.get_sessions(
            [
                (alpha.source_instance_id, "shared-session"),
                (beta.source_instance_id, "shared-session"),
            ]
        ).keys() == {
            (alpha.source_instance_id, "shared-session"),
            (beta.source_instance_id, "shared-session"),
        }

        accepted = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=alpha_child.row_id,
                parent_session_id=alpha_root.row_id,
                relation="continuation",
                basis="explicit_parent_session_id",
                validation_state="valid",
                content_hash=canonical_hash("alpha-edge"),
            )
        )
        rejected = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=alpha_child.row_id,
                parent_session_id=beta_root.row_id,
                relation="continuation",
                basis="explicit_parent_session_id",
                validation_state="valid",
                content_hash=canonical_hash("cross-namespace-edge"),
            )
        )
        assert accepted.disposition == "inserted"
        assert rejected.disposition == "inserted"
        assert store.connection.execute(
            "SELECT validation_state, veto_reason FROM session_edges WHERE session_edge_id = ?",
            (rejected.row_id,),
        ).fetchone()[:] == ("rejected", "incompatible_source_namespace")

        ambiguous_parent = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=alpha_child.row_id,
                parent_session_id=alpha_other_root.row_id,
                relation="continuation",
                basis="second_explicit_parent",
                validation_state="valid",
                content_hash=canonical_hash("ambiguous-parent"),
            )
        )
        assert store.connection.execute(
            "SELECT validation_state, veto_reason FROM session_edges WHERE session_edge_id = ?",
            (ambiguous_parent.row_id,),
        ).fetchone()[:] == ("rejected", "ambiguous_multiple_parent")

        cycle = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=alpha_root.row_id,
                parent_session_id=alpha_child.row_id,
                relation="continuation",
                basis="cycle-attempt",
                validation_state="valid",
                content_hash=canonical_hash("cycle-attempt"),
            )
        )
        assert store.connection.execute(
            "SELECT validation_state, veto_reason FROM session_edges WHERE session_edge_id = ?",
            (cycle.row_id,),
        ).fetchone()[:] == ("rejected", "lineage_cycle")

        anchor = repository.get_or_create_task_anchor(alpha_root.row_id)
        same_anchor = repository.get_or_create_task_anchor(alpha_root.row_id)
        assert anchor == same_anchor
        assert len(anchor.public_task_id) == 37
        assert anchor.public_task_id.startswith("task_")
        assert "shared-session" not in anchor.public_task_id

        repository.rebuild_minimal_read_models()
        assert repository.get_task(anchor.public_task_id) is not None
        repository.rebuild_minimal_read_models()
        assert repository.get_task_anchor(anchor.public_task_id) == anchor


def test_late_valid_lineage_atomically_retires_the_child_task_anchor(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xA3)
        parent = _session(repository, source.source_instance_id, "late-parent")
        child = _session(
            repository,
            source.source_instance_id,
            "late-child",
            kind="unknown",
            order=1,
        )
        parent_anchor = repository.get_or_create_task_anchor(parent.row_id)
        child_anchor = repository.get_or_create_task_anchor(child.row_id)
        repository.rebuild_minimal_read_models()
        assert {task.public_task_id for task in repository.list_tasks(limit=10)} == {
            parent_anchor.public_task_id,
            child_anchor.public_task_id,
        }

        sequence_before_edge = repository.canonical_sequence()
        edge = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=child.row_id,
                parent_session_id=parent.row_id,
                relation="continuation",
                basis="late_explicit_parent",
                validation_state="valid",
                content_hash=canonical_hash("late-valid-edge"),
            )
        )

        assert edge.disposition == "inserted"
        # The accepted edge and retired anchor are two canonical mutations,
        # committed at one transaction boundary.
        assert edge.canonical_sequence == sequence_before_edge + 2
        assert repository.canonical_sequence() == sequence_before_edge + 2
        assert repository.get_task_anchor(child_anchor.public_task_id) is None
        assert repository.get_task_anchor(parent_anchor.public_task_id) == parent_anchor
        with pytest.raises(ValueError, match="without a valid parent"):
            repository.get_or_create_task_anchor(child.row_id)

        repository.rebuild_minimal_read_models()
        tasks = repository.list_tasks(limit=10)
        assert [task.public_task_id for task in tasks] == [parent_anchor.public_task_id]
        assert tasks[0].session_count == 2


def test_non_valid_missing_and_stale_lineage_retain_the_child_task_anchor(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xA4)
        parent = _session(repository, source.source_instance_id, "retained-parent")
        child = _session(
            repository,
            source.source_instance_id,
            "retained-child",
            kind="unknown",
            order=10,
        )
        child_anchor = repository.get_or_create_task_anchor(child.row_id)

        rejected = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=child.row_id,
                parent_session_id=parent.row_id,
                relation="continuation",
                basis="untrusted-parent",
                validation_state="rejected",
                content_hash=canonical_hash("rejected-edge"),
            )
        )
        assert rejected.disposition == "inserted"
        assert repository.get_task_anchor(child_anchor.public_task_id) == child_anchor

        conflicted = repository.add_session_edge(
            SessionEdgeInput(
                child_session_id=child.row_id,
                parent_session_id=parent.row_id,
                relation="continuation",
                basis="untrusted-parent",
                validation_state="valid",
                content_hash=canonical_hash("conflicting-edge"),
            )
        )
        assert conflicted.disposition == "conflict"
        assert repository.get_task_anchor(child_anchor.public_task_id) == child_anchor

        with pytest.raises(ValueError, match="both session-edge endpoints must exist"):
            repository.add_session_edge(
                SessionEdgeInput(
                    child_session_id=child.row_id,
                    parent_session_id=9_999_999,
                    relation="continuation",
                    basis="missing-parent",
                    validation_state="valid",
                    content_hash=canonical_hash("missing-parent-edge"),
                )
            )
        assert repository.get_task_anchor(child_anchor.public_task_id) == child_anchor

        stale = repository.reconcile_session(
            SessionInput(
                source_instance_id=source.source_instance_id,
                client_session_id="retained-child",
                session_kind="continuation",
                started_at_us=1_784_455_200_000_000 - 100,
                last_activity_at_us=1_784_455_200_000_001,
                observation_order=5,
            )
        )
        assert stale.disposition == "conflict"
        assert repository.get_task_anchor(child_anchor.public_task_id) == child_anchor


def test_valid_lineage_and_anchor_retirement_roll_back_together(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xA5)
        parent = _session(repository, source.source_instance_id, "rollback-parent")
        child = _session(
            repository,
            source.source_instance_id,
            "rollback-child",
            kind="unknown",
        )
        child_anchor = repository.get_or_create_task_anchor(child.row_id)
        sequence_before = repository.canonical_sequence()
        store.connection.execute(
            "CREATE TRIGGER force_anchor_retirement_failure "
            "BEFORE DELETE ON task_anchors BEGIN "
            "SELECT RAISE(ABORT, 'forced anchor retirement failure'); END"
        )

        with pytest.raises(sqlite3.IntegrityError, match="forced anchor retirement failure"):
            repository.add_session_edge(
                SessionEdgeInput(
                    child_session_id=child.row_id,
                    parent_session_id=parent.row_id,
                    relation="continuation",
                    basis="rollback-proof",
                    validation_state="valid",
                    content_hash=canonical_hash("rollback-proof-edge"),
                )
            )

        assert repository.canonical_sequence() == sequence_before
        assert repository.get_task_anchor(child_anchor.public_task_id) == child_anchor
        assert store.connection.execute(
            "SELECT COUNT(*) FROM session_edges WHERE basis = 'rollback-proof'"
        ).fetchone()[0] == 0


def test_session_noop_advances_order_high_watermark_and_rejects_stale_change(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xA2)
        initial = _session(repository, source.source_instance_id, "ordered-session")
        sequence_after_insert = repository.canonical_sequence()
        changes_before_high_watermark = store.connection.total_changes

        exact_newer = repository.reconcile_session(
            SessionInput(
                source_instance_id=source.source_instance_id,
                client_session_id="ordered-session",
                session_kind="root",
                started_at_us=1_784_455_200_000_000 - 100,
                last_activity_at_us=1_784_455_200_000_000,
                observation_order=100,
            )
        )

        assert exact_newer.disposition == "noop"
        assert exact_newer.row_id == initial.row_id
        assert repository.canonical_sequence() == sequence_after_insert
        assert store.connection.total_changes == changes_before_high_watermark + 1
        assert store.connection.execute(
            "SELECT observation_order FROM sessions WHERE session_id = ?",
            (initial.row_id,),
        ).fetchone()[0] == 100

        stale_change = repository.reconcile_session(
            SessionInput(
                source_instance_id=source.source_instance_id,
                client_session_id="ordered-session",
                session_kind="root",
                started_at_us=1_784_455_200_000_000 - 100,
                last_activity_at_us=1_784_455_200_000_050,
                observation_order=50,
            )
        )
        assert stale_change.disposition == "conflict"
        stored = store.connection.execute(
            "SELECT observation_order, last_activity_at_us FROM sessions WHERE session_id = ?",
            (initial.row_id,),
        ).fetchone()
        assert tuple(stored) == (100, 1_784_455_200_000_000)


def test_absorb_session_observation_folds_min_started_max_last(tmp_path: Path) -> None:
    activity = 1_784_455_200_000_000
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xB3)
        initial = _session(repository, source.source_instance_id, "absorb-session")
        seq_after_insert = repository.canonical_sequence()

        # An earlier started_at and a later last_activity are BOTH order-free
        # merges; folding them in advances the sequence and returns "absorbed".
        folded = repository.absorb_session_observation(
            SessionInput(
                source_instance_id=source.source_instance_id,
                client_session_id="absorb-session",
                started_at_us=activity - 500,
                last_activity_at_us=activity + 50,
            )
        )
        assert folded.disposition == "absorbed"
        assert folded.row_id == initial.row_id
        assert repository.canonical_sequence() > seq_after_insert
        row = store.connection.execute(
            "SELECT started_at_us, last_activity_at_us, sort_activity_at_us, observation_order "
            "FROM sessions WHERE session_id = ?",
            (initial.row_id,),
        ).fetchone()
        assert row[0] == activity - 500  # min started folded in
        assert row[1] == activity + 50  # max last folded in
        assert row[2] == activity + 50  # sort mirrors last activity
        assert row[3] == 1  # observation_order (last-wins) is NOT advanced


def test_absorb_session_observation_never_regresses_and_noops(tmp_path: Path) -> None:
    activity = 1_784_455_200_000_000
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xB4)
        initial = _session(repository, source.source_instance_id, "absorb-noop")
        seq_after_insert = repository.canonical_sequence()

        # A LATER start and an EARLIER last cannot lower started or raise last:
        # min/max leave the row untouched, so this is a pure noop.
        noop = repository.absorb_session_observation(
            SessionInput(
                source_instance_id=source.source_instance_id,
                client_session_id="absorb-noop",
                started_at_us=activity + 999,
                last_activity_at_us=activity - 999,
            )
        )
        assert noop.disposition == "noop"
        assert repository.canonical_sequence() == seq_after_insert
        row = store.connection.execute(
            "SELECT started_at_us, last_activity_at_us FROM sessions WHERE session_id = ?",
            (initial.row_id,),
        ).fetchone()
        assert tuple(row) == (activity - 100, activity)  # unchanged


def test_absorb_session_observation_requires_an_existing_row(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0xB5)
        with pytest.raises(ValueError, match="requires an existing session"):
            repository.absorb_session_observation(
                SessionInput(
                    source_instance_id=source.source_instance_id,
                    client_session_id="never-observed",
                    started_at_us=1,
                    last_activity_at_us=2,
                )
            )


def test_fact_replay_conflict_and_explicit_correction(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x11)
        session = _session(repository, source.source_instance_id, "semantic-session")
        anchor = repository.get_or_create_task_anchor(session.row_id)

        original_fact = FactInput(
            source_instance_id=source.source_instance_id,
            source_event_id="semantic-event-1",
            idempotency_scope="synthetic-semantic",
            idempotency_key="semantic-1",
            fact_type="work_claim",
            transport="mcp",
            strength="recorded_claim",
            occurred_at_us=1_784_455_210_000_000,
            source_order=10,
            content_hash=canonical_hash({"status": "started", "title": "Synthetic spike"}),
        )
        original = WorkClaimInput(
            fact=original_fact,
            event_kind="section",
            outcome_axis="execution",
            status="started",
            title="Synthetic spike",
            summary="Synthetic-only work claim.",
        )
        inserted = repository.append_work_claim(original)
        sequence_after_insert = repository.canonical_sequence()
        replayed = repository.append_work_claim(original)

        assert inserted.disposition == "inserted"
        assert replayed.disposition == "noop"
        assert replayed.row_id == inserted.row_id
        assert repository.canonical_sequence() == sequence_after_insert

        assert inserted.row_id is not None
        assert repository.link_fact_to_session(
            FactSessionLinkInput(
                fact_id=inserted.row_id,
                session_id=session.row_id,
                method="explicit_client_session_id",
                confidence="exact",
                rule_version="synthetic-v1",
                validation_state="valid",
            )
        ).disposition == "inserted"

        conflicting = repository.append_work_claim(
            WorkClaimInput(
                fact=replace(
                    original_fact,
                    content_hash=canonical_hash(
                        {"status": "completed", "title": "Synthetic spike"}
                    ),
                ),
                event_kind="section",
                outcome_axis="execution",
                status="completed",
                title="Synthetic spike",
            )
        )
        assert conflicting.disposition == "conflict"
        assert store.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0] == 1

        corrected = repository.append_work_claim(
            WorkClaimInput(
                fact=FactInput(
                    source_instance_id=source.source_instance_id,
                    source_event_id="semantic-event-2",
                    idempotency_scope="synthetic-semantic",
                    idempotency_key="semantic-2",
                    fact_type="work_claim",
                    transport="mcp",
                    strength="recorded_claim",
                    # Corrections supersede by explicit identity, not by an
                    # assumed timestamp order.
                    occurred_at_us=1_784_455_200_000_000,
                    source_order=11,
                    content_hash=canonical_hash({"status": "completed", "revision": 2}),
                    supersedes_fact_id=inserted.row_id,
                ),
                event_kind="section",
                outcome_axis="outcome",
                status="completed",
                title="Synthetic spike",
            )
        )
        assert corrected.disposition == "inserted"
        assert store.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
        assert store.connection.execute(
            "SELECT supersedes_fact_id FROM facts WHERE fact_id = ?", (corrected.row_id,)
        ).fetchone()[0] == inserted.row_id

        assert corrected.row_id is not None
        linked = repository.link_fact_to_session(
            FactSessionLinkInput(
                fact_id=corrected.row_id,
                session_id=session.row_id,
                method="explicit_client_session_id",
                confidence="exact",
                rule_version="synthetic-v1",
                validation_state="valid",
            )
        )
        # The correction atomically inherits the predecessor's valid scope, so
        # replaying the explicit link is a no-op rather than a second claim.
        assert linked.disposition == "noop"
        assert repository.link_fact_to_session(
            FactSessionLinkInput(
                fact_id=corrected.row_id,
                session_id=session.row_id,
                method="explicit_client_session_id",
                confidence="exact",
                rule_version="synthetic-v1",
                validation_state="valid",
            )
        ).disposition == "noop"
        repository.rebuild_minimal_read_models()
        task = repository.get_task(anchor.public_task_id)
        assert task is not None
        assert task.title == "Synthetic spike"
        assert task.projected_state == "reported_complete"

        correction_branch = repository.append_work_claim(
            WorkClaimInput(
                fact=FactInput(
                    source_instance_id=source.source_instance_id,
                    source_event_id="semantic-event-branch",
                    idempotency_scope="synthetic-semantic",
                    idempotency_key="semantic-branch",
                    fact_type="work_claim",
                    transport="mcp",
                    strength="recorded_claim",
                    occurred_at_us=1_784_455_240_000_000,
                    source_order=12,
                    content_hash=canonical_hash({"status": "blocked", "branch": True}),
                    supersedes_fact_id=inserted.row_id,
                ),
                event_kind="section",
                outcome_axis="outcome",
                status="blocked",
                title="Conflicting correction",
            )
        )
        assert correction_branch.disposition == "conflict"
        assert store.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2

        identity_collision = repository.append_work_claim(
            WorkClaimInput(
                fact=FactInput(
                    source_instance_id=source.source_instance_id,
                    source_event_id="semantic-event-1",
                    idempotency_scope="synthetic-semantic",
                    idempotency_key="semantic-2",
                    fact_type="work_claim",
                    transport="mcp",
                    strength="recorded_claim",
                    occurred_at_us=1_784_455_230_000_000,
                    source_order=12,
                    content_hash=canonical_hash({"identity": "collision"}),
                ),
                event_kind="section",
                outcome_axis="execution",
                status="completed",
            )
        )
        assert identity_collision.disposition == "conflict"
        assert store.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_fact_stable_identities_are_scoped_to_the_source_instance(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        first_source = _source(repository, 0x13)
        second_source = _source(repository, 0x14)
        shared_fact = FactInput(
            source_instance_id=first_source.source_instance_id,
            source_event_id="shared-native-event",
            idempotency_scope="shared-writer-scope",
            idempotency_key="shared-retry-key",
            fact_type="work_claim",
            transport="mcp",
            strength="recorded_claim",
            occurred_at_us=1_784_455_250_000_000,
            content_hash=canonical_hash({"shared": "content"}),
        )
        first_claim = WorkClaimInput(
            fact=shared_fact,
            event_kind="section",
            outcome_axis="execution",
            status="started",
            title="Source-scoped identity",
        )
        second_claim = replace(
            first_claim,
            fact=replace(
                shared_fact,
                source_instance_id=second_source.source_instance_id,
            ),
        )

        first = repository.append_work_claim(first_claim)
        second = repository.append_work_claim(second_claim)
        first_replay = repository.append_work_claim(first_claim)
        second_replay = repository.append_work_claim(second_claim)

        assert first.disposition == second.disposition == "inserted"
        assert first.row_id != second.row_id
        assert (first_replay.disposition, first_replay.row_id) == ("noop", first.row_id)
        assert (second_replay.disposition, second_replay.row_id) == ("noop", second.row_id)
        assert [
            tuple(row)
            for row in store.connection.execute(
                "SELECT source_instance_id, source_event_id, idempotency_scope, idempotency_key "
                "FROM facts ORDER BY source_instance_id"
            )
        ] == [
            (
                first_source.source_instance_id,
                "shared-native-event",
                "shared-writer-scope",
                "shared-retry-key",
            ),
            (
                second_source.source_instance_id,
                "shared-native-event",
                "shared-writer-scope",
                "shared-retry-key",
            ),
        ]


def test_correction_cannot_move_claim_to_another_task(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x12)
        original_session = _session(
            repository,
            source.source_instance_id,
            "correction-original-session",
        )
        unrelated_session = _session(
            repository,
            source.source_instance_id,
            "correction-unrelated-session",
        )
        original_task = repository.get_or_create_task_anchor(original_session.row_id)
        unrelated_task = repository.get_or_create_task_anchor(unrelated_session.row_id)

        original = repository.append_work_claim(
            WorkClaimInput(
                fact=FactInput(
                    source_instance_id=source.source_instance_id,
                    source_event_id="scoped-original",
                    fact_type="work_claim",
                    transport="mcp",
                    strength="recorded_claim",
                    occurred_at_us=100,
                    content_hash=canonical_hash({"status": "started"}),
                ),
                event_kind="task",
                outcome_axis="execution",
                status="started",
                title="Original Task",
            )
        )
        assert original.row_id is not None
        assert repository.link_fact_to_session(
            FactSessionLinkInput(
                fact_id=original.row_id,
                session_id=original_session.row_id,
                method="explicit_client_session_id",
                confidence="exact",
                rule_version="synthetic-v1",
                validation_state="valid",
            )
        ).disposition == "inserted"

        correction = repository.append_work_claim(
            WorkClaimInput(
                fact=FactInput(
                    source_instance_id=source.source_instance_id,
                    source_event_id="scoped-correction",
                    fact_type="work_claim",
                    transport="mcp",
                    strength="recorded_claim",
                    occurred_at_us=200,
                    content_hash=canonical_hash({"status": "completed"}),
                    supersedes_fact_id=original.row_id,
                ),
                event_kind="task",
                outcome_axis="outcome",
                status="completed",
                title="Corrected Task",
            )
        )
        assert correction.row_id is not None
        inherited = store.connection.execute(
            "SELECT session_id, validation_state FROM fact_session_links WHERE fact_id = ?",
            (correction.row_id,),
        ).fetchall()
        assert [tuple(row) for row in inherited] == [
            (original_session.row_id, "valid")
        ]

        cross_task = repository.link_fact_to_session(
            FactSessionLinkInput(
                fact_id=correction.row_id,
                session_id=unrelated_session.row_id,
                method="explicit_client_session_id",
                confidence="exact",
                rule_version="synthetic-v1",
                validation_state="valid",
            )
        )
        assert cross_task.disposition == "inserted"
        assert tuple(
            store.connection.execute(
                "SELECT validation_state, veto_reason FROM fact_session_links "
                "WHERE fact_session_link_id = ?",
                (cross_task.row_id,),
            ).fetchone()
        ) == ("rejected", "correction_session_scope_mismatch")

        repository.rebuild_minimal_read_models()
        corrected_task = repository.get_task(original_task.public_task_id)
        untouched_task = repository.get_task(unrelated_task.public_task_id)
        assert corrected_task is not None
        assert corrected_task.title == "Corrected Task"
        assert corrected_task.projected_state == "reported_complete"
        assert untouched_task is not None
        assert untouched_task.title is None
        assert untouched_task.projected_state == "active"


def test_correction_chain_preserves_the_superseded_claim_logical_slot(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x12)
        session = _session(repository, source.source_instance_id, "correction-order-session")
        assert session.row_id is not None
        anchor = repository.get_or_create_task_anchor(session.row_id)

        def append_claim(
            *,
            event_id: str,
            status: str,
            title: str,
            occurred_at_us: int,
            source_order: int,
            supersedes_fact_id: int | None = None,
        ) -> int:
            result = repository.append_work_claim(
                WorkClaimInput(
                    fact=FactInput(
                        source_instance_id=source.source_instance_id,
                        source_event_id=event_id,
                        fact_type="work_claim",
                        transport="mcp",
                        strength="recorded_claim",
                        occurred_at_us=occurred_at_us,
                        source_order=source_order,
                        content_hash=canonical_hash(
                            {"event_id": event_id, "status": status, "title": title}
                        ),
                        supersedes_fact_id=supersedes_fact_id,
                    ),
                    event_kind="section",
                    outcome_axis="outcome",
                    status=status,
                    title=title,
                )
            )
            assert result.disposition == "inserted"
            assert result.row_id is not None
            link = repository.link_fact_to_session(
                FactSessionLinkInput(
                    fact_id=result.row_id,
                    session_id=session.row_id,
                    method="explicit_client_session_id",
                    confidence="exact",
                    rule_version="synthetic-v1",
                    validation_state="valid",
                )
            )
            assert link.disposition == (
                "noop" if supersedes_fact_id is not None else "inserted"
            )
            return result.row_id

        original_id = append_claim(
            event_id="logical-slot-original",
            status="started",
            title="Original latest claim",
            occurred_at_us=300,
            source_order=1,
        )
        append_claim(
            event_id="logical-slot-interleaved",
            status="blocked",
            title="Interleaved older claim",
            occurred_at_us=200,
            source_order=2,
        )
        append_claim(
            event_id="logical-slot-correction",
            status="completed",
            title="Corrected latest claim",
            occurred_at_us=100,
            source_order=3,
            supersedes_fact_id=original_id,
        )

        repository.rebuild_minimal_read_models()

        task = repository.get_task(anchor.public_task_id)
        assert task is not None
        assert task.title == "Corrected latest claim"
        assert task.projected_state == "reported_complete"


def test_usage_missingness_precedence_checkpoints_and_reprice_noops(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x22)
        session = _session(repository, source.source_instance_id, "usage-session")
        anchor = repository.get_or_create_task_anchor(session.row_id)

        authoritative = _usage(session.row_id)
        inserted = repository.reconcile_usage(authoritative)
        sequence_after_insert = repository.canonical_sequence()
        replayed = repository.reconcile_usage(authoritative)
        assert replayed.disposition == "noop"
        assert repository.canonical_sequence() == sequence_after_insert
        newer_poll = replace(
            authoritative,
            source_order=99,
            observed_at_us=authoritative.observed_at_us + 100,
            updated_at_us=authoritative.updated_at_us + 100,
        )
        changes_before_high_watermark = store.connection.total_changes
        assert repository.reconcile_usage(newer_poll).disposition == "noop"
        assert repository.canonical_sequence() == sequence_after_insert
        assert store.connection.total_changes == changes_before_high_watermark + 1
        assert store.connection.execute(
            "SELECT source_order FROM usage_measurements WHERE usage_measurement_id = ?",
            (inserted.row_id,),
        ).fetchone()[0] == 99
        stale_change = repository.reconcile_usage(
            replace(
                authoritative,
                input_tokens=1_001,
                total_tokens=1_001,
                source_order=50,
                observed_at_us=authoritative.observed_at_us + 50,
                updated_at_us=authoritative.updated_at_us + 50,
            )
        )
        assert stale_change.disposition == "conflict"
        stored_after_stale = store.connection.execute(
            "SELECT source_order, input_tokens, total_tokens FROM usage_measurements "
            "WHERE usage_measurement_id = ?",
            (inserted.row_id,),
        ).fetchone()
        assert tuple(stored_after_stale) == (99, 1_000, 1_000)
        with pytest.raises(ValueError, match="cumulative-only"):
            replace(authoritative, update_semantics="atomic_increment")

        stored = store.connection.execute(
            "SELECT input_tokens, input_tokens_reported, output_tokens, output_tokens_reported, "
            "cached_input_tokens, cached_input_tokens_reported FROM usage_measurements "
            "WHERE usage_measurement_id = ?",
            (inserted.row_id,),
        ).fetchone()
        assert tuple(stored) == (1000, 1, 0, 1, None, 0)

        held_fallback = _usage(
            session.row_id,
            representation="rollout",
            precedence_role="fallback",
            totals_eligible=False,
            held_reason="authoritative_representation_available",
            input_tokens=900,
            output_tokens=100,
            total_tokens=1000,
        )
        held = repository.reconcile_usage(held_fallback)
        assert held.disposition == "inserted"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM usage_measurements WHERE session_id = ? AND totals_eligible = 1",
            (session.row_id,),
        ).fetchone()[0] == 1
        promoted_overlap = repository.reconcile_usage(
            replace(
                held_fallback,
                totals_eligible=True,
                held_reason=None,
                source_order=2,
                updated_at_us=held_fallback.updated_at_us + 1,
            )
        )
        assert promoted_overlap.disposition == "conflict"

        checkpointed = repository.reconcile_usage(
            authoritative,
            checkpoint=("terminal", "synthetic-terminal"),
        )
        assert checkpointed.disposition == "updated"
        checkpoint_sequence = repository.canonical_sequence()
        assert repository.reconcile_usage(
            authoritative,
            checkpoint=("terminal", "synthetic-terminal"),
        ).disposition == "noop"
        assert repository.canonical_sequence() == checkpoint_sequence

        assert inserted.row_id is not None
        usage_hash = repository.usage_content_hash(authoritative)
        initial_price = CostCalculationInput(
            usage_measurement_id=inserted.row_id,
            usage_content_hash=usage_hash,
            price_source="synthetic-catalog",
            price_version="2026-07-v1",
            price_effective_at_us=1_783_000_000_000_000,
            formula_version="token-price-v1",
            completeness="complete",
            result_microusd=12_000,
            calculated_at_us=1_784_455_230_000_000,
        )
        price_insert = repository.record_cost(initial_price)
        sequence_after_price = repository.canonical_sequence()
        assert price_insert.disposition == "inserted"
        assert repository.record_cost(initial_price).disposition == "noop"
        assert repository.canonical_sequence() == sequence_after_price

        repriced = repository.record_cost(
            replace(
                initial_price,
                price_version="2026-07-v2",
                result_microusd=12_500,
                calculated_at_us=1_784_455_240_000_000,
            )
        )
        assert repriced.disposition == "inserted"
        assert store.connection.execute("SELECT COUNT(*) FROM usage_measurements").fetchone()[0] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM cost_calculations").fetchone()[0] == 2

        assert held.row_id is not None
        assert repository.record_cost(
            CostCalculationInput(
                usage_measurement_id=held.row_id,
                usage_content_hash=repository.usage_content_hash(held_fallback),
                price_source="synthetic-catalog",
                price_version="2026-07-v1",
                price_effective_at_us=1_783_000_000_000_000,
                formula_version="token-price-v1",
                completeness="unavailable",
                result_microusd=None,
                calculated_at_us=1_784_455_250_000_000,
            )
        ).disposition == "noop"

        repository.rebuild_minimal_read_models()
        task = repository.get_task(anchor.public_task_id)
        assert task is not None
        assert task.input_tokens == 1000
        assert task.output_tokens == 0
        assert task.total_tokens == 1000
        assert task.usage_missing_field_count == 4

        corrected_usage = replace(
            authoritative,
            output_tokens=100,
            total_tokens=1100,
            source_order=100,
            observed_at_us=newer_poll.observed_at_us + 1,
            updated_at_us=newer_poll.updated_at_us + 1,
        )
        corrected = repository.reconcile_usage(corrected_usage)
        assert corrected.disposition == "updated"
        checkpoint_snapshot = store.connection.execute(
            "SELECT output_tokens, output_tokens_reported, total_tokens, total_tokens_reported "
            "FROM usage_checkpoints WHERE usage_measurement_id = ?",
            (inserted.row_id,),
        ).fetchone()
        assert tuple(checkpoint_snapshot) == (0, 1, 1000, 1)
        repository.rebuild_minimal_read_models()
        daily = store.connection.execute(
            "SELECT estimated_cost_microusd, cost_completeness FROM rm_usage_day"
        ).fetchone()
        assert tuple(daily) == (None, "unavailable")


def test_partial_usage_keeps_explicit_zero_and_visible_missingness(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x33)
        session = _session(repository, source.source_instance_id, "partial-session")
        anchor = repository.get_or_create_task_anchor(session.row_id)
        partial = _usage(
            session.row_id,
            totals_eligible=False,
            held_reason="total_unavailable",
            input_tokens=0,
            input_reported=True,
            output_tokens=None,
            output_reported=False,
            total_tokens=None,
            total_reported=False,
        )
        repository.reconcile_usage(partial)
        repository.rebuild_minimal_read_models()

        task = repository.get_task(anchor.public_task_id)
        assert task is not None
        assert task.input_tokens == 0
        assert task.output_tokens is None
        assert task.total_tokens is None
        assert task.usage_missing_field_count == 6
        held_day = store.connection.execute(
            "SELECT usage_basis, measurement_count, held_measurement_count, "
            "input_tokens, input_reported_count, input_missing_count, "
            "output_tokens, output_reported_count, output_missing_count "
            "FROM rm_usage_day"
        ).fetchone()
        assert tuple(held_day) == (
            "authoritative_held_non_additive",
            1,
            1,
            0,
            1,
            0,
            None,
            0,
            1,
        )


def test_usage_day_projection_deltas_cumulative_snapshots_across_days(
    tmp_path: Path,
) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x34)
        session = _session(repository, source.source_instance_id, "cross-day-session")
        first_day = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)

        first = replace(
            _usage(
                session.row_id,
                input_tokens=100,
                output_tokens=0,
                total_tokens=100,
            ),
            observed_at_us=int(first_day.timestamp() * 1_000_000),
            updated_at_us=int(first_day.timestamp() * 1_000_000),
        )
        second = replace(
            first,
            input_tokens=150,
            total_tokens=150,
            source_order=2,
            observed_at_us=int((first_day + timedelta(days=1)).timestamp() * 1_000_000),
            updated_at_us=int((first_day + timedelta(days=1)).timestamp() * 1_000_000),
        )
        reset = replace(
            second,
            input_tokens=20,
            total_tokens=20,
            source_order=3,
            observed_at_us=int((first_day + timedelta(days=2)).timestamp() * 1_000_000),
            updated_at_us=int((first_day + timedelta(days=2)).timestamp() * 1_000_000),
        )

        assert repository.reconcile_usage(
            first,
            checkpoint=("day", "2026-07-18"),
        ).disposition == "inserted"
        assert repository.reconcile_usage(
            second,
            checkpoint=("day", "2026-07-19"),
        ).disposition == "updated"
        assert repository.reconcile_usage(
            reset,
            checkpoint=("day", "2026-07-20"),
        ).disposition == "updated"

        repository.rebuild_minimal_read_models()
        rows = store.connection.execute(
            "SELECT day, usage_basis, input_tokens, input_missing_count, "
            "total_tokens, total_missing_count FROM rm_usage_day ORDER BY day"
        ).fetchall()

        assert [tuple(row) for row in rows] == [
            ("2026-07-18", "authoritative_cumulative_delta", 100, 0, 100, 0),
            ("2026-07-19", "authoritative_cumulative_delta", 50, 0, 50, 0),
            ("2026-07-20", "authoritative_cumulative_delta", None, 1, None, 1),
        ]
        assert sum(row["input_tokens"] or 0 for row in rows) == 150


def test_1_1m_equivalent_replay_collapses_before_sqlite_write(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = _source(repository, 0x44)
        session = _session(repository, source.source_instance_id, "churn-session")
        usage = _usage(session.row_id)
        repository.reconcile_usage(usage)
        sequence_before = repository.canonical_sequence()
        changes_before = store.connection.total_changes
        bytes_before = store.file_bytes()

        result = repository.reconcile_usage_replay_batch(usage, attempts=1_100_000)

        assert result == {
            "attempts": 1_100_000,
            "canonical_writes": 0,
            "canonical_sequence_delta": 0,
            "inserted": 0,
            "noops": 1_100_000,
        }
        assert repository.canonical_sequence() == sequence_before
        assert store.connection.total_changes == changes_before
        assert store.file_bytes() == bytes_before
        assert store.connection.execute("SELECT COUNT(*) FROM usage_measurements").fetchone()[0] == 1
