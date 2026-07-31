from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import agentacct.canonical.legacy_import as legacy_import_module
from agentacct.client_usage import ClientUsageEvent
from agentacct.canonical.legacy_import import (
    LegacyImportError,
    LegacyImporter,
    import_legacy_snapshot,
)
from agentacct.canonical.product_parity import (
    build_legacy_product_parity_report,
)
from agentacct.canonical.snapshot import VerifiedSnapshot
from agentacct.canonical.sqlite import CanonicalStore
from agentacct.usage_truth import mark_trusted_local_usage_import_event


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "canonical" / "v1" / "spike.json"


def _manifest_entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _verified_events_snapshot(
    tmp_path: Path,
    content: bytes,
    *,
    name: str = "legacy",
) -> VerifiedSnapshot:
    root = tmp_path / f"verified-{name}-snapshot"
    root.mkdir()
    (root / "events.jsonl").write_bytes(content)
    manifest = tmp_path / f"{name}-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [_manifest_entry("events.jsonl", content)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return VerifiedSnapshot.verify(root=root.resolve(), manifest=manifest.resolve())


def _session_event(
    *,
    session_id: str,
    namespace: str,
    observed_at: int,
    session_kind: str | None = "root",
    parent_session_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "client": "codex",
        "client_session_id": session_id,
        "source_namespace_fingerprint": namespace,
        "updated_at": observed_at,
    }
    if session_kind is not None:
        metadata["client_session_kind"] = session_kind
    if parent_session_id is not None:
        metadata["parent_client_session_id"] = parent_session_id
    return {
        "event_id": f"session-{session_id}-{observed_at}",
        "event_type": "session_observed",
        "source": "synthetic-legacy-import",
        "created_at": observed_at,
        "metadata": metadata,
    }


def _usage_event(
    fixture: dict[str, Any],
    usage_ref: str,
    *,
    parent_session_id: str | None = None,
) -> dict[str, Any]:
    usage = next(row for row in fixture["usage_observations"] if row["ref"] == usage_ref)
    session = next(row for row in fixture["sessions"] if row["ref"] == usage["session_ref"])
    source = next(
        row for row in fixture["source_instances"] if row["ref"] == usage["source_instance_ref"]
    )
    token_fields = usage["token_fields"]
    metadata: dict[str, Any] = {
        "client": source["client"],
        "client_session_id": session["client_session_id"],
        "client_session_kind": session["session_kind"],
        "source_namespace_fingerprint": source["namespace_digest"],
        "started_at": session["observed_at_ms"] / 1_000,
        "updated_at": session["observed_at_ms"] / 1_000,
        "usage_row_lane": usage["lane"],
        "usage_update_semantics": usage.get("update_semantics", "cumulative_snapshot"),
        "usage_granularity": usage.get("granularity", "session"),
        "precedence_role": usage["precedence_role"],
        "usage_additive": usage["totals_eligible_expected"],
        "held_reason": usage.get("held_reason"),
        "cached_input_tokens": token_fields["cached_input_tokens"],
        "cached_input_tokens_reported": token_fields["cached_input_tokens_reported"],
        "cache_creation_input_tokens": token_fields["cache_creation_tokens"],
        "cache_creation_tokens_reported": token_fields["cache_creation_tokens_reported"],
        "cache_read_input_tokens": token_fields["cache_read_tokens"],
        "cache_read_tokens_reported": token_fields["cache_read_tokens_reported"],
        "reasoning_output_tokens": token_fields["reasoning_tokens"],
        "reasoning_tokens_reported": token_fields["reasoning_tokens_reported"],
        "total_tokens": token_fields["total_tokens"],
        "total_tokens_reported": token_fields["total_tokens_reported"],
    }
    if parent_session_id is not None:
        metadata["parent_client_session_id"] = parent_session_id
    event: dict[str, Any] = {
        "event_id": f"event-{usage_ref}",
        "event_type": "model_usage",
        "source": "synthetic-legacy-import",
        "provider": usage.get("provider"),
        "model": usage.get("model"),
        "usage_confidence": usage.get("usage_confidence", "legacy_unknown"),
        "created_at": session["observed_at_ms"] / 1_000,
        "metadata": metadata,
    }
    if token_fields["input_tokens_reported"]:
        event["estimated_input_tokens"] = token_fields["input_tokens"]
    if token_fields["output_tokens_reported"]:
        event["estimated_output_tokens"] = token_fields["output_tokens"]
    return event


def _semantic_events(
    fixture: dict[str, Any],
    namespace: str,
    *,
    include_conflict: bool = True,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    original_event_id = fixture["semantic_ingest_attempts"][0]["source_event_id"]
    for attempt in fixture["semantic_ingest_attempts"]:
        if (
            not include_conflict
            and attempt["expected"]["disposition"] == "quarantined_conflict"
        ):
            continue
        normalized = attempt["normalized"]
        metadata = {
            "sentinel_semantic_kind": "section",
            "client": "codex",
            "client_session_id": "shared-session-001",
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "section_status": normalized["status"],
            "section_title": normalized["title"],
            "summary": normalized["summary"],
            "outcome_axis": normalized["outcome_axis"],
            "work_id": normalized["work_id"],
            "section_id": normalized["section_id"],
        }
        if attempt.get("supersedes_fact_ref"):
            metadata["supersedes_event_id"] = original_event_id
        events.append(
            {
                "event_id": attempt["source_event_id"],
                "event_type": f"section_{normalized['status']}",
                "source": "synthetic-legacy-import",
                "created_at": attempt["occurred_at_ms"] / 1_000,
                "metadata": metadata,
            }
        )
    return events


def _synthetic_legacy_payload(
    *,
    include_conflict: bool = True,
) -> tuple[dict[str, Any], bytes]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    alpha = fixture["source_instances"][0]["namespace_digest"]
    beta = fixture["source_instances"][2]["namespace_digest"]
    rows: list[bytes] = []
    rows.append(json.dumps(_usage_event(fixture, "usage_alpha_authoritative"), sort_keys=True).encode())
    rows.append(
        json.dumps(
            _usage_event(
                fixture,
                "usage_alpha_child_authoritative",
                parent_session_id="shared-session-001",
            ),
            sort_keys=True,
        ).encode()
    )
    # Same raw client session ID in another namespace must remain a distinct
    # session. Its partial usage preserves input=0 and output=NULL.
    rows.append(json.dumps(_usage_event(fixture, "usage_beta_partial"), sort_keys=True).encode())
    # An explicit cross-namespace parent reference is retained as a rejected
    # edge, never used to join the two Tasks.
    rows.append(
        json.dumps(
            {
                "event_id": "event-beta-cross-namespace-child",
                "event_type": "session_observed",
                "source": "synthetic-legacy-import",
                "created_at": 1_784_455_260,
                "metadata": {
                    "client": "codex",
                    "client_session_id": "beta-continuation-001",
                    "client_session_kind": "continuation",
                    "parent_client_session_id": "shared-session-001",
                    "source_namespace_fingerprint": beta,
                    "parent_source_namespace_fingerprint": alpha,
                    "started_at": 1_784_455_260,
                    "updated_at": 1_784_455_260,
                },
            },
            sort_keys=True,
        ).encode()
    )
    rows.extend(
        json.dumps(event, sort_keys=True).encode()
        for event in _semantic_events(
            fixture,
            alpha,
            include_conflict=include_conflict,
        )
    )
    rows.insert(2, b"{not-valid-json")
    return fixture, b"\n".join(rows) + b"\n"


def test_is_usage_event_anchors_on_model_usage_type() -> None:
    is_usage = legacy_import_module._is_usage_event

    # The one real usage type is caught.
    assert is_usage({"event_type": "model_usage"}) is True

    # The two former false-positive arms are gone:
    #  (a) any type whose name merely contains the substring "usage" — most
    #      importantly agent_usage_debug_reported, the MCP debug snapshot that
    #      is explicitly NOT billing truth.
    assert is_usage({"event_type": "agent_usage_debug_reported"}) is False
    assert is_usage({"event_type": "usage_note"}) is False
    #  (b) a non-usage event that merely CARRIES the estimated_* token keys with
    #      None values (the envelope ships them on task_completed, task_started,
    #      etc.).
    assert (
        is_usage(
            {
                "event_type": "task_completed",
                "estimated_input_tokens": None,
                "estimated_output_tokens": None,
            }
        )
        is False
    )
    # Even non-None token values do not override an explicit non-usage type.
    assert (
        is_usage(
            {
                "event_type": "task_completed",
                "estimated_input_tokens": 100,
                "estimated_output_tokens": 50,
            }
        )
        is False
    )

    # A pre-taxonomy row with NO event_type still falls back to a real token
    # VALUE (not a merely-present, possibly-None key).
    assert is_usage({"estimated_input_tokens": 5}) is True
    assert is_usage({"estimated_input_tokens": None, "estimated_output_tokens": None}) is False
    assert is_usage({}) is False


def test_verified_legacy_import_preserves_missingness_and_surfaces_conflict(
    tmp_path: Path,
) -> None:
    fixture, payload = _synthetic_legacy_payload()
    snapshot = _verified_events_snapshot(tmp_path, payload)
    source_digest_before = hashlib.sha256(snapshot.path_for("events.jsonl").read_bytes()).hexdigest()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        connection = store.connection

        assert report.lines_seen == 9
        assert report.parsed_events == 8
        assert report.malformed_or_excluded_lines == 2
        assert report.migration_issue_count == 2
        assert report.parity.matches is False
        assert report.parity.differences == {
            "unresolved_hard_conflicts": {"source": 1, "candidate": 0}
        }
        assert report.parity.exclusions_visible is True
        assert report.projection_rebuilt is True

        source = report.parity.source
        assert source.source_instance_count == 2
        assert source.session_count == 4
        assert source.task_anchor_count == 2
        assert source.accepted_session_edge_count == 1
        assert source.rejected_session_edge_count == 1
        assert source.semantic_fact_count == fixture["snapshot_parity_expectations"]["canonical_semantic_fact_count"]
        assert source.usage_measurement_count == 3
        assert source.totals_eligible_usage_measurement_count == 1
        assert source.held_usage_measurement_count == 2
        assert source.token_totals["input_tokens"] == 1_000
        assert source.token_totals["output_tokens"] == 0
        assert source.token_totals["total_tokens"] == 1_100
        assert source.token_missing_counts["output_tokens"] == 1
        assert source.token_zero_counts["input_tokens"] == 1
        assert source.token_zero_counts["output_tokens"] == 1
        assert source.token_zero_counts["cached_input_tokens"] == 1

        shared_sessions = connection.execute(
            "SELECT source_instance_id, session_id FROM sessions WHERE client_session_id = ? ORDER BY source_instance_id",
            (fixture["identity_assertions"]["same_client_session_id_across_namespaces"]["client_session_id"],),
        ).fetchall()
        assert len(shared_sessions) == fixture["identity_assertions"]["same_client_session_id_across_namespaces"]["expected_distinct_session_rows"]
        assert shared_sessions[0][0] != shared_sessions[1][0]
        assert connection.execute(
            "SELECT COUNT(*) FROM session_edges WHERE validation_state = 'rejected'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0] == 1
        alpha_task = connection.execute(
            "SELECT task.input_tokens, task.output_tokens, task.total_tokens, "
            "task.usage_measurement_count FROM rm_task_current task "
            "WHERE task.usage_measurement_count = 2"
        ).fetchone()
        assert tuple(alpha_task) == (1_000, 0, 1_100, 2)
        beta_task = connection.execute(
            "SELECT task.input_tokens, task.output_tokens, task.total_tokens "
            "FROM rm_task_current task WHERE task.usage_measurement_count = 1"
        ).fetchone()
        assert tuple(beta_task) == (0, None, None)
        held_child_day = connection.execute(
            "SELECT held_measurement_count, input_tokens, input_reported_count, "
            "input_missing_count, output_tokens, output_reported_count, "
            "output_missing_count, total_tokens, total_reported_count, "
            "total_missing_count FROM rm_usage_day "
            "WHERE usage_basis = 'authoritative_held_non_additive' "
            "AND model = 'synthetic-model-a'"
        ).fetchone()
        assert tuple(held_child_day) == (
            1,
            None,
            1,
            0,
            None,
            1,
            0,
            None,
            1,
            0,
        )
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT reason, disposition FROM migration_issues"
            )
        } == {
            ("malformed_jsonl", "invalid"),
            ("semantic_fact_conflict", "quarantined"),
        }
        assert store.quick_check()["ok"] is True
        assert hashlib.sha256(snapshot.path_for("events.jsonl").read_bytes()).hexdigest() == source_digest_before
    finally:
        store.close()


def test_exact_rerun_is_noop_and_persistent_task_ids_do_not_churn(tmp_path: Path) -> None:
    _fixture, payload = _synthetic_legacy_payload(include_conflict=False)
    snapshot = _verified_events_snapshot(tmp_path, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        importer = LegacyImporter(snapshot=snapshot, store=store, scratch_root=scratch.resolve())
        first = importer.import_events()
        task_ids_before = tuple(
            row[0] for row in store.connection.execute("SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id")
        )
        counts_before = dict(store.repository().table_counts())
        sequence_before = store.repository().canonical_sequence()

        second = importer.import_events()

        assert first.parity.matches is True
        assert second.parity.matches is True
        assert second.projection_rebuilt is False
        assert second.canonical_sequence_before == sequence_before
        assert second.canonical_sequence_after == sequence_before
        assert second.write_dispositions["sessions"] == {"noop": 4}
        assert second.write_dispositions["session_edges"] == {"noop": 2}
        assert second.write_dispositions["usage"] == {"noop": 3}
        assert second.write_dispositions["facts"] == {"noop": 3}
        assert dict(store.repository().table_counts()) == counts_before
        assert tuple(
            row[0] for row in store.connection.execute("SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id")
        ) == task_ids_before
        assert all(task_id.startswith("task_") and len(task_id) == 37 for task_id in task_ids_before)
    finally:
        store.close()


def test_real_reader_semantics_map_to_cumulative_and_preflag_codex_presence_is_visible(
    tmp_path: Path,
) -> None:
    namespace = "d1" * 32

    def usage_row(
        *,
        client: str,
        session_id: str,
        source: str,
        semantics: str,
        flagged: bool,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "client": client,
            "client_session_id": session_id,
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "usage_source": "local_client_session_store",
            "usage_update_semantics": semantics,
            "usage_additive": True,
            "reasoning_output_tokens": 0,
        }
        if flagged:
            metadata.update(
                {
                    "input_tokens_reported": True,
                    "output_tokens_reported": True,
                    "reasoning_output_tokens_reported": True,
                }
            )
        return {
            "event_id": f"usage-{session_id}",
            "event_type": "model_usage",
            "source": source,
            "provider": "test-provider",
            "model": "test-model",
            "estimated_input_tokens": 10,
            "estimated_output_tokens": 0,
            "created_at": 1_750_000_000,
            "metadata": metadata,
        }

    rows = [
        usage_row(
            client="codex",
            session_id="old-codex",
            source="codex-local-session-import",
            semantics="codex_rollout_token_count_events",
            flagged=False,
        ),
        usage_row(
            client="codex",
            session_id="flagged-codex",
            source="codex-local-session-import",
            semantics="codex_rollout_lineage_delta_v1",
            flagged=True,
        ),
        usage_row(
            client="claude-code",
            session_id="legacy-claude",
            source="claude-code-local-session-import",
            semantics="claude_assistant_message_usage_rows",
            flagged=False,
        ),
    ]
    payload = b"\n".join(json.dumps(row, sort_keys=True).encode() for row in rows) + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload, name="real-reader-semantics")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        imported = {
            str(row[0]): tuple(row[1:])
            for row in store.connection.execute(
                "SELECT session.client_session_id, usage.update_semantics, "
                "usage.input_tokens, usage.input_tokens_reported, "
                "usage.output_tokens, usage.output_tokens_reported, "
                "usage.reasoning_output_tokens, usage.reasoning_output_tokens_reported "
                "FROM usage_measurements usage "
                "JOIN sessions session ON session.session_id = usage.session_id"
            )
        }
        assert imported["old-codex"] == (
            "cumulative_snapshot",
            None,
            0,
            None,
            0,
            None,
            0,
        )
        assert imported["flagged-codex"] == (
            "cumulative_snapshot",
            10,
            1,
            0,
            1,
            0,
            1,
        )
        assert imported["legacy-claude"] == (
            "cumulative_snapshot",
            10,
            1,
            0,
            1,
            0,
            1,
        )
        assert report.parity.matches is True
        assert report.parity.exclusions_visible is True
        assert report.migration_issue_count == 3
        assert report.issue_lines == 1
        assert report.excluded_lines == 0
        assert report.processed_with_issues_lines == 1
        assert {
            tuple(row)
            for row in store.connection.execute(
                "SELECT reason, disposition FROM migration_issues"
            )
        } == {
            (
                "legacy_codex_input_tokens_presence_unavailable",
                "requires_choice",
            ),
            (
                "legacy_codex_output_tokens_presence_unavailable",
                "requires_choice",
            ),
            (
                "legacy_codex_reasoning_output_tokens_presence_unavailable",
                "requires_choice",
            ),
        }
    finally:
        store.close()


def test_legacy_claude_model_suffix_uses_base_session_and_existing_product_lane(
    tmp_path: Path,
) -> None:
    base_session = "claude-session"
    model = "claude-sonnet-4"
    event = {
        "event_id": "legacy-claude-model-row",
        "event_type": "model_usage",
        "source": "claude-code-local-session-import",
        "provider": "anthropic",
        "model": model,
        "estimated_input_tokens": 12,
        "estimated_output_tokens": 3,
        "created_at": 1_750_000_000,
        "metadata": {
            "client": "claude-code",
            "client_session_id": f"{base_session}:model:{model}",
            "client_session_kind": "root",
            "source_namespace_fingerprint": "e4" * 32,
            "usage_source": "local_client_session_store",
            "usage_update_semantics": "claude_assistant_message_usage_rows",
            "usage_additive": True,
            # Historical rows derived this lane from the suffixed identity.
            # It was not always persisted explicitly.
        },
    }
    event = mark_trusted_local_usage_import_event(event)
    payload = json.dumps(event, sort_keys=True).encode() + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload, name="claude-model-suffix")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert report.parity.matches is True
        assert tuple(
            store.connection.execute(
                "SELECT client_session_id FROM sessions"
            ).fetchone()
        ) == (base_session,)
        assert tuple(
            store.connection.execute(
                "SELECT lane FROM usage_measurements"
            ).fetchone()
        ) == (f"model:{model}",)

        task_ids = tuple(
            str(row[0])
            for row in store.connection.execute(
                "SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id"
            )
        )
        counts = dict(store.repository().table_counts())
        sequence = store.repository().canonical_sequence()
        changes = store.connection.total_changes
        rerun = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        product_report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=rerun,
            legacy_store_scope="custom",
            rerun_evidence={
                "canonical_writes": store.connection.total_changes - changes,
                "canonical_sequence_delta": (
                    store.repository().canonical_sequence() - sequence
                ),
                "task_ids_stable": tuple(
                    str(row[0])
                    for row in store.connection.execute(
                        "SELECT public_task_id FROM task_anchors "
                        "ORDER BY task_anchor_id"
                    )
                )
                == task_ids,
                "opaque_task_ids_valid": all(
                    value.startswith("task_") and len(value) == 37
                    for value in task_ids
                ),
                "table_counts_stable": (
                    dict(store.repository().table_counts()) == counts
                ),
                "projection_rebuilt": rerun.projection_rebuilt,
                "second_import": {
                    "write_dispositions": rerun.write_dispositions,
                    "internal_parity_matches": rerun.parity.matches,
                    "internal_parity_difference_keys": sorted(
                        rerun.parity.differences
                    ),
                },
            },
        )
        mismatched_surfaces = [
            comparison["surface"]
            for comparison in product_report["comparisons"]
            if comparison["matches"] is not True
        ]
        assert mismatched_surfaces == []
        assert product_report["acceptance"]["core_truth_slice_passed"] is True
    finally:
        store.close()


@pytest.mark.parametrize(
    "column, corrupted_value",
    [
        ("provider", "corrupted-provider"),
        ("granularity", "turn"),
        ("usage_confidence", "corrupted-confidence"),
        ("client_reported_cost_microusd", 123),
        ("source_order", 9_000_000_000_000_000_000),
    ],
)
def test_parity_fingerprint_detects_candidate_usage_corruption(
    tmp_path: Path,
    column: str,
    corrupted_value: str | int,
) -> None:
    _fixture, payload = _synthetic_legacy_payload(include_conflict=False)
    snapshot = _verified_events_snapshot(tmp_path, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        importer = LegacyImporter(snapshot=snapshot, store=store, scratch_root=scratch.resolve())
        assert importer.import_events().parity.matches is True
        store.connection.execute(
            f"UPDATE usage_measurements SET {column} = ? "
            "WHERE usage_measurement_id = "
            "(SELECT MIN(usage_measurement_id) FROM usage_measurements)",
            (corrupted_value,),
        )

        report = importer.import_events()

        assert report.parity.matches is False
        assert "usage_row_fingerprints" in report.parity.differences
    finally:
        store.close()


def test_events_without_source_namespace_are_excluded_not_merged(tmp_path: Path) -> None:
    rows = []
    for client in ("codex", "claude"):
        rows.append(
            json.dumps(
                {
                    "event_id": f"{client}-event",
                    "event_type": "session_observed",
                    "source": "synthetic-legacy-import",
                    "metadata": {
                        "client": client,
                        "client_session_id": "same-raw-session-id",
                    },
                },
                sort_keys=True,
            ).encode()
        )
    snapshot = _verified_events_snapshot(tmp_path, b"\n".join(rows) + b"\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert report.lines_seen == 2
        assert report.parsed_events == 0
        assert report.malformed_or_excluded_lines == 2
        assert report.migration_issue_count == 2
        assert report.parity.matches is True
        assert report.parity.exclusions_visible is True
        assert store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert {
            row[0]
            for row in store.connection.execute("SELECT reason FROM migration_issues")
        } == {"unresolved_source_namespace"}
    finally:
        store.close()


def test_importer_rejects_unverified_or_out_of_scratch_inputs(tmp_path: Path) -> None:
    _fixture, payload = _synthetic_legacy_payload()
    snapshot = _verified_events_snapshot(tmp_path, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    store = CanonicalStore.create((outside / "candidate.sqlite3").resolve())
    try:
        with pytest.raises(LegacyImportError, match="strictly under"):
            LegacyImporter(snapshot=snapshot, store=store, scratch_root=scratch.resolve())
        with pytest.raises(LegacyImportError, match="VerifiedSnapshot"):
            LegacyImporter(snapshot=object(), store=store, scratch_root=outside.resolve())  # type: ignore[arg-type]
    finally:
        store.close()


def test_importer_rejects_candidate_replaced_before_construction(
    tmp_path: Path,
) -> None:
    _fixture, payload = _synthetic_legacy_payload(include_conflict=False)
    snapshot = _verified_events_snapshot(
        tmp_path,
        payload,
        name="candidate-replaced-before-construction",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    candidate = (scratch / "candidate.sqlite3").resolve()
    replacement_path = (scratch / "replacement.sqlite3").resolve()
    CanonicalStore.create(replacement_path).close()
    store = CanonicalStore.create(candidate)
    displaced = scratch / "displaced-candidate.sqlite3"
    candidate.replace(displaced)
    replacement_path.replace(candidate)
    try:
        with pytest.raises(LegacyImportError, match="identity changed"):
            LegacyImporter(
                snapshot=snapshot,
                store=store,
                scratch_root=scratch.resolve(),
            )
    finally:
        store.close()


def test_importer_rejects_candidate_replaced_before_final_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture, payload = _synthetic_legacy_payload(include_conflict=False)
    snapshot = _verified_events_snapshot(
        tmp_path,
        payload,
        name="candidate-replaced-before-final-report",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    candidate = (scratch / "candidate.sqlite3").resolve()
    replacement_path = (scratch / "replacement.sqlite3").resolve()
    CanonicalStore.create(replacement_path).close()
    store = CanonicalStore.create(candidate)
    importer = LegacyImporter(
        snapshot=snapshot,
        store=store,
        scratch_root=scratch.resolve(),
    )
    displaced = scratch / "displaced-candidate.sqlite3"
    real_verify = importer._verify_candidate_identity
    verification_calls = 0

    def replace_before_final_verification() -> None:
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 2:
            candidate.replace(displaced)
            replacement_path.replace(candidate)
        real_verify()

    monkeypatch.setattr(
        importer,
        "_verify_candidate_identity",
        replace_before_final_verification,
    )
    try:
        with pytest.raises(LegacyImportError, match="identity changed"):
            importer.import_events()
    finally:
        store.close()


def test_parent_without_kind_never_anchors_task_and_collapses_prior_root(
    tmp_path: Path,
) -> None:
    namespace = "ab" * 32
    initial_rows = [
        _session_event(
            session_id="parent-session",
            namespace=namespace,
            observed_at=1,
        ),
        _session_event(
            session_id="later-child",
            namespace=namespace,
            observed_at=1,
        ),
    ]
    initial_payload = b"\n".join(
        json.dumps(row, sort_keys=True).encode() for row in initial_rows
    ) + b"\n"
    initial_snapshot = _verified_events_snapshot(
        tmp_path,
        initial_payload,
        name="initial-root-classification",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        first = import_legacy_snapshot(
            snapshot=initial_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        assert first.parity.matches is True
        initial_task_ids = {
            str(row[0]): str(row[1])
            for row in store.connection.execute(
                "SELECT session.client_session_id, anchor.public_task_id "
                "FROM task_anchors anchor "
                "JOIN sessions session ON session.session_id = anchor.primary_session_id"
            )
        }
        assert set(initial_task_ids) == {"parent-session", "later-child"}

        corrected_rows = [
            _session_event(
                session_id="parent-session",
                namespace=namespace,
                observed_at=2,
            ),
            _session_event(
                session_id="later-child",
                namespace=namespace,
                observed_at=2,
                session_kind=None,
                parent_session_id="parent-session",
            ),
        ]
        corrected_payload = b"\n".join(
            json.dumps(row, sort_keys=True).encode() for row in corrected_rows
        ) + b"\n"
        corrected_snapshot = _verified_events_snapshot(
            tmp_path,
            corrected_payload,
            name="corrected-child-classification",
        )

        corrected = import_legacy_snapshot(
            snapshot=corrected_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert corrected.parity.matches is True
        assert corrected.parity.source.task_anchor_count == 1
        assert corrected.parity.candidate.task_anchor_count == 1
        assert corrected.malformed_or_excluded_lines == 1
        assert corrected.migration_issue_count == 1
        assert corrected.write_dispositions["task_anchors"] == {"removed": 1}
        remaining = store.connection.execute(
            "SELECT session.client_session_id, anchor.public_task_id "
            "FROM task_anchors anchor "
            "JOIN sessions session ON session.session_id = anchor.primary_session_id"
        ).fetchall()
        assert [tuple(row) for row in remaining] == [
            ("parent-session", initial_task_ids["parent-session"])
        ]
        child = store.connection.execute(
            "SELECT session_kind FROM sessions WHERE client_session_id = 'later-child'"
        ).fetchone()
        assert child is not None and child[0] == "unknown"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM session_edges WHERE validation_state = 'valid'"
        ).fetchone()[0] == 1
        task_projection = store.connection.execute(
            "SELECT session_count FROM rm_task_current"
        ).fetchone()
        assert task_projection is not None and task_projection[0] == 2
        assert {
            tuple(row)
            for row in store.connection.execute(
                "SELECT reason, disposition FROM migration_issues"
            )
        } == {
            (
                "task_anchor_removed_after_parent_reclassification",
                "quarantined",
            )
        }
    finally:
        store.close()


def test_missing_parent_reclassification_retains_established_task_anchor(
    tmp_path: Path,
) -> None:
    namespace = "ac" * 32
    initial_payload = (
        json.dumps(
            _session_event(
                session_id="standalone-child",
                namespace=namespace,
                observed_at=1,
            ),
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    initial_snapshot = _verified_events_snapshot(
        tmp_path,
        initial_payload,
        name="missing-parent-initial",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        first = import_legacy_snapshot(
            snapshot=initial_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        assert first.parity.matches is True
        original_task_id = str(
            store.connection.execute(
                "SELECT public_task_id FROM task_anchors"
            ).fetchone()[0]
        )

        corrected_payload = (
            json.dumps(
                _session_event(
                    session_id="standalone-child",
                    namespace=namespace,
                    observed_at=2,
                    session_kind=None,
                    parent_session_id="absent-parent",
                ),
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        corrected_snapshot = _verified_events_snapshot(
            tmp_path,
            corrected_payload,
            name="missing-parent-corrected",
        )

        corrected = import_legacy_snapshot(
            snapshot=corrected_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert corrected.parity.matches is False
        assert corrected.parity.differences == {
            "task_anchor_count": {"source": 0, "candidate": 1}
        }
        assert corrected.parity.exclusions_visible is True
        assert corrected.migration_issue_count == 1
        assert corrected.write_dispositions.get("task_anchors") is None
        assert store.connection.execute(
            "SELECT public_task_id FROM task_anchors"
        ).fetchone()[0] == original_task_id
        assert store.connection.execute(
            "SELECT COUNT(*) FROM session_edges"
        ).fetchone()[0] == 0
        assert tuple(
            store.connection.execute(
                "SELECT reason, disposition FROM migration_issues"
            ).fetchone()
        ) == ("parent_session_not_in_snapshot", "requires_choice")
    finally:
        store.close()


def test_rejected_parent_edge_reclassification_retains_established_task_anchor(
    tmp_path: Path,
) -> None:
    parent_namespace = "ad" * 32
    child_namespace = "ae" * 32
    initial_rows = [
        _session_event(
            session_id="cross-parent",
            namespace=parent_namespace,
            observed_at=1,
        ),
        _session_event(
            session_id="cross-child",
            namespace=child_namespace,
            observed_at=1,
        ),
    ]
    initial_payload = b"\n".join(
        json.dumps(row, sort_keys=True).encode() for row in initial_rows
    ) + b"\n"
    initial_snapshot = _verified_events_snapshot(
        tmp_path,
        initial_payload,
        name="rejected-edge-initial",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        first = import_legacy_snapshot(
            snapshot=initial_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        assert first.parity.matches is True
        original_task_ids = {
            str(row[0]): str(row[1])
            for row in store.connection.execute(
                "SELECT session.client_session_id, anchor.public_task_id "
                "FROM task_anchors anchor "
                "JOIN sessions session ON session.session_id = anchor.primary_session_id"
            )
        }

        child = _session_event(
            session_id="cross-child",
            namespace=child_namespace,
            observed_at=2,
            session_kind=None,
            parent_session_id="cross-parent",
        )
        child["metadata"]["parent_source_namespace_fingerprint"] = parent_namespace
        corrected_rows = [
            _session_event(
                session_id="cross-parent",
                namespace=parent_namespace,
                observed_at=2,
            ),
            child,
        ]
        corrected_payload = b"\n".join(
            json.dumps(row, sort_keys=True).encode() for row in corrected_rows
        ) + b"\n"
        corrected_snapshot = _verified_events_snapshot(
            tmp_path,
            corrected_payload,
            name="rejected-edge-corrected",
        )

        corrected = import_legacy_snapshot(
            snapshot=corrected_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert corrected.parity.matches is False
        assert corrected.parity.differences == {
            "task_anchor_count": {"source": 1, "candidate": 2}
        }
        assert corrected.parity.source.rejected_session_edge_count == 1
        assert corrected.parity.candidate.rejected_session_edge_count == 1
        assert corrected.write_dispositions.get("task_anchors") is None
        retained_task_ids = {
            str(row[0]): str(row[1])
            for row in store.connection.execute(
                "SELECT session.client_session_id, anchor.public_task_id "
                "FROM task_anchors anchor "
                "JOIN sessions session ON session.session_id = anchor.primary_session_id"
            )
        }
        assert retained_task_ids == original_task_ids
        edge = store.connection.execute(
            "SELECT validation_state, veto_reason FROM session_edges"
        ).fetchone()
        assert edge is not None
        assert tuple(edge) == ("rejected", "incompatible_source_namespace")
    finally:
        store.close()


def test_stale_divergent_parent_observation_cannot_create_lineage_or_retire_anchor(
    tmp_path: Path,
) -> None:
    namespace = "af" * 32
    initial_rows = [
        _session_event(
            session_id="stale-parent",
            namespace=namespace,
            observed_at=100,
        ),
        _session_event(
            session_id="stale-child",
            namespace=namespace,
            observed_at=100,
        ),
    ]
    initial_payload = b"\n".join(
        json.dumps(row, sort_keys=True).encode() for row in initial_rows
    ) + b"\n"
    initial_snapshot = _verified_events_snapshot(
        tmp_path,
        initial_payload,
        name="stale-lineage-initial",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        first = import_legacy_snapshot(
            snapshot=initial_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        assert first.parity.matches is True
        original_task_ids = tuple(
            row[0]
            for row in store.connection.execute(
                "SELECT public_task_id FROM task_anchors ORDER BY public_task_id"
            )
        )

        stale_rows = [
            _session_event(
                session_id="stale-parent",
                namespace=namespace,
                observed_at=50,
            ),
            _session_event(
                session_id="stale-child",
                namespace=namespace,
                observed_at=50,
                session_kind=None,
                parent_session_id="stale-parent",
            ),
        ]
        stale_payload = b"\n".join(
            json.dumps(row, sort_keys=True).encode() for row in stale_rows
        ) + b"\n"
        stale_snapshot = _verified_events_snapshot(
            tmp_path,
            stale_payload,
            name="stale-lineage-corrected",
        )

        stale = import_legacy_snapshot(
            snapshot=stale_snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert stale.write_dispositions["sessions"] == {"conflict": 2}
        assert stale.write_dispositions.get("session_edges") is None
        assert stale.write_dispositions.get("task_anchors") is None
        assert stale.parity.matches is False
        assert stale.parity.differences == {
            "task_anchor_count": {"source": 1, "candidate": 2}
        }
        assert store.connection.execute(
            "SELECT COUNT(*) FROM session_edges"
        ).fetchone()[0] == 0
        assert tuple(
            row[0]
            for row in store.connection.execute(
                "SELECT public_task_id FROM task_anchors ORDER BY public_task_id"
            )
        ) == original_task_ids
        assert store.connection.execute(
            "SELECT COUNT(*) FROM source_conflicts "
            "WHERE native_entity_kind = 'session' "
            "AND reason = 'unordered_content_change'"
        ).fetchone()[0] == 2
        assert {
            tuple(row)
            for row in store.connection.execute(
                "SELECT client_session_id, session_kind FROM sessions"
            )
        } == {
            ("stale-parent", "root"),
            ("stale-child", "root"),
        }
    finally:
        store.close()


def test_explicit_internal_session_kind_is_preserved(tmp_path: Path) -> None:
    namespace = "ca" * 32
    rows = [
        _session_event(
            session_id="internal-parent",
            namespace=namespace,
            observed_at=1,
        ),
        _session_event(
            session_id="internal-review",
            namespace=namespace,
            observed_at=2,
            session_kind="internal",
            parent_session_id="internal-parent",
        ),
    ]
    payload = b"\n".join(
        json.dumps(row, sort_keys=True).encode() for row in rows
    ) + b"\n"
    snapshot = _verified_events_snapshot(
        tmp_path,
        payload,
        name="explicit-internal-kind",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)

    with CanonicalStore.create((scratch / "candidate.sqlite3").resolve()) as store:
        result = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert result.parity.matches is True
        assert {
            tuple(row)
            for row in store.connection.execute(
                "SELECT client_session_id, session_kind FROM sessions"
            )
        } == {
            ("internal-parent", "root"),
            ("internal-review", "internal"),
        }
        assert store.connection.execute(
            "SELECT COUNT(*) FROM task_anchors"
        ).fetchone()[0] == 1


def test_issue_line_metric_counts_distinct_lines_not_issue_triples(tmp_path: Path) -> None:
    event = _session_event(
        session_id="invalid-token-fields",
        namespace="bc" * 32,
        observed_at=1,
    )
    event.update(
        {
            "event_type": "model_usage",
            "estimated_input_tokens": -1,
            "estimated_output_tokens": "not-an-integer",
        }
    )
    event["metadata"].update(
        {
            "usage_additive": True,
            "usage_row_lane": "invalid-token-fields",
            "usage_update_semantics": "cumulative_snapshot",
        }
    )
    payload = json.dumps(event, sort_keys=True).encode() + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert report.malformed_or_excluded_lines == 1
        assert report.issue_lines == 1
        assert report.excluded_lines == 0
        assert report.processed_with_issues_lines == 1
        assert report.migration_issue_count == 2
        assert report.parity.matches is True
        assert store.connection.execute(
            "SELECT COUNT(*) FROM usage_measurements"
        ).fetchone()[0] == 1
        assert {
            row[0]
            for row in store.connection.execute("SELECT reason FROM migration_issues")
        } == {"invalid_input_tokens", "invalid_output_tokens"}
    finally:
        store.close()


def test_split_cache_flags_control_combined_presence_and_explicit_zero(
    tmp_path: Path,
) -> None:
    rows: list[dict[str, Any]] = []
    for observed_at, session_id, reported in (
        (1, "cache-unavailable", False),
        (2, "cache-explicit-zero", True),
    ):
        event = _session_event(
            session_id=session_id,
            namespace="c1" * 32,
            observed_at=observed_at,
        )
        event.update(
            {
                "event_type": "model_usage",
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
            }
        )
        event["metadata"].update(
            {
                "usage_additive": True,
                "usage_update_semantics": "cumulative_snapshot",
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_tokens_reported": reported,
                "cache_read_tokens_reported": reported,
            }
        )
        rows.append(event)
    payload = b"\n".join(json.dumps(row, sort_keys=True).encode() for row in rows) + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload, name="cache-presence")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        imported = {
            str(row[0]): tuple(row[1:])
            for row in store.connection.execute(
                "SELECT session.client_session_id, usage.cached_input_tokens, "
                "usage.cached_input_tokens_reported, "
                "usage.cache_creation_input_tokens, "
                "usage.cache_creation_input_tokens_reported, "
                "usage.cache_read_input_tokens, "
                "usage.cache_read_input_tokens_reported "
                "FROM usage_measurements usage JOIN sessions session "
                "ON session.session_id = usage.session_id"
            )
        }
        assert imported == {
            "cache-unavailable": (None, 0, None, 0, None, 0),
            "cache-explicit-zero": (0, 1, 0, 1, 0, 1),
        }
        assert report.migration_issue_count == 0
        assert report.parity.matches is True
    finally:
        store.close()


def test_codex_sqlite_fallback_retains_non_authoritative_basis(
    tmp_path: Path,
) -> None:
    event = _session_event(
        session_id="codex-sqlite-fallback",
        namespace="c2" * 32,
        observed_at=1,
    )
    event.update(
        {
            "event_type": "model_usage",
            "source": "codex-local-session-import",
            "estimated_input_tokens": 42,
            "estimated_output_tokens": 0,
        }
    )
    event["metadata"].update(
        {
            "usage_source": "local_client_session_store",
            "usage_additive": True,
            "usage_update_semantics": "codex_sqlite_tokens_used_fallback",
            "usage_representation": "codex-sqlite-tokens-used-fallback-v1",
            "precedence_role": "fallback",
            "input_tokens_reported": False,
            "output_tokens_reported": False,
            "reasoning_output_tokens": 0,
            "reasoning_output_tokens_reported": False,
            "total_tokens": 42,
            "total_tokens_reported": True,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_tokens_reported": False,
            "cache_read_tokens_reported": False,
        }
    )
    payload = json.dumps(event, sort_keys=True).encode() + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload, name="codex-fallback")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        row = store.connection.execute(
            "SELECT representation, update_semantics, precedence_role, "
            "input_tokens, input_tokens_reported, cached_input_tokens, "
            "cached_input_tokens_reported, total_tokens, total_tokens_reported "
            "FROM usage_measurements"
        ).fetchone()
        assert row is not None
        assert tuple(row) == (
            "codex-sqlite-tokens-used-fallback-v1",
            "cumulative_snapshot",
            "fallback",
            None,
            0,
            None,
            0,
            42,
            1,
        )
        assert report.migration_issue_count == 0
        assert report.parity.matches is True
    finally:
        store.close()


def test_codex_unproven_normalized_input_imports_as_visible_missingness(
    tmp_path: Path,
) -> None:
    reader_event = ClientUsageEvent(
        client="codex",
        client_session_id="codex-missing-cache-read",
        source_path=tmp_path / "synthetic-rollout.jsonl",
        title=None,
        cwd="/synthetic/project",
        model="gpt-5.5",
        # Compatibility numerics remain available to legacy product readers,
        # but input is not canonical truth without the applicable cache split.
        input_tokens=2_500,
        input_tokens_reported=False,
        output_tokens=125,
        output_tokens_reported=True,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=False,
        cache_read_tokens_reported=False,
        reasoning_output_tokens=22,
        reasoning_output_tokens_reported=True,
        total_tokens=2_625,
        total_tokens_reported=True,
        started_at=100,
        updated_at=200,
        source_namespace_fingerprint="c4" * 32,
    )
    event = reader_event.to_sentinel_event()
    event.update(
        {
            "event_id": "codex-missing-cache-read-usage",
            "created_at": 200,
        }
    )
    payload = json.dumps(event, sort_keys=True).encode() + b"\n"
    snapshot = _verified_events_snapshot(
        tmp_path,
        payload,
        name="codex-missing-cache-read",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        row = store.connection.execute(
            "SELECT input_tokens, input_tokens_reported, "
            "output_tokens, output_tokens_reported, "
            "cached_input_tokens, cached_input_tokens_reported, "
            "cache_creation_input_tokens, cache_creation_input_tokens_reported, "
            "cache_read_input_tokens, cache_read_input_tokens_reported, "
            "reasoning_output_tokens, reasoning_output_tokens_reported, "
            "total_tokens, total_tokens_reported "
            "FROM usage_measurements"
        ).fetchone()
        assert row is not None
        assert tuple(row) == (
            None,
            0,
            125,
            1,
            None,
            0,
            None,
            0,
            None,
            0,
            22,
            1,
            2_625,
            1,
        )
        assert report.migration_issue_count == 0
        assert report.parity.matches is True
    finally:
        store.close()


def test_sqlite_int64_overflow_is_visible_missingness_not_binder_failure(
    tmp_path: Path,
) -> None:
    event = _session_event(
        session_id="overflow-values",
        namespace="c3" * 32,
        observed_at=1,
    )
    event.update(
        {
            "event_type": "model_usage",
            "created_at": 2**63,
            "estimated_input_tokens": 2**63,
            "estimated_output_tokens": 0,
        }
    )
    event["metadata"].update(
        {
            "usage_additive": True,
            "usage_update_semantics": "cumulative_snapshot",
            "client_reported_cost_usd": 10**20,
        }
    )
    payload = json.dumps(event, sort_keys=True).encode() + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload, name="overflow-values")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        row = store.connection.execute(
            "SELECT input_tokens, input_tokens_reported, "
            "client_reported_cost_microusd, observed_at_us, updated_at_us "
            "FROM usage_measurements"
        ).fetchone()
        assert row is not None
        assert tuple(row) == (None, 0, None, 0, 1_000_000)
        assert report.parity.matches is True
        assert report.parity.exclusions_visible is True
        assert report.migration_issue_count == 3
        assert {
            str(row[0])
            for row in store.connection.execute(
                "SELECT reason FROM migration_issues"
            )
        } == {
            "invalid_client_reported_cost",
            "invalid_input_tokens",
            "invalid_timestamp",
        }
    finally:
        store.close()


def test_invalid_usage_semantics_are_visible_and_never_imported(tmp_path: Path) -> None:
    rows = []
    for observed_at, lane, semantics in (
        (1, "invalid-semantics", "rolling_total"),
        (2, "valid-cumulative", "cumulative_snapshot"),
    ):
        event = _session_event(
            session_id="usage-session",
            namespace="cd" * 32,
            observed_at=observed_at,
        )
        event.update(
            {
                "event_type": "model_usage",
                "estimated_input_tokens": observed_at,
                "estimated_output_tokens": observed_at,
            }
        )
        event["metadata"].update(
            {
                "usage_additive": True,
                "usage_row_lane": lane,
                "usage_update_semantics": semantics,
            }
        )
        rows.append(event)
    payload = b"\n".join(json.dumps(row, sort_keys=True).encode() for row in rows) + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert report.malformed_or_excluded_lines == 1
        assert report.migration_issue_count == 1
        assert report.parity.matches is True
        assert report.parity.source.usage_measurement_count == 1
        assert [
            row[0]
            for row in store.connection.execute(
                "SELECT lane FROM usage_measurements ORDER BY usage_measurement_id"
            )
        ] == ["valid-cumulative"]
        issue = store.connection.execute(
            "SELECT reason, disposition FROM migration_issues"
        ).fetchone()
        assert issue is not None
        assert tuple(issue) == ("invalid_usage_update_semantics", "excluded")
    finally:
        store.close()


def test_oversized_jsonl_line_is_bounded_and_next_record_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = json.dumps(
        _session_event(
            session_id="after-oversized-line",
            namespace="de" * 32,
            observed_at=1,
        ),
        sort_keys=True,
    ).encode()
    line_limit = len(valid) + 1
    monkeypatch.setattr(legacy_import_module, "MAX_JSONL_LINE_BYTES", line_limit)
    oversized = b'{"padding":"' + (b"x" * (line_limit * 3)) + b'"}'
    snapshot = _verified_events_snapshot(
        tmp_path,
        oversized + b"\n" + valid + b"\n",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        assert report.lines_seen == 2
        assert report.parsed_events == 1
        assert report.malformed_or_excluded_lines == 1
        assert report.migration_issue_count == 1
        assert report.parity.matches is True
        assert store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM task_anchors").fetchone()[0] == 1
        issue = store.connection.execute(
            "SELECT reason, disposition FROM migration_issues"
        ).fetchone()
        assert issue is not None
        assert tuple(issue) == ("jsonl_line_too_large", "invalid")
    finally:
        store.close()


def test_missing_event_id_with_float_metadata_imports_deterministically(
    tmp_path: Path,
) -> None:
    event = _session_event(
        session_id="float-meta",
        namespace="d7" * 32,
        observed_at=1_750_000_100,
    )
    event["event_type"] = "section_started"
    del event["event_id"]
    event["metadata"].update(
        {"section_id": "s1", "section_status": "started", "duration_s": 1.5}
    )
    payload = json.dumps(event, sort_keys=True).encode() + b"\n"
    snapshot = _verified_events_snapshot(tmp_path, payload, name="float-meta")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        row = store.connection.execute(
            "SELECT source_event_id FROM facts WHERE fact_type = 'work_claim'"
        ).fetchone()
        assert row is not None
        assert str(row[0]).startswith("legacy:")
    finally:
        store.close()


def test_absent_timestamps_record_visible_issue_with_epoch_sentinel(
    tmp_path: Path,
) -> None:
    namespace = "d8" * 32
    dated_session = _session_event(
        session_id="undated", namespace=namespace, observed_at=1_750_000_200
    )
    undated_work = {
        "event_id": "undated-work",
        "event_type": "section_started",
        "source": "synthetic-legacy-import",
        "metadata": {
            "client": "codex",
            "client_session_id": "undated",
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "section_id": "s1",
            "section_status": "started",
        },
    }
    undated_usage = {
        "event_id": "undated-usage",
        "event_type": "model_usage",
        "source": "synthetic-legacy-import",
        "estimated_input_tokens": 7,
        "estimated_output_tokens": 2,
        "metadata": {
            "client": "codex",
            "client_session_id": "undated",
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "usage_additive": True,
            "usage_update_semantics": "cumulative_snapshot",
        },
    }
    undated_usage = mark_trusted_local_usage_import_event(undated_usage)
    payload = b"".join(
        json.dumps(item, sort_keys=True).encode() + b"\n"
        for item in (dated_session, undated_work, undated_usage)
    )
    snapshot = _verified_events_snapshot(tmp_path, payload, name="undated")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        issues = {
            (str(row[0]), str(row[1]))
            for row in store.connection.execute(
                "SELECT reason, disposition FROM migration_issues"
            )
        }
        assert ("missing_event_timestamp", "invalid") in issues
        fact_row = store.connection.execute(
            "SELECT occurred_at_us FROM facts WHERE source_event_id = 'undated-work'"
        ).fetchone()
        assert fact_row is not None
        assert int(fact_row[0]) == 0
        usage_row = store.connection.execute(
            "SELECT observed_at_us FROM usage_measurements"
        ).fetchone()
        assert usage_row is not None
        assert int(usage_row[0]) == 0
    finally:
        store.close()


def test_aborted_import_leaves_candidate_at_pre_import_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentacct.canonical.sqlite import CanonicalRepository

    namespace = "d9" * 32
    session = _session_event(
        session_id="abort-mid-run", namespace=namespace, observed_at=1_750_000_300
    )
    usage = {
        "event_id": "abort-usage",
        "event_type": "model_usage",
        "source": "synthetic-legacy-import",
        "estimated_input_tokens": 11,
        "estimated_output_tokens": 5,
        "created_at": 1_750_000_301,
        "metadata": {
            "client": "codex",
            "client_session_id": "abort-mid-run",
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "usage_additive": True,
            "usage_update_semantics": "cumulative_snapshot",
        },
    }
    usage = mark_trusted_local_usage_import_event(usage)
    payload = b"".join(
        json.dumps(item, sort_keys=True).encode() + b"\n"
        for item in (session, usage)
    )
    snapshot = _verified_events_snapshot(tmp_path, payload, name="abort-mid-run")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        real_reconcile_usage = CanonicalRepository.reconcile_usage

        def _boom(self: CanonicalRepository, *args: object, **kwargs: object) -> object:
            raise RuntimeError("mid-import failure")

        monkeypatch.setattr(CanonicalRepository, "reconcile_usage", _boom)
        with pytest.raises(RuntimeError, match="mid-import failure"):
            import_legacy_snapshot(
                snapshot=snapshot,
                store=store,
                scratch_root=scratch.resolve(),
            )

        counts = store.repository().table_counts()
        assert all(value == 0 for value in counts.values()), counts
        sequence = store.connection.execute(
            "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        assert int(sequence[0]) == 0

        monkeypatch.setattr(CanonicalRepository, "reconcile_usage", real_reconcile_usage)
        report = import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )
        assert report.parity.matches is True
        rerun_counts = store.repository().table_counts()
        assert rerun_counts["sessions"] == 1
        assert rerun_counts["usage_measurements"] == 1
    finally:
        store.close()


def test_unparseable_client_event_timestamp_records_visible_issue(
    tmp_path: Path,
) -> None:
    namespace = "da" * 32
    dated_session = _session_event(
        session_id="garbage-ts", namespace=namespace, observed_at=1_750_000_400
    )
    garbage_work = {
        "event_id": "garbage-ts-work",
        "event_type": "section_started",
        "source": "synthetic-legacy-import",
        "metadata": {
            "client": "codex",
            "client_session_id": "garbage-ts",
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "section_id": "s1",
            "section_status": "started",
            "client_event_timestamp": "not-a-timestamp",
        },
    }
    payload = b"".join(
        json.dumps(item, sort_keys=True).encode() + b"\n"
        for item in (dated_session, garbage_work)
    )
    snapshot = _verified_events_snapshot(tmp_path, payload, name="garbage-ts")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    try:
        import_legacy_snapshot(
            snapshot=snapshot,
            store=store,
            scratch_root=scratch.resolve(),
        )

        issues = {
            (str(row[0]), str(row[1]))
            for row in store.connection.execute(
                "SELECT reason, disposition FROM migration_issues"
            )
        }
        assert ("invalid_timestamp", "invalid") in issues
        fact_row = store.connection.execute(
            "SELECT occurred_at_us FROM facts WHERE source_event_id = 'garbage-ts-work'"
        ).fetchone()
        assert fact_row is not None
        assert int(fact_row[0]) == 0
    finally:
        store.close()
