from __future__ import annotations

from agentacct.service import summarize_events
from agentacct.work_ledger import build_work_ledger


def _usage_event(
    *,
    session: str,
    client: str = "codex",
    transcript: str | None = None,
    event_id: str = "usage",
    cost: float = 0.25,
    created_at: float = 10,
    started_at: float | None = None,
    updated_at: float | None = None,
) -> dict:
    return {
        "event_id": f"evt_{event_id}",
        "created_at": created_at,
        "source": f"{client}-local-session-import",
        "event_type": "model_usage",
        "run_id": f"client_{client}_{session}",
        "provider": client,
        "model": "gpt-5.5",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "estimated_cost_usd": cost,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            "cached_input_tokens": 50,
            "project_dir": "/tmp/project",
            "started_at": started_at,
            "updated_at": updated_at,
        },
    }


def _section_event(
    *,
    session: str,
    client: str = "codex",
    transcript: str | None = None,
    section_id: str = "mcp-v1",
    status: str = "checkpoint",
    run_id: str | None = None,
    project_dir: str = "/tmp/project",
    files: list[str] | None = None,
    blocker: str | None = None,
    next_step: str | None = None,
    created_at: float = 20,
) -> dict:
    return {
        "event_id": f"evt_section_{section_id}_{status}",
        "created_at": created_at,
        "source": client,
        "event_type": f"section_{status}",
        "run_id": run_id,
        "metadata": {
            "sentinel_semantic_kind": "section",
            "client": client,
            "client_session_id": session,
            "client_transcript_id": transcript,
            # Post-fix persisted shape: the MCP server stamps explicitly
            # supplied ids as server-authored; ids without this marker are
            # capped below exact.
            "client_context_keys_authored": [
                key for key, value in (("client_session_id", session), ("client_transcript_id", transcript)) if value
            ],
            "project_dir": project_dir,
            "section_id": section_id,
            "section_status": status,
            "section_title": "MCP v1 convergence",
            "summary": "Converged MCP fields into the work ledger.",
            "kind": "implementation",
            "files": files or ["src/agentacct/work_ledger.py"],
            "blocker": blocker,
            "next_step": next_step,
        },
    }


def _evidence_event(*, section_id: str = "mcp-v1", result: str = "passed", artifact_path: str = "/tmp/private/report.json") -> dict:
    return {
        "event_id": f"evt_evidence_{section_id}_{result}",
        "created_at": 30,
        "source": "codex",
        "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence",
            "section_id": section_id,
            "evidence_type": "test",
            "result": result,
            "summary": "Tests passed.",
            "command": "pytest tests/test_work_ledger.py --token secret",
            "exit_code": 0,
            "artifact_path": artifact_path,
            "artifact_url": "https://example.test/report?token=secret",
        },
    }


def _blocker_resolution_event(
    *,
    target_event_id: str,
    section_id: str = "publish-pr",
    scope: str = "full",
    created_at: float = 30,
    source: str = "codex",
    client: str | None = "codex",
    project_dir: str = "/tmp/project",
    namespace_fingerprint: str | None = None,
    event_id: str = "evt_blocker_resolution",
) -> dict:
    return {
        "event_id": event_id,
        "created_at": created_at,
        "source": source,
        "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence",
            "evidence_type": "artifact",
            "result": "passed",
            "summary": "The dependency is available now.",
            "exit_code": 0,
            "section_id": section_id,
            "client": client,
            "project_dir": project_dir,
            "session_namespace_fingerprint": namespace_fingerprint,
            "resolves_blocked_event_id": target_event_id,
            "resolution_scope": scope,
            "resolution_summary": "The exact reported blocker was cleared by a later passing check.",
            "resolution_objective_basis": "exit_code",
            "blocker_resolution_contract": "server_validated_v1",
        },
    }


def _generic_model_usage_event() -> dict:
    return {
        "event_id": "evt_generic_usage",
        "created_at": 11,
        "source": "manual-agent",
        "event_type": "model_usage",
        "provider": "openai",
        "model": "gpt-5.5",
        "estimated_input_tokens": 999,
        "estimated_output_tokens": 1,
        "estimated_cost_usd": 42.0,
        "usage_confidence": "client_reported",
        "cost_confidence": "client_reported",
        "metadata": {"client": "codex", "client_session_id": "codex-session"},
    }


def _usage_debug_event() -> dict:
    return {
        "event_id": "evt_usage_debug",
        "created_at": 15,
        "source": "codex",
        "event_type": "agent_usage_debug_reported",
        "estimated_input_tokens": None,
        "estimated_output_tokens": None,
        "estimated_cost_usd": None,
        "usage_confidence": "unknown",
        "cost_confidence": "unknown",
        "metadata": {
            "sentinel_semantic_kind": "agent_usage_debug",
            "client": "codex",
            "client_session_id": "codex-session",
            "reporting_basis": "visible_client_usage",
            "agent_reported_total_tokens": 999_999,
            "agent_reported_cost_usd": 123.45,
            "summary": "Agent-visible usage is diagnostics only.",
        },
    }


def test_work_ledger_joins_usage_to_section_by_exact_client_session_id() -> None:
    ledger = build_work_ledger([_usage_event(session="codex-session"), _section_event(session="codex-session"), _evidence_event()])

    assert ledger["usage_events"][0]["total_tokens"] == 175
    assert ledger["attributions"][0]["join_strategy"] == "exact_client_session_id"
    assert ledger["attributions"][0]["join_confidence"] == "exact"
    item = ledger["work_items"][0]
    assert item["work_id"] == "codex::codex-session::mcp-v1"
    assert item["section_id"] == "mcp-v1"
    assert item["usage_total"] == 175
    assert item["estimated_cost_total"] == 0.25
    assert item["evidence_status"] == "strong"
    assert item["join_explanation"]["usage_join_state"] == "attributed"
    assert item["join_explanation"]["join_confidence"] == "exact"
    assert item["join_explanation"]["candidate_usage_count"] == 1
    assert ledger["overview"]["attributed_usage_count"] == 1
    assert ledger["usage_reconciliation"][0]["usage_join_state"] == "attributed"


def test_later_completed_snapshot_clears_stale_blocker_and_recovery_step() -> None:
    blocked = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="GitHub authentication is unavailable.",
        next_step="Re-authenticate GitHub.",
        created_at=20,
    )
    completed = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="completed",
        created_at=30,
    )
    completed["event_id"] = "evt_section_publish-pr_completed_later"

    item = build_work_ledger([blocked, completed])["work_items"][0]

    assert item["latest_status"] == "completed"
    assert item["latest_event_id"] == "evt_section_publish-pr_completed_later"
    assert item["blocker"] is None
    assert item["next_step"] is None

    completed_with_follow_up = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="completed",
        next_step="Review the published artifact.",
        created_at=40,
    )
    completed_with_follow_up["event_id"] = "evt_section_publish-pr_completed_with_follow_up"
    item = build_work_ledger([blocked, completed_with_follow_up])["work_items"][0]
    assert item["blocker"] is None
    assert item["next_step"] == "Review the published artifact."


def test_handed_off_section_status_survives_ingestion_as_a_terminal() -> None:
    # DECISION 1: handed_off is an accepted terminal work status. Before it was
    # added to WORK_STATUSES, _work_event coerced the unknown status to
    # "checkpoint" (an in-progress state), erasing the clean-stop signal.
    event = _section_event(
        session="codex-session",
        section_id="handoff-step",
        status="handed_off",
        created_at=40,
    )

    item = build_work_ledger([event])["work_items"][0]

    assert item["latest_status"] == "handed_off"


def test_nonterminal_snapshots_without_copying_text_keep_last_blocker() -> None:
    first = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="GitHub authentication is unavailable.",
        next_step="Re-authenticate GitHub.",
        created_at=20,
    )
    for status in ("started", "checkpoint", "blocked"):
        repeated = _section_event(
            session="codex-session",
            section_id="publish-pr",
            status=status,
            created_at=30,
        )
        repeated["event_id"] = f"evt_section_publish-pr_{status}_later"

        item = build_work_ledger([first, repeated])["work_items"][0]

        assert item["latest_status"] == status
        assert item["blocker"] == "GitHub authentication is unavailable."
        assert item["next_step"] == "Re-authenticate GitHub."


def test_full_resolution_clears_only_the_exact_current_blocker_without_faking_completion() -> None:
    blocked = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="GitHub authentication is unavailable.",
        next_step="Re-authenticate GitHub.",
        created_at=20,
    )
    resolution = _blocker_resolution_event(
        target_event_id=str(blocked["event_id"]),
    )

    ledger = build_work_ledger([blocked, resolution])
    item = ledger["work_items"][0]

    assert item["latest_status"] == "resolved"
    assert item["semantic_latest_status"] == "blocked"
    assert item["blocker"] is None
    assert item["next_step"] is None
    assert item["blocker_resolution"]["state"] == "resolved"
    assert item["blocker_resolution"]["authoritative"] is False
    assert item["blocker_resolution"]["basis"] == "agent_claim_with_passed_check"
    assert ledger["insights"]["blocker_resolution"]["accepted_full"] == 1
    assert ledger["insights"]["blocker_resolution"]["rejected"] == 0
    assert ledger["overview"]["active_work_items"] == 0
    assert ledger["overview"]["resolved_work_items"] == 1
    assert ledger["overview"]["blocked_work_items"] == 0
    session = ledger["session_rollup"]["sessions"][0]
    assert session["work"]["counts"]["active"] == 0
    assert session["work"]["counts"]["resolved"] == 1
    assert session["work"]["counts"]["blocked"] == 0


def test_partial_resolution_keeps_blocker_and_needs_input_semantics() -> None:
    blocked = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="PR publication and repository cleanup are both blocked.",
        next_step="Resolve both dependencies.",
        created_at=20,
    )
    resolution = _blocker_resolution_event(
        target_event_id=str(blocked["event_id"]),
        scope="partial",
    )

    ledger = build_work_ledger([blocked, resolution])
    item = ledger["work_items"][0]

    assert item["latest_status"] == "blocked"
    assert item["blocker"] == "PR publication and repository cleanup are both blocked."
    assert item["blocker_resolution"]["state"] == "partially_resolved"
    assert item["updated_at"] == 30
    assert item["outcome_updated_at"] == 30
    assert ledger["insights"]["blocker_resolution"]["accepted_partial"] == 1


def test_newer_blocked_episode_reopens_after_an_older_resolution() -> None:
    first = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="GitHub authentication is unavailable.",
        created_at=20,
    )
    resolution = _blocker_resolution_event(
        target_event_id=str(first["event_id"]),
        created_at=30,
    )
    reopened = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="Repository policy now blocks publication.",
        created_at=40,
    )
    reopened["event_id"] = "evt_section_publish-pr_blocked_reopened"

    ledger = build_work_ledger([first, resolution, reopened])
    item = ledger["work_items"][0]

    assert item["latest_status"] == "blocked"
    assert item["blocker"] == "Repository policy now blocks publication."
    assert "blocker_resolution" not in item
    assert ledger["insights"]["blocker_resolution"]["rejected_by_reason"] == {
        "target_not_current_blocker": 1
    }


def test_resolution_rejects_same_basename_with_different_full_project_path() -> None:
    blocked = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        project_dir="/owners/one/shared-name",
        blocker="Publication is blocked.",
        created_at=20,
    )
    resolution = _blocker_resolution_event(
        target_event_id=str(blocked["event_id"]),
        project_dir="/owners/two/shared-name",
    )

    ledger = build_work_ledger([blocked, resolution])

    assert ledger["work_items"][0]["latest_status"] == "blocked"
    assert ledger["insights"]["blocker_resolution"]["rejected_by_reason"] == {
        "project_identity_mismatch": 1
    }


def test_resolution_fail_closed_guards_reject_ambiguous_or_conflicting_targets() -> None:
    base = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="Publication is blocked.",
        created_at=20,
    )
    cases: list[tuple[str, dict, str]] = []

    wrong_time = _blocker_resolution_event(
        target_event_id=str(base["event_id"]),
        created_at=20,
        event_id="evt_resolution_wrong_time",
    )
    cases.append(("wrong_time", wrong_time, "evidence_not_after_blocker"))

    wrong_source = _blocker_resolution_event(
        target_event_id=str(base["event_id"]),
        source="claude-code",
        event_id="evt_resolution_wrong_source",
    )
    cases.append(("wrong_source", wrong_source, "source_mismatch"))

    wrong_client = _blocker_resolution_event(
        target_event_id=str(base["event_id"]),
        client="claude-code",
        event_id="evt_resolution_wrong_client",
    )
    cases.append(("wrong_client", wrong_client, "client_mismatch"))

    wrong_section = _blocker_resolution_event(
        target_event_id=str(base["event_id"]),
        section_id="another-section",
        event_id="evt_resolution_wrong_section",
    )
    cases.append(("wrong_section", wrong_section, "section_mismatch"))

    namespaced_base = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="Publication is blocked.",
        created_at=20,
    )
    namespaced_base["metadata"]["session_namespace_fingerprint"] = "sha256:one"
    wrong_namespace = _blocker_resolution_event(
        target_event_id=str(namespaced_base["event_id"]),
        namespace_fingerprint="sha256:two",
        event_id="evt_resolution_wrong_namespace",
    )

    for _name, resolution, expected_reason in cases:
        ledger = build_work_ledger([base, resolution])
        assert ledger["work_items"][0]["latest_status"] == "blocked"
        assert ledger["insights"]["blocker_resolution"]["rejected_by_reason"] == {
            expected_reason: 1
        }

    ledger = build_work_ledger([namespaced_base, wrong_namespace])
    assert ledger["work_items"][0]["latest_status"] == "blocked"
    assert ledger["insights"]["blocker_resolution"]["rejected_by_reason"] == {
        "namespace_mismatch": 1
    }


def test_resolution_requires_unique_current_blocked_event_id() -> None:
    first = _section_event(
        session="codex-session",
        section_id="publish-pr",
        status="blocked",
        blocker="Publication is blocked.",
        created_at=20,
    )
    duplicate = _section_event(
        session="other-session",
        section_id="another-section",
        status="blocked",
        blocker="Another task is blocked.",
        created_at=21,
    )
    duplicate["event_id"] = first["event_id"]
    resolution = _blocker_resolution_event(
        target_event_id=str(first["event_id"]),
        created_at=30,
    )

    ledger = build_work_ledger([first, duplicate, resolution])

    assert all(item["latest_status"] == "blocked" for item in ledger["work_items"])
    assert ledger["insights"]["blocker_resolution"]["rejected_by_reason"] == {
        "target_not_unique": 1
    }


def test_mixed_full_project_identity_work_cohort_is_quarantined_before_resolution() -> None:
    blocked = _section_event(
        session="shared-session",
        section_id="publish-pr",
        status="blocked",
        project_dir="/owners/one/shared-name",
        blocker="Publication is blocked.",
        created_at=20,
    )
    other_project_checkpoint = _section_event(
        session="shared-session",
        section_id="publish-pr",
        status="checkpoint",
        project_dir="/owners/two/shared-name",
        created_at=25,
    )
    other_project_checkpoint["event_id"] = "evt_other_project_checkpoint"
    resolution = _blocker_resolution_event(
        target_event_id=str(blocked["event_id"]),
        project_dir="/owners/one/shared-name",
        created_at=30,
    )

    ledger = build_work_ledger([blocked, other_project_checkpoint, resolution])

    assert ledger["work_items"] == []
    assert ledger["insights"]["work_event_namespace_quarantine"] == {
        "ambiguous_cohorts": 1,
        "quarantined_snapshots": 2,
    }
    assert ledger["insights"]["blocker_resolution"]["accepted"] == 0
    assert ledger["insights"]["blocker_resolution"]["rejected_by_reason"] == {
        "target_work_not_projectable": 1
    }


def test_ledger_insights_report_healthy_attributed_evidence_backed_work() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="codex-session"),
            _section_event(session="codex-session", status="completed"),
            _evidence_event(),
        ]
    )

    insights = ledger["insights"]
    usage = insights["usage_attribution_summary"]
    trust = insights["trust_summary"]

    assert insights["ledger_health"] == "good"
    assert usage["usage_truth_count"] == 1
    assert usage["attributed_count"] == 1
    assert usage["unknown_or_unattributed_tokens"] == 0
    assert usage["attributed_tokens"] == 175
    assert usage["attributed_cost_usd"] == 0.25
    assert trust["completed_work_count"] == 1
    assert trust["evidence_backed_completed_count"] == 1
    assert trust["completed_without_strong_evidence_count"] == 0
    assert insights["blind_spots"] == []
    assert insights["top_next_actions"][0].startswith("No urgent reconciliation action.")


def test_work_ledger_joins_usage_to_section_by_exact_client_transcript_id_only() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="usage-session", transcript="transcript-1"),
            _section_event(session="", transcript="transcript-1"),
            _evidence_event(),
        ]
    )

    item = ledger["work_items"][0]
    assert item["client_transcript_id"] == "transcript-1"
    assert ledger["attributions"][0]["join_strategy"] == "exact_client_transcript_id"
    assert ledger["attributions"][0]["join_confidence"] == "exact"
    assert item["join_explanation"]["usage_join_state"] == "attributed"
    assert item["join_explanation"]["missing_join_keys"] == []
    assert "missing_client_session_id" not in {attention["attention_type"] for attention in ledger["attention_items"]}
    assert ledger["usage_reconciliation"][0]["usage_reconciliation_state"] == "attributed"


def test_work_ledger_keeps_unmatched_usage_unattributed() -> None:
    ledger = build_work_ledger([_usage_event(session="codex-session"), _section_event(session="other-session")])

    assert ledger["attributions"][0]["join_strategy"] == "unjoined"
    assert ledger["attributions"][0]["join_confidence"] == "unjoined"
    assert ledger["overview"]["unattributed_usage_count"] == 1
    assert ledger["overview"]["usage_without_mcp_context_count"] == 1
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["work_items"][0]["usage_total"] == 0


def test_join_inspector_explains_mcp_only_completed_work_without_fake_cost() -> None:
    ledger = build_work_ledger([_section_event(session="codex-session", status="completed"), _evidence_event()])

    item = ledger["work_items"][0]
    assert item["latest_status"] == "completed"
    assert item["evidence_status"] == "strong"
    assert item["usage_total"] == 0
    assert item["estimated_cost_total"] == 0
    assert item["join_explanation"]["usage_join_state"] == "no_usage_found"
    assert item["join_explanation"]["recommended_next_step"] == "Run local usage import to refresh Codex/Claude usage."
    assert ledger["overview"]["total_tokens"] == 0
    assert ledger["overview"]["estimated_cost_total"] == 0
    assert ledger["attention_items"][0]["attention_type"] == "completed_evidenced_work_without_attributed_usage"
    assert "Usage unknown / not attributed" in ledger["attention_items"][0]["summary"]


def test_join_inspector_prioritizes_no_usage_over_missing_join_keys() -> None:
    ledger = build_work_ledger([_section_event(session="", section_id="missing-session")])

    item = ledger["work_items"][0]
    assert item["join_explanation"]["usage_join_state"] == "no_usage_found"
    assert item["join_explanation"]["join_strategy"] == "no_usage_import"
    assert item["join_explanation"]["recommended_next_step"] == "Run local usage import to refresh Codex/Claude usage."


def test_join_inspector_surfaces_usage_truth_without_mcp_context() -> None:
    ledger = build_work_ledger([_usage_event(session="codex-session")])

    assert ledger["overview"]["usage_truth_event_count"] == 1
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["usage_without_mcp_context_count"] == 1
    assert ledger["usage_reconciliation"][0]["usage_reconciliation_state"] == "usage_without_mcp_context"
    assert ledger["attention_items"][0]["attention_type"] == "usage_truth_without_mcp_context"
    assert ledger["attention_items"][0]["recommended_next_step"] == "Usage exists but no MCP work context matched."


def test_ledger_insights_surface_usage_without_mcp_context() -> None:
    ledger = build_work_ledger([_usage_event(session="codex-session")])

    insights = ledger["insights"]
    blind_spot = insights["blind_spots"][0]

    assert insights["ledger_health"] == "partial"
    assert blind_spot["type"] == "usage_without_mcp_context"
    assert blind_spot["severity"] == "medium"
    assert blind_spot["tokens"] == 175
    assert blind_spot["estimated_cost_usd"] == 0.25
    assert "MCP context attach" in blind_spot["recommended_next_step"]
    assert any("MCP context attach" in action for action in insights["top_next_actions"])


def test_usage_debug_does_not_enter_usage_or_cost_totals() -> None:
    events = [_usage_event(session="codex-session", cost=0.01), _usage_debug_event()]
    summary = summarize_events(events, limit=20)
    ledger = build_work_ledger(events)

    assert summary["estimated_input_tokens"] == 100
    assert summary["estimated_output_tokens"] == 25
    assert summary["estimated_cost_usd"] == 0.01
    assert ledger["overview"]["total_tokens"] == 175
    assert ledger["overview"]["estimated_cost_total"] == 0.01
    assert ledger["usage_debug_events"][0]["agent_reported_cost_usd"] == 123.45


def test_overview_reports_attributed_and_unattributed_usage() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="codex-session", event_id="usage_joined"),
            _usage_event(session="other-session", event_id="usage_unjoined"),
            _section_event(session="codex-session"),
        ]
    )

    overview = ledger["overview"]
    assert overview["usage_event_count"] == 2
    assert overview["attributed_usage_count"] == 1
    assert overview["unattributed_usage_count"] == 1
    assert overview["unattributed_usage_percentage"] == 50.0


def test_work_ledger_uses_usage_occurrence_time_not_import_time() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(
                session="codex-session",
                created_at=1_782_990_000,
                started_at=1_781_949_600,
                updated_at=1_782_036_000,
            ),
            _section_event(session="codex-session"),
        ]
    )

    usage = ledger["usage_events"][0]
    usage_timeline = next(entry for entry in ledger["timeline"] if entry["event_kind"] == "usage")
    assert usage["recorded_at"] == 1_782_990_000
    assert usage["occurred_at"] == 1_782_036_000
    assert usage["created_at"] == 1_782_036_000
    assert usage["time_source"] == "metadata.updated_at"
    assert usage_timeline["time"] == 1_782_036_000
    assert ledger["overview"]["last_import_time"] == 1_782_990_000


def test_work_ledger_keeps_proxy_and_generic_model_usage_out_of_usage_truth_totals() -> None:
    ledger = build_work_ledger(
        [_usage_event(session="codex-session", cost=0.01), _generic_model_usage_event()],
        cost_events=[
            {
                "event_id": "cost_proxy",
                "created_at": 12,
                "run_id": "run_proxy",
                "provider": "openai",
                "model": "gpt-5.5",
                "estimated_input_tokens": 500,
                "estimated_output_tokens": 100,
                "estimated_cost_usd": 12.34,
                "usage_confidence": "provider_reported",
                "cost_confidence": "provider_billed",
            }
        ],
    )

    assert ledger["overview"]["total_tokens"] == 175
    assert ledger["overview"]["estimated_cost_total"] == 0.01
    assert ledger["overview"]["usage_truth_event_count"] == 1
    assert ledger["overview"]["proxy_usage_event_count"] == 1
    assert ledger["proxy_usage_events"][0]["estimated_cost_usd"] == 12.34
    assert ledger["diagnostic_usage_events"][0]["estimated_cost_usd"] == 42.0
    assert {entry["event_kind"] for entry in ledger["timeline"]} >= {"usage", "proxy_usage", "usage_diagnostic"}


def test_work_ledger_does_not_allocate_ambiguous_same_session_usage_to_sections() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="codex-session"),
            _section_event(session="codex-session", section_id="planning"),
            _section_event(session="codex-session", section_id="implementation"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    assert attribution["join_confidence"] == "medium"
    assert attribution["work_id"] is None
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["context_matched_unallocated_usage_count"] == 0
    assert ledger["overview"]["ambiguous_usage_count"] == 1
    assert ledger["overview"]["unattributed_usage_count"] == 1
    assert all(item["usage_total"] == 0 for item in ledger["work_items"])
    assert {item["join_explanation"]["usage_join_state"] for item in ledger["work_items"]} == {"ambiguous"}
    assert ledger["usage_reconciliation"][0]["usage_reconciliation_state"] == "ambiguous"
    assert {item["attention_type"] for item in ledger["attention_items"]} == {"ambiguous_same_session_attribution"}


def test_ledger_insights_surface_ambiguous_attribution() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="codex-session"),
            _section_event(session="codex-session", section_id="planning"),
            _section_event(session="codex-session", section_id="implementation"),
        ]
    )

    insights = ledger["insights"]
    blind_spot = insights["blind_spots"][0]

    assert insights["ledger_health"] == "partial"
    assert blind_spot["type"] == "ambiguous_attribution"
    assert blind_spot["tokens"] == 175
    assert "does not allocate section-level billing" in blind_spot["summary"]
    assert any("client_transcript_id" in action for action in insights["top_next_actions"])


def test_work_ledger_uses_unique_transcript_to_disambiguate_shared_session() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="shared", transcript="tx-target"),
            _section_event(session="shared", transcript="tx-other", section_id="section-a"),
            _section_event(session="shared", transcript="tx-target", section_id="section-b"),
        ]
    )

    attribution = ledger["attributions"][0]
    work_by_id = {item["section_id"]: item for item in ledger["work_items"]}

    assert attribution["work_id"] == "codex::shared::section-b"
    assert attribution["section_id"] == "section-b"
    assert attribution["join_strategy"] == "exact_client_transcript_id"
    assert attribution["join_confidence"] == "exact"
    assert ledger["overview"]["attributed_usage_count"] == 1
    assert ledger["overview"]["ambiguous_usage_count"] == 0
    assert ledger["overview"]["unattributed_usage_count"] == 0
    assert work_by_id["section-b"]["usage_total"] == 175
    assert work_by_id["section-b"]["join_explanation"]["usage_join_state"] == "attributed"
    assert work_by_id["section-b"]["join_explanation"]["join_strategy"] == "exact_client_transcript_id"
    assert work_by_id["section-a"]["usage_total"] == 0
    assert work_by_id["section-a"]["join_explanation"]["usage_join_state"] != "ambiguous"
    assert ledger["usage_reconciliation"][0]["usage_reconciliation_state"] == "attributed"
    assert "ambiguous_same_session_attribution" not in {item["attention_type"] for item in ledger["attention_items"]}


def test_join_inspector_reports_missing_client_session_id() -> None:
    ledger = build_work_ledger([_usage_event(session="codex-session"), _section_event(session="", section_id="missing-session")])

    item = ledger["work_items"][0]
    assert item["join_explanation"]["usage_join_state"] == "missing_join_keys"
    assert "client_session_id" in item["join_explanation"]["missing_join_keys"]
    assert item["join_explanation"]["recommended_next_step"] == "MCP context is missing client_session_id; attach client context at session start."
    assert item["join_explanation"]["nearest_usage_summary"]["total_tokens"] == 175
    assert "missing_client_session_id" in {attention["attention_type"] for attention in ledger["attention_items"]}


def test_ledger_insights_surface_completed_work_without_evidence() -> None:
    ledger = build_work_ledger([_section_event(session="codex-session", section_id="done-no-evidence", status="completed")])

    insights = ledger["insights"]
    blind_spot = insights["blind_spots"][0]

    assert insights["ledger_health"] == "poor"
    assert blind_spot["type"] == "completed_without_evidence"
    assert blind_spot["severity"] == "high"
    assert "lack strong machine-check evidence" in blind_spot["summary"]
    assert insights["trust_summary"]["completed_without_strong_evidence_count"] == 1
    assert any("machine-check evidence" in action for action in insights["top_next_actions"])


def test_ledger_insights_surface_completed_evidenced_work_without_attributed_usage() -> None:
    ledger = build_work_ledger([_section_event(session="codex-session", status="completed"), _evidence_event()])

    insights = ledger["insights"]
    blind_spot = insights["blind_spots"][0]

    assert insights["ledger_health"] == "partial"
    assert blind_spot["type"] == "completed_without_attributed_usage"
    assert blind_spot["severity"] == "medium"
    assert "Usage unknown / not attributed" in blind_spot["summary"]
    assert "zero-cost" in blind_spot["summary"]
    assert blind_spot["estimated_cost_usd"] is None
    assert any("usage import/watch" in action for action in insights["top_next_actions"])


def test_usage_reconciliation_counts_are_mutually_exclusive() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="joined", event_id="usage_joined"),
            _usage_event(session="ambiguous", event_id="usage_ambiguous"),
            _usage_event(session="run-only", event_id="usage_run_only"),
            _usage_event(session="missing", event_id="usage_missing"),
            _section_event(session="joined", section_id="joined"),
            _section_event(session="ambiguous", section_id="ambiguous-a"),
            _section_event(session="ambiguous", section_id="ambiguous-b"),
            _section_event(session="other", section_id="run-only", run_id="client_codex_run-only"),
        ]
    )

    overview = ledger["overview"]
    states = {row["usage_event_id"]: row["usage_reconciliation_state"] for row in ledger["usage_reconciliation"]}

    assert overview["usage_truth_count"] == 4
    assert overview["attributed_count"] == 1
    assert overview["ambiguous_count"] == 1
    assert overview["context_matched_unallocated_count"] == 1
    assert overview["usage_without_mcp_context_count"] == 1
    assert overview["unattributed_count"] == 3
    assert overview["unattributed_usage_count"] == 3
    assert states["evt_usage_joined"] == "attributed"
    assert states["evt_usage_ambiguous"] == "ambiguous"
    assert states["evt_usage_run_only"] == "context_matched_unallocated"
    assert states["evt_usage_missing"] == "usage_without_mcp_context"


def test_work_ledger_treats_run_id_only_as_low_confidence_grouping_hint() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="codex-session", event_id="usage_by_run"),
            _section_event(session="other-session", section_id="run-only", run_id="client_codex_codex-session"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "exact_run_id_grouping_hint"
    assert attribution["join_confidence"] == "low"
    assert attribution["work_id"] is None
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["unattributed_usage_count"] == 1
    assert ledger["overview"]["context_matched_unallocated_usage_count"] == 1
    assert ledger["work_items"][0]["usage_total"] == 0


def test_work_ledger_redacts_public_derived_paths_and_commands() -> None:
    ledger = build_work_ledger(
        [
            _section_event(
                session="codex-session",
                project_dir="C:\\Users\\alice\\private-repo",
                files=[
                    "src\\agentacct\\api.py",
                    "..\\secret.py",
                    "C:\\Users\\alice\\private-repo\\secret.py",
                    "/Users/alice/private-repo/absolute.py",
                ],
            ),
            _evidence_event(),
        ]
    )

    item = ledger["work_items"][0]
    evidence = ledger["evidence_events"][0]
    assert item["project_dir"] == "private-repo"
    assert item["files"] == ["src/agentacct/api.py"]
    assert evidence["command"] is None
    assert evidence["command_redacted"] is True
    assert evidence["artifact_path"] is None
    assert evidence["artifact_path_redacted"] is True
    assert evidence["artifact_url"] is None
    assert evidence["artifact_url_redacted"] is True


def test_work_ledger_normalizes_safe_relative_backslash_artifact_paths() -> None:
    ledger = build_work_ledger([_section_event(session="codex-session"), _evidence_event(artifact_path="reports\\pytest.json")])

    assert ledger["evidence_events"][0]["artifact_path"] == "reports/pytest.json"
    assert ledger["evidence_events"][0]["artifact_path_redacted"] is False


def test_parent_child_usage_is_never_allocated() -> None:
    # Keep testing the generic low-confidence parent/child matcher with an
    # importer whose child rows are additive.  Legacy Codex descendants are
    # now held earlier by the cumulative-replay quarantine.
    usage = _usage_event(session="child-session", client="claude-code")
    usage["metadata"]["parent_client_session_id"] = "parent-session"
    ledger = build_work_ledger(
        [
            usage,
            _section_event(
                session="parent-session",
                status="completed",
                client="claude-code",
            ),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "parent_child_context_hint"
    assert attribution["join_confidence"] == "low"
    assert "does not allocate" in attribution["join_reason"]
    row = ledger["usage_reconciliation"][0]
    assert row["usage_reconciliation_state"] == "context_matched_unallocated"
    assert "parent/child links group but never allocate" in row["recommended_next_step"]
    assert ledger["work_items"][0]["usage_total"] == 0
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["context_matched_unallocated_usage_count"] == 1


def test_transcript_conflict_vetoes_session_join() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="shared-session", transcript="usage-transcript"),
            _section_event(session="shared-session", transcript="section-transcript"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "unjoined"
    assert attribution["join_confidence"] == "unjoined"
    assert "conflicting client_transcript_id" in attribution["join_reason"]
    item = ledger["work_items"][0]
    assert item["usage_total"] == 0
    assert item["join_explanation"]["candidate_usage_count"] == 0


def test_session_conflict_vetoes_transcript_join() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="usage-session", transcript="shared-transcript"),
            _section_event(session="section-session", transcript="shared-transcript"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "unjoined"
    assert attribution["join_confidence"] == "unjoined"
    assert "conflicting client_session_id" in attribution["join_reason"]
    assert ledger["work_items"][0]["usage_total"] == 0


def test_legacy_section_without_authored_marker_caps_at_high() -> None:
    section = _section_event(session="codex-session")
    del section["metadata"]["client_context_keys_authored"]
    ledger = build_work_ledger([_usage_event(session="codex-session"), section])

    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "unverified_client_session_id"
    assert attribution["join_confidence"] == "high"
    assert "unverified" in attribution["join_reason"]
    assert attribution["work_id"] == "codex::codex-session::mcp-v1"
    assert ledger["work_items"][0]["usage_total"] == 175


def test_same_section_id_across_sessions_forms_distinct_work_items() -> None:
    section_b = _section_event(session="session-b", section_id="shared-section")
    section_b["event_id"] = "evt_section_shared_b"
    ledger = build_work_ledger(
        [
            _usage_event(session="session-a"),
            _section_event(session="session-a", section_id="shared-section"),
            section_b,
        ]
    )

    items_by_work_id = {item["work_id"]: item for item in ledger["work_items"]}
    assert set(items_by_work_id) == {"codex::session-a::shared-section", "codex::session-b::shared-section"}
    assert all(item["section_id"] == "shared-section" for item in ledger["work_items"])
    attribution = ledger["attributions"][0]
    assert attribution["work_id"] == "codex::session-a::shared-section"
    assert attribution["join_confidence"] == "exact"
    assert items_by_work_id["codex::session-a::shared-section"]["usage_total"] == 175
    assert items_by_work_id["codex::session-b::shared-section"]["usage_total"] == 0


def test_evidence_links_only_unique_namespaced_candidate() -> None:
    section_b = _section_event(session="session-b", section_id="shared-section")
    section_b["event_id"] = "evt_section_shared_b"
    ambiguous_evidence = _evidence_event(section_id="shared-section")
    ledger = build_work_ledger(
        [
            _section_event(session="session-a", section_id="shared-section"),
            section_b,
            ambiguous_evidence,
        ]
    )
    # Two items share the raw section_id and the evidence carries no client
    # session: linking either would be a guess, so it links neither.
    assert all(item["evidence_events"] == [] for item in ledger["work_items"])

    scoped_evidence = _evidence_event(section_id="shared-section")
    scoped_evidence["metadata"]["client_session_id"] = "session-a"
    ledger = build_work_ledger(
        [
            _section_event(session="session-a", section_id="shared-section"),
            section_b,
            scoped_evidence,
        ]
    )
    items_by_work_id = {item["work_id"]: item for item in ledger["work_items"]}
    assert len(items_by_work_id["codex::session-a::shared-section"]["evidence_events"]) == 1
    assert items_by_work_id["codex::session-b::shared-section"]["evidence_events"] == []


def test_task_events_are_not_work_items_but_stay_listed(tmp_path) -> None:
    from agentacct.service import SentinelService

    service = SentinelService(tmp_path / "state")
    service.record_event({"source": "codex", "event_type": "task_started", "metadata": {"task": "legacy task event"}})
    service.record_event({"source": "codex", "event_type": "task_completed", "metadata": {"result": "ok"}})

    ledger = build_work_ledger(service.list_all_events())
    assert ledger["work_events"] == []
    assert ledger["work_items"] == []
    assert ledger["timeline"] == []
    listed = service.list_events(limit=10)
    assert {event["event_type"] for event in listed} == {"task_started", "task_completed"}


def _legacy_suffixed_claude_usage_event(*, session_key: str = "S:model:claude-opus-4-8") -> dict:
    return {
        "event_id": "evt_legacy_suffixed_usage",
        "created_at": 10,
        "source": "claude-code-local-session-import",
        "event_type": "model_usage",
        "run_id": "client_claude_code_S_model_claude-opus-4-8",
        "provider": "claude-code",
        "model": "claude-opus-4-8",
        "estimated_input_tokens": 100,
        "estimated_output_tokens": 25,
        "estimated_cost_usd": 0.25,
        "usage_confidence": "client_reported",
        "cost_confidence": "estimated_from_tokens",
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "claude-code",
            "client_session_id": session_key,
            "cached_input_tokens": 50,
            "project_dir": "/tmp/project",
        },
    }


def _claude_section_event(*, session: str = "S", inherited: bool = False) -> dict:
    metadata = {
        "sentinel_semantic_kind": "section",
        "client": "claude-code",
        "client_session_id": session,
        "project_dir": "/tmp/project",
        "section_id": "claude-work",
        "section_status": "completed",
        "section_title": "Claude section",
        "summary": "Work done in the claude session.",
        "kind": "implementation",
        "files": ["src/agentacct/work_ledger.py"],
    }
    if inherited:
        metadata["client_context_inherited_keys"] = ["client_session_id"]
        metadata["client_context_source"] = "claude_code_hook"
    else:
        metadata["client_context_keys_authored"] = ["client_session_id"]
    return {
        "event_id": "evt_section_claude_work_completed",
        "created_at": 20,
        "source": "claude-code",
        "event_type": "section_completed",
        "run_id": None,
        "metadata": metadata,
    }


def test_usage_with_legacy_model_suffixed_key_joins_base_session() -> None:
    # Un-migrated stores: read-time normalization reverses Chronicle's own
    # ':model:' key artifact so the row joins by its TRUE client session id.
    ledger = build_work_ledger([_legacy_suffixed_claude_usage_event(), _claude_section_event()])

    assert ledger["usage_events"][0]["client_session_id"] == "S"
    attribution = ledger["attributions"][0]
    assert attribution["join_strategy"] == "exact_client_session_id"
    assert attribution["join_confidence"] == "exact"
    assert ledger["work_items"][0]["usage_total"] == 175


def test_usage_with_legacy_model_suffixed_key_and_inherited_id_stays_capped() -> None:
    # Base normalization must not upgrade provenance: an inherited section id
    # still joins below exact.
    ledger = build_work_ledger([_legacy_suffixed_claude_usage_event(), _claude_section_event(inherited=True)])

    assert ledger["usage_events"][0]["client_session_id"] == "S"
    attribution = ledger["attributions"][0]
    assert attribution["join_confidence"] == "high"
    assert attribution["join_strategy"] == "client_derived_client_session_id"


def test_child_stem_suffix_is_not_stripped_by_base_normalization() -> None:
    # Child transcript keys 'S:stem' are real distinct sessions, not artifacts:
    # they must NOT collapse into the parent session at read time.
    ledger = build_work_ledger(
        [_legacy_suffixed_claude_usage_event(session_key="S:agent-a123"), _claude_section_event()]
    )

    assert ledger["usage_events"][0]["client_session_id"] == "S:agent-a123"
    assert ledger["overview"]["attributed_usage_count"] == 0


# ---------------------------------------------------------------------------
# Section-scoped ambiguity guard (Phase 1 adversarial review, finding 0):
# the guard counts SECTIONS that matched by any id key, never per-key tallies.
# ---------------------------------------------------------------------------


def test_transcript_bearing_section_cannot_steal_whole_session_usage() -> None:
    """Exact adversarial repro: one co-session section omits the transcript id
    (the documented explicit-session-only path) while its sibling carries it.
    The old per-key guard allocated 100% to the transcript-bearing section at
    exact with a false 'only matching' reason; the section-scoped guard must
    refuse (missing beats wrong)."""
    ledger = build_work_ledger(
        [
            _usage_event(session="shared", transcript="tx-shared"),
            _section_event(session="shared", section_id="section-a"),
            _section_event(session="shared", transcript="tx-shared", section_id="section-b"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    assert attribution["join_confidence"] == "medium"
    assert set(attribution["ambiguous_candidate_work_ids"]) == {
        "codex::shared::section-a",
        "codex::shared::section-b",
    }
    # Reason strings render in HTML title attrs/notes: id-free (counts + key
    # name only). Full candidate ids stay in ambiguous_candidate_work_ids
    # (JSON-only surface), asserted above.
    assert attribution["join_reason"] == "usage shares client_session_id with 2 sections; not allocating to a section"
    assert "codex::shared" not in attribution["join_reason"]
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["ambiguous_usage_count"] == 1
    assert all(item["usage_total"] == 0 for item in ledger["work_items"])
    assert "ambiguous_same_session_attribution" in {item["attention_type"] for item in ledger["attention_items"]}


def test_two_transcript_matched_sections_refuse_with_transcript_ambiguity_strategy() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="shared", transcript="tx-shared"),
            _section_event(session="shared", transcript="tx-shared", section_id="section-a"),
            _section_event(session="shared", transcript="tx-shared", section_id="section-b"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "exact_client_transcript_id_ambiguous_sections"
    assert attribution["join_confidence"] == "medium"
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["ambiguous_usage_count"] == 1


def test_mixed_transcript_and_session_matched_sections_refuse_allocation() -> None:
    """One section matched only by transcript, another only by session: two
    candidate sections exist, so nothing is allocated."""
    ledger = build_work_ledger(
        [
            _usage_event(session="shared", transcript="tx-shared"),
            _section_event(session="", transcript="tx-shared", section_id="transcript-only"),
            _section_event(session="shared", section_id="session-only"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] is None
    assert attribution["join_strategy"] == "exact_client_session_id_ambiguous_sections"
    assert attribution["join_confidence"] == "medium"
    assert set(attribution["ambiguous_candidate_work_ids"]) == {
        "codex::tx-shared::transcript-only",
        "codex::shared::session-only",
    }
    assert ledger["overview"]["attributed_usage_count"] == 0
    assert ledger["overview"]["ambiguous_usage_count"] == 1
    assert all(item["usage_total"] == 0 for item in ledger["work_items"])


def test_single_section_carrying_both_ids_still_allocates_via_transcript() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="solo", transcript="tx-solo"),
            _section_event(session="solo", transcript="tx-solo", section_id="only-work"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] == "codex::solo::only-work"
    assert attribution["join_strategy"] == "exact_client_transcript_id"
    assert attribution["join_confidence"] == "exact"
    assert ledger["overview"]["attributed_usage_count"] == 1
    assert ledger["work_items"][0]["usage_total"] == 175


def test_conflict_vetoed_co_section_does_not_create_ambiguity() -> None:
    """A section whose transcript CONFLICTS with the usage row is vetoed out
    entirely; it must not count as an ambiguity candidate, so the surviving
    section still allocates (pins the existing disambiguation behavior against
    the new section-scoped guard)."""
    ledger = build_work_ledger(
        [
            _usage_event(session="shared", transcript="tx-target"),
            _section_event(session="shared", transcript="tx-other", section_id="section-a"),
            _section_event(session="shared", transcript="tx-target", section_id="section-b"),
        ]
    )

    attribution = ledger["attributions"][0]
    assert attribution["work_id"] == "codex::shared::section-b"
    assert attribution["join_strategy"] == "exact_client_transcript_id"
    assert attribution["join_confidence"] == "exact"
    assert ledger["overview"]["attributed_usage_count"] == 1
    assert ledger["overview"]["ambiguous_usage_count"] == 0


# ---------------------------------------------------------------------------
# Legacy ':model:' shadowed base rows (findings 4 + 6): the stale base row of
# an un-migrated legacy pairing is excluded from usage truth, visibly.
# ---------------------------------------------------------------------------


def _legacy_pairing_events() -> list[dict]:
    """Verifier repro: stale base 'S'=1000 + 'S:model:opus'=3000 +
    'S:model:sonnet'=2000 tokens, one section in session S. True usage: 5000."""

    def _row(event_id: str, session_key: str, input_tokens: int, model: str) -> dict:
        row = _legacy_suffixed_claude_usage_event(session_key=session_key)
        row["event_id"] = event_id
        row["model"] = model
        row["estimated_input_tokens"] = input_tokens
        row["estimated_output_tokens"] = 0
        row["metadata"]["cached_input_tokens"] = 0
        return row

    return [
        _row("evt_stale_base", "S", 1000, "claude-opus-4-8"),
        _row("evt_lane_opus", "S:model:claude-opus-4-8", 3000, "claude-opus-4-8"),
        _row("evt_lane_sonnet", "S:model:claude-sonnet-4-9", 2000, "claude-sonnet-4-9"),
        _claude_section_event(),
    ]


def test_unmigrated_legacy_pairing_attributes_true_usage_not_double_count() -> None:
    ledger = build_work_ledger(_legacy_pairing_events())

    attributed = [attr for attr in ledger["attributions"] if attr.get("work_id")]
    assert {attr["join_confidence"] for attr in attributed} == {"exact"}
    assert sum(attr["usage_tokens"] for attr in attributed) == 5000
    assert len(ledger["usage_events"]) == 2
    assert "evt_stale_base" not in {event["usage_event_id"] for event in ledger["usage_events"]}
    assert ledger["work_items"][0]["usage_total"] == 5000
    # The exclusion is visible, not silent.
    assert ledger["insights"]["legacy_shadowed_rows"] == 1
    assert ledger["insights"]["legacy_shadowed_row_event_ids"] == ["evt_stale_base"]


def test_base_row_without_suffixed_siblings_is_not_excluded() -> None:
    events = [event for event in _legacy_pairing_events() if event["event_id"] not in {"evt_lane_opus", "evt_lane_sonnet"}]
    ledger = build_work_ledger(events)

    assert len(ledger["usage_events"]) == 1
    assert ledger["work_items"][0]["usage_total"] == 1000
    assert ledger["insights"]["legacy_shadowed_rows"] == 0


def test_new_scheme_lane_rows_are_never_shadow_excluded() -> None:
    """Mixed-version store: base-keyed lane rows (new scheme) coexisting with
    re-added suffixed rows must never be dropped as 'stale base rows'."""
    events = _legacy_pairing_events()
    for event in events:
        if event.get("event_id") == "evt_stale_base":
            event["metadata"]["usage_row_lane"] = "model:claude-opus-4-8"
    ledger = build_work_ledger(events)

    assert len(ledger["usage_events"]) == 3
    assert ledger["insights"]["legacy_shadowed_rows"] == 0


def test_non_claude_clients_with_model_marker_are_untouched() -> None:
    events = [
        _usage_event(session="S", event_id="codex_base"),
        _usage_event(session="S:model:gpt-5.5", event_id="codex_marker"),
        _section_event(session="S", section_id="codex-work"),
    ]
    ledger = build_work_ledger(events)

    assert len(ledger["usage_events"]) == 2
    assert {event["client_session_id"] for event in ledger["usage_events"]} == {"S", "S:model:gpt-5.5"}
    assert ledger["insights"]["legacy_shadowed_rows"] == 0
    attributed = [attr for attr in ledger["attributions"] if attr.get("work_id")]
    assert len(attributed) == 1
    assert attributed[0]["usage_tokens"] == 175


# ---------------------------------------------------------------------------
# Phase 2 Batch A. 2c: cache-aware triple — ONE definition of fresh
# (input + output), cache creation and cache reads separate, everywhere.
# ---------------------------------------------------------------------------


def _cache_split_usage_event(
    *,
    session: str = "codex-session",
    event_id: str = "usage_cache",
    input_tokens: int = 100,
    output_tokens: int = 25,
    cache_creation: int | None = 20,
    cache_read: int | None = 30,
    cached: int | None = None,
) -> dict:
    event = _usage_event(session=session, event_id=event_id)
    event["estimated_input_tokens"] = input_tokens
    event["estimated_output_tokens"] = output_tokens
    metadata = event["metadata"]
    if cache_creation is None and cache_read is None:
        metadata["cached_input_tokens"] = cached or 0
        metadata.pop("cache_creation_input_tokens", None)
        metadata.pop("cache_read_input_tokens", None)
    else:
        metadata["cache_creation_input_tokens"] = cache_creation or 0
        metadata["cache_read_input_tokens"] = cache_read or 0
        metadata["cached_input_tokens"] = (cache_creation or 0) + (cache_read or 0)
    return event


def test_cache_triple_is_additive_and_consistent_across_all_surfaces() -> None:
    ledger = build_work_ledger([_cache_split_usage_event(), _section_event(session="codex-session")])

    usage = ledger["usage_events"][0]
    assert usage["fresh_tokens"] == 125
    assert usage["cache_creation_tokens"] == 20
    assert usage["cache_read_tokens"] == 30
    assert usage["fresh_tokens"] + usage["cache_creation_tokens"] + usage["cache_read_tokens"] == usage["total_tokens"]

    attribution = ledger["attributions"][0]
    assert attribution["usage_fresh_tokens"] == 125
    assert attribution["usage_cache_read_tokens"] == 30
    assert attribution["usage_cache_creation_tokens"] == 20

    item = ledger["work_items"][0]
    assert item["usage_total"] == 175  # existing key keeps its meaning
    assert item["usage_fresh_total"] == 125
    assert item["usage_cache_read_total"] == 30
    assert item["usage_cache_creation_total"] == 20

    row = ledger["usage_reconciliation"][0]
    assert row["total_tokens"] == 175
    assert row["fresh_tokens"] == 125
    assert row["cache_read_tokens"] == 30
    assert row["cache_creation_tokens"] == 20

    overview = ledger["overview"]
    assert overview["total_tokens"] == 175  # existing key keeps its meaning
    assert overview["total_fresh_tokens"] == 125
    assert overview["total_cache_read_tokens"] == 30
    assert overview["total_cache_creation_tokens"] == 20

    timeline_usage = next(entry for entry in ledger["timeline"] if entry["event_kind"] == "usage")
    assert timeline_usage["tokens"] == 175
    assert timeline_usage["tokens_fresh"] == 125
    assert timeline_usage["tokens_cache_read"] == 30
    assert timeline_usage["tokens_cache_creation"] == 20

    breakdown = overview["usage_confidence_breakdown"][0]
    assert breakdown["tokens"] == 175
    assert breakdown["fresh_tokens"] == 125
    assert breakdown["cache_read_tokens"] == 30
    assert breakdown["cache_creation_tokens"] == 20

    summary = ledger["insights"]["usage_attribution_summary"]
    assert summary["attributed_tokens"] == 175
    assert summary["attributed_fresh_tokens"] == 125
    assert summary["attributed_cache_read_tokens"] == 30
    assert summary["attributed_cache_creation_tokens"] == 20


def test_merged_only_legacy_cache_rows_fall_back_to_cache_reads() -> None:
    # Rows without the creation/read split: the merged figure is honestly
    # treated as cache reads (creation 0), never dropped or double-guessed.
    ledger = build_work_ledger([_cache_split_usage_event(cache_creation=None, cache_read=None, cached=50)])

    usage = ledger["usage_events"][0]
    assert usage["cache_creation_tokens"] == 0
    assert usage["cache_read_tokens"] == 50
    assert usage["fresh_tokens"] + usage["cache_creation_tokens"] + usage["cache_read_tokens"] == usage["total_tokens"] == 175


def test_cache_heavy_row_shows_small_fresh_figure_on_every_surface() -> None:
    # The exact real-store distortion: 1M cache reads at full weight made the
    # headline read 1.001M when only 1k tokens were fresh compute.
    ledger = build_work_ledger(
        [
            _cache_split_usage_event(input_tokens=900, output_tokens=100, cache_creation=0, cache_read=1_000_000),
            _section_event(session="codex-session"),
        ]
    )

    assert ledger["overview"]["total_tokens"] == 1_001_000
    assert ledger["overview"]["total_fresh_tokens"] == 1_000
    assert ledger["overview"]["total_cache_read_tokens"] == 1_000_000
    assert ledger["usage_reconciliation"][0]["fresh_tokens"] == 1_000
    assert ledger["work_items"][0]["usage_fresh_total"] == 1_000
    assert ledger["insights"]["usage_attribution_summary"]["attributed_fresh_tokens"] == 1_000
    blind_spots = ledger["insights"]["blind_spots"]
    assert all("fresh_tokens" in spot for spot in blind_spots)
    entry = ledger["session_rollup"]["sessions"][0]
    assert entry["usage"]["fresh_tokens"] == 1_000
    assert entry["usage"]["cache_read_tokens"] == 1_000_000


def test_blind_spots_carry_fresh_split_and_keep_honest_none() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="unmatched-session"),
            _section_event(session="codex-session", status="completed"),
            _evidence_event(),
        ]
    )

    spots = {spot["type"]: spot for spot in ledger["insights"]["blind_spots"]}
    unmatched = spots["usage_without_mcp_context"]
    assert unmatched["tokens"] == 175
    assert unmatched["fresh_tokens"] == 125
    assert unmatched["cache_read_tokens"] == 50
    no_usage = spots["completed_without_attributed_usage"]
    # Usage-unknown stays unknown — never a fake zero.
    assert no_usage["tokens"] is None
    assert no_usage["fresh_tokens"] is None


# ---------------------------------------------------------------------------
# Phase 2 Batch A. 2d: attributed-first reconciliation ordering + shared cap
# metadata (the old alphabetical-desc sort was the review bug: the single
# attributed row sat at index 480 of 481 and the 120-row cap hid it).
# ---------------------------------------------------------------------------


def test_reconciliation_orders_states_attributed_first() -> None:
    ledger = build_work_ledger(
        [
            _usage_event(session="joined", event_id="usage_joined"),
            _usage_event(session="ambiguous", event_id="usage_ambiguous"),
            _usage_event(session="run-only", event_id="usage_run_only"),
            _usage_event(session="missing", event_id="usage_missing"),
            _section_event(session="joined", section_id="joined"),
            _section_event(session="ambiguous", section_id="ambiguous-a"),
            _section_event(session="ambiguous", section_id="ambiguous-b"),
            _section_event(session="other", section_id="run-only", run_id="client_codex_run-only"),
        ]
    )

    states = [row["usage_reconciliation_state"] for row in ledger["usage_reconciliation"]]
    assert states == ["attributed", "ambiguous", "context_matched_unallocated", "usage_without_mcp_context"]


def test_reconciliation_tiebreak_is_fresh_tokens_desc_not_total() -> None:
    small_total_big_fresh = _cache_split_usage_event(
        session="fresh-heavy", event_id="usage_fresh_heavy", input_tokens=200, output_tokens=0, cache_creation=0, cache_read=0
    )
    big_total_small_fresh = _cache_split_usage_event(
        session="cache-heavy", event_id="usage_cache_heavy", input_tokens=100, output_tokens=0, cache_creation=0, cache_read=10_000
    )
    ledger = build_work_ledger([big_total_small_fresh, small_total_big_fresh])

    rows = ledger["usage_reconciliation"]
    assert [row["usage_event_id"] for row in rows] == ["evt_usage_fresh_heavy", "evt_usage_cache_heavy"]


def test_attributed_row_survives_the_120_row_cap_at_the_data_level() -> None:
    """Exact review regression class: >120 unattributed rows + 1 attributed.
    The attributed row must be rows[0] and inside any capped slice."""
    from agentacct.work_ledger import capped_rows

    events = [
        _usage_event(session=f"noise-session-{index:03d}", event_id=f"usage_noise_{index:03d}")
        for index in range(121)
    ]
    events.append(_usage_event(session="attributed-session", event_id="usage_attributed"))
    events.append(_section_event(session="attributed-session", section_id="the-work"))
    ledger = build_work_ledger(events)

    rows = ledger["usage_reconciliation"]
    assert len(rows) == 122
    assert rows[0]["usage_reconciliation_state"] == "attributed"
    assert rows[0]["usage_event_id"] == "evt_usage_attributed"

    capped = capped_rows(rows, 120)
    assert capped["total"] == 122
    assert capped["shown"] == 120
    assert len(capped["rows"]) == 120
    assert any(row["usage_event_id"] == "evt_usage_attributed" for row in capped["rows"])


def test_capped_rows_metadata_when_not_truncated() -> None:
    from agentacct.work_ledger import capped_rows

    capped = capped_rows([1, 2, 3], 10)
    assert capped == {"rows": [1, 2, 3], "total": 3, "shown": 3}


# ---------------------------------------------------------------------------
# Phase 2 Batch A. 2b: attention cause-groups — headline = causes, not one
# item per flooded usage row; groups derive FROM the detail items.
# ---------------------------------------------------------------------------


def test_attention_groups_cover_all_five_causes_and_match_item_counts() -> None:
    from collections import Counter

    events = [
        # completed_without_strong_evidence
        _section_event(session="cause1-session", section_id="done-no-ev", status="completed"),
        # completed_evidenced_work_without_attributed_usage
        _section_event(session="cause2-session", section_id="done-ev", status="completed"),
        _evidence_event(section_id="done-ev"),
        # ambiguous_same_session_attribution
        _usage_event(session="cause3-session", event_id="usage_amb"),
        _section_event(session="cause3-session", section_id="amb-a"),
        _section_event(session="cause3-session", section_id="amb-b"),
        # missing_client_session_id
        _section_event(session="", section_id="no-session"),
        # usage_truth_without_mcp_context (flooded: several identical rows)
        _usage_event(session="cause5-session-aaaa", event_id="usage_ctx_a"),
        _usage_event(session="cause5-session-bbbb", event_id="usage_ctx_b"),
        _usage_event(session="cause5-session-cccc", event_id="usage_ctx_c"),
        _usage_event(session="cause5-session-dddd", event_id="usage_ctx_d"),
    ]
    ledger = build_work_ledger(events)

    groups = {group["cause"]: group for group in ledger["attention_groups"]["groups"]}
    item_counts = Counter(item["attention_type"] for item in ledger["attention_items"])
    assert set(groups) == set(item_counts) and set(groups) >= {
        "completed_without_strong_evidence",
        "completed_evidenced_work_without_attributed_usage",
        "ambiguous_same_session_attribution",
        "missing_client_session_id",
        "usage_truth_without_mcp_context",
    }
    for cause, group in groups.items():
        assert group["count"] == item_counts[cause], cause
        assert group["recommended_next_step"]
        assert str(group["count"]) in group["title"]
        assert len(group["example_refs"]) <= 3
    assert ledger["attention_groups"]["total_items"] == len(ledger["attention_items"])

    usage_group = groups["usage_truth_without_mcp_context"]
    assert usage_group["count"] == 4
    assert len(usage_group["example_refs"]) == 3  # bounded
    for ref in usage_group["example_refs"]:
        assert ref["kind"] == "usage"
        assert "cause5-session" not in ref["label"]  # 8-char truncation, no full ids
        assert "codex" in ref["label"]
        assert not ref["label"].startswith("/")  # never an absolute path
        assert "/tmp" not in ref["label"]
    # Token sums only for the usage-bearing cause.
    assert usage_group["fresh_tokens"] == 4 * 125
    assert usage_group["total_tokens"] == 4 * 175
    assert groups["completed_without_strong_evidence"]["fresh_tokens"] is None
    work_ref = groups["completed_without_strong_evidence"]["example_refs"][0]
    assert work_ref["kind"] == "work"
    assert work_ref["work_id"]
    assert len(work_ref["label"]) <= 60

    # Severity desc, then count desc.
    severities = [group["severity"] for group in ledger["attention_groups"]["groups"]]
    assert severities == sorted(severities, key=lambda severity: {"high": 3, "medium": 2, "low": 1}[severity], reverse=True)

    # Overview headline: group count with the raw count alongside.
    assert ledger["overview"]["attention_group_count"] == len(groups)
    assert ledger["overview"]["attention_item_count"] == len(ledger["attention_items"])
    assert ledger["insights"]["source_overview"]["attention_group_count"] == len(groups)


def test_attention_groups_empty_ledger() -> None:
    ledger = build_work_ledger([])

    assert ledger["attention_groups"] == {"groups": [], "total_items": 0}
    assert ledger["overview"]["attention_group_count"] == 0


# ---------------------------------------------------------------------------
# Phase 2 Batch A. 2e: Chronicle's own diagnostic tool events never reach
# user-facing builders; the exclusion is a labeled count, never silent.
# ---------------------------------------------------------------------------


def _doctor_probe_event() -> dict:
    return {
        "event_id": "evt_doctor_probe",
        "created_at": 40,
        "source": "agent-sentinel-mcp-doctor",
        "event_type": "mcp_doctor_test",
        "run_id": "mcp_doctor_test",
        "metadata": {"summary": "safe local MCP doctor test event"},
    }


def _smoke_usage_shaped_event(*, run_id: str = "user-chosen-run") -> dict:
    """Forward-looking guarantee: a smoke event that would otherwise QUALIFY as
    a trusted usage-truth row (model_usage + import markers) must still be
    excluded by its diagnostic source."""

    event = _usage_event(session="smoke-session", event_id="usage_smoke")
    event["source"] = "agent-sentinel-mcp-workflow-smoke"
    event["run_id"] = run_id
    return event


def test_diagnostic_tool_events_are_invisible_to_user_facing_builders() -> None:
    events = [
        _usage_event(session="codex-session"),
        _section_event(session="codex-session"),
        _doctor_probe_event(),
        _smoke_usage_shaped_event(),
    ]
    ledger = build_work_ledger(events)

    assert {usage["usage_event_id"] for usage in ledger["usage_events"]} == {"evt_usage"}
    assert ledger["overview"]["usage_truth_count"] == 1
    assert ledger["overview"]["total_tokens"] == 175
    assert {entry.get("event_id") for entry in ledger["timeline"]} == {"evt_usage", "evt_section_mcp-v1_checkpoint"}
    assert [entry["client_session_id"] for entry in ledger["session_rollup"]["sessions"]] == ["codex-session"]
    assert all("smoke" not in str(row.get("usage_event_id")) for row in ledger["usage_reconciliation"])
    # The exclusion is visible, never silent.
    assert ledger["insights"]["diagnostic_tool_events"] == {
        "count": 2,
        "by_event_type": {"mcp_doctor_test": 1, "model_usage": 1},
        "by_source": {"agent-sentinel-mcp-doctor": 1, "agent-sentinel-mcp-workflow-smoke": 1},
    }


def test_smoke_event_with_custom_run_id_is_still_excluded_by_source() -> None:
    ledger = build_work_ledger([_smoke_usage_shaped_event(run_id="totally-custom-run")])

    assert ledger["usage_events"] == []
    assert ledger["insights"]["diagnostic_tool_events"]["count"] == 1


def test_real_user_event_with_diagnostic_run_id_is_never_hidden() -> None:
    # run_id is user-controlled free text: a collision must not hide real work.
    event = _usage_event(session="codex-session", event_id="usage_real")
    event["run_id"] = "mcp_doctor_test"
    ledger = build_work_ledger([event])

    assert [usage["usage_event_id"] for usage in ledger["usage_events"]] == ["evt_usage_real"]
    assert ledger["insights"]["diagnostic_tool_events"]["count"] == 0


def test_work_ledger_schema_version_bumped_to_v2() -> None:
    # v2: session_rollup + attention_groups, cache triples, attributed-first
    # reconciliation, diagnostic filtering (release-note item).
    assert build_work_ledger([])["schema_version"] == "agent-sentinel.work-ledger.v2"


def _join_index_parity_events() -> list[dict]:
    """Events that drive every attribution / join-inspector path the join index
    must preserve: two sibling sections in one session (ambiguity guard), an
    exact single-section allocation with a superseded check, a transcript-only
    match, a run_id grouping hint across sessions, and unmatched usage."""

    return [
        # sess-A: two sibling sections -> usage stays unallocated (ambiguous).
        _usage_event(session="sess-A", event_id="a", created_at=10),
        _section_event(session="sess-A", section_id="A1", status="completed", created_at=11),
        _section_event(session="sess-A", section_id="A2", status="checkpoint", created_at=12),
        # sess-B: single section -> exact session allocation; failed check then
        # a later passing check in the same scope (supersession).
        _usage_event(session="sess-B", event_id="b", created_at=20),
        _section_event(session="sess-B", section_id="B1", status="completed", created_at=21),
        _evidence_event(section_id="B1", result="failed"),
        _evidence_event(section_id="B1", result="passed"),
        # transcript-only match (section carries no session id).
        _usage_event(session="sess-C", transcript="tx-C", event_id="c", created_at=30),
        _section_event(session="", transcript="tx-C", section_id="C1", status="completed", created_at=31),
        # run_id grouping hint: usage.run_id == section.run_id, different sessions.
        _usage_event(session="sess-D", event_id="d", created_at=40),
        _section_event(session="sess-E", section_id="E1", run_id="client_codex_sess-D", status="checkpoint", created_at=41),
        # usage with no matching section at all.
        _usage_event(session="sess-Z", event_id="z", created_at=50),
    ]


def test_join_index_matches_a_full_scan_byte_for_byte() -> None:
    """The id-key index that lets the attribution loops skip non-matching pairs
    must produce a ledger identical to the full O(usage x work) scan. Build both
    ways and assert deep equality, on an event set that exercises the ambiguity
    guard, exact/transcript allocation, run_id grouping, and unmatched usage."""

    events = _join_index_parity_events()
    indexed = build_work_ledger(events, use_index=True)
    full = build_work_ledger(events, use_index=False)

    assert indexed == full

    # Non-vacuous: the set must actually drive an ambiguous refusal AND real
    # allocations, so the parity assertion is proven on the paths that matter.
    strategies = [attribution["join_strategy"] for attribution in indexed["attributions"]]
    assert any("ambiguous" in strategy for strategy in strategies)
    assert "exact_client_session_id" in strategies
    assert "exact_client_transcript_id" in strategies
