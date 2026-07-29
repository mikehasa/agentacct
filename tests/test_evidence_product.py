from __future__ import annotations

import json

from agentacct.evidence_product import build_evidence_product


def _envelope(
    evidence_id: str,
    *,
    assertion: str,
    source_type: str,
    source_system: str,
    dimensions: list[str],
    basis: str,
    subjects: dict[str, str],
    payload: dict[str, object],
    event_timestamp: str = "2026-07-13T00:00:00Z",
    observed_at: str | None = None,
    event_type: str = "fixture",
    source_instance: str = "fixture-instance",
    completeness_status: str = "complete",
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "event_type": event_type,
        "event_timestamp": event_timestamp,
        "observed_at": observed_at or event_timestamp,
        "assertion": assertion,
        "source_type": source_type,
        "source_system": source_system,
        "source_instance": source_instance,
        "dimensions": dimensions,
        "measurement_basis": {dimension: basis for dimension in dimensions},
        "completeness": {"status": completeness_status},
        "subjects": subjects,
        "payload": payload,
    }


def test_evidence_product_keeps_sources_and_claims_separate() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_hook",
                assertion="observed",
                source_type="client_hook",
                source_system="codex",
                dimensions=["activity"],
                basis="client_hook_observed",
                subjects={"client_session_id": "session-1", "tool_call_id": "tool-1"},
                payload={"tool_category": "shell", "exit_code": 0},
            ),
            _envelope(
                "evd_mcp",
                assertion="claimed",
                source_type="mcp_agent_reported",
                source_system="codex",
                dimensions=["task_semantics"],
                basis="agent_claimed",
                subjects={"client_session_id": "session-1", "work_id": "work-1"},
                payload={"summary": "Implemented evidence core."},
            ),
        ]
    )

    assert product["summary"] == {
        "evidence_count": 2,
        "observed_count": 1,
        "claimed_count": 1,
        "source_count": 2,
        "discrepancy_count": 0,
    }
    matrix = product["evidence_matrix"]["rows"]
    assert {(row["source_type"], row["dimension"]) for row in matrix} == {
        ("client_hook", "activity"),
        ("mcp_agent_reported", "task_semantics"),
    }
    hook_row = next(row for row in matrix if row["source_type"] == "client_hook")
    mcp_row = next(row for row in matrix if row["source_type"] == "mcp_agent_reported")
    assert hook_row["authority_counts"] == {"authoritative": 1}
    assert mcp_row["authority_counts"] == {"claimed": 1}
    assert product["work_graph"]["node_count"] == 3
    assert {edge["link_confidence"] for edge in product["work_graph"]["edges"]} == {"observed", "claimed"}


def test_work_graph_scopes_same_raw_session_and_run_ids_by_client_home() -> None:
    envelopes = [
        _envelope(
            f"evd_home_{suffix}",
            assertion="observed",
            source_type="local_client_log",
            source_system="codex",
            dimensions=["session_identity"],
            basis="source_observed",
            subjects={
                "run_id": "shared-run",
                "client_session_id": "shared-session",
                "extra": {
                    "source_namespace_fingerprint": f"sha256:{suffix * 64}"
                },
            },
            payload={"observation_basis": "local_client_session_record"},
        )
        for suffix in ("a", "b")
    ]

    graph = build_evidence_product(envelopes)["work_graph"]
    session_nodes = [
        node for node in graph["nodes"] if node["kind"] == "client_session_id"
    ]
    run_nodes = [node for node in graph["nodes"] if node["kind"] == "run_id"]
    assert len(session_nodes) == 2
    assert len(run_nodes) == 2
    assert len({node["namespace"] for node in session_nodes}) == 2
    assert len({node["namespace"] for node in run_nodes}) == 2

    node_by_id = {node["node_id"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        source_namespace = node_by_id[edge["from"]]["namespace"]
        target_namespace = node_by_id[edge["to"]]["namespace"]
        assert json.loads(source_namespace)["source_home"] == json.loads(
            target_namespace
        )["source_home"]


def test_unlinked_cross_source_cost_claims_are_not_false_conflicts_or_summed() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_paperclip",
                assertion="claimed",
                source_type="orchestrator_api",
                source_system="paperclip",
                dimensions=["cost"],
                basis="orchestrator_claimed",
                subjects={"run_id": "run-1"},
                payload={"cost_usd": 0.0},
            ),
            _envelope(
                "evd_provider",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={"run_id": "run-1"},
                payload={"provider_billed_cost_usd": 1.25},
            ),
        ]
    )

    assert "cost_value_conflict" not in product["discrepancies"]["by_kind"]
    basis = product["cost_outcome_basis"]
    assert basis["record_count"] == 2
    assert basis["aggregation_policy"] == "claims_are_not_summed_without_non_overlapping_scope_evidence"
    assert "total_cost_usd" not in basis


def test_same_source_same_point_cost_corrections_are_exposed() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_original",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={"run_id": "run-1"},
                payload={"provider_billed_cost_usd": 1.0},
            ),
            _envelope(
                "evd_corrected",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={"run_id": "run-1"},
                payload={"provider_billed_cost_usd": 1.25},
            ),
        ]
    )

    discrepancy = next(item for item in product["discrepancies"]["items"] if item["kind"] == "cost_value_conflict")
    assert discrepancy["severity"] == "high"
    assert discrepancy["title"] == "Cost values disagree"
    assert discrepancy["action_code"] == "resolve_cost_value_conflict"
    assert discrepancy["subject"]["kind"] == "run_id"
    assert discrepancy["subject"]["id"] == "run-1"
    assert discrepancy["fact"] == "fixture|at:2026-07-13T00:00:00Z"


def test_numeric_equivalence_and_cost_basis_variance_are_not_value_conflicts() -> None:
    equivalent = build_evidence_product(
        [
            _envelope(
                "evd_int",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={"run_id": "run-1"},
                payload={"provider_billed_cost_usd": 1},
            ),
            _envelope(
                "evd_float",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={"run_id": "run-1"},
                payload={"provider_billed_cost_usd": 1.0},
            ),
        ]
    )
    assert "cost_value_conflict" not in equivalent["discrepancies"]["by_kind"]

    different_bases = build_evidence_product(
        [
            _envelope(
                "evd_estimate",
                assertion="observed",
                source_type="provider_response",
                source_system="openai",
                dimensions=["cost"],
                basis="estimated_from_tokens",
                subjects={"run_id": "run-1"},
                payload={"estimated_cost_usd": 0.9},
            ),
            _envelope(
                "evd_invoice",
                assertion="observed",
                source_type="provider_response",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={"run_id": "run-1"},
                payload={"provider_billed_cost_usd": 1.0},
            ),
        ]
    )
    assert "cost_value_conflict" not in different_bases["discrepancies"]["by_kind"]
    variance = next(item for item in different_bases["discrepancies"]["items"] if item["kind"] == "cost_basis_variance")
    assert variance["severity"] == "info"
    assert variance["action_code"] == "review_cost_basis_variance"
    assert {candidate["basis_family"] for candidate in variance["candidates"]} == {"estimated", "provider_billed"}


def test_equivalent_usage_components_and_total_are_not_a_false_conflict() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_components",
                assertion="observed",
                source_type="otlp_http_json",
                source_system="openlit",
                dimensions=["usage"],
                basis="telemetry_reported",
                subjects={"client_session_id": "session-1"},
                payload={"input_tokens": 10, "output_tokens": 5},
            ),
            _envelope(
                "evd_total",
                assertion="observed",
                source_type="otlp_http_json",
                source_system="openlit",
                dimensions=["usage"],
                basis="telemetry_reported",
                subjects={"client_session_id": "session-1"},
                payload={"total_tokens": 15},
            ),
        ]
    )

    assert "usage_value_conflict" not in product["discrepancies"]["by_kind"]


def test_different_comparable_usage_totals_remain_a_conflict() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_components",
                assertion="observed",
                source_type="client_hook",
                source_system="codex",
                dimensions=["usage"],
                basis="telemetry_reported",
                subjects={"client_session_id": "session-1"},
                payload={"input_tokens": 10, "output_tokens": 5},
            ),
            _envelope(
                "evd_total",
                assertion="observed",
                source_type="local_client_log",
                source_system="codex",
                dimensions=["usage"],
                basis="client_reported",
                subjects={"client_session_id": "session-1"},
                payload={"total_tokens": 17},
            ),
        ]
    )

    discrepancy = next(
        item for item in product["discrepancies"]["items"] if item["kind"] == "usage_value_conflict"
    )
    assert discrepancy["action_code"] == "resolve_usage_value_conflict"
    assert {candidate["value"] for candidate in discrepancy["candidates"]} == {15, 17}


def test_same_session_measurements_at_different_times_are_not_conflicts() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_first",
                assertion="observed",
                source_type="local_client_log",
                source_system="codex",
                dimensions=["usage"],
                basis="client_reported",
                subjects={"client_session_id": "session-1"},
                payload={"total_tokens": 100},
                event_timestamp="2026-07-13T00:00:00Z",
            ),
            _envelope(
                "evd_later",
                assertion="observed",
                source_type="local_client_log",
                source_system="codex",
                dimensions=["usage"],
                basis="client_reported",
                subjects={"client_session_id": "session-1"},
                payload={"total_tokens": 200},
                event_timestamp="2026-07-13T00:01:00Z",
            ),
        ]
    )

    assert "usage_value_conflict" not in product["discrepancies"]["by_kind"]


def test_same_raw_ids_in_different_sources_or_projects_do_not_merge() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_a",
                assertion="observed",
                source_type="otlp_http_json",
                source_system="openlit",
                dimensions=["usage"],
                basis="telemetry_reported",
                subjects={"project_id": "project-a", "run_id": "run-1"},
                payload={"total_tokens": 100},
            ),
            _envelope(
                "evd_b",
                assertion="observed",
                source_type="local_client_log",
                source_system="codex",
                dimensions=["usage"],
                basis="client_reported",
                subjects={"project_id": "project-b", "run_id": "run-1"},
                payload={"total_tokens": 200},
            ),
        ]
    )

    run_nodes = [node for node in product["work_graph"]["nodes"] if node["kind"] == "run_id"]
    assert len(run_nodes) == 2
    assert len({node["namespace"] for node in run_nodes}) == 2
    assert "usage_value_conflict" not in product["discrepancies"]["by_kind"]


def test_observed_session_without_mcp_is_visible_as_missing_semantics() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_session",
                assertion="observed",
                source_type="client_hook",
                source_system="claude-code",
                dimensions=["activity"],
                basis="client_hook_observed",
                subjects={"client_session_id": "session-no-mcp"},
                payload={"status": "started"},
            )
        ]
    )

    assert product["summary"]["evidence_count"] == 1
    assert product["discrepancies"]["items"] == [
        {
            "kind": "semantic_context_missing",
            "severity": "info",
            "subject": {
                "namespace": '{"organization":"unscoped","project":"unscoped","system":"claude-code"}',
                "kind": "client_session_id",
                "id": "session-no-mcp",
            },
            "dimension": "task_semantics",
            "title": "Work meaning is missing",
            "explanation": (
                "Observed client activity has no matching claimed task-semantic evidence in the current scope."
            ),
            "recommended_next_step": (
                "Attach a Work Event or MCP semantic report when available; do not infer task meaning from activity "
                "alone."
            ),
            "action_code": "attach_semantic_context",
        }
    ]


def test_store_conflicts_are_promoted_to_high_severity_discrepancies() -> None:
    product = build_evidence_product([], store_conflicts=[{"idempotency_key": "idem-1", "version_count": 2}])

    assert product["discrepancies"]["items"] == [
        {
            "kind": "source_identity_conflict",
            "severity": "high",
            "idempotency_key": "idem-1",
            "version_count": 2,
            "title": "A source event has conflicting versions",
            "explanation": (
                "The same deterministic source identity arrived with different normalized content, so agentacct "
                "preserved each version instead of silently replacing one."
            ),
            "recommended_next_step": (
                "Inspect the preserved versions and correct the source adapter identity or normalization rule before "
                "relying on it."
            ),
            "action_code": "inspect_source_identity_conflict",
        }
    ]


def test_explicit_claimed_link_is_rendered_with_validation_confidence() -> None:
    claim = _envelope(
        "evd_claim",
        assertion="claimed",
        source_type="mcp_agent_reported",
        source_system="codex",
        dimensions=["task_semantics"],
        basis="agent_claimed",
        subjects={"work_id": "work-1"},
        payload={},
    )
    observation = _envelope(
        "evd_observed",
        assertion="observed",
        source_type="client_hook",
        source_system="codex",
        dimensions=["activity"],
        basis="client_hook_observed",
        subjects={"client_session_id": "session-1"},
        payload={},
    )

    product = build_evidence_product(
        [claim, observation],
        claimed_links=[
            {
                "link": {
                    "link_id": "clm_link",
                    "claimed_evidence_id": "evd_claim",
                    "observed_evidence_id": "evd_observed",
                    "relationship": "explains",
                    "dimensions": ["task_semantics"],
                    "rationale_code": "exact_join_key",
                },
                "validation_state": "valid",
            }
        ],
    )

    explicit = next(edge for edge in product["work_graph"]["edges"] if edge.get("link_id") == "clm_link")
    work_node = next(node for node in product["work_graph"]["nodes"] if node["kind"] == "work_id")
    session_node = next(node for node in product["work_graph"]["nodes"] if node["kind"] == "client_session_id")
    assert explicit["from"] == work_node["node_id"]
    assert explicit["to"] == session_node["node_id"]
    assert explicit["link_confidence"] == "valid"
    assert explicit["link_basis"] == "exact_join_key"


def test_namespace_encoding_cannot_collide_and_extra_cannot_override_core_subjects() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_a",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={
                    "organization": "alpha|project=beta",
                    "project_id": "gamma",
                    "run_id": "run-1",
                    "extra": {"project_id": "forged-project"},
                },
                payload={"provider_billed_cost_usd": 1},
            ),
            _envelope(
                "evd_b",
                assertion="observed",
                source_type="provider_invoice",
                source_system="openai",
                dimensions=["cost"],
                basis="provider_billed",
                subjects={
                    "organization": "alpha",
                    "project_id": "beta|project=gamma",
                    "run_id": "run-1",
                },
                payload={"provider_billed_cost_usd": 2},
            ),
        ]
    )

    run_nodes = [node for node in product["work_graph"]["nodes"] if node["kind"] == "run_id"]
    assert len(run_nodes) == 2
    assert len({node["namespace"] for node in run_nodes}) == 2
    assert all("forged-project" not in node["namespace"] for node in run_nodes)
    assert "cost_value_conflict" not in product["discrepancies"]["by_kind"]


def test_source_coverage_groups_instances_without_claiming_connection_health() -> None:
    product = build_evidence_product(
        [
            _envelope(
                "evd_local_observed",
                assertion="observed",
                source_type="client_hook",
                source_system="codex",
                source_instance="local-a",
                dimensions=["activity", "usage"],
                basis="client_hook_observed",
                subjects={"client_session_id": "session-1"},
                payload={"total_tokens": 10},
                event_timestamp="2026-07-13T00:00:00Z",
                observed_at="2026-07-13T00:05:00Z",
            ),
            _envelope(
                "evd_local_claimed",
                assertion="claimed",
                source_type="client_hook",
                source_system="codex",
                source_instance="local-a",
                dimensions=["task_semantics"],
                basis="agent_claimed",
                subjects={"client_session_id": "session-1"},
                payload={"summary": "metadata is not projected"},
                event_timestamp="2026-07-13T00:10:00Z",
                observed_at="2026-07-13T00:12:00Z",
                completeness_status="partial",
            ),
            _envelope(
                "evd_other_instance",
                assertion="observed",
                source_type="client_hook",
                source_system="codex",
                source_instance="local-b",
                dimensions=["activity"],
                basis="client_hook_observed",
                subjects={"client_session_id": "session-2"},
                payload={},
            ),
        ]
    )

    coverage = product["source_coverage"]
    assert coverage["source_count"] == 2
    local = next(source for source in coverage["sources"] if source["source_instance"] == "local-a")
    assert local["evidence_count"] == 2
    assert local["observed_count"] == 1
    assert local["claimed_count"] == 1
    assert local["complete_count"] == 1
    assert local["partial_count"] == 1
    assert local["unknown_count"] == 0
    assert local["dimensions"] == ["activity", "task_semantics", "usage"]
    assert local["measurement_bases"] == {"agent_claimed": 1, "client_hook_observed": 2}
    assert local["measurement_bases_by_dimension"] == {
        "activity": {"client_hook_observed": 1},
        "task_semantics": {"agent_claimed": 1},
        "usage": {"client_hook_observed": 1},
    }
    assert local["first_event_at"] == "2026-07-13T00:00:00Z"
    assert local["last_event_at"] == "2026-07-13T00:10:00Z"
    assert local["last_observed_at"] == "2026-07-13T00:12:00Z"
    assert {source["connection_state"] for source in coverage["sources"]} == {"not_verified"}


def test_work_evidence_uses_conservative_scope_priority_and_omits_content() -> None:
    work = _envelope(
        "evd_work",
        assertion="observed",
        source_type="client_hook",
        source_system="codex",
        dimensions=["activity"],
        basis="client_hook_observed",
        subjects={
            "work_id": "work-1",
            "section_id": "section-1",
            "client_session_id": "session-1",
            "run_id": "run-1",
        },
        payload={"secret_canary": "NEVER_PROJECT_PAYLOAD_CANARY"},
    )
    work["raw_ref"] = {"uri": "NEVER_PROJECT_RAW_REF"}
    work["raw_digest"] = "NEVER_PROJECT_RAW_DIGEST"
    section_a = _envelope(
        "evd_section_a",
        assertion="claimed",
        source_type="mcp_agent_reported",
        source_system="codex",
        dimensions=["task_semantics"],
        basis="agent_claimed",
        subjects={"section_id": "shared-section", "client_session_id": "session-a"},
        payload={},
    )
    section_b = _envelope(
        "evd_section_b",
        assertion="claimed",
        source_type="mcp_agent_reported",
        source_system="codex",
        dimensions=["task_semantics"],
        basis="agent_claimed",
        subjects={"section_id": "shared-section", "client_session_id": "session-b"},
        payload={},
    )
    section_only = _envelope(
        "evd_section_only",
        assertion="claimed",
        source_type="mcp_agent_reported",
        source_system="codex",
        dimensions=["task_semantics"],
        basis="agent_claimed",
        subjects={"section_id": "orphan-section"},
        payload={},
    )
    run_fallback = _envelope(
        "evd_run",
        assertion="observed",
        source_type="local_client_log",
        source_system="codex",
        dimensions=["usage"],
        basis="client_reported",
        subjects={"section_id": "section-without-session", "run_id": "run-fallback"},
        payload={"total_tokens": 5},
    )
    session_fallback = _envelope(
        "evd_session",
        assertion="observed",
        source_type="local_client_log",
        source_system="codex",
        dimensions=["activity"],
        basis="client_reported",
        subjects={"client_session_id": "session-fallback"},
        payload={},
    )

    projection = build_evidence_product(
        [work, section_a, section_b, section_only, run_fallback, session_fallback]
    )["work_evidence"]

    assert projection["item_count"] == 5
    work_item = next(item for item in projection["items"] if item["match_kind"] == "work_id")
    assert work_item["match_fields"] == {"work_id": "work-1"}
    assert work_item["summary"]["sources"] == ["codex"]
    assert work_item["summary"]["dimensions"] == ["activity"]
    section_items = [item for item in projection["items"] if item["match_kind"] == "section_session"]
    assert len(section_items) == 2
    assert {item["match_fields"]["client_session_id"] for item in section_items} == {"session-a", "session-b"}
    assert len({item["group_id"] for item in section_items}) == 2
    run_item = next(item for item in projection["items"] if item["match_kind"] == "run_id")
    assert run_item["match_fields"] == {"run_id": "run-fallback"}
    session_item = next(item for item in projection["items"] if item["match_kind"] == "client_session_id")
    assert session_item["match_fields"] == {"client_session_id": "session-fallback"}
    assert all("orphan-section" not in item["match_fields"].values() for item in projection["items"])

    record = work_item["records"][0]
    assert set(record) == {
        "evidence_id",
        "source",
        "assertion",
        "event_type",
        "time",
        "dimensions",
        "basis",
        "authority",
        "completeness",
    }
    assert record["source"] == {
        "source_type": "client_hook",
        "source_system": "codex",
        "source_instance": "fixture-instance",
    }
    assert record["basis"] == {"activity": "client_hook_observed"}
    assert record["authority"] == {"activity": "authoritative"}
    serialized = json.dumps(projection, sort_keys=True)
    assert "NEVER_PROJECT_PAYLOAD_CANARY" not in serialized
    assert "NEVER_PROJECT_RAW_REF" not in serialized
    assert "NEVER_PROJECT_RAW_DIGEST" not in serialized
    assert '"payload"' not in serialized
    assert '"raw_ref"' not in serialized
    assert '"raw_digest"' not in serialized


def test_work_evidence_records_are_bounded_and_newest_first() -> None:
    envelopes = [
        _envelope(
            f"evd_{index:02d}",
            assertion="observed",
            source_type="client_hook",
            source_system="codex",
            dimensions=["activity"],
            basis="client_hook_observed",
            subjects={"work_id": "work-bounded"},
            payload={"ignored": index},
            event_timestamp=f"2026-07-13T00:00:{index:02d}Z",
        )
        for index in range(25)
    ]

    digest = build_evidence_product(envelopes)["work_evidence"]["items"][0]

    assert digest["summary"]["evidence_count"] == 25
    assert digest["shown_record_count"] == 20
    assert digest["records_truncated"] is True
    assert len(digest["records"]) == 20
    assert digest["records"][0]["evidence_id"] == "evd_24"
    assert digest["records"][-1]["evidence_id"] == "evd_05"
