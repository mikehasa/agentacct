from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import agent_chronicle.canonical.product_parity as product_parity_module
from agent_chronicle.canonical.legacy_import import (
    MigrationReport,
    import_legacy_snapshot,
)
from agent_chronicle.canonical.product_parity import (
    build_legacy_product_parity_report,
)
from agent_chronicle.canonical.snapshot import VerifiedSnapshot
from agent_chronicle.canonical.sqlite import CanonicalStore
from agent_chronicle.client_usage import ClientUsageEvent
from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event


RAW_ROOT_SESSION = "legacy-root-session-raw-secret"
RAW_CHILD_SESSION = "legacy-child-session-raw-secret"
RAW_FALLBACK_SESSION = "legacy-fallback-session-raw-secret"
RAW_SOURCE_PATH = "/offline/raw-client-home/private-rollout.jsonl"
RAW_PROJECT_PATH = "/offline/raw-project/private-repository"
SOURCE_NAMESPACE = "a5" * 32


def _manifest_entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _usage_event(
    *,
    event_id: str,
    session_id: str,
    session_kind: str,
    parent_session_id: str | None,
    created_at: int,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, Any]:
    total_tokens = input_tokens + output_tokens + cached_input_tokens
    event = ClientUsageEvent(
        client="codex",
        client_session_id=session_id,
        source_path=Path(RAW_SOURCE_PATH),
        title=None,
        cwd=RAW_PROJECT_PATH,
        model="gpt-parity-fixture",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached_input_tokens,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=reasoning_output_tokens,
        provider_name="codex",
        started_at=created_at,
        updated_at=created_at + 1,
        turn_count=2,
        client_session_kind=session_kind,
        parent_client_session_id=parent_session_id,
        usage_row_lane="model:gpt-parity-fixture",
        source_namespace_fingerprint=SOURCE_NAMESPACE,
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=total_tokens,
        total_tokens_reported=True,
    ).to_sentinel_event()
    event.update(
        {
            "event_id": event_id,
            "created_at": float(created_at),
        }
    )
    return mark_trusted_local_usage_import_event(event)


def _fallback_usage_event() -> dict[str, Any]:
    event = ClientUsageEvent(
        client="codex",
        client_session_id=RAW_FALLBACK_SESSION,
        source_path=Path(RAW_SOURCE_PATH),
        title=None,
        cwd=RAW_PROJECT_PATH,
        model="gpt-parity-fixture",
        # Compatibility value for the existing product only.  The reported
        # bit below is the source truth and the canonical importer must retain
        # this field as unavailable.
        input_tokens=42,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=False,
        cache_read_tokens_reported=False,
        reasoning_output_tokens=0,
        provider_name="codex",
        started_at=1_780_000_020,
        updated_at=1_780_000_021,
        turn_count=0,
        usage_row_lane="model:gpt-parity-fixture",
        source_namespace_fingerprint=SOURCE_NAMESPACE,
        input_tokens_reported=False,
        output_tokens_reported=False,
        reasoning_output_tokens_reported=False,
        total_tokens=42,
        total_tokens_reported=True,
        usage_update_semantics_override="codex_sqlite_tokens_used_fallback",
        usage_representation="codex-sqlite-tokens-used-fallback-v1",
        usage_precedence_role="fallback",
    ).to_sentinel_event()
    event.update(
        {
            "event_id": "legacy-usage-fallback-secret",
            "created_at": 1_780_000_020.0,
        }
    )
    return mark_trusted_local_usage_import_event(event)


def _verified_legacy_chronicle_snapshot(
    tmp_path: Path,
    *,
    events: list[dict[str, Any]] | None = None,
    name: str = "legacy-chronicle",
) -> VerifiedSnapshot:
    if events is None:
        events = [
            _usage_event(
                event_id="legacy-usage-root-secret",
                session_id=RAW_ROOT_SESSION,
                session_kind="root",
                parent_session_id=None,
                created_at=1_780_000_000,
                input_tokens=101,
                output_tokens=0,
                cached_input_tokens=11,
                reasoning_output_tokens=0,
            ),
            _usage_event(
                event_id="legacy-usage-child-secret",
                session_id=RAW_CHILD_SESSION,
                session_kind="child",
                parent_session_id=RAW_ROOT_SESSION,
                created_at=1_780_000_010,
                input_tokens=50,
                output_tokens=7,
                cached_input_tokens=3,
                reasoning_output_tokens=2,
            ),
        ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    root = tmp_path / f"manifest-verified-{name}"
    root.mkdir()
    (root / "events.jsonl").write_bytes(payload)
    manifest_path = tmp_path / f"{name}-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [_manifest_entry("events.jsonl", payload)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return VerifiedSnapshot.verify(
        root=root.resolve(),
        manifest=manifest_path.resolve(),
    )


def _task_ids(store: CanonicalStore) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in store.connection.execute(
            "SELECT public_task_id FROM task_anchors ORDER BY task_anchor_id"
        ).fetchall()
    )


def _import_with_verified_rerun(
    tmp_path: Path,
    *,
    events: list[dict[str, Any]] | None = None,
    name: str = "legacy-chronicle",
) -> tuple[VerifiedSnapshot, CanonicalStore, MigrationReport, dict[str, object]]:
    snapshot = _verified_legacy_chronicle_snapshot(
        tmp_path,
        events=events,
        name=name,
    )
    scratch = tmp_path / "isolated-candidate"
    scratch.mkdir(mode=0o700)
    store = CanonicalStore.create((scratch / "candidate.sqlite3").resolve())
    first = import_legacy_snapshot(
        snapshot=snapshot,
        store=store,
        scratch_root=scratch.resolve(),
    )

    task_ids_before = _task_ids(store)
    counts_before = dict(store.repository().table_counts())
    sequence_before = store.repository().canonical_sequence()
    changes_before = store.connection.total_changes
    second = import_legacy_snapshot(
        snapshot=snapshot,
        store=store,
        scratch_root=scratch.resolve(),
    )
    rerun_evidence: dict[str, object] = {
        "canonical_writes": store.connection.total_changes - changes_before,
        "canonical_sequence_delta": (
            store.repository().canonical_sequence() - sequence_before
        ),
        "task_ids_stable": _task_ids(store) == task_ids_before,
        "opaque_task_ids_valid": all(
            value.startswith("task_")
            and len(value) == 37
            and all(character in "0123456789abcdef" for character in value[5:])
            for value in _task_ids(store)
        ),
        "table_counts_stable": dict(store.repository().table_counts()) == counts_before,
        "projection_rebuilt": second.projection_rebuilt,
        "second_import": {
            "write_dispositions": second.write_dispositions,
            "internal_parity_matches": second.parity.matches,
            "internal_parity_difference_keys": sorted(second.parity.differences),
        },
    }
    return snapshot, store, first, rerun_evidence


def _comparison_by_surface(report: dict[str, object]) -> dict[str, dict[str, object]]:
    comparisons = report["comparisons"]
    assert isinstance(comparisons, list)
    return {
        str(row["surface"]): row
        for row in comparisons
        if isinstance(row, dict)
    }


def _assert_report_redacts_raw_identity_and_paths(
    report: dict[str, object],
    *,
    snapshot: VerifiedSnapshot,
    store: CanonicalStore,
) -> None:
    encoded = json.dumps(report, sort_keys=True)
    for forbidden in (
        RAW_ROOT_SESSION,
        RAW_CHILD_SESSION,
        RAW_SOURCE_PATH,
        RAW_PROJECT_PATH,
        str(snapshot.root),
        str(snapshot.manifest.path),
        str(store.path),
    ):
        assert forbidden not in encoded


def test_manifest_verified_snapshot_has_independent_core_truth_slice_parity(
    tmp_path: Path,
) -> None:
    snapshot, store, migration, rerun = _import_with_verified_rerun(tmp_path)
    try:
        assert migration.parity.matches is True
        assert migration.migration_issue_count == 0
        assert rerun == {
            "canonical_writes": 0,
            "canonical_sequence_delta": 0,
            "task_ids_stable": True,
            "opaque_task_ids_valid": True,
            "table_counts_stable": True,
            "projection_rebuilt": False,
            "second_import": {
                "write_dispositions": {
                    "sessions": {"noop": 2},
                    "session_edges": {"noop": 1},
                    "usage": {"noop": 2},
                },
                "internal_parity_matches": True,
                "internal_parity_difference_keys": [],
            },
        }
        task_ids = _task_ids(store)
        assert len(task_ids) == 1
        assert task_ids[0].startswith("task_")
        assert len(task_ids[0]) == 37

        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        comparisons = _comparison_by_surface(report)
        assert set(comparisons) == {
            "sessions",
            "task_membership",
            "usage_presence",
            "usage_aggregates",
        }
        assert all(row["required_core"] is True for row in comparisons.values())
        assert all(row["matches"] is True for row in comparisons.values())
        assert comparisons["sessions"]["source_count"] == 2
        assert comparisons["task_membership"]["source_count"] == 1
        assert comparisons["usage_presence"]["source_count"] == 2
        assert comparisons["usage_aggregates"]["source_count"] == 1

        acceptance = report["acceptance"]
        assert isinstance(acceptance, dict)
        assert acceptance["manifest_integrity"] is True
        assert acceptance["candidate_integrity"] is True
        assert acceptance["exact_supported_truth"] is True
        assert acceptance["rerun_zero_write_and_stable_ids"] is True
        assert acceptance["core_truth_slice_passed"] is True
        assert acceptance["product_scope_complete"] is False
        assert acceptance["cutover_gate_passed"] is False
        assert report["decision"] == "go-core-truth-slice"
        assert report["cutover_decision"] == "no-go"
        _assert_report_redacts_raw_identity_and_paths(
            report,
            snapshot=snapshot,
            store=store,
        )
    finally:
        store.close()


def test_unresolved_namespace_is_visible_policy_approved_exclusion(
    tmp_path: Path,
) -> None:
    event = {
        "event_id": "legacy-unscoped-machine-check",
        "event_type": "machine_check",
        "created_at": 1_780_000_000.0,
        "metadata": {"check_name": "legacy-check", "status": "passed"},
    }
    snapshot, store, migration, rerun = _import_with_verified_rerun(
        tmp_path,
        events=[event],
        name="legacy-chronicle-approved-unscoped-exclusion",
    )
    try:
        assert migration.parity.matches is True
        assert migration.lines_seen == 1
        assert migration.parsed_events == 0
        assert migration.excluded_lines == 1
        assert migration.migration_issue_count == 1

        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        policy = report["migration_disposition_policy"]
        assert isinstance(policy, dict)
        assert policy["policy_id"] == "canonical_best_effort_migration_v1"
        assert policy["policy_version"] == "v1"
        assert policy["approved_exclusions"] == [
            {
                "reason": "unresolved_source_namespace",
                "disposition": "requires_choice",
                "count": 1,
            }
        ]
        assert policy["unapproved_exclusions"] == []
        assert policy["approved_exclusion_count"] == 1
        assert policy["unapproved_exclusion_count"] == 0
        assert policy["source_line_conservation"] is True
        assert policy["approved_exclusion_conservation"] is True

        acceptance = report["acceptance"]
        assert isinstance(acceptance, dict)
        assert acceptance["migration_policy_applied"] is True
        assert acceptance["approved_exclusion_count"] == 1
        assert acceptance["unapproved_exclusion_count"] == 0
        assert acceptance["core_truth_slice_passed"] is True
        assert report["decision"] == "go-core-truth-slice"
        assert report["cutover_decision"] == "no-go"
        assert report["exclusions"] == policy["approved_exclusions"]
        assert all(
            comparison["matches"] is True
            for comparison in report["comparisons"]
            if isinstance(comparison, dict)
        )
    finally:
        store.close()


def test_unscoped_semantic_event_is_quarantined_without_core_contribution(
    tmp_path: Path,
) -> None:
    event = {
        "event_id": "legacy-unscoped-section",
        "event_type": "section_started",
        "source": "codex",
        "run_id": "legacy-run",
        "created_at": 1_780_000_000.0,
        "metadata": {
            "client": "codex",
            "project_dir": "/private/legacy-project",
            "section_id": "legacy-section",
            "section_status": "started",
            "section_title": "Legacy work",
            "sentinel_semantic_kind": "section",
        },
    }
    snapshot, store, migration, rerun = _import_with_verified_rerun(
        tmp_path,
        events=[event],
        name="legacy-chronicle-approved-unscoped-semantic-exclusion",
    )
    try:
        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        assert report["acceptance"]["approved_exclusion_count"] == 1
        assert report["acceptance"]["unapproved_exclusion_count"] == 0
        assert report["acceptance"]["exact_supported_truth"] is True
        assert report["acceptance"]["core_truth_slice_passed"] is True
        assert report["decision"] == "go-core-truth-slice"
        assert all(
            comparison["source_count"] == comparison["candidate_count"] == 0
            for comparison in report["comparisons"]
            if isinstance(comparison, dict)
        )
    finally:
        store.close()


def test_internal_codex_session_kind_has_product_parity(tmp_path: Path) -> None:
    events = [
        _usage_event(
            event_id="legacy-internal-parent-event",
            session_id=RAW_ROOT_SESSION,
            session_kind="root",
            parent_session_id=None,
            created_at=1_780_000_000,
            input_tokens=101,
            output_tokens=0,
            cached_input_tokens=11,
            reasoning_output_tokens=0,
        ),
        _usage_event(
            event_id="legacy-internal-review-event",
            session_id=RAW_CHILD_SESSION,
            session_kind="internal",
            parent_session_id=RAW_ROOT_SESSION,
            created_at=1_780_000_010,
            input_tokens=50,
            output_tokens=7,
            cached_input_tokens=3,
            reasoning_output_tokens=2,
        ),
    ]
    snapshot, store, migration, rerun = _import_with_verified_rerun(
        tmp_path,
        events=events,
        name="legacy-chronicle-internal-session",
    )
    try:
        assert migration.parity.matches is True
        assert store.connection.execute(
            "SELECT session_kind FROM sessions WHERE client_session_id = ?",
            (RAW_CHILD_SESSION,),
        ).fetchone()[0] == "internal"

        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        comparisons = _comparison_by_surface(report)
        assert comparisons["sessions"]["matches"] is True
        assert comparisons["sessions"]["mismatch_count"] == 0
        assert all(row["matches"] is True for row in comparisons.values())
    finally:
        store.close()


def test_total_only_sqlite_fallback_preserves_missingness_and_product_parity(
    tmp_path: Path,
) -> None:
    snapshot, store, migration, rerun = _import_with_verified_rerun(
        tmp_path,
        events=[_fallback_usage_event()],
        name="legacy-chronicle-total-only-fallback",
    )
    try:
        canonical = store.connection.execute(
            "SELECT input_tokens, input_tokens_reported, total_tokens, "
            "total_tokens_reported, representation, precedence_role "
            "FROM usage_measurements"
        ).fetchone()
        assert canonical is not None
        assert tuple(canonical) == (
            None,
            0,
            42,
            1,
            "codex-sqlite-tokens-used-fallback-v1",
            "fallback",
        )
        assert migration.migration_issue_count == 0
        assert migration.parity.matches is True

        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        comparisons = _comparison_by_surface(report)
        assert comparisons["usage_presence"]["matches"] is True
        assert comparisons["usage_aggregates"]["matches"] is True
        assert all(row["matches"] is True for row in comparisons.values())
        assert report["usage_aggregate_parity_basis"] == {
            "schema_version": "agent-chronicle.usage-aggregate-parity-basis.v1",
            "surface": "existing-product-output",
            "compatibility_projection": (
                "codex-sqlite-total-only-fallback-as-legacy-input-and-total"
            ),
            "source_total_only_fallback_rows": 1,
            "candidate_compatibility_reprojected_rows": 1,
            "counts_match": True,
            "canonical_missingness_surface": "usage_presence",
        }
        acceptance = report["acceptance"]
        assert isinstance(acceptance, dict)
        assert acceptance["exact_supported_truth"] is True
        assert acceptance["core_truth_slice_passed"] is True
        assert report["decision"] == "go-core-truth-slice"
        assert report["cutover_decision"] == "no-go"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("mutation_target", "mutation_key", "mutation_value"),
    [
        ("client", "client", "claude-code"),
        ("basis", "representation", "legacy-v1"),
        ("basis", "update_semantics", "unsupported:atomic_increment"),
        ("basis", "precedence_role", "authoritative"),
        ("basis", "granularity", "turn"),
        ("token", "input_tokens", True),
        ("token", "output_tokens", True),
        ("token", "cached_input_tokens", True),
        ("token", "cache_creation_input_tokens", True),
        ("token", "cache_read_input_tokens", True),
        ("token", "reasoning_output_tokens", True),
        ("token", "total_tokens", False),
    ],
)
def test_total_only_sqlite_fallback_predicate_requires_the_exact_basis(
    mutation_target: str,
    mutation_key: str,
    mutation_value: object,
) -> None:
    client: object = "codex"
    basis: dict[str, object] = {
        "representation": "codex-sqlite-tokens-used-fallback-v1",
        "update_semantics": "cumulative_snapshot",
        "precedence_role": "fallback",
        "granularity": "session",
        "held_reason_present": False,
    }
    tokens: dict[str, dict[str, object]] = {
        field_name: {"reported": False, "value": None}
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "reasoning_output_tokens",
        )
    }
    tokens["total_tokens"] = {"reported": True, "value": 42}

    if mutation_target == "client":
        client = mutation_value
    elif mutation_target == "basis":
        basis[mutation_key] = mutation_value
    else:
        tokens[mutation_key]["reported"] = mutation_value

    assert product_parity_module._uses_codex_sqlite_total_only_fallback(
        client=client,
        basis=basis,
        tokens=tokens,
    ) is False


@pytest.mark.parametrize(
    "mutation",
    (
        "non_codex_client",
        "turn_granularity",
        "authoritative_precedence",
        "reported_output_component",
        "reported_reasoning_component",
    ),
)
def test_non_exact_sqlite_fallback_basis_cannot_receive_compatibility_projection(
    tmp_path: Path,
    mutation: str,
) -> None:
    event = _fallback_usage_event()
    metadata = event["metadata"]
    assert isinstance(metadata, dict)
    if mutation == "non_codex_client":
        event["source"] = "claude-code-local-session-import"
        event["provider"] = "claude-code"
        metadata["client"] = "claude-code"
    elif mutation == "turn_granularity":
        metadata["usage_granularity"] = "turn"
    elif mutation == "authoritative_precedence":
        metadata["precedence_role"] = "authoritative"
    elif mutation == "reported_output_component":
        metadata["output_tokens_reported"] = True
    else:
        metadata["reasoning_output_tokens_reported"] = True

    snapshot, store, migration, rerun = _import_with_verified_rerun(
        tmp_path,
        events=[event],
        name=f"legacy-chronicle-non-exact-fallback-{mutation}",
    )
    try:
        assert migration.migration_issue_count == 0
        assert migration.parity.matches is True
        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        comparisons = _comparison_by_surface(report)
        assert comparisons["usage_presence"]["matches"] is True
        assert comparisons["usage_aggregates"]["matches"] is False
        basis = report["usage_aggregate_parity_basis"]
        assert isinstance(basis, dict)
        assert basis["source_total_only_fallback_rows"] == 0
        assert basis["candidate_compatibility_reprojected_rows"] == 0
        assert basis["counts_match"] is True
        acceptance = report["acceptance"]
        assert isinstance(acceptance, dict)
        assert acceptance["exact_supported_truth"] is False
        assert acceptance["core_truth_slice_passed"] is False
        assert report["decision"] == "no-go"
        assert report["cutover_decision"] == "no-go"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("corruption", "mismatched_surfaces"),
    [
        ("output_presence", {"usage_presence"}),
        ("input_value", {"usage_presence", "usage_aggregates"}),
        ("precedence_role", {"usage_presence"}),
        ("representation", {"usage_presence"}),
        ("granularity", {"usage_presence"}),
    ],
)
def test_candidate_single_token_field_corruption_is_detected_independently(
    tmp_path: Path,
    corruption: str,
    mismatched_surfaces: set[str],
) -> None:
    snapshot, store, migration, rerun = _import_with_verified_rerun(tmp_path)
    try:
        assert migration.parity.matches is True
        with store.transaction(write=True) as connection:
            if corruption == "output_presence":
                cursor = connection.execute(
                    "UPDATE usage_measurements "
                    "SET output_tokens = NULL, output_tokens_reported = 0 "
                    "WHERE totals_eligible = 1"
                )
            elif corruption == "input_value":
                cursor = connection.execute(
                    "UPDATE usage_measurements SET input_tokens = input_tokens + 1 "
                    "WHERE totals_eligible = 1"
                )
            else:
                replacement = {
                    "precedence_role": "fallback",
                    "representation": "corrupted-representation-v1",
                    "granularity": "turn",
                }[corruption]
                cursor = connection.execute(
                    f"UPDATE usage_measurements SET {corruption} = ? "
                    "WHERE totals_eligible = 1",
                    (replacement,),
                )
            assert cursor.rowcount == 1

        report = build_legacy_product_parity_report(
            snapshot=snapshot,
            repository=store.repository(),
            migration=migration,
            legacy_store_scope="custom",
            rerun_evidence=rerun,
        )

        comparisons = _comparison_by_surface(report)
        actual_mismatches = {
            surface for surface, row in comparisons.items() if row["matches"] is False
        }
        assert actual_mismatches == mismatched_surfaces
        for surface in mismatched_surfaces:
            comparison = comparisons[surface]
            assert int(comparison["mismatch_count"]) > 0
            # External parity evidence is deliberately counts-only.  Unsalted
            # hashes of low-entropy aggregate rows are enumerable and stable
            # across reports, so they are not a privacy boundary.
            assert "source_fingerprint" not in comparison
            assert "candidate_fingerprint" not in comparison
            assert "redacted_mismatch_digests" not in comparison

        # The importer's already-computed parity remains green; the independent
        # existing-product oracle is what catches the post-import corruption.
        assert report["migration"]["internal_parity_matches"] is True
        acceptance = report["acceptance"]
        assert isinstance(acceptance, dict)
        assert acceptance["candidate_integrity"] is True
        assert acceptance["exact_supported_truth"] is False
        assert acceptance["core_truth_slice_passed"] is False
        assert acceptance["product_scope_complete"] is False
        assert acceptance["cutover_gate_passed"] is False
        assert report["decision"] == "no-go"
        assert report["cutover_decision"] == "no-go"
        _assert_report_redacts_raw_identity_and_paths(
            report,
            snapshot=snapshot,
            store=store,
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("event", "field_name", "expected"),
    [
        (
            {
                "metadata": {
                    "cached_input_tokens": 0,
                    "cache_creation_tokens_reported": False,
                    "cache_read_tokens_reported": False,
                }
            },
            "cached_input_tokens",
            {"reported": False, "value": None},
        ),
        (
            {
                "metadata": {
                    "cached_input_tokens": 0,
                    "cache_creation_tokens_reported": True,
                    "cache_read_tokens_reported": True,
                }
            },
            "cached_input_tokens",
            {"reported": True, "value": 0},
        ),
        (
            {
                "metadata": {
                    "cached_input_tokens": 0,
                    "cache_creation_tokens_reported": "false",
                    "cache_read_tokens_reported": False,
                }
            },
            "cached_input_tokens",
            {"reported": False, "value": None},
        ),
        (
            {
                "event_type": "model_usage",
                "source": "codex-local-session-import",
                "estimated_input_tokens": 0,
                "metadata": {
                    "client": "codex",
                    "usage_source": "local_client_session_store",
                },
            },
            "input_tokens",
            {"reported": False, "value": None},
        ),
    ],
)
def test_existing_product_presence_oracle_has_explicit_goldens(
    event: dict[str, Any],
    field_name: str,
    expected: dict[str, object],
) -> None:
    assert product_parity_module._source_token(event, field_name) == expected
