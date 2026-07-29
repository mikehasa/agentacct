from __future__ import annotations

import pytest

from agentacct.canonical.migration_disposition_policy import (
    MIGRATION_DISPOSITION_POLICY_RULES_DIGEST,
    build_migration_disposition_policy_evidence,
)


def _evidence(*, reason: str, disposition: str) -> dict[str, object]:
    return build_migration_disposition_policy_evidence(
        snapshot_manifest_sha256="a" * 64,
        issues=[{"reason": reason, "disposition": disposition, "count": 1}],
        source_lines_seen=1,
        importer_lines_seen=1,
        parsed_events=0,
        malformed_or_excluded_lines=1,
        issue_lines=1,
        excluded_lines=1,
        processed_with_issues_lines=0,
        migration_issue_count=1,
    )


def test_policy_digest_is_stable_and_exact_tuple_is_approved() -> None:
    evidence = _evidence(
        reason="unresolved_source_namespace",
        disposition="requires_choice",
    )

    assert MIGRATION_DISPOSITION_POLICY_RULES_DIGEST == (
        "7898f612dc793bb11f3c288a1d1a369fec3919ef23bc13d15e1f90743f07f6d6"
    )
    assert evidence["approved_exclusion_count"] == 1
    assert evidence["unapproved_exclusion_count"] == 0
    assert evidence["approved_exclusion_conservation"] is True


def test_policy_fails_closed_for_new_reason_or_disposition() -> None:
    for reason, disposition in (
        ("new_reason", "requires_choice"),
        ("unresolved_source_namespace", "quarantined"),
    ):
        evidence = _evidence(reason=reason, disposition=disposition)
        assert evidence["approved_exclusion_count"] == 0
        assert evidence["unapproved_exclusion_count"] == 1
        assert evidence["approved_exclusion_conservation"] is False


@pytest.mark.parametrize("count", [0, -1, True])
def test_policy_refuses_nonpositive_or_boolean_issue_counts(count: object) -> None:
    with pytest.raises(ValueError, match="invalid row"):
        build_migration_disposition_policy_evidence(
            snapshot_manifest_sha256="a" * 64,
            issues=[
                {
                    "reason": "unresolved_source_namespace",
                    "disposition": "requires_choice",
                    "count": count,
                }
            ],
            source_lines_seen=0,
            importer_lines_seen=0,
            parsed_events=0,
            malformed_or_excluded_lines=0,
            issue_lines=0,
            excluded_lines=0,
            processed_with_issues_lines=0,
            migration_issue_count=0,
        )


def test_policy_refuses_duplicate_aggregate_rows() -> None:
    row = {
        "reason": "unresolved_source_namespace",
        "disposition": "requires_choice",
        "count": 1,
    }
    with pytest.raises(ValueError, match="duplicate row"):
        build_migration_disposition_policy_evidence(
            snapshot_manifest_sha256="a" * 64,
            issues=[row, row],
            source_lines_seen=2,
            importer_lines_seen=2,
            parsed_events=0,
            malformed_or_excluded_lines=2,
            issue_lines=2,
            excluded_lines=2,
            processed_with_issues_lines=0,
            migration_issue_count=2,
        )


def test_zero_issue_snapshot_is_a_conserved_policy_pass() -> None:
    evidence = build_migration_disposition_policy_evidence(
        snapshot_manifest_sha256="a" * 64,
        issues=[],
        source_lines_seen=2,
        importer_lines_seen=2,
        parsed_events=2,
        malformed_or_excluded_lines=0,
        issue_lines=0,
        excluded_lines=0,
        processed_with_issues_lines=0,
        migration_issue_count=0,
    )

    assert evidence["approved_exclusions"] == []
    assert evidence["unapproved_exclusions"] == []
    assert evidence["source_line_conservation"] is True
    assert evidence["approved_exclusion_conservation"] is True


def test_mixed_approved_and_unknown_issues_fail_closed() -> None:
    evidence = build_migration_disposition_policy_evidence(
        snapshot_manifest_sha256="a" * 64,
        issues=[
            {
                "reason": "unresolved_source_namespace",
                "disposition": "requires_choice",
                "count": 2,
            },
            {
                "reason": "unexpected_issue",
                "disposition": "invalid",
                "count": 1,
            },
        ],
        source_lines_seen=3,
        importer_lines_seen=3,
        parsed_events=0,
        malformed_or_excluded_lines=3,
        issue_lines=3,
        excluded_lines=3,
        processed_with_issues_lines=0,
        migration_issue_count=3,
    )

    assert evidence["approved_exclusion_count"] == 2
    assert evidence["unapproved_exclusion_count"] == 1
    assert evidence["approved_exclusion_conservation"] is False


def test_independent_source_and_importer_line_mismatch_is_not_conserved() -> None:
    evidence = build_migration_disposition_policy_evidence(
        snapshot_manifest_sha256="a" * 64,
        issues=[],
        source_lines_seen=2,
        importer_lines_seen=1,
        parsed_events=1,
        malformed_or_excluded_lines=0,
        issue_lines=0,
        excluded_lines=0,
        processed_with_issues_lines=0,
        migration_issue_count=0,
    )

    assert evidence["source_line_conservation"] is False


def test_policy_refuses_extra_issue_fields() -> None:
    with pytest.raises(ValueError, match="incompatible fields"):
        build_migration_disposition_policy_evidence(
            snapshot_manifest_sha256="a" * 64,
            issues=[
                {
                    "reason": "unresolved_source_namespace",
                    "disposition": "requires_choice",
                    "count": 1,
                    "raw_path": "/private/source.jsonl",
                }
            ],
            source_lines_seen=1,
            importer_lines_seen=1,
            parsed_events=0,
            malformed_or_excluded_lines=1,
            issue_lines=1,
            excluded_lines=1,
            processed_with_issues_lines=0,
            migration_issue_count=1,
        )
