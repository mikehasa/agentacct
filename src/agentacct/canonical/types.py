"""Typed inputs and results for the isolated canonical SQLite spike."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal


SQLITE_INT64_MAX = (1 << 63) - 1
RECOVERY_FACT_TRANSPORT = "legacy_client_log_recovery_v1"
RECOVERY_LINK_METHOD = "client_log_evidenced_recovery"
RECOVERY_LINK_RULE_VERSION = "legacy-client-log-recovery-v1"


def _validate_sqlite_int64(
    name: str,
    value: object,
    *,
    allow_none: bool = False,
    minimum: int = 0,
) -> None:
    """Reject values that Python's SQLite binder cannot store as INTEGER."""

    if value is None:
        if allow_none:
            return
        raise ValueError(f"{name} must be a SQLite signed 64-bit integer")
    if type(value) is not int or not minimum <= value <= SQLITE_INT64_MAX:
        qualifier = "non-negative" if minimum == 0 else "positive"
        raise ValueError(
            f"{name} must be a {qualifier} SQLite signed 64-bit integer "
            f"no greater than {SQLITE_INT64_MAX}"
        )


WriteDisposition = Literal["inserted", "updated", "noop", "conflict"]
CheckpointBoundary = Literal["terminal", "day", "rotation", "correction", "migration"]
TokenField = Literal[
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
]

TOKEN_FIELDS: tuple[TokenField, ...] = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True)
class WriteResult:
    disposition: WriteDisposition
    row_id: int | None
    canonical_sequence: int
    conflict_id: int | None = None

    @property
    def changed(self) -> bool:
        return self.disposition in {"inserted", "updated"}


@dataclass(frozen=True)
class SourceInstanceInput:
    client: str
    adapter: str
    representation: str
    namespace_digest: bytes
    namespace_scheme: str = "hmac-sha256-v1"
    source_schema_version: str | None = None
    privacy_label: str | None = None

    def __post_init__(self) -> None:
        if len(self.namespace_digest) != 32:
            raise ValueError("namespace_digest must contain exactly 32 bytes")
        if not self.client or not self.adapter or not self.representation:
            raise ValueError("client, adapter, and representation are required")


@dataclass(frozen=True)
class SourceInstance:
    source_instance_id: int
    client: str
    adapter: str
    representation: str
    namespace_scheme: str
    namespace_digest: bytes
    source_schema_version: str | None
    privacy_label: str | None


@dataclass(frozen=True)
class SessionInput:
    source_instance_id: int
    client_session_id: str
    session_kind: Literal["root", "child", "internal", "continuation", "retry", "unknown"] = "root"
    started_at_us: int | None = None
    last_activity_at_us: int | None = None
    observation_order: int | None = None
    observation_hash: bytes | None = None
    source_revision_id: int | None = None
    # Observation-level absorb state: the last-wins raw parent the source
    # claimed. The three fields travel together; edge validation is separate.
    parent_client: str | None = None
    parent_client_session_id: str | None = None
    parent_namespace_digest: bytes | None = None

    def __post_init__(self) -> None:
        _validate_sqlite_int64("source_instance_id", self.source_instance_id, minimum=1)
        if not self.client_session_id:
            raise ValueError("client_session_id is required")
        if self.observation_hash is not None and len(self.observation_hash) != 32:
            raise ValueError("observation_hash must contain exactly 32 bytes")
        parent_fields = (
            self.parent_client,
            self.parent_client_session_id,
            self.parent_namespace_digest,
        )
        if any(field is None for field in parent_fields) != all(
            field is None for field in parent_fields
        ):
            raise ValueError(
                "parent_client, parent_client_session_id, and parent_namespace_digest "
                "must be supplied together"
            )
        if self.parent_namespace_digest is not None and len(self.parent_namespace_digest) != 32:
            raise ValueError("parent_namespace_digest must contain exactly 32 bytes")
        for name in (
            "started_at_us",
            "last_activity_at_us",
            "observation_order",
            "source_revision_id",
        ):
            _validate_sqlite_int64(name, getattr(self, name), allow_none=True)


@dataclass(frozen=True)
class Session:
    session_id: int
    source_instance_id: int
    client_session_id: str
    session_kind: str
    started_at_us: int | None
    last_activity_at_us: int | None
    # The keyset-pagination sort key (0 when no activity timestamp exists —
    # the column's sentinel, distinct from last_activity_at_us being None).
    sort_activity_at_us: int
    observation_order: int | None
    observation_hash: bytes | None


@dataclass(frozen=True)
class SessionEdgeInput:
    child_session_id: int
    parent_session_id: int
    relation: Literal["parent", "root", "continuation", "retry"]
    basis: str
    validation_state: Literal["valid", "rejected", "conflict", "pending"]
    content_hash: bytes
    source_revision_id: int | None = None

    def __post_init__(self) -> None:
        _validate_sqlite_int64("child_session_id", self.child_session_id, minimum=1)
        _validate_sqlite_int64("parent_session_id", self.parent_session_id, minimum=1)
        _validate_sqlite_int64(
            "source_revision_id", self.source_revision_id, allow_none=True
        )
        if self.child_session_id == self.parent_session_id:
            raise ValueError("a session cannot be its own parent")
        if len(self.content_hash) != 32:
            raise ValueError("content_hash must contain exactly 32 bytes")


@dataclass(frozen=True)
class TaskAnchor:
    task_anchor_id: int
    public_task_id: str
    primary_session_id: int


@dataclass(frozen=True)
class FactInput:
    source_instance_id: int
    fact_type: Literal[
        "work_claim",
        "machine_check",
        "artifact",
        "finding_action",
        "mechanical_observation",
    ]
    transport: str
    strength: Literal[
        "recorded_claim",
        "mechanical_observation",
        "machine_check",
        "inspectable_artifact",
    ]
    occurred_at_us: int
    content_hash: bytes
    source_event_id: str | None = None
    idempotency_scope: str | None = None
    idempotency_key: str | None = None
    source_order: int | None = None
    supersedes_fact_id: int | None = None

    def __post_init__(self) -> None:
        _validate_sqlite_int64("source_instance_id", self.source_instance_id, minimum=1)
        _validate_sqlite_int64("occurred_at_us", self.occurred_at_us)
        _validate_sqlite_int64("source_order", self.source_order, allow_none=True)
        _validate_sqlite_int64(
            "supersedes_fact_id",
            self.supersedes_fact_id,
            allow_none=True,
            minimum=1,
        )
        if len(self.content_hash) != 32:
            raise ValueError("content_hash must contain exactly 32 bytes")
        if (self.idempotency_scope is None) != (self.idempotency_key is None):
            raise ValueError("idempotency_scope and idempotency_key must be supplied together")


@dataclass(frozen=True)
class WorkClaimInput:
    fact: FactInput
    event_kind: Literal["task", "section", "checkpoint", "completion", "blocker", "event"]
    outcome_axis: Literal["intent", "execution", "verification", "outcome", "finding"]
    status: str | None = None
    title: str | None = None
    objective: str | None = None
    summary: str | None = None
    blocker: str | None = None
    next_step: str | None = None
    work_id: str | None = None
    section_id: str | None = None

    def __post_init__(self) -> None:
        if self.fact.fact_type != "work_claim":
            raise ValueError("WorkClaimInput requires fact_type='work_claim'")


@dataclass(frozen=True)
class FactSessionLinkInput:
    fact_id: int
    session_id: int
    method: str
    confidence: Literal["exact", "high", "medium", "low", "ambiguous", "rejected", "conflict"]
    rule_version: str
    validation_state: Literal["valid", "pending", "rejected", "conflict"]
    veto_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_sqlite_int64("fact_id", self.fact_id, minimum=1)
        _validate_sqlite_int64("session_id", self.session_id, minimum=1)
        if not self.method or not self.rule_version:
            raise ValueError("link method and rule_version are required")


@dataclass(frozen=True)
class RecoveredWorkClaimInput:
    """One candidate-only claim whose recovery provenance is not caller-tunable."""

    claim: WorkClaimInput
    session_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.claim, WorkClaimInput):
            raise ValueError("claim must be a WorkClaimInput")
        _validate_sqlite_int64("session_id", self.session_id, minimum=1)
        fact = self.claim.fact
        if fact.transport != RECOVERY_FACT_TRANSPORT:
            raise ValueError(
                "RecoveredWorkClaimInput requires the locked recovery transport"
            )
        if fact.strength != "recorded_claim":
            raise ValueError(
                "RecoveredWorkClaimInput requires recorded_claim strength"
            )
        if (
            fact.source_event_id is None
            and fact.idempotency_key is None
        ):
            raise ValueError(
                "RecoveredWorkClaimInput requires a stable fact identity"
            )
        if fact.source_event_id is not None and not isinstance(
            fact.source_event_id, str
        ):
            raise ValueError("source_event_id must be text when present")
        if fact.idempotency_key is not None and (
            not isinstance(fact.idempotency_scope, str)
            or not isinstance(fact.idempotency_key, str)
        ):
            raise ValueError("idempotency identity must contain text")
        if (
            not isinstance(self.claim.event_kind, str)
            or self.claim.event_kind
            not in {"task", "section", "checkpoint", "completion", "blocker", "event"}
        ):
            raise ValueError("recovered work claim event_kind is invalid")
        if (
            not isinstance(self.claim.outcome_axis, str)
            or self.claim.outcome_axis
            not in {"intent", "execution", "verification", "outcome", "finding"}
        ):
            raise ValueError("recovered work claim outcome_axis is invalid")
        if self.claim.status is not None:
            if not isinstance(self.claim.status, str) or self.claim.status not in {
                "started",
                "checkpoint",
                "completed",
                "blocked",
                "reported",
                "aborted",
            }:
                raise ValueError("recovered work claim status is invalid")
        for name, limit in (
            ("title", 512),
            ("objective", 4096),
            ("summary", 8192),
            ("blocker", 4096),
            ("next_step", 4096),
            ("work_id", 256),
            ("section_id", 256),
        ):
            value = getattr(self.claim, name)
            if value is not None and (
                not isinstance(value, str) or len(value) > limit
            ):
                raise ValueError(f"recovered work claim {name} is invalid")


@dataclass(frozen=True)
class UsageMeasurementInput:
    session_id: int
    lane: str
    representation: str
    update_semantics: Literal["cumulative_snapshot"]
    precedence_role: Literal["authoritative", "fallback", "enrichment"]
    granularity: Literal["request", "turn", "session", "task_only", "unavailable"]
    totals_eligible: bool
    usage_confidence: str
    observed_at_us: int
    updated_at_us: int
    provider: str | None = None
    model: str | None = None
    held_reason: str | None = None
    input_tokens: int | None = None
    input_tokens_reported: bool = False
    output_tokens: int | None = None
    output_tokens_reported: bool = False
    cached_input_tokens: int | None = None
    cached_input_tokens_reported: bool = False
    cache_creation_input_tokens: int | None = None
    cache_creation_input_tokens_reported: bool = False
    cache_read_input_tokens: int | None = None
    cache_read_input_tokens_reported: bool = False
    reasoning_output_tokens: int | None = None
    reasoning_output_tokens_reported: bool = False
    total_tokens: int | None = None
    total_tokens_reported: bool = False
    client_reported_cost_microusd: int | None = None
    source_order: int | None = None
    source_revision_id: int | None = None

    def __post_init__(self) -> None:
        if self.update_semantics != "cumulative_snapshot":
            raise ValueError(
                "UsageMeasurementInput is cumulative-only; atomic usage requires a separate immutable event input"
            )
        if not self.lane or not self.representation or not self.usage_confidence:
            raise ValueError("lane, representation, and usage_confidence are required")
        _validate_sqlite_int64("session_id", self.session_id, minimum=1)
        _validate_sqlite_int64("observed_at_us", self.observed_at_us)
        _validate_sqlite_int64("updated_at_us", self.updated_at_us)
        if self.updated_at_us < self.observed_at_us:
            raise ValueError("usage timestamps are invalid")
        if self.totals_eligible and self.precedence_role == "enrichment":
            raise ValueError("enrichment usage cannot be totals eligible")
        if self.totals_eligible and self.held_reason is not None:
            raise ValueError("held usage cannot be totals eligible")
        for name in TOKEN_FIELDS:
            reported = getattr(self, f"{name}_reported")
            if type(reported) is not bool:
                raise ValueError(f"{name}_reported must be a boolean")
            value = getattr(self, name)
            if reported:
                _validate_sqlite_int64(name, value)
            elif value is not None:
                raise ValueError(f"{name} must be None when unavailable")
        for name in (
            "client_reported_cost_microusd",
            "source_order",
            "source_revision_id",
        ):
            _validate_sqlite_int64(name, getattr(self, name), allow_none=True)

    def token_values(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_FIELDS}

    def reported_values(self) -> dict[str, bool]:
        return {f"{name}_reported": bool(getattr(self, f"{name}_reported")) for name in TOKEN_FIELDS}


@dataclass(frozen=True)
class CostCalculationInput:
    usage_measurement_id: int
    usage_content_hash: bytes
    price_source: str
    price_version: str
    price_effective_at_us: int
    formula_version: str
    completeness: Literal["complete", "partial", "unavailable"]
    result_microusd: int | None
    calculated_at_us: int
    currency: Literal["USD"] = "USD"

    def __post_init__(self) -> None:
        _validate_sqlite_int64(
            "usage_measurement_id", self.usage_measurement_id, minimum=1
        )
        _validate_sqlite_int64("price_effective_at_us", self.price_effective_at_us)
        _validate_sqlite_int64("calculated_at_us", self.calculated_at_us)
        _validate_sqlite_int64("result_microusd", self.result_microusd, allow_none=True)
        if len(self.usage_content_hash) != 32:
            raise ValueError("usage_content_hash must contain exactly 32 bytes")
        if not self.price_source or not self.price_version or not self.formula_version:
            raise ValueError("price source/version and formula version are required")
        if self.completeness == "unavailable" and self.result_microusd is not None:
            raise ValueError("unavailable cost cannot have a numeric result")
        if self.completeness in {"complete", "partial"} and self.result_microusd is None:
            raise ValueError("complete or partial cost requires a numeric result")


@dataclass(frozen=True)
class TaskCurrent:
    public_task_id: str
    task_anchor_id: int
    primary_session_id: int
    title: str | None
    projected_state: str
    last_activity_at_us: int | None
    # The keyset-pagination sort key (0 when no session carried an activity
    # timestamp — distinct from last_activity_at_us being None).
    sort_activity_at_us: int
    session_count: int
    usage_measurement_count: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    usage_missing_field_count: int
    finding_count: int
    built_through_sequence: int


@dataclass(frozen=True)
class TaskCursor:
    sort_activity_at_us: int
    public_task_id: str

    def __post_init__(self) -> None:
        _validate_sqlite_int64("sort_activity_at_us", self.sort_activity_at_us)


@dataclass(frozen=True)
class UsageDayQuery:
    start_day: str
    end_day: str
    client: str | None = None
    model: str | None = None
    limit: int = 400


def dataclass_dict(value: object) -> dict[str, object]:
    """Return a shallow, stable mapping without accepting arbitrary objects."""

    return {field.name: getattr(value, field.name) for field in fields(value)}


__all__ = [
    "CheckpointBoundary",
    "CostCalculationInput",
    "FactInput",
    "FactSessionLinkInput",
    "RECOVERY_FACT_TRANSPORT",
    "RECOVERY_LINK_METHOD",
    "RECOVERY_LINK_RULE_VERSION",
    "RecoveredWorkClaimInput",
    "Session",
    "SessionEdgeInput",
    "SessionInput",
    "SQLITE_INT64_MAX",
    "SourceInstance",
    "SourceInstanceInput",
    "TOKEN_FIELDS",
    "TaskAnchor",
    "TaskCurrent",
    "TaskCursor",
    "UsageDayQuery",
    "UsageMeasurementInput",
    "WorkClaimInput",
    "WriteResult",
    "dataclass_dict",
]
