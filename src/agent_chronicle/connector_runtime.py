from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .connectors import ConnectorRecord
from .evidence import EvidenceEnvelope
from .evidence_runtime import EvidenceRuntime


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ConnectorImportResult:
    connector: str
    record_count: int
    inserted_count: int
    duplicate_count: int
    conflict_count: int
    error_count: int
    evidence_ids: tuple[str, ...]
    errors: tuple[str, ...]
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "record_count": self.record_count,
            "inserted_count": self.inserted_count,
            "duplicate_count": self.duplicate_count,
            "conflict_count": self.conflict_count,
            "error_count": self.error_count,
            "evidence_ids": list(self.evidence_ids),
            "errors": list(self.errors),
            "dry_run": self.dry_run,
        }


def _same_connector_content(left: EvidenceEnvelope, right: EvidenceEnvelope) -> bool:
    """Compare source content while ignoring import-observation timestamps.

    Exported snapshots sometimes omit an event timestamp. The first import time
    then fills event/observation time, but replaying the exact same exported row
    later must be a duplicate receipt, not a false conflict. A changed payload,
    digest, subject, basis, or completeness still creates a real conflict.
    """

    ignored = {
        "event_timestamp",
        "observed_at",
        "emitted_at",
        "event_end_timestamp",
        "idempotency_key",
        "integrity_hash",
        "evidence_id",
    }
    left_value = {key: value for key, value in left.to_dict().items() if key not in ignored}
    right_value = {key: value for key, value in right.to_dict().items() if key not in ignored}
    return left_value == right_value


def import_connector_records(
    runtime: EvidenceRuntime,
    records: Iterable[ConnectorRecord],
    *,
    connector: str,
    observed_at: str | None = None,
    dry_run: bool = False,
) -> ConnectorImportResult:
    """Normalize connector records and optionally append them to Evidence v2.

    Connector reads are side-effect free upstream. A non-dry import changes
    only agentacct's local evidence spool/projection. The returned counts make
    partial durable prefixes explicit if a later record fails.
    """

    record_list = tuple(records)
    if not runtime.enabled and not dry_run:
        return ConnectorImportResult(
            connector=connector,
            record_count=len(record_list),
            inserted_count=0,
            duplicate_count=0,
            conflict_count=0,
            error_count=1,
            evidence_ids=(),
            errors=("evidence_v2_disabled",),
            dry_run=False,
        )
    observation_time = observed_at or _utc_now()
    inserted = duplicate = conflict = 0
    evidence_ids: list[str] = []
    errors: list[str] = []
    for record in record_list:
        try:
            envelope = record.to_evidence_envelope(observed_at=observation_time)
            if not dry_run:
                existing = runtime.store.query(idempotency_key=envelope.idempotency_key, limit=100)
                exact_content = next(
                    (
                        candidate.envelope
                        for candidate in existing
                        if _same_connector_content(candidate.envelope, envelope)
                    ),
                    None,
                )
                if exact_content is not None:
                    envelope = exact_content
            evidence_ids.append(envelope.evidence_id)
            if dry_run:
                continue
            result = runtime.append(envelope)
            inserted += int(result.disposition == "inserted")
            duplicate += int(result.disposition == "duplicate")
            conflict += int(result.disposition == "conflict")
        except Exception as exc:  # noqa: BLE001 - return a truthful partial-import report.
            errors.append(f"{record.source_event_id}: {type(exc).__name__}: {exc}"[:1000])
    return ConnectorImportResult(
        connector=connector,
        record_count=len(record_list),
        inserted_count=inserted,
        duplicate_count=duplicate,
        conflict_count=conflict,
        error_count=len(errors),
        evidence_ids=tuple(evidence_ids),
        errors=tuple(errors),
        dry_run=dry_run,
    )


__all__ = ["ConnectorImportResult", "import_connector_records"]
