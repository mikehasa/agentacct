from __future__ import annotations

import json

from agent_chronicle.context_bridge import build_usage_context_bridge
from agent_chronicle.service import SentinelService, summarize_events


def _usage_event(*, session: str, client: str = "codex", parent: str | None = None) -> dict:
    return {
        "event_id": f"evt_usage_{session}",
        "created_at": 1,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "run_id": f"client_{client}_{session}",
        "provider": client,
        "model": "gpt-5.5",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "estimated_cost_usd": 0.01,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": client,
            "client_session_id": session,
            "parent_client_session_id": parent,
            "client_session_kind": "child" if parent else "root",
            "cached_input_tokens": 50,
        },
    }


def _section_event(*, session: str, client: str = "codex", title: str = "Dashboard refresh", section_id: str = "dashboard-refresh") -> dict:
    return {
        "event_id": f"evt_section_{session}",
        "created_at": 2,
        "source": client,
        "event_type": "section_checkpoint",
        "run_id": "task_run",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "usage_join_strategy": "agent_reported_section_context",
            "client": client,
            "client_session_id": session,
            # Post-fix persisted shape: the MCP server stamps explicitly
            # supplied ids as server-authored (required for exact).
            "client_context_keys_authored": ["client_session_id"],
            "section_id": section_id,
            "section_status": "checkpoint",
            "section_title": title,
            "summary": "Connected the refresh flow.",
        },
    }


def _usage_debug_event(*, session: str, client: str = "codex") -> dict:
    return {
        "event_id": f"evt_debug_{session}",
        "created_at": 3,
        "source": client,
        "event_type": "agent_usage_debug_reported",
        "provider": "openai",
        "model": "gpt-5.5",
        "estimated_input_tokens": None,
        "estimated_output_tokens": None,
        "estimated_cost_usd": None,
        "usage_confidence": "unknown",
        "cost_confidence": "unknown",
        "metadata": {
            "sentinel_semantic_kind": "agent_usage_debug",
            "usage_join_strategy": "agent_reported_usage_debug",
            "client": client,
            "client_session_id": session,
            "reporting_basis": "unavailable",
            "summary": "Usage was not visible to the agent.",
            "agent_reported_input_tokens": 999,
            "agent_reported_cost_usd": 9.99,
        },
    }


def test_context_bridge_links_usage_to_mcp_context_by_client_session() -> None:
    bridge = build_usage_context_bridge([_usage_event(session="codex-session"), _section_event(session="codex-session"), _usage_debug_event(session="codex-session")])

    assert bridge["usage_records"] == 1
    assert bridge["context_events"] == 2
    assert bridge["context_matched_usage_records"] == 1
    assert bridge["attributed_usage_records"] == 1
    assert bridge["linked_usage_records"] == 1
    assert bridge["unlinked_context_events"] == 0
    link = bridge["links"][0]
    assert link["join_confidence"] == "exact"
    assert link["join_strategy"] == "exact_client_session_id"
    # The section id is server-authored (explicit); the usage-debug snapshot's
    # agent-reported id has unverified provenance and is labelled as such.
    assert link["join_keys"] == ["client_session_id", "client_session_id_unverified"]
    assert link["context_event_count"] == 2
    assert link["attribution_status"] == "attributed"
    assert link["section_count"] == 1
    assert link["usage_debug_count"] == 1
    assert link["sections"][0]["section_title"] == "Dashboard refresh"
    assert link["latest_usage_debug"]["reporting_basis"] == "unavailable"


def test_parent_child_hint_agrees_across_ledger_inspector_and_bridge() -> None:
    """parent/child session links group usage with the parent session but are
    a non-allocating low-confidence hint everywhere: ledger, inspector,
    bridge.  Claude is used here because legacy Codex descendants now follow
    the separate cumulative-replay quarantine contract."""
    from agent_chronicle.work_ledger import build_work_ledger

    events = [
        _usage_event(
            session="child-session",
            parent="root-session",
            client="claude-code",
        ),
        _section_event(session="root-session", client="claude-code"),
    ]
    bridge = build_usage_context_bridge(events)

    link = bridge["links"][0]
    assert link["client_session_id"] == "child-session"
    assert link["join_confidence"] == "low"
    assert link["join_strategy"] == "parent_child_context_hint"
    assert link["join_keys"] == ["parent_client_session_id"]
    assert link["section_count"] == 1
    assert link["attribution_status"] == "context_matched_unallocated"

    ledger = build_work_ledger(events)
    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert (attribution["join_strategy"], attribution["join_confidence"]) == ("parent_child_context_hint", "low")
    row = ledger["usage_reconciliation"][0]
    assert row["usage_reconciliation_state"] == "context_matched_unallocated"
    assert "parent/child links group but never allocate" in row["recommended_next_step"]
    assert all(item["usage_total"] == 0 for item in ledger["work_items"])


def test_context_bridge_keeps_unlinked_context_events_separate() -> None:
    bridge = build_usage_context_bridge([_usage_event(session="codex-session"), _section_event(session="other-session")])

    assert bridge["linked_usage_records"] == 0
    assert bridge["unlinked_context_events"] == 1
    assert bridge["links"][0]["join_confidence"] == "unjoined"
    assert bridge["unlinked_contexts"][0]["section_id"] == "dashboard-refresh"


def test_context_bridge_marks_same_session_multi_section_as_ambiguous() -> None:
    bridge = build_usage_context_bridge(
        [
            _usage_event(session="codex-session"),
            _section_event(session="codex-session", section_id="planning"),
            _section_event(session="codex-session", section_id="implementation"),
        ]
    )

    link = bridge["links"][0]
    assert link["join_confidence"] == "medium"
    assert link["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    assert link["section_count"] == 2
    assert link["attribution_status"] == "context_matched_unallocated"
    assert bridge["context_matched_usage_records"] == 1
    assert bridge["attributed_usage_records"] == 0
    assert bridge["linked_usage_records"] == 0


def test_context_bridge_ignores_generic_model_usage_for_usage_truth() -> None:
    generic = _usage_event(session="codex-session")
    generic["event_id"] = "evt_generic_usage"
    generic["metadata"] = {"client": "codex", "client_session_id": "codex-session"}

    bridge = build_usage_context_bridge([generic, _section_event(session="codex-session")])

    assert bridge["usage_records"] == 0
    assert bridge["linked_usage_records"] == 0
    assert bridge["unlinked_context_events"] == 1


def test_event_summary_includes_bridge_without_counting_usage_debug_totals() -> None:
    summary = summarize_events([_usage_event(session="codex-session"), _usage_debug_event(session="codex-session")], limit=20)

    assert summary["estimated_input_tokens"] == 100
    assert summary["estimated_output_tokens"] == 25
    assert summary["estimated_cost_usd"] == 0.01
    assert summary["usage_context_bridge"]["linked_usage_records"] == 0
    assert summary["usage_context_bridge"]["links"][0]["usage_debug_count"] == 1


def test_client_context_join_health_flags_non_joinable_context() -> None:
    from agent_chronicle.context_bridge import build_client_context_join_health

    events = [
        _usage_event(session="sess-1"),
        {
            "event_id": "evt_ctx",
            "created_at": 2,
            "source": "codex",
            "event_type": "client_context_attached",
            "metadata": {"sentinel_semantic_kind": "client_context", "client": "codex", "project_dir": "/tmp/p"},
        },
        {
            "event_id": "evt_sec",
            "created_at": 3,
            "source": "codex",
            "event_type": "section_started",
            "metadata": {"sentinel_semantic_kind": "section", "section_id": "s1"},
        },
    ]
    health = build_client_context_join_health(events)

    assert health["usage_rows"] == 1
    assert health["usage_rows_with_join_keys"] == 1
    assert health["context_events"] == 1
    assert health["context_events_with_join_keys"] == 0
    assert health["section_events"] == 1
    assert health["section_events_with_join_keys"] == 0
    assert health["joinable"] is False
    assert health["coverage_complete"] is False
    assert len(health["warnings"]) == 2


def test_client_context_join_health_ok_with_joinable_sections() -> None:
    from agent_chronicle.context_bridge import build_client_context_join_health

    events = [
        _usage_event(session="sess-1"),
        {
            "event_id": "evt_sec",
            "created_at": 3,
            "source": "codex",
            "event_type": "section_started",
            "metadata": {"sentinel_semantic_kind": "section", "section_id": "s1", "client_session_id": "sess-1"},
        },
    ]
    health = build_client_context_join_health(events)

    assert health["section_events_with_join_keys"] == 1
    assert health["joinable"] is True
    assert health["coverage_complete"] is True
    assert health["warnings"] == []


def test_client_context_join_health_degrades_when_only_one_usage_row_is_attributed() -> None:
    """One joinable section must not make a mostly-unjoined store look healthy."""
    from agent_chronicle.context_bridge import build_client_context_join_health

    events = [_usage_event(session=f"sess-{index}") for index in range(10)]
    events.append(_section_event(session="sess-0"))

    health = build_client_context_join_health(events)

    assert health["usage_rows"] == 10
    assert health["usage_rows_with_join_keys"] == 10
    assert health["context_match_coverage_ratio"] == 0.1
    assert health["attribution_coverage_ratio"] == 0.1
    assert health["health_status"] == "degraded"
    assert health["joinable"] is True
    assert health["coverage_complete"] is False
    assert health["degraded_reasons"] == [
        "usage_without_matching_context",
        "usage_without_work_attribution",
    ]


def test_codex_child_quarantine_is_counted_but_excluded_from_bridge_health_and_event_totals() -> None:
    from agent_chronicle.context_bridge import build_client_context_join_health

    events = [
        _usage_event(session="root-session"),
        _section_event(session="root-session"),
        _usage_event(session="child-session", parent="root-session"),
        _section_event(session="child-session", section_id="child-work"),
    ]

    bridge = build_usage_context_bridge(events)
    assert bridge["usage_records"] == 1
    assert bridge["excluded_non_additive_usage_records"] == 1
    assert bridge["context_matched_usage_records"] == 1
    assert bridge["attributed_usage_records"] == 1
    assert bridge["context_match_coverage_ratio"] == 1.0
    assert bridge["attribution_coverage_ratio"] == 1.0
    assert [link["event_id"] for link in bridge["links"]] == [
        "evt_usage_root-session"
    ]
    assert [row["usage_event_id"] for row in bridge["attributions"]] == [
        "evt_usage_root-session"
    ]

    health = build_client_context_join_health(events)
    assert health["usage_rows"] == 1
    assert health["excluded_non_additive_usage_rows"] == 1
    assert health["usage_rows_with_join_keys"] == 1
    assert health["context_matched_usage_rows"] == 1
    assert health["attributed_usage_rows"] == 1
    assert health["context_match_coverage_ratio"] == 1.0
    assert health["attribution_coverage_ratio"] == 1.0
    assert health["health_status"] == "healthy"

    summary = summarize_events(events, limit=20)
    assert summary["excluded_non_additive_usage_events"] == 1
    assert summary["estimated_input_tokens"] == 100
    assert summary["estimated_output_tokens"] == 25
    assert summary["cache_read_input_tokens"] == 50
    assert summary["total_tokens_including_cached"] == 175
    assert summary["estimated_cost_usd"] == 0.01
    assert summary["tokens_by_provider"]["codex"]["event_count"] == 1
    assert summary["tokens_by_provider"]["codex"][
        "total_tokens_including_cached"
    ] == 175


def test_service_summary_uses_full_store_for_bridge_truth_and_marks_limited_details(tmp_path) -> None:
    """The recent-event limit may cap detail rows, never canonical coverage."""
    events = [
        _usage_event(session="older-a"),
        _section_event(session="older-a"),
        _usage_event(session="older-b"),
        _section_event(session="older-b"),
        {
            "event_id": "evt_latest_note",
            "created_at": 100,
            "source": "manual",
            "event_type": "note",
            "metadata": {"summary": "Newest event pushes bridge evidence out of the recent window."},
        },
    ]
    # Keep all fixture ids/timestamps deterministic while ensuring both usage
    # rows sort behind the latest note in the limited summary window.
    for index, event in enumerate(events[:-1], start=1):
        event["created_at"] = index
    store_root = tmp_path / "state"
    store_root.mkdir()
    (store_root / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

    summary = SentinelService(store_root, create=False).summarize_events(limit=1)

    # Existing recent-summary aggregates stay limited and say so explicitly.
    assert summary["event_count"] == 1
    assert summary["result_scope"] == {
        "partial": True,
        "event_limit": 1,
        "events_summarized": 1,
        "events_total": 5,
        "canonical_metrics_scope": "all_matching_store_events",
    }
    # Bridge counters and coverage use every event in the store. Only verbose
    # detail arrays are capped by the caller's requested limit.
    bridge = summary["usage_context_bridge"]
    assert bridge["usage_records"] == 2
    assert bridge["context_matched_usage_records"] == 2
    assert bridge["attributed_usage_records"] == 2
    assert bridge["context_match_coverage_ratio"] == 1.0
    assert bridge["attribution_coverage_ratio"] == 1.0
    assert bridge["health_status"] == "healthy"
    assert bridge["degraded_reasons"] == []
    assert len(bridge["links"]) == 1
    assert len(bridge["attributions"]) == 1
    assert bridge["detail_scope"] == {
        "partial": True,
        "limit": 1,
        "links_returned": 1,
        "links_total": 2,
        "attributions_returned": 1,
        "attributions_total": 2,
        "unlinked_contexts_returned": 0,
        "unlinked_contexts_total": 0,
    }


def test_client_context_join_health_counts_legacy_sections_without_semantic_kind() -> None:
    from agent_chronicle.context_bridge import build_client_context_join_health

    events = [
        _usage_event(session="sess-1"),
        {
            "event_id": "evt_legacy",
            "created_at": 2,
            "source": "codex",
            "event_type": "section_completed",
            "metadata": {"section_id": "legacy-work", "client": "codex", "client_session_id": "sess-1"},
        },
    ]
    health = build_client_context_join_health(events)

    assert health["section_events"] == 1
    assert health["section_events_with_join_keys"] == 1
    assert health["joinable"] is True
    assert health["warnings"] == []


def test_run_id_link_is_low_everywhere() -> None:
    """run_id is Chronicle-authored grouping, never client-derived usage truth:
    the ledger AND the bridge report it as a low, non-allocating hint."""
    from agent_chronicle.work_ledger import build_work_ledger

    section = _section_event(session="other-session")
    section["run_id"] = "client_codex_sess-1"
    events = [_usage_event(session="sess-1"), section]

    ledger = build_work_ledger(events)
    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert (attribution["join_strategy"], attribution["join_confidence"]) == ("exact_run_id_grouping_hint", "low")

    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert (link["join_strategy"], link["join_confidence"]) == ("exact_run_id_grouping_hint", "low")
    assert link["attribution_status"] == "context_matched_unallocated"


def _matrix_section(*, session: str = "sess-1", tier: str = "explicit", section_id: str = "matrix-work", event_id: str | None = None) -> dict:
    section = _section_event(session=session, section_id=section_id)
    if event_id:
        section["event_id"] = event_id
    metadata = section["metadata"]
    if tier == "explicit":
        pass
    elif tier == "hook":
        metadata.pop("client_context_keys_authored")
        metadata["client_context_inherited_keys"] = ["client_session_id"]
        metadata["client_context_source"] = "claude_code_hook"
    elif tier == "attach":
        metadata.pop("client_context_keys_authored")
        metadata["client_context_inherited_keys"] = ["client_session_id"]
    elif tier == "unverified":
        metadata.pop("client_context_keys_authored")
    return section


def test_ledger_and_bridge_agree_on_strategy_and_confidence_matrix() -> None:
    """The three join surfaces share one matcher: for every provenance tier and
    every hint/conflict scenario the bridge link must carry the canonical
    confidence and the canonical strategy family."""
    from agent_chronicle.work_ledger import build_work_ledger

    run_id_section = _section_event(session="other-session")
    run_id_section["run_id"] = "client_codex_sess-1"
    conflict_usage = _usage_event(session="sess-1")
    conflict_usage["metadata"]["client_transcript_id"] = "usage-transcript"
    conflict_section = _matrix_section()
    conflict_section["metadata"]["client_transcript_id"] = "section-transcript"
    conflict_section["metadata"]["client_context_keys_authored"] = ["client_session_id", "client_transcript_id"]

    scenarios = [
        ("explicit", [_usage_event(session="sess-1"), _matrix_section(tier="explicit")], "exact_client_session_id", "exact_client_session_id", "exact"),
        ("hook", [_usage_event(session="sess-1"), _matrix_section(tier="hook")], "client_derived_client_session_id", "client_derived_client_context", "high"),
        ("attach", [_usage_event(session="sess-1"), _matrix_section(tier="attach")], "inherited_client_session_id", "inherited_client_context", "medium"),
        ("unverified", [_usage_event(session="sess-1"), _matrix_section(tier="unverified")], "unverified_client_session_id", "unverified_client_context", "high"),
        (
            "ambiguous",
            [
                _usage_event(session="sess-1"),
                _matrix_section(section_id="matrix-a", event_id="evt_matrix_a"),
                _matrix_section(section_id="matrix-b", event_id="evt_matrix_b"),
            ],
            "exact_client_session_id_ambiguous_sections",
            "exact_client_session_id_ambiguous_sections",
            "medium",
        ),
        ("run_id", [_usage_event(session="sess-1"), run_id_section], "exact_run_id_grouping_hint", "exact_run_id_grouping_hint", "low"),
        (
            "parent_child",
            [
                _usage_event(
                    session="child-1",
                    parent="root-1",
                    client="claude-code",
                ),
                _section_event(session="root-1", client="claude-code"),
            ],
            "parent_child_context_hint",
            "parent_child_context_hint",
            "low",
        ),
        ("conflict", [conflict_usage, conflict_section], "unjoined", "unjoined", "unjoined"),
    ]

    for name, events, expected_strategy, expected_bridge_strategy, expected_confidence in scenarios:
        ledger = build_work_ledger(events)
        attribution = ledger["attributions"][0]
        assert attribution["join_strategy"] == expected_strategy, name
        assert attribution["join_confidence"] == expected_confidence, name

        bridge = build_usage_context_bridge(events)
        link = bridge["links"][0]
        assert link["join_strategy"] == expected_bridge_strategy, name
        assert link["join_confidence"] == expected_confidence, name


# ---------------------------------------------------------------------------
# Finding 1: an unattributed usage row must never display exact/high anywhere.
# Context/debug-only matches (canonical decision: unjoined) are capped at
# medium under the dedicated 'context_only_client_context' strategy.
# ---------------------------------------------------------------------------


def _attach_context_event(*, session: str, client: str = "codex", tier: str = "explicit") -> dict:
    metadata = {
        "sentinel_semantic_kind": "client_context",
        "usage_join_strategy": "agent_reported_client_context",
        "client": client,
        "client_session_id": session,
    }
    if tier == "explicit":
        metadata["client_context_keys_authored"] = ["client_session_id"]
    elif tier == "hook":
        metadata["client_context_inherited_keys"] = ["client_session_id"]
        metadata["client_context_source"] = "claude_code_hook"
    elif tier == "attach":
        metadata["client_context_inherited_keys"] = ["client_session_id"]
    return {
        "event_id": f"evt_attach_{session}_{tier}",
        "created_at": 2,
        "source": client,
        "event_type": "client_context_attached",
        "metadata": metadata,
    }


def test_context_only_authored_attach_match_is_capped_at_medium() -> None:
    """Adversarial repro: usage row matches ONLY a client_context attach event
    (authored id, no sections). The canonical ledger says unjoined; the bridge
    used to render exact (green) anyway."""
    from agent_chronicle.work_ledger import build_work_ledger

    events = [_usage_event(session="shared"), _attach_context_event(session="shared", tier="explicit")]

    ledger = build_work_ledger(events)
    assert ledger["attributions"][0]["join_strategy"] == "unjoined"

    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert link["join_strategy"] == "context_only_client_context"
    assert link["join_confidence"] == "medium"
    assert link["attribution_status"] == "context_matched_unallocated"
    assert bridge["attributed_usage_records"] == 0
    assert bridge["context_matched_usage_records"] == 1


def test_context_only_hook_and_unverified_matches_never_show_high() -> None:
    for tier in ("hook", "attach"):
        events = [_usage_event(session="shared"), _attach_context_event(session="shared", tier=tier)]
        bridge = build_usage_context_bridge(events)
        link = bridge["links"][0]
        assert link["join_strategy"] == "context_only_client_context", tier
        assert link["join_confidence"] == "medium", tier
        assert link["attribution_status"] == "context_matched_unallocated", tier

    # Usage-debug-only match (unverified tier) is capped the same way.
    events = [_usage_event(session="shared"), _usage_debug_event(session="shared")]
    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert link["join_strategy"] == "context_only_client_context"
    assert link["join_confidence"] == "medium"
    assert link["attribution_status"] == "context_matched_unallocated"


def test_no_bridge_link_ever_outranks_its_canonical_attribution() -> None:
    """Property over assorted event mixes: for every link, confidence above
    medium requires attribution_status == 'attributed'."""
    from agent_chronicle.join_rules import JOIN_RANK

    scenarios = [
        [_usage_event(session="s1"), _attach_context_event(session="s1")],
        [_usage_event(session="s2"), _attach_context_event(session="s2", tier="hook"), _usage_debug_event(session="s2")],
        [_usage_event(session="s3"), _section_event(session="s3")],
        [_usage_event(session="s4"), _section_event(session="s4"), _attach_context_event(session="s4")],
    ]
    for events in scenarios:
        bridge = build_usage_context_bridge(events)
        for link in bridge["links"]:
            if JOIN_RANK.get(str(link["join_confidence"]), 0) > JOIN_RANK["medium"]:
                assert link["attribution_status"] == "attributed", link


# ---------------------------------------------------------------------------
# Finding 6: the bridge applies the same read-time ':model:' normalization as
# the ledger, so un-migrated legacy stores cannot make the surfaces disagree.
# ---------------------------------------------------------------------------


def _legacy_claude_usage_event(*, session_key: str, event_id: str, input_tokens: int = 100) -> dict:
    event = _usage_event(session=session_key, client="claude-code")
    event["event_id"] = event_id
    event["source"] = "claude-code-local-session-import"
    event["provider"] = "claude-code"
    event["estimated_input_tokens"] = input_tokens
    event["estimated_output_tokens"] = 0
    event["metadata"]["cached_input_tokens"] = 0
    return event


def _claude_authored_section(*, session: str = "S") -> dict:
    section = _section_event(session=session, client="claude-code", section_id="claude-work")
    section["metadata"]["client_context_keys_authored"] = ["client_session_id"]
    return section


def test_bridge_normalizes_legacy_model_suffixed_keys_like_the_ledger() -> None:
    from agent_chronicle.work_ledger import build_work_ledger

    events = [
        _legacy_claude_usage_event(session_key="S:model:claude-opus-4-8", event_id="evt_suffixed"),
        _claude_authored_section(),
    ]

    ledger = build_work_ledger(events)
    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "exact_client_session_id"
    assert attribution["join_confidence"] == "exact"

    bridge = build_usage_context_bridge(events)
    link = bridge["links"][0]
    assert link["client_session_id"] == "S"
    assert (link["join_strategy"], link["join_confidence"]) == ("exact_client_session_id", "exact")
    assert link["attribution_status"] == "attributed"
    assert bridge["attributed_usage_records"] == 1


def test_bridge_excludes_shadowed_stale_base_rows_like_the_ledger() -> None:
    from agent_chronicle.work_ledger import build_work_ledger

    events = [
        _legacy_claude_usage_event(session_key="S", event_id="evt_stale_base", input_tokens=1000),
        _legacy_claude_usage_event(session_key="S:model:claude-opus-4-8", event_id="evt_lane_opus", input_tokens=3000),
        _legacy_claude_usage_event(session_key="S:model:claude-sonnet-4-9", event_id="evt_lane_sonnet", input_tokens=2000),
        _claude_authored_section(),
    ]

    ledger = build_work_ledger(events)
    ledger_attributed_tokens = sum(attr["usage_tokens"] for attr in ledger["attributions"] if attr.get("work_id"))
    assert ledger_attributed_tokens == 5000

    bridge = build_usage_context_bridge(events)
    assert bridge["usage_records"] == 2
    assert bridge["attributed_usage_records"] == 2
    assert {link["event_id"] for link in bridge["links"]} == {"evt_lane_opus", "evt_lane_sonnet"}
    assert sum(int(link["total_tokens_including_cached"]) for link in bridge["links"]) == 5000
    assert all(link["attribution_status"] == "attributed" for link in bridge["links"])


# ---------------------------------------------------------------------------
# Phase 2 Batch A. 2e: diagnostic tool events are split out at bridge intake
# too (defense in depth with the ledger's own intake split).
# ---------------------------------------------------------------------------


def _doctor_context_shaped_event() -> dict:
    """Worst case: a diagnostic event that would otherwise count as MCP context."""

    return {
        "event_id": "evt_doctor_context",
        "created_at": 4,
        "source": "agent-sentinel-mcp-doctor",
        "event_type": "mcp_doctor_test",
        "metadata": {
            "sentinel_semantic_kind": "client_context",
            "client": "codex",
            "client_session_id": "codex-session",
        },
    }


def _smoke_usage_shaped_event() -> dict:
    """Worst case: a smoke event that would otherwise qualify as usage truth."""

    event = _usage_event(session="smoke-session")
    event["event_id"] = "evt_smoke_usage"
    event["source"] = "agent-sentinel-mcp-workflow-smoke"
    return event


def test_bridge_intake_excludes_diagnostic_tool_events() -> None:
    bridge = build_usage_context_bridge(
        [
            _usage_event(session="codex-session"),
            _section_event(session="codex-session"),
            _doctor_context_shaped_event(),
            _smoke_usage_shaped_event(),
        ]
    )

    assert bridge["usage_records"] == 1
    assert bridge["context_events"] == 1
    link = bridge["links"][0]
    assert link["client_session_id"] == "codex-session"
    assert link["context_event_count"] == 1
    # The doctor's context-shaped event neither linked nor appears unlinked.
    assert bridge["unlinked_context_events"] == 0
    assert all("doctor" not in str(context.get("event_id")) for context in bridge["unlinked_contexts"])


def test_join_health_intake_excludes_diagnostic_tool_events() -> None:
    from agent_chronicle.context_bridge import build_client_context_join_health

    health = build_client_context_join_health(
        [
            _usage_event(session="codex-session"),
            _section_event(session="codex-session"),
            _doctor_context_shaped_event(),
            _smoke_usage_shaped_event(),
        ]
    )

    assert health["usage_rows"] == 1
    assert health["context_events"] == 0
    assert health["section_events"] == 1
    assert health["joinable"] is True
