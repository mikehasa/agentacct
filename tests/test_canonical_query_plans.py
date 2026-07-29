from __future__ import annotations

from pathlib import Path

from agentacct.canonical import (
    CanonicalStore,
    FactInput,
    SessionInput,
    SourceInstanceInput,
    UsageDayQuery,
    UsageMeasurementInput,
    canonical_hash,
)


def _seed_candidate(store: CanonicalStore) -> str:
    repository = store.repository()
    source = repository.get_or_create_source(
        SourceInstanceInput(
            client="codex",
            adapter="synthetic-query-plan",
            representation="sqlite",
            namespace_digest=b"q" * 32,
        )
    )
    session = repository.reconcile_session(
        SessionInput(
            source_instance_id=source.source_instance_id,
            client_session_id="query-plan-session",
            started_at_us=1_784_455_200_000_000,
            last_activity_at_us=1_784_455_300_000_000,
            observation_order=1,
        )
    )
    assert session.row_id is not None
    task = repository.get_or_create_task_anchor(session.row_id)
    repository.reconcile_usage(
        UsageMeasurementInput(
            session_id=session.row_id,
            lane="session_total",
            representation="sqlite",
            update_semantics="cumulative_snapshot",
            precedence_role="authoritative",
            granularity="session",
            totals_eligible=True,
            provider="openai",
            model="synthetic-model",
            usage_confidence="observed",
            input_tokens=100,
            input_tokens_reported=True,
            output_tokens=25,
            output_tokens_reported=True,
            total_tokens=125,
            total_tokens_reported=True,
            source_order=1,
            observed_at_us=1_784_455_300_000_000,
            updated_at_us=1_784_455_300_000_000,
        )
    )
    repository.rebuild_minimal_read_models()
    return task.public_task_id


def test_bounded_read_models_use_indexes_and_never_scan_canonical_facts(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        public_task_id = _seed_candidate(store)
        repository = store.repository()
        plans = repository.explain_core_queries()
        flattened = "\n".join(detail for plan in plans.values() for detail in plan)

        assert set(plans) == {"work", "task", "usage", "sessions", "health"}
        assert "idx_rm_task_work_recent" in " ".join(plans["work"])
        assert "sqlite_autoindex_rm_task_current_1" in " ".join(plans["task"])
        assert len(plans["usage"]) == 4
        assert "idx_rm_usage_day_range" in " ".join(plans["usage"])
        assert "idx_rm_usage_client_range" in " ".join(plans["usage"])
        assert "idx_rm_usage_model_range" in " ".join(plans["usage"])
        sessions_plans = " ".join(plans["sessions"])
        assert "idx_sessions_recent" in sessions_plans
        assert len(plans["sessions"]) == 4, "phase-4.2 by-ids/edge lookups are plan-guarded too"
        assert "idx_session_edges_child" in sessions_plans
        assert "SCAN sessions" not in sessions_plans
        assert "SCAN source_instances" not in sessions_plans
        assert "SCAN session_edges" not in sessions_plans
        assert len(plans["health"]) == 3
        assert "INTEGER PRIMARY KEY" in " ".join(plans["health"])
        assert "projection_generations" in " ".join(plans["health"])
        assert "source_health_current" in " ".join(plans["health"])
        assert "SCAN facts" not in flattened
        assert "SCAN usage_measurements" not in flattened
        assert "SCAN work_claims" not in flattened

        changes_before = store.connection.total_changes
        assert repository.get_task(public_task_id) is not None
        assert len(repository.list_tasks(limit=10)) == 1
        sessions = repository.list_sessions(limit=10)
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        assert set(repository.get_sessions_by_ids([session_id])) == {session_id}
        assert set(
            repository.get_source_instances_by_ids([sessions[0].source_instance_id])
        ) == {sessions[0].source_instance_id}
        assert repository.valid_lineage_edges([session_id]) == {}
        assert len(
            repository.usage_days(
                UsageDayQuery(
                    start_day="2026-01-01",
                    end_day="2026-12-31",
                    client="codex",
                )
            )
        ) == 1
        repository.health_summary()
        assert store.connection.total_changes == changes_before


def test_fact_identity_lookups_use_source_scoped_unique_indexes(tmp_path: Path) -> None:
    with CanonicalStore.create(tmp_path / "candidate.sqlite3") as store:
        repository = store.repository()
        source = repository.get_or_create_source(
            SourceInstanceInput(
                client="codex",
                adapter="synthetic-query-plan",
                representation="sqlite",
                namespace_digest=b"i" * 32,
            )
        )
        repository.append_fact(
            FactInput(
                source_instance_id=source.source_instance_id,
                source_event_id="query-plan-event",
                idempotency_scope="query-plan-scope",
                idempotency_key="query-plan-key",
                fact_type="mechanical_observation",
                transport="synthetic",
                strength="mechanical_observation",
                occurred_at_us=1,
                content_hash=canonical_hash("query-plan-fact"),
            )
        )

        source_event_plan = " ".join(
            str(row[3])
            for row in store.connection.execute(
                "EXPLAIN QUERY PLAN SELECT fact_id, content_hash FROM facts "
                "WHERE source_instance_id = ? AND source_event_id = ?",
                (source.source_instance_id, "query-plan-event"),
            )
        )
        idempotency_plan = " ".join(
            str(row[3])
            for row in store.connection.execute(
                "EXPLAIN QUERY PLAN SELECT fact_id, content_hash FROM facts "
                "WHERE source_instance_id = ? AND idempotency_scope = ? "
                "AND idempotency_key = ?",
                (source.source_instance_id, "query-plan-scope", "query-plan-key"),
            )
        )

        assert "uq_facts_source_event" in source_event_plan
        assert "uq_facts_idempotency" in idempotency_plan
