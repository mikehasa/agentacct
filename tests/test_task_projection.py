from __future__ import annotations

from typing import Any

from agentacct.api import _attach_evidence_to_task_projection
from agentacct.task_projection import build_task_projection
from agentacct.work_ledger import build_work_ledger


def _session(
    session_id: str,
    *,
    client: str = "codex",
    parent: str | None = None,
    namespace_fingerprint: str | None = None,
    kind: str = "root",
    fresh_tokens: int = 10,
    total_tokens: int | None = None,
    cost: float | None = 0.10,
    model: str | None = None,
    last_activity_at: float = 1.0,
) -> dict[str, Any]:
    usage = {
        "rows": 1,
        "priced_rows": 1 if cost is not None else 0,
        "unpriced_rows": 0 if cost is not None else 1,
        "fresh_tokens": fresh_tokens,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_reported_rows": 1,
        "cache_creation_unreported_rows": 0,
        "cache_read_reported_rows": 1,
        "cache_read_unreported_rows": 0,
        "cache_creation_reporting": "reported",
        "cache_read_reporting": "reported",
        "total_tokens": total_tokens if total_tokens is not None else fresh_tokens,
        "estimated_cost_usd": cost,
        "model_lanes": [{"model": model or f"model-{session_id}"}],
    }
    return {
        "client": client,
        "client_session_id": session_id,
        "identity_scope_state": "explicit" if namespace_fingerprint else "unscoped",
        "namespace_fingerprint": namespace_fingerprint,
        "session_kind": kind,
        "last_activity_at": last_activity_at,
        "related": {
            "parent": {"client_session_id": parent} if parent else None,
            # Must never be added to the Task's own usage aggregate.
            "children_usage": {"fresh_tokens": 999_999},
        },
        "usage": usage,
    }


def _task_by_id(projection: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(task for task in projection["tasks"] if task["task_id"] == task_id)


def test_parent_lineage_refuses_different_explicit_namespaces() -> None:
    projection = build_task_projection(
        [
            _session("root", namespace_fingerprint="ns:project-b"),
            _session(
                "child",
                parent="root",
                namespace_fingerprint="ns:project-a",
                kind="child",
            ),
        ],
        [],
    )

    assert projection["summary"]["task_count"] == 2
    assert _task_by_id(projection, "session:codex:child")["primary_root"] == {
        "client": "codex",
        "client_session_id": "child",
    }


def test_parent_lineage_refuses_explicit_child_to_legacy_unscoped_root() -> None:
    projection = build_task_projection(
        [
            _session("legacy-root"),
            _session(
                "mechanical-child",
                parent="legacy-root",
                namespace_fingerprint="ns:project-a",
                kind="child",
            ),
        ],
        [],
    )

    assert projection["summary"]["task_count"] == 2
    child_task = _task_by_id(projection, "session:codex:mechanical-child")
    assert child_task["session_count"] == 1

    _attach_evidence_to_task_projection(
        projection,
        [
            {
                "event_id": "child-check",
                "source_type": "client_hook",
                "client": "codex",
                "client_session_id": "mechanical-child",
                "namespace_fingerprint": "ns:project-a",
                "result": "failed",
            }
        ],
        require_namespace_for_client_hook=True,
    )
    root_task = _task_by_id(projection, "session:codex:legacy-root")
    assert root_task["task_evidence_events"] == []
    assert [event["event_id"] for event in child_task["task_evidence_events"]] == ["child-check"]


def test_parent_lineage_refuses_legacy_unscoped_child_to_explicit_root() -> None:
    projection = build_task_projection(
        [
            _session("mechanical-root", namespace_fingerprint="ns:project-b"),
            _session(
                "legacy-child",
                parent="mechanical-root",
                kind="child",
            ),
        ],
        [],
    )

    assert projection["summary"]["task_count"] == 2
    child_task = _task_by_id(projection, "session:codex:legacy-child")
    assert child_task["session_count"] == 1


def test_parent_lineage_keeps_matching_explicit_namespace_compatible() -> None:
    projection = build_task_projection(
        [
            _session("root", namespace_fingerprint="ns:project-a"),
            _session(
                "child",
                parent="root",
                namespace_fingerprint="ns:project-a",
                kind="child",
            ),
        ],
        [],
    )

    assert projection["summary"]["task_count"] == 1
    assert projection["tasks"][0]["session_count"] == 2


def test_parent_lineage_refuses_expected_source_namespace_mismatch() -> None:
    root = _session("root")
    root["source_namespace_fingerprint"] = "sha256:home-b"
    child = _session("child", parent="root", kind="child")
    child["source_namespace_fingerprint"] = "sha256:home-a"
    child["parent_source_namespace_fingerprint"] = "sha256:home-a"

    projection = build_task_projection([root, child], [])

    assert projection["summary"]["task_count"] == 2
    child_task = _task_by_id(projection, "session:codex:child")
    assert child_task["root_groups"][0]["lineage_state"] == (
        "orphan_parent_source_namespace_mismatch"
    )


def test_parent_lineage_refuses_expected_source_namespace_when_parent_missing_it() -> None:
    root = _session("root")
    child = _session("child", parent="root", kind="child")
    child["source_namespace_fingerprint"] = "sha256:home-a"
    child["parent_source_namespace_fingerprint"] = "sha256:home-a"

    projection = build_task_projection([root, child], [])

    assert projection["summary"]["task_count"] == 2


def test_parent_lineage_refuses_child_source_to_missing_parent_without_expectation() -> None:
    root = _session("root")
    child = _session("child", parent="root", kind="child")
    child["source_namespace_fingerprint"] = "sha256:home-a"

    projection = build_task_projection([root, child], [])

    assert projection["summary"]["task_count"] == 2


def test_parent_lineage_refuses_missing_child_source_to_explicit_parent() -> None:
    root = _session("root")
    root["source_namespace_fingerprint"] = "sha256:home-a"
    child = _session("child", parent="root", kind="child")

    projection = build_task_projection([root, child], [])

    assert projection["summary"]["task_count"] == 2


def test_parent_lineage_accepts_matching_source_namespaces_and_legacy_missing_pair() -> None:
    root = _session("root")
    root["source_namespace_fingerprint"] = "sha256:home-a"
    child = _session("child", parent="root", kind="child")
    child["source_namespace_fingerprint"] = "sha256:home-a"
    child["parent_source_namespace_fingerprint"] = "sha256:home-a"

    matched = build_task_projection([root, child], [])
    legacy = build_task_projection(
        [_session("legacy-root"), _session("legacy-child", parent="legacy-root", kind="child")],
        [],
    )

    assert matched["summary"]["task_count"] == 1
    assert legacy["summary"]["task_count"] == 1


def test_persisted_parent_source_namespace_flows_from_usage_into_task_lineage() -> None:
    def _usage_event(
        event_id: str,
        session_id: str,
        *,
        parent_id: str | None = None,
        source_namespace: str,
        parent_source_namespace: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "created_at": 100.0,
            "source": "codex-local-session-import",
            "event_type": "model_usage",
            "provider": "openai",
            "model": "gpt-5",
            "estimated_input_tokens": 10,
            "estimated_output_tokens": 2,
            "usage_confidence": "client_reported",
            "cost_confidence": "unknown",
            "metadata": {
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "client": "codex",
                "client_session_id": session_id,
                "parent_client_session_id": parent_id,
                "client_session_kind": "child" if parent_id else "root",
                "source_namespace_fingerprint": source_namespace,
                "parent_source_namespace_fingerprint": parent_source_namespace,
                "project_dir": "/tmp/project",
            },
        }

    matching_ledger = build_work_ledger(
        [
            _usage_event(
                "root-a",
                "root",
                source_namespace="sha256:home-a",
            ),
            _usage_event(
                "child-a",
                "child",
                parent_id="root",
                source_namespace="sha256:home-a",
                parent_source_namespace="sha256:home-a",
            ),
        ],
        store_scope="custom",
    )
    mismatch_ledger = build_work_ledger(
        [
            _usage_event(
                "root-b",
                "root",
                source_namespace="sha256:home-b",
            ),
            _usage_event(
                "child-a",
                "child",
                parent_id="root",
                source_namespace="sha256:home-a",
                parent_source_namespace="sha256:home-a",
            ),
        ],
        store_scope="custom",
    )

    matching = build_task_projection(
        matching_ledger["session_rollup"]["sessions"], []
    )
    mismatch = build_task_projection(
        mismatch_ledger["session_rollup"]["sessions"], []
    )

    assert matching["summary"]["task_count"] == 1
    assert matching["tasks"][0]["session_count"] == 2
    assert mismatch["summary"]["task_count"] == 2
    mismatch_child = _task_by_id(mismatch, "session:codex:child")
    assert mismatch_child["root_groups"][0]["lineage_state"] == (
        "orphan_parent_source_namespace_mismatch"
    )


def test_direct_work_session_attachment_refuses_namespace_mismatch() -> None:
    projection = build_task_projection(
        [_session("shared-session", namespace_fingerprint="ns:project-a")],
        [
            {
                "work_id": "foreign-work",
                "client": "codex",
                "client_session_id": "shared-session",
                "identity_scope_state": "explicit",
                "session_namespace_fingerprint": "ns:project-b",
            }
        ],
    )

    assert projection["tasks"][0]["work_items"] == []
    assert projection["summary"]["associated_work_count"] == 0
    assert projection["summary"]["unresolved_work_count"] == 1
    assert projection["unresolved_work"][0]["reason"] == "work_session_namespace_mismatch"


def test_project_rollup_policy_allows_direct_legacy_work_attachment() -> None:
    session = _session("shared-session", namespace_fingerprint="ns:project-a")
    session["allow_legacy_unscoped_namespace_join"] = True
    projection = build_task_projection(
        [session],
        [
            {
                "work_id": "legacy-work",
                "client": "codex",
                "client_session_id": "shared-session",
            }
        ],
    )

    assert [item["work_id"] for item in projection["tasks"][0]["work_items"]] == [
        "legacy-work"
    ]
    assert projection["unresolved_work"] == []


def test_direct_work_session_attachment_refuses_explicit_to_missing_by_default() -> None:
    projection = build_task_projection(
        [_session("shared-session", namespace_fingerprint="ns:project-a")],
        [
            {
                "work_id": "legacy-work",
                "client": "codex",
                "client_session_id": "shared-session",
            }
        ],
    )

    assert projection["tasks"][0]["work_items"] == []
    assert projection["unresolved_work"][0]["reason"] == "work_session_namespace_mismatch"


def test_cross_namespace_collision_stays_isolated_from_ledger_through_task_projection() -> None:
    ledger = build_work_ledger(
        [
            {
                "event_id": "usage-a",
                "created_at": 100.0,
                "source": "codex-local-session-import",
                "event_type": "model_usage",
                "provider": "openai",
                "model": "gpt-5",
                "estimated_input_tokens": 100,
                "estimated_output_tokens": 25,
                "estimated_cost_usd": 0.50,
                "usage_confidence": "client_reported",
                "cost_confidence": "estimated_from_tokens",
                "metadata": {
                    "usage_source": "local_client_session_store",
                    "usage_provenance": "agent_sentinel_local_usage_import",
                    "client": "codex",
                    "client_session_id": "shared-session",
                    "session_namespace_fingerprint": "ns:project-a",
                    "identity_scope_state": "explicit",
                    "project_dir": "/tmp/project-a",
                },
            },
            {
                "event_id": "section-b",
                "created_at": 101.0,
                "source": "codex",
                "event_type": "section_completed",
                "metadata": {
                    "sentinel_semantic_kind": "section",
                    "client": "codex",
                    "client_session_id": "shared-session",
                    "client_context_keys_authored": ["client_session_id"],
                    "session_namespace_fingerprint": "ns:project-b",
                    "identity_scope_state": "explicit",
                    "project_dir": "/tmp/project-b",
                    "section_id": "foreign-work",
                    "section_status": "completed",
                    "section_title": "Foreign work",
                },
            },
        ],
        store_scope="custom",
    )
    projection = build_task_projection(
        ledger["session_rollup"]["sessions"],
        ledger["work_items"],
    )

    assert len(projection["tasks"]) == 1
    task = projection["tasks"][0]
    assert task["usage"]["total_tokens"] == 125
    assert task["work_items"] == []
    assert task["models"] == ["gpt-5"]
    assert projection["summary"]["unresolved_work_count"] == 1
    unresolved = projection["unresolved_work"][0]
    assert unresolved["work_id"] == "codex::shared-session::foreign-work"
    assert unresolved["reason"] == "work_session_namespace_mismatch"


def test_legacy_session_without_capability_fields_stays_unknown_not_zero() -> None:
    legacy = _session("legacy")
    for key in (
        "cache_creation_reported_rows",
        "cache_creation_unreported_rows",
        "cache_read_reported_rows",
        "cache_read_unreported_rows",
        "cache_creation_reporting",
        "cache_read_reporting",
    ):
        legacy["usage"].pop(key)

    usage = build_task_projection([legacy], [])["tasks"][0]["usage"]

    assert usage["cache_creation_tokens"] == 0
    assert usage["cache_creation_reporting"] == "unknown"
    assert usage["cache_creation_unknown_rows"] == 1
    assert usage["cache_read_tokens"] == 0
    assert usage["cache_read_reporting"] == "unknown"
    assert usage["cache_read_unknown_rows"] == 1


def test_transitive_session_root_is_task_boundary_and_usage_is_deduped() -> None:
    root = _session("root", fresh_tokens=100, cost=1.0, last_activity_at=1.0)
    child = _session(
        "child",
        parent="root",
        kind="child",
        fresh_tokens=50,
        cost=0.5,
        last_activity_at=2.0,
    )
    internal = _session(
        "review",
        parent="child",
        kind="internal",
        fresh_tokens=25,
        cost=0.25,
        last_activity_at=3.0,
    )
    projection = build_task_projection(
        [root, child, internal, dict(child)],
        [
            {
                "work_id": "root-work",
                "client": "codex",
                "client_session_id": "root",
                "run_id": "reported-install",
            },
            {
                "work_id": "review-work",
                "client": "codex",
                "client_session_id": "review",
                "run_id": "reported-review",
            },
        ],
    )

    assert projection["summary"]["task_count"] == 1
    assert projection["summary"]["duplicate_session_rows_ignored"] == 1
    task = projection["tasks"][0]
    assert task["task_id"] == "session:codex:root"
    assert task["primary_root"] == {"client": "codex", "client_session_id": "root"}
    assert task["session_count"] == 3
    assert task["supporting_count"] == 2
    assert task["child_count"] == 1
    assert task["internal_count"] == 1
    assert task["usage"]["fresh_tokens"] == 175
    assert task["usage"]["estimated_cost_usd"] == 1.75
    assert task["last_activity_at"] == 3.0
    assert {group["run_id"] for group in task["run_subgroups"]} == {
        "reported-install",
        "reported-review",
    }
    root_association = next(
        association
        for association in task["work_associations"]
        if association["work_id"] == "root-work"
    )
    assert root_association["session_attribution"] == "upstream"
    assert root_association["exact_session_id"] is None


def test_task_keeps_held_codex_child_as_supporting_identity_without_adding_usage() -> None:
    root = _session(
        "root",
        fresh_tokens=100,
        total_tokens=150,
        cost=1.0,
        last_activity_at=1.0,
    )
    root["usage"]["cache_read_tokens"] = 50
    child = _session(
        "child",
        parent="root",
        kind="child",
        fresh_tokens=0,
        total_tokens=0,
        cost=None,
        last_activity_at=2.0,
    )
    child["usage"].update(
        {
            "rows": 1,
            "additive_rows": 0,
            "excluded_non_additive_rows": 1,
            "priced_rows": 0,
            "unpriced_rows": 0,
            "fresh_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
            "model_lanes": [],
        }
    )

    projection = build_task_projection([root, child], [])

    assert projection["summary"]["task_count"] == 1
    task = projection["tasks"][0]
    assert task["primary_root"] == {
        "client": "codex",
        "client_session_id": "root",
    }
    assert task["session_count"] == 2
    assert task["supporting_count"] == 1
    assert task["child_count"] == 1
    assert task["usage"]["rows"] == 2
    assert task["usage"]["additive_rows"] == 1
    assert task["usage"]["excluded_non_additive_rows"] == 1
    assert task["usage"]["fresh_tokens"] == 100
    assert task["usage"]["total_tokens"] == 150
    assert task["usage"]["known_additive_cost_usd"] == 1.0
    # A known root estimate remains useful, but the Task must not imply that
    # cost is complete while a supporting row is held.
    assert task["usage"]["estimated_cost_usd"] is None
    assert task["usage"]["cost_complete"] is False
    assert task["usage"]["usage_availability"] == "partial"


def test_explicit_continuation_joins_roots_and_selects_primary_title() -> None:
    projection = build_task_projection(
        [
            _session("first-chat", fresh_tokens=40, last_activity_at=1.0),
            _session("continued-chat", fresh_tokens=60, last_activity_at=2.0),
        ],
        [
            {
                "work_id": "first-step",
                "client": "codex",
                "client_session_id": "first-chat",
            },
            {
                "work_id": "continued-step",
                "client": "codex",
                "client_session_id": "continued-chat",
            },
        ],
        continuation_memberships={
            "schema_version": "agent-chronicle.continuation-projection.v1",
            "tasks": [
                {
                    "task_id": "task-long-refactor",
                    "title": "Finish the long refactor",
                    "memberships": [
                        {
                            "session": {
                                "client": "codex",
                                "client_session_id": "first-chat",
                            },
                            "role": "primary",
                        },
                        {
                            "session": {
                                "client": "codex",
                                "client_session_id": "continued-chat",
                            },
                            "role": "continuation",
                        },
                    ],
                }
            ],
        },
    )

    assert projection["summary"]["task_count"] == 1
    task = _task_by_id(projection, "task-long-refactor")
    assert task["identity_basis"] == "explicit_continuation"
    assert task["title_override"] == "Finish the long refactor"
    assert task["primary_root"]["client_session_id"] == "first-chat"
    assert [root["client_session_id"] for root in task["root_keys"]] == [
        "first-chat",
        "continued-chat",
    ]
    assert task["session_count"] == 2
    assert task["supporting_count"] == 0
    assert task["usage"]["fresh_tokens"] == 100


def test_idless_multi_donor_work_joins_common_root_without_session_attribution() -> None:
    projection = build_task_projection(
        [
            _session("root"),
            _session("child", parent="root", kind="child"),
        ],
        [
            {
                "work_id": "ambiguous-but-one-task",
                "client": "codex",
                "log_evidence_candidate_sessions": [
                    {"client": "codex", "client_session_id": "root"},
                    {"client": "codex", "client_session_id": "child"},
                ],
            }
        ],
    )

    task = _task_by_id(projection, "session:codex:root")
    assert [item["work_id"] for item in task["work_items"]] == ["ambiguous-but-one-task"]
    association = task["work_associations"][0]
    assert association["basis"] == "common_task_from_candidate_sessions"
    assert association["session_unlinked"] is True
    assert association["client_session"] is None
    assert association["exact_session_id"] is None
    assert association["provenance"] == ["log_evidence_candidate_sessions"]
    assert task["has_session_unlinked_work"] is True
    assert task["session_unlinked_work_count"] == 1
    assert projection["unresolved_work"] == []


def test_candidates_across_continued_roots_join_task_but_not_exact_session() -> None:
    projection = build_task_projection(
        [_session("chat-a"), _session("chat-b")],
        [{"work_id": "continued-work", "client": "codex"}],
        continuation_memberships=[
            {
                "task_id": "continued-task",
                "sessions": [
                    {"client": "codex", "client_session_id": "chat-a"},
                    {"client": "codex", "client_session_id": "chat-b"},
                ],
            }
        ],
        work_session_evidence=[
            {
                "work_id": "continued-work",
                "candidate_sessions": [
                    {"client": "codex", "client_session_id": "chat-a"},
                    {"client": "codex", "client_session_id": "chat-b"},
                ],
            }
        ],
    )

    task = _task_by_id(projection, "continued-task")
    association = task["work_associations"][0]
    assert association["basis"] == "common_task_from_candidate_sessions"
    assert association["exact_session_id"] is None
    assert association["provenance"] == ["work_session_evidence"]


def test_run_id_never_merges_tasks_and_cross_task_candidates_stay_unresolved() -> None:
    projection = build_task_projection(
        [_session("root-a"), _session("root-b")],
        [
            {
                "work_id": "work-a",
                "client": "codex",
                "client_session_id": "root-a",
                "run_id": "shared-run",
            },
            {
                "work_id": "work-b",
                "client": "codex",
                "client_session_id": "root-b",
                "run_id": "shared-run",
            },
            {
                "work_id": "idless",
                "client": "codex",
                "run_id": "shared-run",
                "log_evidence_candidate_sessions": [
                    {"client": "codex", "client_session_id": "root-a"},
                    {"client": "codex", "client_session_id": "root-b"},
                ],
            },
        ],
    )

    assert {task["task_id"] for task in projection["tasks"]} == {
        "session:codex:root-a",
        "session:codex:root-b",
    }
    for task in projection["tasks"]:
        assert task["run_subgroups"] == [
            {
                "run_id": "shared-run",
                "work_ids": [task["work_items"][0]["work_id"]],
            }
        ]
    assert projection["summary"]["unresolved_work_count"] == 1
    unresolved = projection["unresolved_work"][0]
    assert unresolved["work_id"] == "idless"
    assert unresolved["reason"] == "candidate_sessions_span_tasks"
    assert unresolved["candidate_task_ids"] == [
        "session:codex:root-a",
        "session:codex:root-b",
    ]


def test_unique_resolved_run_is_safe_task_hint_but_never_session_attribution() -> None:
    projection = build_task_projection(
        [_session("root", fresh_tokens=75)],
        [
            {
                "work_id": "session-linked",
                "client": "codex",
                "client_session_id": "root",
                "project_dir": "/work/project",
                "run_id": "one-run",
            },
            {
                "work_id": "idless-sibling",
                "client": "codex",
                "project_dir": "/work/project",
                "run_id": "one-run",
            },
        ],
    )

    task = _task_by_id(projection, "session:codex:root")
    assert {item["work_id"] for item in task["work_items"]} == {
        "session-linked",
        "idless-sibling",
    }
    hinted = next(
        association
        for association in task["work_associations"]
        if association["work_id"] == "idless-sibling"
    )
    assert hinted == {
        "work_id": "idless-sibling",
        "task_id": "session:codex:root",
        "basis": "unique_task_from_run_id",
        "session_unlinked": True,
        "client_session": None,
        "session_attribution": "unresolved",
        "exact_session_id": None,
        "candidate_sessions": [],
        "provenance": ["run_id_grouping_hint"],
    }
    assert task["usage"]["fresh_tokens"] == 75
    assert task["session_unlinked_work_count"] == 1
    assert projection["unresolved_work"] == []


def test_run_hint_refuses_cross_namespace_work() -> None:
    projection = build_task_projection(
        [_session("root", namespace_fingerprint="ns:a")],
        [
            {
                "work_id": "session-linked",
                "client": "codex",
                "client_session_id": "root",
                "project_dir": "/work/project",
                "run_id": "one-run",
                "session_namespace_fingerprint": "ns:a",
                "identity_scope_state": "explicit",
            },
            {
                "work_id": "foreign-idless",
                "client": "codex",
                "project_dir": "/work/project",
                "run_id": "one-run",
                "session_namespace_fingerprint": "ns:b",
                "identity_scope_state": "explicit",
            },
        ],
    )

    task = _task_by_id(projection, "session:codex:root")
    assert [item["work_id"] for item in task["work_items"]] == ["session-linked"]
    assert task["session_unlinked_work_count"] == 0
    assert projection["summary"]["unresolved_work_count"] == 1
    unresolved = projection["unresolved_work"][0]
    assert unresolved["work_id"] == "foreign-idless"
    assert unresolved["reason"] == "no_session_or_candidate_evidence"
    assert unresolved["run_id"] == "one-run"


def test_run_hint_refuses_zero_or_multiple_resolved_tasks() -> None:
    projection = build_task_projection(
        [_session("root-a"), _session("root-b")],
        [
            {
                "work_id": "a",
                "client": "codex",
                "client_session_id": "root-a",
                "project_dir": "/work/project",
                "run_id": "ambiguous-run",
            },
            {
                "work_id": "b",
                "client": "codex",
                "client_session_id": "root-b",
                "project_dir": "/work/project",
                "run_id": "ambiguous-run",
            },
            {
                "work_id": "ambiguous-idless",
                "client": "codex",
                "project_dir": "/work/project",
                "run_id": "ambiguous-run",
            },
            {
                "work_id": "no-anchor",
                "client": "codex",
                "project_dir": "/work/project",
                "run_id": "unseen-run",
            },
        ],
    )

    by_work = {item["work_id"]: item for item in projection["unresolved_work"]}
    assert by_work["ambiguous-idless"]["reason"] == "run_id_spans_tasks"
    assert by_work["ambiguous-idless"]["candidate_task_ids"] == [
        "session:codex:root-a",
        "session:codex:root-b",
    ]
    assert by_work["no-anchor"]["reason"] == "no_session_or_candidate_evidence"
    assert by_work["no-anchor"]["candidate_task_ids"] == []
