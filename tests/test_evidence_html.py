from __future__ import annotations

from agentacct.evidence_html import (
    ADVANCED_WORK_EVIDENCE_LIMIT,
    ADVANCED_WORK_EVIDENCE_RECORD_LIMIT,
    render_advanced_index_body,
    render_cost_outcome_basis_body,
    render_discrepancies_body,
    render_evidence_drawer,
    render_evidence_matrix_body,
    render_source_coverage_compact,
    render_work_graph_body,
)


def _product() -> dict[str, object]:
    return {
        "summary": {
            "evidence_count": 2,
            "observed_count": 1,
            "claimed_count": 1,
            "source_count": 2,
            "discrepancy_count": 1,
        },
        "work_graph": {
            "node_count": 2,
            "edge_count": 1,
            "nodes": [
                {"node_id": "work_id:<script>", "kind": "work_id", "value": "<script>", "evidence_count": 1},
                {"node_id": "run_id:run-1", "kind": "run_id", "value": "run-1", "evidence_count": 1},
            ],
            "edges": [
                {
                    "from": "work_id:<script>",
                    "to": "run_id:run-1",
                    "relation": "has_run",
                    "assertion": "claimed",
                    "dimensions": ["task_semantics"],
                    "evidence_id": "evd-1",
                }
            ],
        },
        "evidence_matrix": {
            "rows": [
                {
                    "dimension": "cost",
                    "source_type": "provider_invoice",
                    "source_system": "provider",
                    "observed_count": 1,
                    "claimed_count": 0,
                    "complete_count": 1,
                    "partial_count": 0,
                    "unknown_count": 0,
                    "measurement_bases": {"provider_billed": 1},
                    "authority_counts": {"authoritative": 1},
                }
            ]
        },
        "discrepancies": {
            "items": [
                {
                    "kind": "cost_value_conflict",
                    "severity": "high",
                    "subject": {"kind": "run_id", "id": "run-1"},
                    "dimension": "cost",
                    "candidates": [{"value": {"cost_usd": 1.0}, "evidence_ids": ["evd-1"]}],
                    "title": "Provider and estimate disagree",
                    "explanation": "Two cost records cover the same measured fact.",
                    "recommended_next_step": "Verify the provider-billed record before acting.",
                }
            ]
        },
        "cost_outcome_basis": {
            "records": [
                {
                    "dimension": "cost",
                    "source_system": "provider",
                    "source_type": "provider_invoice",
                    "assertion": "observed",
                    "measurement_basis": "provider_billed",
                    "authority": "authoritative",
                    "completeness": "complete",
                    "values": {"cost_usd": 1.0},
                    "evidence_id": "evd-1",
                }
            ]
        },
        "source_coverage": {
            "source_count": 1,
            "sources": [
                {
                    "source_type": "otlp_http_json",
                    "source_system": "openlit",
                    "source_instance": "RAW_INSTANCE_CANARY",
                    "evidence_count": 2,
                    "observed_count": 2,
                    "claimed_count": 0,
                    "complete_count": 1,
                    "partial_count": 1,
                    "unknown_count": 0,
                    "dimensions": ["usage", "lifecycle"],
                    "measurement_bases": {"telemetry_reported": 2},
                    "first_event_at": "2026-07-13T00:00:00Z",
                    "last_event_at": "2026-07-13T00:05:00Z",
                    "last_observed_at": "2026-07-13T00:05:01Z",
                    "connection_state": "not_verified",
                }
            ],
        },
        "work_evidence": {
            "item_count": 1,
            "items": [
                {
                    "match_kind": "work_id",
                    "match_value": "RAW_MATCH_ID_CANARY",
                    "match_label": "Build evidence core",
                    "payload": "RAW_PAYLOAD_CANARY",
                    "summary": {
                        "evidence_count": 1,
                        "observed_count": 1,
                        "claimed_count": 0,
                        "sources": ["openlit"],
                        "dimensions": ["usage"],
                    },
                    "records": [
                        {
                            "evidence_id": "evd_abc/one?",
                            "source": {"source_system": "<script>", "source_type": "otlp_http_json"},
                            "assertion": "observed",
                            "event_type": "coding_agent.usage.observed",
                            "time": "2026-07-13T00:05:00Z",
                            "dimensions": ["usage"],
                            "basis": {"usage": "telemetry_reported"},
                            "authority": {"usage": "corroborating"},
                            "completeness": "partial",
                            "raw_content": "RAW_CONTENT_CANARY",
                        }
                    ],
                }
            ],
        },
    }


def test_evidence_bodies_render_the_four_product_views() -> None:
    product = _product()

    assert "Work Graph" in render_work_graph_body(product)
    assert "Evidence Matrix" in render_evidence_matrix_body(product)
    assert "Discrepancies" in render_discrepancies_body(product)
    assert "Cost &amp; Outcome Basis" in render_cost_outcome_basis_body(product)


def test_evidence_html_escapes_source_controlled_values() -> None:
    body = render_work_graph_body(_product())

    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_source_coverage_compact_is_honest_and_hides_source_instance() -> None:
    body = render_source_coverage_compact(_product())

    assert "Source coverage" in body
    assert "OpenLIT · OTLP / HTTP JSON" in body
    assert "Showing" not in body
    assert "2 evidence received" in body
    assert "2 observed" in body
    assert "0 claimed" in body
    assert "1 partial" in body
    assert "Covers Usage, Lifecycle" in body
    assert "Not verified" in body
    assert "does not prove that the source is installed, connected, or healthy now" in body
    assert "<table" not in body
    assert "RAW_INSTANCE_CANARY" not in body


def test_evidence_drawer_uses_native_details_and_keeps_raw_ids_out_of_summary() -> None:
    digest = _product()["work_evidence"]["items"][0]  # type: ignore[index]
    body = render_evidence_drawer(digest)  # type: ignore[arg-type]

    assert '<details class="evidence-drawer">' in body
    assert "<summary>" in body
    summary = body[body.index("<summary>") : body.index("</summary>")]
    assert "Build evidence core" in summary
    assert "1 evidence · 1 observed · 0 claimed" in summary
    assert "RAW_MATCH_ID_CANARY" not in summary
    assert "evd_abc/one?" not in summary
    assert "RAW_PAYLOAD_CANARY" not in body
    assert "RAW_CONTENT_CANARY" not in body
    assert "Sources: OpenLIT · Dimensions: Usage" in body
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert 'href="/evidence/events/evd_abc%2Fone%3F"' in body
    assert ">evd_abc/one?<" not in body


def test_advanced_hub_separates_product_views_from_forensic_inspectors() -> None:
    body = render_advanced_index_body(_product())

    assert "Inspect only when you need to" in body
    assert "Evidence projections" in body
    assert "Bounded diagnostic views" in body
    assert 'href="/raw"' in body
    assert 'href="/work-graph"' in body
    assert 'href="/evidence-matrix"' in body
    assert "Forensic inspectors" in body
    assert "opaque ids and complete normalized metadata" in body
    assert "Read the larger bounded forensic projection" in body
    assert "used by the server-rendered evidence views" not in body
    assert 'href="/evidence/events"' in body
    assert 'href="/evidence/claimed-links"' in body
    assert '<details class="evidence-drawer">' in body
    assert "RAW_PAYLOAD_CANARY" not in body


def test_advanced_work_evidence_is_recent_stable_and_explicitly_bounded() -> None:
    product = _product()
    items = []
    for index in range(ADVANCED_WORK_EVIDENCE_LIMIT + 5):
        event_time = f"2026-07-13T{index // 60:02d}:{index % 60:02d}:00Z"
        items.append(
            {
                "group_id": f"group-{index:03d}",
                "display_label": f"Match {index:03d}",
                "summary": {
                    "evidence_count": 8,
                    "observed_count": 8,
                    "claimed_count": 0,
                    "sources": ["codex"],
                    "dimensions": ["activity"],
                },
                "records": [
                    {
                        "evidence_id": f"evd_{index:03d}_{record_index}",
                        "source": {"source_system": "codex", "source_type": "client_hook"},
                        "assertion": "observed",
                        "event_type": "tool_completed",
                        "time": event_time,
                        "dimensions": ["activity"],
                        "basis": {"activity": "client_hook_observed"},
                        "authority": {"activity": "authoritative"},
                        "completeness": "complete",
                    }
                    for record_index in range(8)
                ],
            }
        )
    # Equal timestamps are ordered by stable group id, never input order.
    items[-2]["records"][0]["time"] = items[-1]["records"][0]["time"]  # type: ignore[index]
    items[-2]["group_id"] = "group-tie-a"
    items[-1]["group_id"] = "group-tie-b"
    product["work_evidence"] = {"item_count": len(items), "items": list(reversed(items))}
    product["summary"]["evidence_count"] = 10_000  # type: ignore[index]
    product["status"] = {
        "scope": "advanced_html_preview",
        "truncated": True,
        "limit": 10_000,
        "store_stats_included": False,
    }

    body = render_advanced_index_body(product)

    assert body.count('<details class="evidence-drawer">') == ADVANCED_WORK_EVIDENCE_LIMIT
    assert body.count(">Inspect record</a>") == (
        ADVANCED_WORK_EVIDENCE_LIMIT * ADVANCED_WORK_EVIDENCE_RECORD_LIMIT
    )
    assert body.count("Showing 5 of 8 evidence records.") == ADVANCED_WORK_EVIDENCE_LIMIT
    assert f"Showing {ADVANCED_WORK_EVIDENCE_LIMIT} of {len(items)} projected work matches." in body
    assert (
        "Preview capped at 10,000 latest evidence records; older indexed history remains available in the "
        "forensic inspectors."
    ) in body
    assert 'href="/evidence/events"' in body
    assert 'href="/evidence/product"' in body
    assert "Match 004" not in body
    assert "Match 005" in body
    assert body.index("Match 103") < body.index("Match 104")
    assert len(body.encode("utf-8")) < 500_000


def test_discrepancy_primary_table_uses_human_action_text_without_raw_ids() -> None:
    product = _product()
    item = product["discrepancies"]["items"][0]  # type: ignore[index]
    item["subject"] = {"namespace": "RAW_NAMESPACE_CANARY", "kind": "run_id", "id": "RAW_RUN_ID_CANARY"}
    item["candidates"] = [{"value": "RAW_VALUE_CANARY", "evidence_ids": ["RAW_EVIDENCE_ID_CANARY"]}]

    body = render_discrepancies_body(product)

    assert "Provider and estimate disagree" in body
    assert "Two cost records cover the same measured fact." in body
    assert "Verify the provider-billed record before acting." in body
    assert "Recommended next step" in body
    for canary in (
        "RAW_NAMESPACE_CANARY",
        "RAW_RUN_ID_CANARY",
        "RAW_VALUE_CANARY",
        "RAW_EVIDENCE_ID_CANARY",
    ):
        assert canary not in body


def test_existing_evidence_tables_announce_every_render_cap() -> None:
    product = _product()
    product["work_graph"] = {
        "node_count": 201,
        "edge_count": 301,
        "nodes": [
            {"node_id": f"node-{index}", "kind": "run_id", "value": f"run-{index}", "evidence_count": 1}
            for index in range(201)
        ],
        "edges": [
            {
                "from": f"node-{index}",
                "to": f"node-{index + 1}",
                "relation": "has_run",
                "assertion": "observed",
                "dimensions": ["activity"],
            }
            for index in range(301)
        ],
    }
    matrix_row = _product()["evidence_matrix"]["rows"][0]  # type: ignore[index]
    product["evidence_matrix"] = {"row_count": 501, "rows": [dict(matrix_row) for _ in range(501)]}
    discrepancy = _product()["discrepancies"]["items"][0]  # type: ignore[index]
    product["discrepancies"] = {"count": 501, "items": [dict(discrepancy) for _ in range(501)]}
    basis_record = _product()["cost_outcome_basis"]["records"][0]  # type: ignore[index]
    product["cost_outcome_basis"] = {"record_count": 501, "records": [dict(basis_record) for _ in range(501)]}

    graph = render_work_graph_body(product)
    assert "Showing 200 of 201 entity nodes." in graph
    assert "Showing 300 of 301 evidence-backed edges." in graph
    assert "Showing 500 of 501 source-dimension rows." in render_evidence_matrix_body(product)
    assert "Showing 500 of 501 discrepancies." in render_discrepancies_body(product)
    assert "Showing 500 of 501 basis records." in render_cost_outcome_basis_body(product)
