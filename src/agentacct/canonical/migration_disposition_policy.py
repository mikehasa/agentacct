"""Owner-approved disposition policy for best-effort canonical migration.

The raw legacy/client records remain the retained recovery source. This module
decides only which visible importer exclusions may remain outside a derived
canonical candidate without blocking the narrow JSON read cutover. Unknown
reasons always remain unapproved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


MIGRATION_DISPOSITION_POLICY_SCHEMA_VERSION = (
    "agent-chronicle.canonical-best-effort-migration-policy-evidence.v1"
)
MIGRATION_DISPOSITION_POLICY_ID = "canonical_best_effort_migration_v1"
MIGRATION_DISPOSITION_POLICY_VERSION = "v1"
MIGRATION_DISPOSITION_RETENTION_BASIS = (
    "manifest-verified-snapshot-retained-and-legacy-files-untouched-by-runner"
)

_APPROVED_EXCLUSION_RULES = (
    {
        "reason": "unresolved_source_namespace",
        "disposition": "requires_choice",
    },
)
_POLICY_DEFINITION = {
    "schema_version": MIGRATION_DISPOSITION_POLICY_SCHEMA_VERSION,
    "policy_id": MIGRATION_DISPOSITION_POLICY_ID,
    "policy_version": MIGRATION_DISPOSITION_POLICY_VERSION,
    "retention_basis": MIGRATION_DISPOSITION_RETENTION_BASIS,
    "approved_exclusion_rules": list(_APPROVED_EXCLUSION_RULES),
    "issue_summary_contract": "positive-integer-unique-aggregate-rows",
    "source_line_conservation_contract": (
        "independent-source-lines-equal-importer-lines-equal-"
        "parsed-events-plus-excluded-lines"
    ),
    "approved_exclusion_conservation_contract": (
        "only-approved-issues-one-issue-per-excluded-line-zero-processed-with-issues"
    ),
}
MIGRATION_DISPOSITION_POLICY_RULES_DIGEST = hashlib.sha256(
    json.dumps(
        _POLICY_DEFINITION,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_APPROVED_EXCLUSION_KEYS = frozenset(
    (str(rule["reason"]), str(rule["disposition"]))
    for rule in _APPROVED_EXCLUSION_RULES
)


def _classified_exclusions(
    issues: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    approved: list[dict[str, object]] = []
    unapproved: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str]] = set()
    for issue in issues:
        if set(issue) != {"reason", "disposition", "count"}:
            raise ValueError("migration issue summary contains incompatible fields")
        reason = issue.get("reason")
        disposition = issue.get("disposition")
        count = issue.get("count")
        if (
            not isinstance(reason, str)
            or not reason
            or not isinstance(disposition, str)
            or not disposition
            or type(count) is not int
            or count <= 0
        ):
            raise ValueError("migration issue summary contains an invalid row")
        key = (reason, disposition)
        if key in seen_keys:
            raise ValueError("migration issue summary contains a duplicate row")
        seen_keys.add(key)
        row = {
            "reason": reason,
            "disposition": disposition,
            "count": count,
        }
        target = (
            approved
            if key in _APPROVED_EXCLUSION_KEYS
            else unapproved
        )
        target.append(row)
    return approved, unapproved


def build_migration_disposition_policy_evidence(
    *,
    snapshot_manifest_sha256: str,
    issues: Sequence[Mapping[str, object]],
    source_lines_seen: int,
    importer_lines_seen: int,
    parsed_events: int,
    malformed_or_excluded_lines: int,
    issue_lines: int,
    excluded_lines: int,
    processed_with_issues_lines: int,
    migration_issue_count: int,
) -> dict[str, Any]:
    """Classify visible issues and bind aggregate source-line conservation."""

    approved, unapproved = _classified_exclusions(issues)
    approved_count = sum(int(row["count"]) for row in approved)
    unapproved_count = sum(int(row["count"]) for row in unapproved)
    visible_issue_count = approved_count + unapproved_count
    source_line_conservation = bool(
        source_lines_seen >= 0
        and importer_lines_seen >= 0
        and parsed_events >= 0
        and excluded_lines >= 0
        and source_lines_seen == importer_lines_seen
        and importer_lines_seen == parsed_events + excluded_lines
    )
    approved_exclusion_conservation = bool(
        not unapproved
        and unapproved_count == 0
        and visible_issue_count == migration_issue_count
        and approved_count == malformed_or_excluded_lines
        and approved_count == issue_lines
        and approved_count == excluded_lines
        and processed_with_issues_lines == 0
    )
    return {
        "schema_version": MIGRATION_DISPOSITION_POLICY_SCHEMA_VERSION,
        "policy_id": MIGRATION_DISPOSITION_POLICY_ID,
        "policy_version": MIGRATION_DISPOSITION_POLICY_VERSION,
        "rules_digest": MIGRATION_DISPOSITION_POLICY_RULES_DIGEST,
        "retention_basis": MIGRATION_DISPOSITION_RETENTION_BASIS,
        "snapshot_manifest_sha256": snapshot_manifest_sha256,
        "approved_exclusions": approved,
        "unapproved_exclusions": unapproved,
        "approved_exclusion_count": approved_count,
        "unapproved_exclusion_count": unapproved_count,
        "source_lines_seen": source_lines_seen,
        "importer_lines_seen": importer_lines_seen,
        "parsed_events": parsed_events,
        "malformed_or_excluded_lines": malformed_or_excluded_lines,
        "issue_lines": issue_lines,
        "excluded_lines": excluded_lines,
        "processed_with_issues_lines": processed_with_issues_lines,
        "migration_issue_count": migration_issue_count,
        "source_line_conservation": source_line_conservation,
        "approved_exclusion_conservation": approved_exclusion_conservation,
    }


__all__ = [
    "MIGRATION_DISPOSITION_POLICY_ID",
    "MIGRATION_DISPOSITION_POLICY_RULES_DIGEST",
    "MIGRATION_DISPOSITION_POLICY_SCHEMA_VERSION",
    "MIGRATION_DISPOSITION_POLICY_VERSION",
    "MIGRATION_DISPOSITION_RETENTION_BASIS",
    "build_migration_disposition_policy_evidence",
]
