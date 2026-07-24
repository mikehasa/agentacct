"""Read-only Paperclip export/API-snapshot normalization.

Paperclip is authoritative for its own organization, assignment, and runtime
claims.  It is not treated as provider billing truth.  The adapter accepts a
decoded/exported snapshot only; it has no API client and no mutation methods.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .base import ConnectorError, ConnectorRecord, ReadOnlyConnector, load_json_document, stable_digest


PAPERCLIP_UPSTREAM_SHA = "c36f1a4afd91e4ddf0e5c7224b288ce722c7404f"
PAPERCLIP_LICENSE = "MIT"


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _identifier(row: Mapping[str, Any], *names: str) -> str | None:
    value = _first(row, *names)
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 256 or "\n" in text or "\r" in text:
        return None
    return text


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _collection(document: Mapping[str, Any], *names: str) -> list[Mapping[str, Any]]:
    value: Any = None
    for name in names:
        if name in document:
            value = document[name]
            break
    if value is None:
        return []
    if isinstance(value, Mapping):
        for wrapper in ("data", "items", "results"):
            nested = value.get(wrapper)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                value = nested
                break
        else:
            value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConnectorError(f"Paperclip collection {names[0]} must be an array")
    return [dict(item) for item in value if isinstance(item, Mapping)]


_ENTITY_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "company": {
        "name": ("name", "displayName", "display_name"),
        "status": ("status",),
        "created_at": ("createdAt", "created_at"),
        "updated_at": ("updatedAt", "updated_at"),
    },
    "agent": {
        "name": ("name", "displayName", "display_name"),
        "role": ("role", "agentRole", "agent_role"),
        "status": ("status",),
        "model": ("model", "modelName", "model_name"),
        "created_at": ("createdAt", "created_at"),
        "updated_at": ("updatedAt", "updated_at"),
    },
    "issue": {
        "status": ("status",),
        "priority": ("priority",),
        "kind": ("kind", "type", "issueType", "issue_type"),
        "created_at": ("createdAt", "created_at"),
        "updated_at": ("updatedAt", "updated_at"),
        "started_at": ("startedAt", "started_at"),
        "completed_at": ("completedAt", "completed_at"),
    },
    "run": {
        "status": ("status",),
        "trigger": ("trigger", "triggerType", "trigger_type"),
        "started_at": ("startedAt", "started_at"),
        "completed_at": ("completedAt", "completed_at", "endedAt", "ended_at"),
        "duration_ms": ("durationMs", "duration_ms"),
        "exit_code": ("exitCode", "exit_code"),
    },
    "work_product": {
        "kind": ("kind", "type", "workProductType", "work_product_type"),
        "status": ("status",),
        "digest": ("digest", "sha", "commitSha", "commit_sha"),
        "created_at": ("createdAt", "created_at"),
        "updated_at": ("updatedAt", "updated_at"),
    },
}


def _safe_fields(kind: str, row: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for canonical, aliases in _ENTITY_FIELDS[kind].items():
        value = _first(row, *aliases)
        if isinstance(value, str) and value.strip() and len(value) <= 256 and "\n" not in value and "\r" not in value:
            output[canonical] = value
        elif isinstance(value, (bool, int)):
            output[canonical] = value
        elif isinstance(value, float) and math.isfinite(value):
            output[canonical] = value
    return output


class PaperclipSnapshotConnector(ReadOnlyConnector):
    name = "paperclip"
    source_type = "orchestrator_snapshot"
    upstream_sha = PAPERCLIP_UPSTREAM_SHA
    license_id = PAPERCLIP_LICENSE

    def read(self, source: Any = None) -> tuple[ConnectorRecord, ...]:
        document = load_json_document(source)
        if not isinstance(document, Mapping):
            raise ConnectorError("Paperclip snapshot root must be an object")

        records: list[ConnectorRecord] = []
        company_rows = _collection(document, "companies")
        if isinstance(document.get("company"), Mapping):
            company_rows.append(dict(document["company"]))
        default_company_id = _identifier(company_rows[0], "id", "companyId", "company_id") if company_rows else None

        for row in company_rows:
            company_id = _identifier(row, "id", "companyId", "company_id")
            if company_id:
                records.append(
                    self._record(
                        "orchestrator.company.claim",
                        company_id,
                        row,
                        {"organization": company_id},
                        _safe_fields("company", row),
                        occurred_at=_identifier(row, "updatedAt", "updated_at", "createdAt", "created_at"),
                    )
                )

        for row in _collection(document, "agents"):
            agent_id = _identifier(row, "id", "agentId", "agent_id")
            if not agent_id:
                continue
            company_id = _identifier(row, "companyId", "company_id") or default_company_id
            records.append(
                self._record(
                    "orchestrator.agent.claim",
                    agent_id,
                    row,
                    {"principal": agent_id, "organization": company_id or ""},
                    _safe_fields("agent", row),
                    occurred_at=_identifier(row, "updatedAt", "updated_at", "createdAt", "created_at"),
                    source_instance_id=company_id or "local",
                )
            )

        issue_rows = _collection(document, "issues", "tasks", "workItems", "work_items")
        for row in issue_rows:
            issue_id = _identifier(row, "id", "issueId", "issue_id", "taskId", "task_id")
            if not issue_id:
                continue
            company_id = _identifier(row, "companyId", "company_id") or default_company_id
            subjects = {
                "work_item": issue_id,
                "organization": company_id or "",
                "principal": _identifier(row, "assigneeAgentId", "assignee_agent_id", "agentId", "agent_id") or "",
                "parent_work_item": _identifier(row, "parentId", "parent_id", "parentIssueId", "parent_issue_id") or "",
            }
            records.append(
                self._record(
                    "orchestrator.work_item.claim",
                    issue_id,
                    row,
                    subjects,
                    _safe_fields("issue", row),
                    occurred_at=_identifier(row, "updatedAt", "updated_at", "createdAt", "created_at"),
                    source_instance_id=company_id or "local",
                )
            )

        run_rows = _collection(document, "runs", "heartbeatRuns", "heartbeat_runs")
        for row in run_rows:
            run_id = _identifier(row, "id", "runId", "run_id", "heartbeatRunId", "heartbeat_run_id")
            if not run_id:
                continue
            company_id = _identifier(row, "companyId", "company_id") or default_company_id
            subjects = {
                "execution": run_id,
                "organization": company_id or "",
                "principal": _identifier(row, "agentId", "agent_id") or "",
                "work_item": _identifier(row, "issueId", "issue_id", "taskId", "task_id") or "",
            }
            records.append(
                self._record(
                    "orchestrator.execution.claim",
                    run_id,
                    row,
                    subjects,
                    _safe_fields("run", row),
                    occurred_at=_identifier(row, "startedAt", "started_at", "createdAt", "created_at"),
                    source_instance_id=company_id or "local",
                )
            )

        cost_rows = _collection(document, "costEvents", "cost_events", "costs")
        for row in cost_rows:
            cost_id = _identifier(row, "id", "costEventId", "cost_event_id")
            if not cost_id:
                # A stable row digest avoids inventing a claim id from cost amount.
                cost_id = f"digest:{stable_digest(row)}"
            company_id = _identifier(row, "companyId", "company_id") or default_company_id
            attributes: dict[str, Any] = {}
            amount = _finite_number(_first(row, "costUsd", "cost_usd", "amountUsd", "amount_usd"))
            cents = _finite_number(_first(row, "costCents", "cost_cents", "amountCents", "amount_cents"))
            if amount is None and cents is not None and cents >= 0:
                amount = cents / 100
            if amount is not None and amount >= 0:
                attributes["amount_usd"] = amount
                completeness = "complete"
            else:
                attributes["cost_missing"] = True
                completeness = "partial"
            currency = _identifier(row, "currency")
            if currency:
                attributes["currency"] = currency.upper()
            category = _identifier(row, "category", "kind", "type")
            if category:
                attributes["category"] = category
            records.append(
                self._record(
                    "orchestrator.cost.claim",
                    cost_id,
                    row,
                    {
                        "organization": company_id or "",
                        "execution": _identifier(row, "runId", "run_id") or "",
                        "principal": _identifier(row, "agentId", "agent_id") or "",
                        "work_item": _identifier(row, "issueId", "issue_id", "taskId", "task_id") or "",
                    },
                    attributes,
                    occurred_at=_identifier(row, "createdAt", "created_at", "occurredAt", "occurred_at"),
                    completeness=completeness,
                    cost_confidence="orchestrator_claim",
                    source_instance_id=company_id or "local",
                )
            )

        product_rows = _collection(document, "workProducts", "work_products", "artifacts")
        for row in product_rows:
            product_id = _identifier(row, "id", "workProductId", "work_product_id", "artifactId", "artifact_id")
            if not product_id:
                continue
            company_id = _identifier(row, "companyId", "company_id") or default_company_id
            records.append(
                self._record(
                    "orchestrator.work_product.claim",
                    product_id,
                    row,
                    {
                        "artifact": product_id,
                        "organization": company_id or "",
                        "execution": _identifier(row, "runId", "run_id") or "",
                        "work_item": _identifier(row, "issueId", "issue_id", "taskId", "task_id") or "",
                    },
                    _safe_fields("work_product", row),
                    occurred_at=_identifier(row, "createdAt", "created_at", "updatedAt", "updated_at"),
                    source_instance_id=company_id or "local",
                )
            )

        return tuple(sorted(records, key=lambda record: record.record_id))

    def _record(
        self,
        event_kind: str,
        source_event_id: str,
        raw_row: Mapping[str, Any],
        subjects: Mapping[str, str],
        attributes: Mapping[str, Any],
        *,
        occurred_at: str | None,
        completeness: str = "complete",
        cost_confidence: str = "unknown",
        source_instance_id: str = "local",
    ) -> ConnectorRecord:
        return ConnectorRecord(
            connector=self.name,
            source_type=self.source_type,
            source_instance_id=source_instance_id,
            source_event_id=source_event_id,
            event_kind=event_kind,
            evidence_type="claim",
            occurred_at=occurred_at,
            observed_at=occurred_at,
            measurement_basis="orchestrator_claim",
            completeness=completeness,
            subjects=subjects,
            attributes=attributes,
            usage_confidence="unknown",
            cost_confidence=cost_confidence,
            capture_level="metadata_only",
            attribution="direct",
            raw_digest=stable_digest(raw_row),
            upstream_sha=self.upstream_sha,
            license_id=self.license_id,
        )
