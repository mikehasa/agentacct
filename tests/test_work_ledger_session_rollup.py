"""Session rollup (Phase 2 item 2a): pure derivation, exact key equality only.

The rollup reads already-computed decisions (build_usage_events /
build_attributions / build_work_items) and re-matches nothing, so these tests
pin the honesty invariants: it can never contradict the canonical attribution
model, children totals never merge into the parent, and display labels never
leak a full session id or path.
"""

from __future__ import annotations

import random

import agentacct.work_ledger as work_ledger_module
from agentacct.task_projection import build_task_projection
from agentacct.work_ledger import build_work_ledger


def _usage(
    *,
    session: str,
    client: str = "codex",
    event_id: str | None = None,
    transcript: str | None = None,
    parent: str | None = None,
    kind: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 25,
    cache_creation: int = 0,
    cache_read: int = 0,
    model: str = "gpt-5.5",
    lane: str | None = None,
    cost: float | None = 0.25,
    created_at: float = 100.0,
    started_at: float | None = None,
    updated_at: float | None = None,
    project_dir: str | None = "/tmp/project",
    turn_count: object = None,
    namespace_fingerprint: str | None = None,
    source_namespace_fingerprint: str | None = None,
    parent_source_namespace_fingerprint: str | None = None,
) -> dict:
    metadata = {
        "usage_source": "local_client_session_store",
        "usage_provenance": "agent_sentinel_local_usage_import",
        "client": client,
        "client_session_id": session,
        "client_transcript_id": transcript,
        "parent_client_session_id": parent,
        "client_session_kind": kind,
        "cached_input_tokens": cache_creation + cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "project_dir": project_dir,
        "started_at": started_at,
        "updated_at": updated_at,
        "session_namespace_fingerprint": namespace_fingerprint,
        "identity_scope_state": "explicit" if namespace_fingerprint else None,
        "source_namespace_fingerprint": source_namespace_fingerprint,
        "parent_source_namespace_fingerprint": parent_source_namespace_fingerprint,
    }
    if turn_count is not None:
        metadata["turn_count"] = turn_count
    if lane is not None:
        metadata["usage_row_lane"] = lane
    return {
        "event_id": event_id or f"evt_usage_{client}_{session}_{model}",
        "created_at": created_at,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "run_id": None,
        "provider": client,
        "model": model,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": cost,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": metadata,
    }


def _section(
    *,
    session: str | None,
    client: str = "codex",
    section_id: str = "sec-1",
    transcript: str | None = None,
    status: str = "checkpoint",
    created_at: float = 100.0,
    event_id: str | None = None,
    namespace_fingerprint: str | None = None,
    files: list[str] | None = None,
    run_id: str | None = None,
) -> dict:
    return {
        "event_id": event_id or f"evt_section_{client}_{session}_{section_id}_{status}",
        "created_at": created_at,
        "source": client,
        "event_type": f"section_{status}",
        "run_id": run_id,
        "metadata": {
            "sentinel_semantic_kind": "section",
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            "client_context_keys_authored": [
                key for key, value in (("client_session_id", session), ("client_transcript_id", transcript)) if value
            ],
            "project_dir": "/tmp/project",
            "session_namespace_fingerprint": namespace_fingerprint,
            "identity_scope_state": "explicit" if namespace_fingerprint else None,
            "section_id": section_id,
            "section_status": status,
            "section_title": f"Section {section_id}",
            "kind": "implementation",
            "files": list(files or []),
        },
    }


def _machine_check(
    *,
    event_id: str,
    session: str,
    namespace_fingerprint: str,
    section_id: str | None = None,
    run_id: str | None = None,
    result: str = "failed",
    summary: str = "check summary",
) -> dict:
    return {
        "event_id": event_id,
        "created_at": 200.0,
        "source": "codex",
        "event_type": "machine_check",
        "run_id": run_id,
        "metadata": {
            "result": result,
            "evidence_type": "test",
            "summary": summary,
            "section_id": section_id,
            "client": "codex",
            "client_session_id": session,
            "session_namespace_fingerprint": namespace_fingerprint,
            "identity_scope_state": "explicit",
            "project_dir": "/tmp/project",
        },
    }


def _rollup(events: list[dict]) -> dict:
    return build_work_ledger(events)["session_rollup"]


def _entry(rollup: dict, client: str, session: str) -> dict:
    for entry in rollup["sessions"]:
        if entry["client"] == client and entry["client_session_id"] == session:
            return entry
    raise AssertionError(f"no rollup entry for {client}::{session}")


def _mechanical_observation(
    session: str, *, namespace_fingerprint: str | None = None
) -> dict:
    return {
        "client": "codex",
        "client_session_id": session,
        "first_activity_at": 100.0,
        "last_activity_at": 101.0,
        "parent_client_session_id": None,
        "parent_state": "absent",
        "session_kind": "root",
        "project": None,
        "project_source": None,
        "identity_scope_state": "explicit" if namespace_fingerprint else "unscoped",
        "namespace_fingerprint": namespace_fingerprint,
        "activity_time_basis": "host_event_time",
        "missing_host_timestamp_count": 0,
        "observed_models": ["gpt-hook"],
        "source_instances": ["local-hooks"],
        "event_types": ["session_observed"],
        "host_events": ["SessionStart"],
        "observation_count": 1,
    }


def test_custom_store_refuses_unscoped_hook_enrichment_of_existing_usage() -> None:
    ledger = build_work_ledger(
        [_usage(session="shared-session")],
        session_observations=[_mechanical_observation("shared-session")],
        store_scope="custom",
    )
    entry = _entry(ledger["session_rollup"], "codex", "shared-session")

    assert entry["usage"]["rows"] == 1
    assert entry["mechanical_capture"]["observation_count"] == 0
    assert entry["observed_models"] == []
    assert ledger["session_rollup"]["summary"]["mechanical_namespace_join_refusals"] == 1


def test_explicit_hook_namespace_requires_matching_v1_namespace() -> None:
    ledger = build_work_ledger(
        [_usage(session="shared-session")],
        session_observations=[
            _mechanical_observation("shared-session", namespace_fingerprint="ns:project-a")
        ],
        store_scope="project",
    )
    entry = _entry(ledger["session_rollup"], "codex", "shared-session")

    assert entry["usage"]["rows"] == 1
    assert entry["mechanical_capture"]["observation_count"] == 0
    assert ledger["session_rollup"]["summary"]["mechanical_namespace_join_refusals"] == 1


def test_cross_namespace_usage_and_work_never_coalesce_in_custom_store() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                namespace_fingerprint="ns:project-a",
            ),
            _section(
                session="shared-session",
                status="completed",
                namespace_fingerprint="ns:project-b",
            ),
        ],
        store_scope="custom",
    )

    entry = _entry(ledger["session_rollup"], "codex", "shared-session")
    assert entry["namespace_fingerprint"] == "ns:project-a"
    assert entry["usage"]["rows"] == 1
    assert entry["work"]["counts"]["total"] == 0
    assert ledger["session_rollup"]["summary"]["work_namespace_join_refusals"] == 1
    assert ledger["session_rollup"]["summary"]["unassigned_work_item_refs"] == [
        "codex::shared-session::sec-1"
    ]

    # Canonical attribution is isolated too: the work item stays visible but
    # receives neither the usage row nor its token/cost totals.
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"
    assert ledger["attributions"][0]["join_vetoes"] == [
        "namespace_fingerprint_conflict"
    ]
    assert len(ledger["work_items"]) == 1
    assert ledger["work_items"][0]["latest_status"] == "completed"
    assert ledger["work_items"][0]["linked_usage_records"] == 0
    assert ledger["work_items"][0]["usage_total"] == 0


def test_custom_store_refuses_explicit_usage_to_legacy_unscoped_work() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                namespace_fingerprint="ns:project-a",
            ),
            _section(session="shared-session", status="completed"),
        ],
        store_scope="custom",
    )

    entry = _entry(ledger["session_rollup"], "codex", "shared-session")
    assert entry["work"]["counts"]["total"] == 0
    assert ledger["attributions"][0]["join_vetoes"] == [
        "namespace_fingerprint_missing"
    ]
    assert ledger["session_rollup"]["summary"]["work_namespace_join_refusals"] == 1


def test_project_store_keeps_explicit_to_legacy_work_compatibility() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                namespace_fingerprint="ns:project-a",
            ),
            _section(session="shared-session", status="completed"),
        ],
        store_scope="project",
    )

    entry = _entry(ledger["session_rollup"], "codex", "shared-session")
    assert entry["allow_legacy_unscoped_namespace_join"] is True
    assert entry["work"]["counts"]["total"] == 1
    assert ledger["attributions"][0]["work_id"] == "codex::shared-session::sec-1"
    assert ledger["session_rollup"]["summary"]["work_namespace_join_refusals"] == 0


def test_conflicting_usage_namespace_cohort_is_quarantined_from_rollup() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                event_id="usage-project-a",
                namespace_fingerprint="ns:project-a",
            ),
            _usage(
                session="shared-session",
                event_id="usage-project-b",
                namespace_fingerprint="ns:project-b",
            ),
        ],
        store_scope="custom",
    )

    # The trusted facts stay visible, but v1's bare session key is not allowed
    # to erase their identity boundary by manufacturing one combined session.
    assert {row["usage_event_id"] for row in ledger["usage_events"]} == {
        "usage-project-a",
        "usage-project-b",
    }
    assert ledger["session_rollup"]["sessions"] == []
    summary = ledger["session_rollup"]["summary"]
    assert summary["usage_namespace_collision_sessions"] == 1
    assert summary["usage_namespace_collision_rows"] == 2
    assert summary["totals"]["total_tokens"] == 0


def test_conflicting_usage_source_namespace_cohort_is_quarantined() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                event_id="usage-home-a",
                source_namespace_fingerprint="sha256:home-a",
            ),
            _usage(
                session="shared-session",
                event_id="usage-home-b",
                source_namespace_fingerprint="sha256:home-b",
            ),
        ],
        store_scope="project",
    )

    assert len(ledger["usage_events"]) == 2
    assert ledger["session_rollup"]["sessions"] == []
    summary = ledger["session_rollup"]["summary"]
    assert summary["usage_namespace_collision_sessions"] == 1
    assert summary["usage_namespace_collision_rows"] == 2


def test_usage_source_namespace_explicit_to_missing_is_quarantined() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                event_id="usage-home-a",
                source_namespace_fingerprint="sha256:home-a",
            ),
            _usage(session="shared-session", event_id="usage-legacy"),
        ],
        store_scope="project",
    )

    assert ledger["usage_events"] == []
    assert len(ledger["excluded_usage_events"]) == 2
    assert {
        row["usage_normalization_state"]
        for row in ledger["excluded_usage_events"]
    } == {"source_namespace_missing_vs_explicit"}
    assert all(
        row["usage_additive"] is False
        and row["total_tokens"] == 0
        and row["estimated_cost_usd"] is None
        for row in ledger["excluded_usage_events"]
    )
    assert ledger["attributions"] == []
    assert ledger["usage_reconciliation"] == []
    assert ledger["session_rollup"]["sessions"] == []
    summary = ledger["session_rollup"]["summary"]
    assert summary["usage_namespace_collision_rows"] == 2
    assert summary["totals"]["total_tokens"] == 0
    assert ledger["insights"]["usage_additivity_quarantine"] == {
        "excluded_rows": 2,
        "by_state": {"source_namespace_missing_vs_explicit": 2},
        "raw_evidence_preserved": True,
    }


def test_cross_namespace_work_cohort_is_quarantined_before_product_aggregation() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="shared-session",
                event_id="usage-project-a",
                namespace_fingerprint="ns:project-a",
            ),
            _section(
                session="shared-session",
                section_id="shared-work",
                status="started",
                event_id="work-project-a",
                created_at=100.0,
                namespace_fingerprint="ns:project-a",
                files=["src/project_a.py"],
            ),
            _section(
                session="shared-session",
                section_id="shared-work",
                status="completed",
                event_id="work-project-b",
                created_at=101.0,
                namespace_fingerprint="ns:project-b",
                files=["src/project_b.py"],
            ),
        ],
        store_scope="custom",
    )

    # Both raw snapshots remain independently inspectable; neither namespace
    # can donate status/files to a synthetic merged work item.
    assert {
        event["event_id"]: event["files"] for event in ledger["work_events"]
    } == {
        "work-project-a": ["src/project_a.py"],
        "work-project-b": ["src/project_b.py"],
    }
    assert ledger["insights"]["work_event_namespace_quarantine"] == {
        "ambiguous_cohorts": 1,
        "quarantined_snapshots": 2,
    }
    assert ledger["work_items"] == []
    assert ledger["attributions"][0]["work_id"] is None
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"

    entry = _entry(ledger["session_rollup"], "codex", "shared-session")
    assert entry["usage"]["rows"] == 1
    assert entry["work"]["counts"]["total"] == 0
    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )
    assert projection["tasks"][0]["work_items"] == []
    assert projection["summary"]["associated_work_count"] == 0
    assert "project_a.py" not in repr(projection)
    assert "project_b.py" not in repr(projection)


def test_project_store_keeps_legacy_unscoped_work_snapshot_in_cohort() -> None:
    ledger = build_work_ledger(
        [
            _section(
                session="shared-session",
                section_id="shared-work",
                status="started",
                event_id="work-explicit",
                created_at=100.0,
                namespace_fingerprint="ns:project-a",
                files=["src/explicit.py"],
            ),
            _section(
                session="shared-session",
                section_id="shared-work",
                status="completed",
                event_id="work-legacy",
                created_at=101.0,
                files=["src/legacy.py"],
            ),
        ],
        store_scope="project",
    )

    assert ledger["insights"]["work_event_namespace_quarantine"] == {
        "ambiguous_cohorts": 0,
        "quarantined_snapshots": 0,
    }
    assert len(ledger["work_items"]) == 1
    assert ledger["work_items"][0]["files"] == [
        "src/explicit.py",
        "src/legacy.py",
    ]
    entry = _entry(ledger["session_rollup"], "codex", "shared-session")
    assert entry["work"]["counts"]["total"] == 1


def test_malformed_explicit_work_singleton_is_quarantined() -> None:
    event = _section(
        session="shared-session",
        section_id="malformed-work",
        status="completed",
        files=["src/malformed.py"],
    )
    event["metadata"]["identity_scope_state"] = "explicit"
    ledger = build_work_ledger([event], store_scope="project")

    assert len(ledger["work_events"]) == 1
    assert ledger["work_items"] == []
    assert ledger["session_rollup"]["sessions"] == []
    assert ledger["insights"]["work_event_namespace_quarantine"] == {
        "ambiguous_cohorts": 1,
        "quarantined_snapshots": 1,
    }


def test_large_same_work_namespace_cohort_projects_without_pairwise_matching() -> None:
    events = [
        _section(
            session="shared-session",
            section_id="large-cohort",
            status="checkpoint",
            event_id=f"large-{index}",
            created_at=float(index),
            namespace_fingerprint="ns:project-a",
        )
        for index in range(2_000)
    ]
    ledger = build_work_ledger(events, store_scope="custom")

    assert len(ledger["work_events"]) == 2_000
    assert len(ledger["work_items"]) == 1
    assert ledger["insights"]["work_event_namespace_quarantine"] == {
        "ambiguous_cohorts": 0,
        "quarantined_snapshots": 0,
    }


def test_many_work_items_in_one_session_use_linear_namespace_reduction(monkeypatch) -> None:
    calls = 0
    real_join = work_ledger_module.namespace_join_compatible

    def counted_join(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_join(*args, **kwargs)

    monkeypatch.setattr(work_ledger_module, "namespace_join_compatible", counted_join)
    events = [
        _section(
            session="noisy-session",
            section_id=f"section-{index}",
            status="completed",
            event_id=f"noisy-{index}",
            created_at=float(index),
            namespace_fingerprint="ns:project-a",
        )
        for index in range(2_000)
    ]

    ledger = build_work_ledger(events, store_scope="custom")

    assert len(ledger["work_items"]) == 2_000
    assert len(ledger["session_rollup"]["sessions"]) == 1
    assert calls == 0


def test_foreign_namespace_evidence_never_attaches_by_work_ref_or_run() -> None:
    events = [
        _section(
            session="shared-session",
            section_id="shared-section",
            status="completed",
            namespace_fingerprint="ns:project-a",
            run_id="shared-run",
        ),
        _machine_check(
            event_id="foreign-by-ref",
            session="shared-session",
            namespace_fingerprint="ns:project-b",
            section_id="shared-section",
            summary="FOREIGN SECRET BY REF",
        ),
        _machine_check(
            event_id="foreign-by-run",
            session="shared-session",
            namespace_fingerprint="ns:project-b",
            run_id="shared-run",
            summary="FOREIGN SECRET BY RUN",
        ),
        _machine_check(
            event_id="matching-by-ref",
            session="shared-session",
            namespace_fingerprint="ns:project-a",
            section_id="shared-section",
            result="passed",
            summary="matching check",
        ),
    ]

    ledger = build_work_ledger(events, store_scope="custom")

    assert len(ledger["work_items"]) == 1
    item = ledger["work_items"][0]
    assert [event["event_id"] for event in item["evidence_events"]] == [
        "matching-by-ref"
    ]
    assert item["evidence_status"] == "strong"
    assert all(
        "FOREIGN SECRET" not in str(event.get("summary") or "")
        for event in item["evidence_events"]
    )
    projected = {
        event["event_id"]: event for event in ledger["evidence_events"]
    }
    assert projected["foreign-by-ref"]["session_namespace_fingerprint"] == (
        "ns:project-b"
    )


def test_rollup_preserves_source_namespace_separately_from_semantic_namespace() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="child",
                parent="root",
                namespace_fingerprint="semantic:project-a",
                source_namespace_fingerprint="sha256:home-a",
                parent_source_namespace_fingerprint="sha256:home-a",
            )
        ],
        store_scope="custom",
    )
    entry = _entry(ledger["session_rollup"], "codex", "child")

    assert entry["namespace_fingerprint"] == "semantic:project-a"
    assert entry["source_namespace_fingerprint"] == "sha256:home-a"
    assert entry["parent_source_namespace_fingerprint"] == "sha256:home-a"


def test_rollup_one_entry_per_client_and_base_session() -> None:
    rollup = _rollup(
        [
            _usage(session="sess-a", event_id="u1", input_tokens=100, output_tokens=0),
            _usage(session="sess-a", event_id="u2", input_tokens=50, output_tokens=0, model="gpt-5.5-mini"),
            _usage(session="sess-b", event_id="u3", input_tokens=10, output_tokens=0),
            _usage(session="claude-sess", client="claude-code", event_id="u4", input_tokens=7, output_tokens=0, model="claude-opus-4-8"),
        ]
    )

    assert rollup["schema_version"] == "agent-sentinel.session-rollup.v1"
    keys = {entry["session_key"] for entry in rollup["sessions"]}
    assert keys == {"codex::sess-a", "codex::sess-b", "claude-code::claude-sess"}
    entry_a = _entry(rollup, "codex", "sess-a")
    assert entry_a["usage"]["rows"] == 2
    assert entry_a["usage"]["fresh_tokens"] == 150
    assert rollup["summary"]["total_sessions"] == 3
    assert rollup["summary"]["sessions_with_usage"] == 3
    assert rollup["summary"]["totals"]["fresh_tokens"] == 167


def test_rollup_groups_legacy_model_suffixed_rows_under_base_and_excludes_shadowed() -> None:
    def _legacy(event_id: str, session_key: str, tokens: int, model: str) -> dict:
        row = _usage(
            session=session_key,
            client="claude-code",
            event_id=event_id,
            input_tokens=tokens,
            output_tokens=0,
            model=model,
        )
        return row

    events = [
        _legacy("evt_stale_base", "S", 1000, "claude-opus-4-8"),
        _legacy("evt_lane_opus", "S:model:claude-opus-4-8", 3000, "claude-opus-4-8"),
        _legacy("evt_lane_sonnet", "S:model:claude-sonnet-4-9", 2000, "claude-sonnet-4-9"),
    ]
    rollup = _rollup(events)

    # One entry keyed by the TRUE base session id; the stale shadowed base row
    # is excluded (not double-counted into the entry).
    assert [entry["client_session_id"] for entry in rollup["sessions"]] == ["S"]
    entry = _entry(rollup, "claude-code", "S")
    assert entry["usage"]["rows"] == 2
    assert entry["usage"]["fresh_tokens"] == 5000
    assert len(entry["usage"]["model_lanes"]) == 2
    assert {lane["lane"] for lane in entry["usage"]["model_lanes"]} == {
        "model:claude-opus-4-8",
        "model:claude-sonnet-4-9",
    }


def test_rollup_multi_model_claude_session_is_one_entry_with_two_lanes() -> None:
    rollup = _rollup(
        [
            _usage(session="S", client="claude-code", event_id="u1", model="claude-opus-4-8", lane="model:claude-opus-4-8", input_tokens=10, output_tokens=0),
            _usage(session="S", client="claude-code", event_id="u2", model="claude-sonnet-4-9", lane="model:claude-sonnet-4-9", input_tokens=20, output_tokens=0),
        ]
    )

    assert rollup["summary"]["total_sessions"] == 1
    entry = _entry(rollup, "claude-code", "S")
    assert len(entry["usage"]["model_lanes"]) == 2
    lanes = {lane["lane"]: lane for lane in entry["usage"]["model_lanes"]}
    assert lanes["model:claude-opus-4-8"]["fresh_tokens"] == 10
    assert lanes["model:claude-sonnet-4-9"]["fresh_tokens"] == 20


def test_rollup_exact_section_matching_only() -> None:
    """Items attach by their OWN (client, session) only: a transcript-only item
    goes to the unassigned bucket even when a usage row was ATTRIBUTED to it via
    the transcript — the row-level decision still shows in attributed_work, so
    nothing is lost and nothing is allocated."""

    ledger = build_work_ledger(
        [
            _usage(session="usage-session-long-id", event_id="u1", transcript="tx-1"),
            _section(session=None, transcript="tx-1", section_id="tx-only"),
            _section(session=None, section_id="no-ids"),
            _section(session="other-session-long-id", section_id="other-work"),
        ]
    )
    rollup = ledger["session_rollup"]

    attributed = [attr for attr in ledger["attributions"] if attr.get("work_id")]
    assert len(attributed) == 1
    assert attributed[0]["join_strategy"] == "exact_client_transcript_id"

    entry = _entry(rollup, "codex", "usage-session-long-id")
    # The transcript-attributed item is NOT attached as this session's work...
    assert entry["work"]["items"] == []
    # ...but the row-level attribution is still visible, verbatim.
    assert entry["join"]["state"] == "attributed"
    assert [work["work_id"] for work in entry["join"]["attributed_work"]] == [attributed[0]["work_id"]]
    # Both id-less items land in the unassigned bucket; the session-bearing
    # section forms its own entry.
    assert rollup["summary"]["unassigned_work_items"] == 2
    assert len(rollup["summary"]["unassigned_work_item_refs"]) == 2
    other = _entry(rollup, "codex", "other-session-long-id")
    assert [item["section_id"] for item in other["work"]["items"]] == ["other-work"]


def test_rollup_sections_only_usage_only_and_ambiguous_states() -> None:
    rollup = _rollup(
        [
            _section(session="solo-section-session", section_id="solo-work", status="completed"),
            _usage(session="lonely-usage-session", event_id="u1"),
            _usage(session="shared-session-xyz", event_id="u2"),
            _section(session="shared-session-xyz", section_id="amb-a"),
            _section(session="shared-session-xyz", section_id="amb-b"),
        ]
    )

    sections_only = _entry(rollup, "codex", "solo-section-session")
    assert sections_only["usage"]["rows"] == 0
    assert sections_only["usage"]["total_tokens"] == 0
    assert sections_only["usage"]["estimated_cost_usd"] is None
    assert sections_only["join"]["state"] == "sections_only"
    assert "not a zero-cost claim" in sections_only["usage_note"]
    assert sections_only["activity_time_source"] == "mcp_event_time"

    usage_only = _entry(rollup, "codex", "lonely-usage-session")
    assert usage_only["work"]["items"] == []
    assert usage_only["join"]["state"] == "unjoined"
    assert usage_only["usage_note"] is None

    ambiguous = _entry(rollup, "codex", "shared-session-xyz")
    assert ambiguous["join"]["state"] == "ambiguous"
    assert set(ambiguous["join"]["ambiguous_candidate_work_ids"]) == {
        "codex::shared-session-xyz::amb-a",
        "codex::shared-session-xyz::amb-b",
    }
    assert ambiguous["join"]["attributed_work"] == []

    assert rollup["summary"]["sessions_with_sections_only"] == 1
    assert rollup["summary"]["attributed_sessions"] == 0


def test_rollup_codex_children_stay_visible_but_cumulative_usage_is_held() -> None:
    rollup = _rollup(
        [
            _usage(session="parent-session-long", event_id="p1", kind="root", input_tokens=1000, output_tokens=0, cost=1.0),
            _usage(session="parent-session-long:agent-child000000001", event_id="c1", kind="child", parent="parent-session-long", input_tokens=200, output_tokens=0, cost=0.2),
            _usage(session="parent-session-long:agent-child000000002", event_id="c2", kind="child", parent="parent-session-long", input_tokens=300, output_tokens=0, cost=0.3),
        ]
    )

    parent = _entry(rollup, "codex", "parent-session-long")
    # Own totals only.  Codex descendants remain first-class session rows, but
    # their legacy cumulative counters are not additive until lineage replay
    # normalization can prove an exclusive delta.
    assert parent["usage"]["fresh_tokens"] == 1000
    assert parent["usage"]["total_tokens"] == 1000
    assert parent["usage"]["estimated_cost_usd"] == 1.0
    children_usage = parent["related"]["children_usage"]
    assert children_usage == {
        "sessions": 2,
        "fresh_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": None,
        "excluded_non_additive_rows": 2,
    }
    assert parent["related"]["child_session_count"] == 2
    assert len(parent["related"]["child_session_labels"]) == 2
    assert parent["related"]["relationship_source"] == "importer_recorded_parent_id"

    child = _entry(rollup, "codex", "parent-session-long:agent-child000000001")
    assert child["related"]["parent"]["client_session_id"] == "parent-session-long"
    assert child["rollup_group_key"] == "codex::parent-session-long"
    assert parent["rollup_group_key"] == "codex::parent-session-long"
    assert child["usage"]["rows"] == 1
    assert child["usage"]["additive_rows"] == 0
    assert child["usage"]["excluded_non_additive_rows"] == 1
    assert child["usage"]["total_tokens"] == 0
    assert child["usage"]["estimated_cost_usd"] is None
    assert "preserved as raw evidence" in child["usage_note"]
    assert "not a zero-usage or zero-cost claim" in child["usage_note"]

    assert rollup["summary"]["root_sessions"] == 1
    assert rollup["summary"]["child_sessions"] == 2
    # Store totals count only proven-additive usage; the two held rows remain
    # represented by the per-session explicit excluded counters above.
    assert rollup["summary"]["totals"]["fresh_tokens"] == 1000


def test_codex_child_quarantine_never_enters_usage_product_surfaces() -> None:
    ledger = build_work_ledger(
        [
            _usage(
                session="root-session-long",
                event_id="root-usage",
                kind="root",
                input_tokens=1_000,
                output_tokens=0,
                cost=1.0,
            ),
            _usage(
                session="child-session-long",
                event_id="child-cumulative-usage",
                kind="child",
                parent="root-session-long",
                input_tokens=900,
                output_tokens=50,
                cache_read=8_000,
                cost=9.5,
            ),
            _section(
                session="root-session-long",
                section_id="root-work",
                status="checkpoint",
            ),
            _section(
                session="child-session-long",
                section_id="child-work",
                status="checkpoint",
            ),
        ]
    )

    # Raw child evidence is retained with its lineage and original counters,
    # but only the root row participates in attribution and reconciliation.
    assert [row["usage_event_id"] for row in ledger["usage_events"]] == [
        "root-usage"
    ]
    assert [row["usage_event_id"] for row in ledger["excluded_usage_events"]] == [
        "child-cumulative-usage"
    ]
    excluded = ledger["excluded_usage_events"][0]
    assert excluded["client_session_id"] == "child-session-long"
    assert excluded["parent_client_session_id"] == "root-session-long"
    assert excluded["usage_additive"] is False
    assert excluded["raw_cumulative_input_tokens"] == 900
    assert excluded["raw_cumulative_output_tokens"] == 50
    assert excluded["raw_cumulative_cached_input_tokens"] == 8_000
    assert [row["usage_event_id"] for row in ledger["attributions"]] == [
        "root-usage"
    ]
    assert [row["usage_event_id"] for row in ledger["usage_reconciliation"]] == [
        "root-usage"
    ]
    assert ledger["attention_items"] == []

    overview = ledger["overview"]
    assert overview["usage_event_count"] == 1
    assert overview["attributed_usage_count"] == 1
    assert overview["total_tokens"] == 1_000
    assert overview["estimated_cost_total"] == 1.0
    assert ledger["insights"]["usage_additivity_quarantine"] == {
        "excluded_rows": 1,
        "by_state": {"legacy_codex_descendant_cumulative_unproven": 1},
        "raw_evidence_preserved": True,
    }

    child = _entry(
        ledger["session_rollup"], "codex", "child-session-long"
    )
    assert child["related"]["parent"]["client_session_id"] == "root-session-long"
    assert child["usage"]["rows"] == 1
    assert child["usage"]["additive_rows"] == 0
    assert child["usage"]["excluded_non_additive_rows"] == 1
    assert child["join"]["state"] == "sections_only"
    assert "not a zero-usage or zero-cost claim" in child["usage_note"]

    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )
    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert task["primary_root"] == {
        "client": "codex",
        "client_session_id": "root-session-long",
    }
    assert task["session_count"] == 2
    assert task["supporting_count"] == 1
    assert task["child_count"] == 1
    assert task["usage"]["rows"] == 2
    assert task["usage"]["additive_rows"] == 1
    assert task["usage"]["excluded_non_additive_rows"] == 1
    assert task["usage"]["total_tokens"] == 1_000
    assert task["usage"]["known_additive_cost_usd"] == 1.0
    assert task["usage"]["estimated_cost_usd"] is None
    assert task["usage"]["cost_complete"] is False
    assert task["usage"]["usage_availability"] == "partial"


def test_rollup_conflicting_parent_pointers_refuse_and_note() -> None:
    rollup = _rollup(
        [
            _usage(session="confused-session", event_id="u1", parent="parent-a"),
            _usage(session="confused-session", event_id="u2", parent="parent-b"),
        ]
    )

    entry = _entry(rollup, "codex", "confused-session")
    assert entry["related"]["parent"] is None
    assert entry["related"]["note"] == "conflicting parent pointers"
    # No parent claim -> the entry groups under itself.
    assert entry["rollup_group_key"] == "codex::confused-session"


def test_rollup_parent_without_entry_keeps_reference_without_stub() -> None:
    # Tail-unique ids: codex labels keep the LAST 8 chars, so the entry and
    # the ghost parent must not share a tail (that would only test the
    # collision suffixer, covered elsewhere).
    rollup = _rollup([_usage(session="orphan-child-thread", event_id="u1", parent="ghost-parent-session")])

    assert [entry["client_session_id"] for entry in rollup["sessions"]] == ["orphan-child-thread"]
    entry = rollup["sessions"][0]
    assert entry["related"]["parent"]["client_session_id"] == "ghost-parent-session"
    assert entry["related"]["parent"]["label"] == "ghost-parent-session"[-8:]  # codex tail rule, 8 chars
    assert entry["related"]["parent"]["label"] == "-session"
    assert entry["rollup_group_key"] == "codex::ghost-parent-session"


def test_rollup_display_labels_component_rule_and_redaction() -> None:
    root_id = "f224d28b-1111-2222-3333-444455556666"
    child_id = f"{root_id}:agent-ae598f4d012345678"
    rollup = _rollup(
        [
            _usage(session=root_id, client="claude-code", event_id="u1", kind="root", model="claude-opus-4-8"),
            _usage(session=child_id, client="claude-code", event_id="u2", kind="child", parent=root_id, model="claude-opus-4-8"),
        ]
    )

    root = _entry(rollup, "claude-code", root_id)
    child = _entry(rollup, "claude-code", child_id)
    assert root["client_session_id_short"] == "f224d28b"
    assert child["client_session_id_short"] == "f224d28b:ae598f4d"
    assert root["session_kind"] == "root"
    assert child["session_kind"] == "child"
    assert child["related"]["parent"]["label"] == "f224d28b"
    for entry in rollup["sessions"]:
        label = entry["client_session_id_short"]
        assert root_id not in label
        assert "agent-" not in label
        assert "/" not in label
        for component in label.split(":"):
            assert len(component) <= 8 + 5  # component or collision-suffixed component
    # project label is the already-redacted last path segment, never absolute.
    assert root["project"] == "project"


def test_rollup_first8_collisions_get_deterministic_suffix() -> None:
    # claude-code fixtures: codex labels are tail-based now, so the generic
    # first-8 + deterministic-suffix machinery is pinned via a first-8 client.
    rollup = _rollup(
        [
            _usage(session="abcdefgh-one-long-session-id", client="claude-code", event_id="u1"),
            _usage(session="abcdefgh-two-long-session-id", client="claude-code", event_id="u2"),
        ]
    )

    labels = {entry["client_session_id"]: entry["client_session_id_short"] for entry in rollup["sessions"]}
    first = labels["abcdefgh-one-long-session-id"]
    second = labels["abcdefgh-two-long-session-id"]
    assert first != second
    assert first.startswith("abcdefgh~")
    assert second.startswith("abcdefgh~")
    # Deterministic across rebuilds.
    again = _rollup(
        [
            _usage(session="abcdefgh-one-long-session-id", client="claude-code", event_id="u1"),
            _usage(session="abcdefgh-two-long-session-id", client="claude-code", event_id="u2"),
        ]
    )
    assert {entry["client_session_id_short"] for entry in again["sessions"]} == {first, second}


def test_rollup_hermes_labels_keep_the_distinctive_tail() -> None:
    rollup = _rollup(
        [
            _usage(session="20260617_140948_ad14a7", client="hermes", event_id="h1"),
            _usage(session="20260617_150000_bb25b8", client="hermes", event_id="h2"),
        ]
    )

    labels = {entry["client_session_id"]: entry["client_session_id_short"] for entry in rollup["sessions"]}
    assert labels["20260617_140948_ad14a7"] == "8_ad14a7"
    assert labels["20260617_150000_bb25b8"] == "0_bb25b8"


def test_rollup_codex_uuidv7_labels_keep_the_distinctive_tail() -> None:
    """Codex ids are UUIDv7: the first 8 hex chars are the top 32 bits of the
    launch timestamp (~65 s resolution), so same-minute subagent siblings
    ALWAYS share them and every fan-out family degenerated into
    ``<same-8-chars>~<hash-noise>`` labels. Modeled on the real 2026-07-10
    family: root ``…4ac6``, child ``…4ac7-ee01`` spawned in the same minute
    as its internal ``…4ac7-ee5e``. The distinctive part is the random tail —
    the same rule as hermes — so labels are last-8, mutually distinct, and
    need no ``~`` collision suffix."""

    root = "019f4ac6-567e-7391-9cb9-d5a3d7ea464e"
    child = "019f4ac7-ee01-75b3-852f-a3e36693f332"
    internal = "019f4ac7-ee5e-70e0-b73c-fbedc33420f2"
    rollup = _rollup(
        [
            _usage(session=root, event_id="u_root", kind="root"),
            _usage(session=child, event_id="u_child", kind="child", parent=root),
            _usage(session=internal, event_id="u_internal", kind="child", parent=child),
        ]
    )

    labels = {entry["client_session_id"]: entry["client_session_id_short"] for entry in rollup["sessions"]}
    assert labels[root] == "d7ea464e"
    assert labels[child] == "6693f332"
    assert labels[internal] == "c33420f2"
    assert len(set(labels.values())) == 3
    for label in labels.values():
        assert "~" not in label
    # The grandchild internal renders top-level with a "child of" chip; its
    # parent reference must carry the child's OWN short label so the chip is
    # greppable against the child row (and the full ids in JSON/hrefs).
    internal_entry = _entry(rollup, "codex", internal)
    assert internal_entry["related"]["parent"]["label"] == labels[child]


def test_rollup_activity_times_come_from_client_metadata_never_import_time() -> None:
    rollup = _rollup(
        [
            _usage(
                session="timed-session-long-id",
                event_id="u1",
                created_at=2_000_000.0,  # import time
                started_at=1_000_000.0,
                updated_at=1_500_000.0,
            ),
            _usage(session="untimed-session-long-id", event_id="u2", created_at=3_000_000.0),
        ]
    )

    timed = _entry(rollup, "codex", "timed-session-long-id")
    assert timed["first_activity_at"] == 1_000_000.0
    assert timed["last_activity_at"] == 1_500_000.0
    assert timed["activity_time_source"] == "client_metadata"

    untimed = _entry(rollup, "codex", "untimed-session-long-id")
    # created_at is used only as an explicit fallback, and says so.
    assert untimed["activity_time_source"] == "import_time_fallback"
    assert untimed["last_activity_at"] == 3_000_000.0

    # Sort: most recent activity first.
    assert [entry["client_session_id"] for entry in rollup["sessions"]] == [
        "untimed-session-long-id",
        "timed-session-long-id",
    ]


def test_rollup_entries_without_any_activity_time_sort_last() -> None:
    no_time = _usage(session="no-time-session-long", event_id="u1")
    no_time["created_at"] = None
    rollup = _rollup([no_time, _usage(session="timed-session-long", event_id="u2", updated_at=50.0)])

    assert [entry["client_session_id"] for entry in rollup["sessions"]] == [
        "timed-session-long",
        "no-time-session-long",
    ]
    assert rollup["sessions"][-1]["last_activity_at"] is None


def test_rollup_empty_store() -> None:
    rollup = _rollup([])

    assert rollup["sessions"] == []
    assert rollup["summary"]["total_sessions"] == 0
    assert rollup["summary"]["totals"]["fresh_tokens"] == 0
    assert rollup["summary"]["totals"]["estimated_cost_usd"] is None


# ---------------------------------------------------------------------------
# Phase 3.5c additive fields: usage.turns_total + entry duration_seconds
# ---------------------------------------------------------------------------


def test_rollup_turns_total_sums_own_rows_only_and_children_never_merge() -> None:
    rollup = _rollup(
        [
            _usage(session="turns-parent-long-id", event_id="u1", turn_count=3),
            _usage(session="turns-parent-long-id", event_id="u2", model="gpt-5.5-mini", turn_count=4),
            _usage(
                session="turns-parent-long-id:child",
                event_id="u3",
                kind="child",
                parent="turns-parent-long-id",
                turn_count=9,
            ),
        ]
    )

    parent = _entry(rollup, "codex", "turns-parent-long-id")
    child = _entry(rollup, "codex", "turns-parent-long-id:child")
    # Own rows only: the child's 9 turns NEVER merge into the parent.  Its
    # legacy cumulative row is held as one unit, so the unproven turn count is
    # unavailable rather than presented as an additive child total.
    assert parent["usage"]["turns_total"] == 7
    assert child["usage"]["turns_total"] is None
    assert child["usage"]["excluded_non_additive_rows"] == 1
    # The labeled descendants subtotal gains no turns figure either — it
    # stays exactly the token/cost shape it always was.
    assert "turns_total" not in (parent["related"]["children_usage"] or {})


def test_rollup_turns_total_absent_and_hostile_values_stay_none_never_zero() -> None:
    rollup = _rollup(
        [
            # No row carries a turn count -> None (never a guessed 0).
            _usage(session="no-turns-session-long", event_id="u1"),
            # Hostile/implausible values refuse: garbage string and negative
            # both stay out of the sum; the one real count survives.
            _usage(session="mixed-turns-session-a", event_id="u2", turn_count="not-a-number"),
            _usage(session="mixed-turns-session-a", event_id="u3", model="gpt-5.5-mini", turn_count=-2),
            _usage(session="mixed-turns-session-a", event_id="u4", model="gpt-5.5-nano", turn_count=5),
            # A stored 0 is real importer data, not an absence: sums to 0.
            _usage(session="zero-turns-session-a", event_id="u5", turn_count=0),
        ]
    )

    assert _entry(rollup, "codex", "no-turns-session-long")["usage"]["turns_total"] is None
    assert _entry(rollup, "codex", "mixed-turns-session-a")["usage"]["turns_total"] == 5
    assert _entry(rollup, "codex", "zero-turns-session-a")["usage"]["turns_total"] == 0


def test_rollup_duration_seconds_derived_and_guarded() -> None:
    rollup = _rollup(
        [
            # Sane span: first = started_at, last = updated_at -> 2h.
            _usage(session="spanned-session-long", event_id="u1", started_at=1_000_000.0, updated_at=1_007_200.0),
            # Single-instant session: a true 0.0, real data, not a guess.
            _usage(session="instant-session-long", event_id="u2", started_at=1_000_000.0, updated_at=1_000_000.0),
            # Negative span (client clock lies: started after updated) -> None.
            _usage(session="backwards-session-x1", event_id="u3", started_at=1_007_200.0, updated_at=1_000_000.0),
            # Absurd-but-finite endpoint (year ~33658) fails the bad-timestamp
            # guard -> None, even though the subtraction would "work".
            _usage(session="absurd-session-long1", event_id="u4", started_at=1_000_000.0, updated_at=1e12),
        ]
    )

    assert _entry(rollup, "codex", "spanned-session-long")["duration_seconds"] == 7_200.0
    assert _entry(rollup, "codex", "instant-session-long")["duration_seconds"] == 0.0
    assert _entry(rollup, "codex", "backwards-session-x1")["duration_seconds"] is None
    assert _entry(rollup, "codex", "absurd-session-long1")["duration_seconds"] is None


def test_rollup_sections_only_duration_and_missing_endpoint_stay_honest() -> None:
    rollup = _rollup(
        [
            # Sections-only session: MCP event times ARE activity times, so a
            # span across two section events is derivable and honest.
            _section(session="sections-span-session", section_id="s1", status="started", created_at=2_000_000.0),
            _section(session="sections-span-session", section_id="s1", status="completed", created_at=2_003_600.0),
        ]
    )
    entry = _entry(rollup, "codex", "sections-span-session")
    assert entry["duration_seconds"] == 3_600.0

    # An entry with no usable activity time at all serves None, never 0.
    no_time = _usage(session="no-time-session-long", event_id="u1")
    no_time["created_at"] = None
    entry = _entry(_rollup([no_time]), "codex", "no-time-session-long")
    assert entry["duration_seconds"] is None


def _random_store(rng: random.Random) -> list[dict]:
    """Seeded store: sessions with 0..3 rows, 0..2 sections, transcripts,
    parents and shared sessions to exercise every decision family."""

    events: list[dict] = []
    clients = ["codex", "claude-code"]
    sessions = [f"session-{index:02d}-{rng.randrange(16**6):06x}" for index in range(rng.randint(2, 6))]
    for session_index, session in enumerate(sessions):
        client = rng.choice(clients)
        transcript = f"tx-{session}" if rng.random() < 0.5 else None
        parent = rng.choice([None, sessions[0]]) if session_index > 0 and rng.random() < 0.4 else None
        for row_index in range(rng.randint(0, 3)):
            events.append(
                _usage(
                    session=session,
                    client=client,
                    event_id=f"u_{session}_{row_index}",
                    transcript=transcript if rng.random() < 0.7 else None,
                    parent=parent,
                    kind="child" if parent else "root",
                    input_tokens=rng.randint(0, 5000),
                    output_tokens=rng.randint(0, 500),
                    cache_creation=rng.randint(0, 1000),
                    cache_read=rng.randint(0, 100000),
                    cost=rng.choice([None, round(rng.random(), 4)]),
                    updated_at=rng.choice([None, 1_000_000.0 + rng.randint(0, 10000)]),
                )
            )
        for section_index in range(rng.randint(0, 2)):
            events.append(
                _section(
                    session=session if rng.random() < 0.8 else None,
                    client=client,
                    section_id=f"work-{session_index}-{section_index}",
                    transcript=transcript if rng.random() < 0.3 else None,
                    status=rng.choice(["checkpoint", "completed", "blocked"]),
                )
            )
    rng.shuffle(events)
    return events


def test_rollup_never_contradicts_attributions_property() -> None:
    """Invariant over seeded stores: entry.join.state == 'attributed' iff the
    canonical attribution model attributed at least one of that session's OWN
    rows, attributed_work mirrors those decisions verbatim, and entry usage
    totals equal the sum of the session's own rows only."""

    for seed in range(12):
        rng = random.Random(seed)
        ledger = build_work_ledger(_random_store(rng))
        usage_by_id = {str(usage["usage_event_id"]): usage for usage in ledger["usage_events"]}
        all_usage_rows = ledger["usage_events"] + ledger["excluded_usage_events"]

        decisions_by_key: dict[tuple[str, str], list[dict]] = {}
        for attribution in ledger["attributions"]:
            usage = usage_by_id[str(attribution["usage_event_id"])]
            key = (usage["client"], usage["client_session_id"])
            decisions_by_key.setdefault(key, []).append(attribution)

        for entry in ledger["session_rollup"]["sessions"]:
            key = (entry["client"], entry["client_session_id"])
            decisions = decisions_by_key.get(key, [])
            attributed = [
                attr for attr in decisions if attr.get("work_id") and attr.get("join_confidence") != "unjoined"
            ]
            assert (entry["join"]["state"] == "attributed") == bool(attributed), (seed, key)
            expected = {
                (str(attr["work_id"]), str(attr["join_strategy"]), str(attr["join_confidence"]))
                for attr in attributed
            }
            mirrored = {
                (str(work["work_id"]), str(work["join_strategy"]), str(work["join_confidence"]))
                for work in entry["join"]["attributed_work"]
            }
            assert mirrored == expected, (seed, key)
            assert entry["join"]["attributed_total_tokens"] == sum(int(attr["usage_tokens"]) for attr in attributed)

            own_rows = [
                usage
                for usage in all_usage_rows
                if (usage["client"], usage["client_session_id"]) == key
            ]
            additive_own_rows = [
                usage for usage in own_rows if usage.get("usage_additive") is not False
            ]
            assert entry["usage"]["rows"] == len(own_rows), (seed, key)
            assert entry["usage"]["additive_rows"] == len(additive_own_rows), (seed, key)
            assert entry["usage"]["excluded_non_additive_rows"] == len(own_rows) - len(additive_own_rows), (seed, key)
            assert entry["usage"]["total_tokens"] == sum(int(row["total_tokens"]) for row in additive_own_rows)
            assert entry["usage"]["fresh_tokens"] == sum(int(row["fresh_tokens"]) for row in additive_own_rows)
            # The triple always adds up.
            assert (
                entry["usage"]["fresh_tokens"]
                + entry["usage"]["cache_creation_tokens"]
                + entry["usage"]["cache_read_tokens"]
                == entry["usage"]["total_tokens"]
            )
            # Children subtotals never leak into own usage.
            children_usage = entry["related"]["children_usage"]
            if children_usage is not None:
                child_keys = [
                    (other["client"], other["client_session_id"])
                    for other in ledger["session_rollup"]["sessions"]
                    if other["related"]["parent"]
                    and other["related"]["parent"]["client_session_id"] == entry["client_session_id"]
                    and other["client"] == entry["client"]
                ]
                assert children_usage["sessions"] == len(child_keys)
                expected_child_total = sum(
                    int(row["total_tokens"])
                    for row in ledger["usage_events"]
                    if (row["client"], row["client_session_id"]) in child_keys
                )
                assert children_usage["total_tokens"] == expected_child_total
