"""Durable local control-plane records for agentacct-owned work.

The JSONL action log is the source of truth.  Every complete record is hashed,
flushed, and fsynced while an exclusive POSIX flock is held.  Projection is a
deterministic replay: malformed, torn, or invalid-transition records remain
visible as issues but never gain state-changing power.

This module deliberately owns operational intent, not evidence authority.
Callers may mirror committed control events into Work Events / Evidence v2, but
the optional evidence store is never required to recover approvals, schedules,
or process lifecycle state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


CONTROL_EVENT_SCHEMA_VERSION = "agent-chronicle.control-event.v1"
CONTROL_PROJECTION_SCHEMA_VERSION = "agent-chronicle.control-projection.v1"
CONTROL_STORE_DIRNAME = "control-plane"
CONTROL_ACTIONS_FILENAME = "actions.jsonl"

_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_IDEMPOTENCY_KEY = re.compile(r"^[^\r\n\x00]{1,240}$")
_EVENT_ID = re.compile(r"^ctl_[0-9a-f]{32}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

EXECUTION_STATES = frozenset(
    {"pending", "launching", "running", "cancel_requested", "succeeded", "failed", "cancelled", "lost"}
)
OUTCOME_STATES = frozenset({"unknown", "reported", "verified", "finding", "blocked"})
CONTROL_STATES = frozenset({"ready", "awaiting_approval", "policy_hold", "control_failure"})
APPROVAL_STATES = frozenset({"pending", "approved", "rejected", "expired", "cancelled", "consumed"})

_EXECUTION_TRANSITIONS = {
    "pending": frozenset({"launching", "cancelled", "lost"}),
    "launching": frozenset({"running", "failed", "lost", "cancel_requested"}),
    "running": frozenset({"succeeded", "failed", "lost", "cancel_requested"}),
    "cancel_requested": frozenset({"cancelled", "failed", "lost"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "lost": frozenset(),
}
_OUTCOME_TRANSITIONS = {
    "unknown": frozenset({"reported", "verified", "finding", "blocked"}),
    "reported": frozenset({"verified", "finding", "blocked"}),
    "verified": frozenset({"finding", "blocked"}),
    "finding": frozenset({"verified", "blocked"}),
    "blocked": frozenset({"finding", "verified"}),
}
_CONTROL_TRANSITIONS = {
    "ready": frozenset({"awaiting_approval", "policy_hold", "control_failure"}),
    "awaiting_approval": frozenset({"ready", "policy_hold", "control_failure"}),
    "policy_hold": frozenset({"ready", "control_failure"}),
    "control_failure": frozenset({"ready"}),
}


def contract_requires_launch_approval(permission_envelope: Mapping[str, Any]) -> bool:
    """Fail closed for either supported spelling of a mutating contract."""

    return (
        permission_envelope.get("launch_approval_required") is True
        or permission_envelope.get("mutation_mode") == "workspace_write"
    )


class ControlPlaneError(ValueError):
    """Base error for an invalid control-plane request."""


class RecordNotFound(ControlPlaneError):
    pass


class RevisionConflict(ControlPlaneError):
    pass


class InvalidTransition(ControlPlaneError):
    pass


class IdempotencyConflict(ControlPlaneError):
    pass


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ControlPlaneError("control-plane value must be bounded JSON") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _now() -> float:
    return time.time()


def _owner_only(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _id(value: Any, name: str, *, task: bool = False) -> str:
    if not isinstance(value, str) or not (_TASK_ID if task else _SAFE_ID).fullmatch(value):
        raise ControlPlaneError(f"{name} is invalid")
    return value


def _text(value: Any, name: str, *, maximum: int = 1000, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ControlPlaneError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned and not optional:
        raise ControlPlaneError(f"{name} must not be empty")
    if len(cleaned) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise ControlPlaneError(f"{name} is invalid")
    return cleaned or None


def _revision(value: Any, name: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlPlaneError(f"{name} must be a non-negative integer")
    return value


def _positive_revision(value: Any, name: str = "revision") -> int:
    revision = _revision(value, name)
    if revision < 1:
        raise ControlPlaneError(f"{name} must be a positive integer")
    return revision


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ControlPlaneError(f"{name} must be a finite non-negative timestamp")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ControlPlaneError(f"{name} must be a finite non-negative timestamp") from exc
    if not math.isfinite(number) or number < 0:
        raise ControlPlaneError(f"{name} must be a finite non-negative timestamp")
    return number


def _argv(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values:
        raise ControlPlaneError("argv_template must be a non-empty argv array")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or "\x00" in value or "\r" in value or "\n" in value:
            raise ControlPlaneError("argv_template contains an invalid argument")
        if len(value) > 4096:
            raise ControlPlaneError("argv_template argument is too long")
        result.append(value)
    if len(result) > 256:
        raise ControlPlaneError("argv_template contains too many arguments")
    return tuple(result)


def _string_tuple(values: Sequence[str], name: str, *, maximum_items: int = 100) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ControlPlaneError(f"{name} must be an array")
    if len(values) > maximum_items:
        raise ControlPlaneError(f"{name} contains too many entries")
    return tuple(_text(value, name, maximum=500) or "" for value in values)


def _mapping(value: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ControlPlaneError(f"{name} must be an object")
    encoded = _canonical_json_bytes(dict(value))
    if len(encoded) > 64 * 1024:
        raise ControlPlaneError(f"{name} must be <= 65536 encoded bytes")
    return json.loads(encoded)


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], name: str) -> None:
    """Reject silent schema drift in durable records.

    Durable control records are authority-bearing input.  Ignoring an unknown
    field would let a newer or tampered writer believe a constraint was being
    enforced while this projector silently discarded it.
    """

    if not isinstance(value, Mapping):
        raise ControlPlaneError(f"{name} must be an object")
    expected_keys = set(expected)
    observed_keys = set(value)
    if observed_keys != expected_keys:
        missing = sorted(expected_keys - observed_keys)
        unknown = sorted(observed_keys - expected_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        raise ControlPlaneError(f"{name} fields do not match schema ({'; '.join(details)})")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    origin: str
    created_at: float
    revision: int = 1
    merged_into_task_id: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskRecord":
        _exact_keys(value, ("task_id", "origin", "created_at", "revision", "merged_into_task_id"), "task")
        origin = str(value.get("origin") or "")
        if origin not in {"observed", "planned"}:
            raise ControlPlaneError("task origin must be observed or planned")
        merged = value.get("merged_into_task_id")
        return cls(
            task_id=_id(value.get("task_id"), "task_id", task=True),
            origin=origin,
            created_at=_timestamp(value.get("created_at"), "created_at"),
            revision=_positive_revision(value.get("revision")),
            merged_into_task_id=_id(merged, "merged_into_task_id", task=True) if merged is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    revision: int
    objective: str
    workspace_id: str
    permission_envelope: dict[str, Any]
    budget_policy_ids: tuple[str, ...]
    success_checks: tuple[str, ...]
    created_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskContract":
        _exact_keys(
            value,
            (
                "task_id",
                "revision",
                "objective",
                "workspace_id",
                "permission_envelope",
                "budget_policy_ids",
                "success_checks",
                "created_at",
            ),
            "task contract",
        )
        return cls(
            task_id=_id(value.get("task_id"), "task_id", task=True),
            revision=_positive_revision(value.get("revision")),
            objective=_text(value.get("objective"), "objective", maximum=4000) or "",
            workspace_id=_id(value.get("workspace_id"), "workspace_id"),
            permission_envelope=_mapping(value.get("permission_envelope"), "permission_envelope"),
            budget_policy_ids=_string_tuple(value.get("budget_policy_ids") or (), "budget_policy_ids"),
            success_checks=_string_tuple(value.get("success_checks") or (), "success_checks"),
            created_at=_timestamp(value.get("created_at"), "created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["budget_policy_ids"] = list(self.budget_policy_ids)
        value["success_checks"] = list(self.success_checks)
        return value


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display_name: str
    adapter: str
    execution_backend: str
    argv_template: tuple[str, ...]
    capabilities: tuple[str, ...]
    enabled: bool
    revision: int
    updated_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentSpec":
        _exact_keys(
            value,
            (
                "agent_id",
                "display_name",
                "adapter",
                "execution_backend",
                "argv_template",
                "capabilities",
                "enabled",
                "revision",
                "updated_at",
            ),
            "agent spec",
        )
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ControlPlaneError("agent enabled must be boolean")
        return cls(
            agent_id=_id(value.get("agent_id"), "agent_id"),
            display_name=_text(value.get("display_name"), "display_name", maximum=160) or "",
            adapter=_id(value.get("adapter"), "adapter"),
            execution_backend=_id(value.get("execution_backend"), "execution_backend"),
            argv_template=_argv(value.get("argv_template") or ()),
            capabilities=_string_tuple(value.get("capabilities") or (), "capabilities"),
            enabled=enabled,
            revision=_positive_revision(value.get("revision")),
            updated_at=_timestamp(value.get("updated_at"), "updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["argv_template"] = list(self.argv_template)
        value["capabilities"] = list(self.capabilities)
        return value


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    canonical_root: str
    store_dir: str | None
    enabled: bool
    revision: int
    updated_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Workspace":
        _exact_keys(
            value,
            ("workspace_id", "canonical_root", "store_dir", "enabled", "revision", "updated_at"),
            "workspace",
        )
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ControlPlaneError("workspace enabled must be boolean")
        canonical_root = _text(value.get("canonical_root"), "canonical_root", maximum=4096) or ""
        if not Path(canonical_root).is_absolute():
            raise ControlPlaneError("workspace canonical_root must be absolute")
        store_dir = _text(value.get("store_dir"), "store_dir", maximum=4096, optional=True)
        if store_dir is not None and not Path(store_dir).is_absolute():
            raise ControlPlaneError("workspace store_dir must be absolute")
        return cls(
            workspace_id=_id(value.get("workspace_id"), "workspace_id"),
            canonical_root=canonical_root,
            store_dir=store_dir,
            enabled=enabled,
            revision=_positive_revision(value.get("revision")),
            updated_at=_timestamp(value.get("updated_at"), "updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunAttempt:
    attempt_id: str
    task_id: str
    contract_revision: int
    agent_id: str
    agent_revision: int
    workspace_id: str
    execution_state: str
    outcome_state: str
    control_state: str
    manifest_id: str | None
    pid: int | None
    process_group_id: int | None
    process_birth_time: float | None
    process_executable: str | None
    process_cwd: str | None
    ownership_nonce_hash: str | None
    started_at: float | None
    ended_at: float | None
    exit_code: int | None
    revision: int
    reason: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunAttempt":
        _exact_keys(
            value,
            (
                "attempt_id",
                "task_id",
                "contract_revision",
                "agent_id",
                "agent_revision",
                "workspace_id",
                "execution_state",
                "outcome_state",
                "control_state",
                "manifest_id",
                "pid",
                "process_group_id",
                "process_birth_time",
                "process_executable",
                "process_cwd",
                "ownership_nonce_hash",
                "started_at",
                "ended_at",
                "exit_code",
                "revision",
                "reason",
            ),
            "run attempt",
        )
        execution = str(value.get("execution_state") or "")
        outcome = str(value.get("outcome_state") or "")
        control = str(value.get("control_state") or "")
        if execution not in EXECUTION_STATES or outcome not in OUTCOME_STATES or control not in CONTROL_STATES:
            raise ControlPlaneError("attempt contains an invalid state")
        optional_ints: dict[str, int | None] = {}
        for name in ("pid", "process_group_id", "exit_code"):
            raw = value.get(name)
            if raw is None:
                optional_ints[name] = None
            elif isinstance(raw, bool) or not isinstance(raw, int):
                raise ControlPlaneError(f"{name} must be an integer or null")
            else:
                optional_ints[name] = raw
        for name in ("pid", "process_group_id"):
            if optional_ints[name] is not None and optional_ints[name] <= 0:
                raise ControlPlaneError(f"{name} must be positive when present")
        optional_times = {
            name: (_timestamp(value.get(name), name) if value.get(name) is not None else None)
            for name in ("process_birth_time", "started_at", "ended_at")
        }
        nonce_hash = value.get("ownership_nonce_hash")
        if nonce_hash is not None and (not isinstance(nonce_hash, str) or not _HASH.fullmatch(nonce_hash)):
            raise ControlPlaneError("ownership_nonce_hash is invalid")
        if optional_times["process_birth_time"] is not None and optional_times["process_birth_time"] <= 0:
            raise ControlPlaneError("process_birth_time must be positive when present")
        manifest_id = value.get("manifest_id")
        executable = _text(value.get("process_executable"), "process_executable", maximum=4096, optional=True)
        process_cwd = _text(value.get("process_cwd"), "process_cwd", maximum=4096, optional=True)
        if executable is not None and not Path(executable).is_absolute():
            raise ControlPlaneError("process_executable must be absolute")
        if process_cwd is not None and not Path(process_cwd).is_absolute():
            raise ControlPlaneError("process_cwd must be absolute")
        return cls(
            attempt_id=_id(value.get("attempt_id"), "attempt_id"),
            task_id=_id(value.get("task_id"), "task_id", task=True),
            contract_revision=_positive_revision(value.get("contract_revision"), "contract_revision"),
            agent_id=_id(value.get("agent_id"), "agent_id"),
            agent_revision=_positive_revision(value.get("agent_revision"), "agent_revision"),
            workspace_id=_id(value.get("workspace_id"), "workspace_id"),
            execution_state=execution,
            outcome_state=outcome,
            control_state=control,
            manifest_id=_id(manifest_id, "manifest_id") if manifest_id is not None else None,
            pid=optional_ints["pid"],
            process_group_id=optional_ints["process_group_id"],
            process_birth_time=optional_times["process_birth_time"],
            process_executable=executable,
            process_cwd=process_cwd,
            ownership_nonce_hash=nonce_hash,
            started_at=optional_times["started_at"],
            ended_at=optional_times["ended_at"],
            exit_code=optional_ints["exit_code"],
            revision=_positive_revision(value.get("revision")),
            reason=_text(value.get("reason"), "reason", maximum=1000, optional=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_complete_process_proof(self) -> bool:
        return all(
            value is not None and value != ""
            for value in (
                self.pid,
                self.process_group_id,
                self.process_birth_time,
                self.process_executable,
                self.process_cwd,
                self.ownership_nonce_hash,
                self.manifest_id,
            )
        )


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    task_id: str
    attempt_id: str | None
    kind: str
    requested_action: str
    state: str
    requested_by: str
    decided_by: str | None
    expires_at: float
    decided_at: float | None
    consumed_at: float | None
    revision: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalRequest":
        _exact_keys(
            value,
            (
                "approval_id",
                "task_id",
                "attempt_id",
                "kind",
                "requested_action",
                "state",
                "requested_by",
                "decided_by",
                "expires_at",
                "decided_at",
                "consumed_at",
                "revision",
            ),
            "approval request",
        )
        state = str(value.get("state") or "")
        if state not in APPROVAL_STATES:
            raise ControlPlaneError("approval state is invalid")
        attempt_id = value.get("attempt_id")
        return cls(
            approval_id=_id(value.get("approval_id"), "approval_id"),
            task_id=_id(value.get("task_id"), "task_id", task=True),
            attempt_id=_id(attempt_id, "attempt_id") if attempt_id is not None else None,
            kind=_id(value.get("kind"), "kind"),
            requested_action=_id(value.get("requested_action"), "requested_action"),
            state=state,
            requested_by=_text(value.get("requested_by"), "requested_by", maximum=120) or "",
            decided_by=_text(value.get("decided_by"), "decided_by", maximum=120, optional=True),
            expires_at=_timestamp(value.get("expires_at"), "expires_at"),
            decided_at=_timestamp(value.get("decided_at"), "decided_at") if value.get("decided_at") is not None else None,
            consumed_at=_timestamp(value.get("consumed_at"), "consumed_at") if value.get("consumed_at") is not None else None,
            revision=_positive_revision(value.get("revision")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetPolicyRecord:
    policy_id: str
    scope: str
    metric: str
    limit: float
    basis: str
    action: str
    enabled: bool
    revision: int
    updated_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BudgetPolicyRecord":
        _exact_keys(
            value,
            ("policy_id", "scope", "metric", "limit", "basis", "action", "enabled", "revision", "updated_at"),
            "budget policy",
        )
        limit = value.get("limit")
        if isinstance(limit, bool):
            raise ControlPlaneError("budget limit must be positive and finite")
        try:
            limit_number = float(limit)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ControlPlaneError("budget limit must be positive and finite") from exc
        if not math.isfinite(limit_number) or limit_number <= 0:
            raise ControlPlaneError("budget limit must be positive and finite")
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ControlPlaneError("budget enabled must be boolean")
        return cls(
            policy_id=_id(value.get("policy_id"), "policy_id"),
            scope=_id(value.get("scope"), "scope"),
            metric=_id(value.get("metric"), "metric"),
            limit=limit_number,
            basis=_id(value.get("basis"), "basis"),
            action=_id(value.get("action"), "action"),
            enabled=enabled,
            revision=_positive_revision(value.get("revision")),
            updated_at=_timestamp(value.get("updated_at"), "updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScheduleSpec:
    schedule_id: str
    task_id: str
    cadence: str
    interval_seconds: float | None
    next_run_at: float | None
    enabled: bool
    claim_count: int
    last_claimed_at: float | None
    revision: int
    updated_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScheduleSpec":
        _exact_keys(
            value,
            (
                "schedule_id",
                "task_id",
                "cadence",
                "interval_seconds",
                "next_run_at",
                "enabled",
                "claim_count",
                "last_claimed_at",
                "revision",
                "updated_at",
            ),
            "schedule",
        )
        cadence = str(value.get("cadence") or "")
        if cadence not in {"one_shot", "fixed"}:
            raise ControlPlaneError("schedule cadence must be one_shot or fixed")
        interval = value.get("interval_seconds")
        try:
            interval_number = float(interval) if interval is not None else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise ControlPlaneError("schedule interval_seconds must be numeric") from exc
        if cadence == "fixed" and (interval_number is None or not math.isfinite(interval_number) or interval_number < 1):
            raise ControlPlaneError("fixed schedule interval_seconds must be >= 1")
        if cadence == "one_shot" and interval_number is not None:
            raise ControlPlaneError("one_shot schedule cannot have interval_seconds")
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ControlPlaneError("schedule enabled must be boolean")
        return cls(
            schedule_id=_id(value.get("schedule_id"), "schedule_id"),
            task_id=_id(value.get("task_id"), "task_id", task=True),
            cadence=cadence,
            interval_seconds=interval_number,
            next_run_at=_timestamp(value.get("next_run_at"), "next_run_at") if value.get("next_run_at") is not None else None,
            enabled=enabled,
            claim_count=_revision(value.get("claim_count"), "claim_count"),
            last_claimed_at=_timestamp(value.get("last_claimed_at"), "last_claimed_at") if value.get("last_claimed_at") is not None else None,
            revision=_positive_revision(value.get("revision")),
            updated_at=_timestamp(value.get("updated_at"), "updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControlEvent:
    event_id: str
    occurred_at: float
    actor_kind: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    expected_revision: int
    prior_state: str | None
    next_state: str | None
    request_id: str | None
    causal_parent_id: str | None
    idempotency_key: str
    operation_digest: str
    payload: dict[str, Any]
    record_hash: str

    def _content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_EVENT_SCHEMA_VERSION,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "expected_revision": self.expected_revision,
            "prior_state": self.prior_state,
            "next_state": self.next_state,
            "request_id": self.request_id,
            "causal_parent_id": self.causal_parent_id,
            "idempotency_key": self.idempotency_key,
            "operation_digest": self.operation_digest,
            "payload": self.payload,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "record_hash": self.record_hash}

    @classmethod
    def create(
        cls,
        *,
        actor_kind: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        expected_revision: int,
        prior_state: str | None,
        next_state: str | None,
        request_id: str | None,
        causal_parent_id: str | None,
        idempotency_key: str,
        operation_digest: str,
        payload: Mapping[str, Any],
    ) -> "ControlEvent":
        event = cls(
            event_id=f"ctl_{uuid.uuid4().hex}",
            occurred_at=_now(),
            actor_kind=_id(actor_kind, "actor_kind"),
            actor_id=_text(actor_id, "actor_id", maximum=120) or "",
            action=_id(action, "action"),
            target_type=_id(target_type, "target_type"),
            target_id=_text(target_id, "target_id", maximum=128) or "",
            expected_revision=_revision(expected_revision, "expected_revision"),
            prior_state=_text(prior_state, "prior_state", maximum=80, optional=True),
            next_state=_text(next_state, "next_state", maximum=80, optional=True),
            request_id=_text(request_id, "request_id", maximum=240, optional=True),
            causal_parent_id=_text(causal_parent_id, "causal_parent_id", maximum=128, optional=True),
            idempotency_key=idempotency_key,
            operation_digest=operation_digest,
            payload=_mapping(payload, "payload"),
            record_hash="",
        )
        return replace(event, record_hash=_digest(event._content_dict()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ControlEvent":
        _exact_keys(
            value,
            (
                "schema_version",
                "event_id",
                "occurred_at",
                "actor_kind",
                "actor_id",
                "action",
                "target_type",
                "target_id",
                "expected_revision",
                "prior_state",
                "next_state",
                "request_id",
                "causal_parent_id",
                "idempotency_key",
                "operation_digest",
                "payload",
                "record_hash",
            ),
            "control event",
        )
        if value.get("schema_version") != CONTROL_EVENT_SCHEMA_VERSION:
            raise ControlPlaneError("unsupported control event schema")
        event_id = value.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise ControlPlaneError("control event_id is invalid")
        key = value.get("idempotency_key")
        if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
            raise ControlPlaneError("control idempotency_key is invalid")
        operation_digest = value.get("operation_digest")
        record_hash = value.get("record_hash")
        if not isinstance(operation_digest, str) or not _HASH.fullmatch(operation_digest):
            raise ControlPlaneError("control operation_digest is invalid")
        if not isinstance(record_hash, str) or not _HASH.fullmatch(record_hash):
            raise ControlPlaneError("control record_hash is invalid")
        event = cls(
            event_id=event_id,
            occurred_at=_timestamp(value.get("occurred_at"), "occurred_at"),
            actor_kind=_id(value.get("actor_kind"), "actor_kind"),
            actor_id=_text(value.get("actor_id"), "actor_id", maximum=120) or "",
            action=_id(value.get("action"), "action"),
            target_type=_id(value.get("target_type"), "target_type"),
            target_id=_text(value.get("target_id"), "target_id", maximum=128) or "",
            expected_revision=_revision(value.get("expected_revision"), "expected_revision"),
            prior_state=_text(value.get("prior_state"), "prior_state", maximum=80, optional=True),
            next_state=_text(value.get("next_state"), "next_state", maximum=80, optional=True),
            request_id=_text(value.get("request_id"), "request_id", maximum=240, optional=True),
            causal_parent_id=_text(value.get("causal_parent_id"), "causal_parent_id", maximum=128, optional=True),
            idempotency_key=key,
            operation_digest=operation_digest,
            payload=_mapping(value.get("payload"), "payload"),
            record_hash=record_hash,
        )
        if _digest(event._content_dict()) != event.record_hash:
            raise ControlPlaneError("control event record_hash mismatch")
        return event


@dataclass(frozen=True)
class ProjectionIssue:
    line_number: int
    code: str
    message: str
    event_id: str | None = None


@dataclass
class ControlProjection:
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    contracts: dict[str, TaskContract] = field(default_factory=dict)
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    attempts: dict[str, RunAttempt] = field(default_factory=dict)
    approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    budget_policies: dict[str, BudgetPolicyRecord] = field(default_factory=dict)
    schedules: dict[str, ScheduleSpec] = field(default_factory=dict)
    events: list[ControlEvent] = field(default_factory=list)
    idempotency: dict[str, ControlEvent] = field(default_factory=dict)
    issues: list[ProjectionIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_PROJECTION_SCHEMA_VERSION,
            "tasks": [item.to_dict() for item in self.tasks.values()],
            "contracts": [item.to_dict() for item in self.contracts.values()],
            "agents": [item.to_dict() for item in self.agents.values()],
            "workspaces": [item.to_dict() for item in self.workspaces.values()],
            "attempts": [item.to_dict() for item in self.attempts.values()],
            "approvals": [item.to_dict() for item in self.approvals.values()],
            "budget_policies": [item.to_dict() for item in self.budget_policies.values()],
            "schedules": [item.to_dict() for item in self.schedules.values()],
            "events": [item.to_dict() for item in self.events],
            "issues": [asdict(item) for item in self.issues],
        }


def _same_or_transition(current: str, next_state: str, transitions: Mapping[str, frozenset[str]], name: str) -> None:
    if next_state != current and next_state not in transitions[current]:
        raise InvalidTransition(f"invalid {name} transition: {current} -> {next_state}")


def _require_revision(current: int, expected: int, target: str) -> None:
    if current != expected:
        raise RevisionConflict(f"{target} revision is {current}, expected {expected}")


def _record_payload(event: ControlEvent) -> Mapping[str, Any]:
    value = event.payload.get("record")
    if not isinstance(value, Mapping):
        raise ControlPlaneError("control event has no record payload")
    return value


def _require_event_target(event: ControlEvent, target_type: str, target_id: str) -> None:
    if event.target_type != target_type or event.target_id != target_id:
        raise ControlPlaneError("control event target does not match its record payload")


def _apply_event(projection: ControlProjection, event: ControlEvent) -> None:
    previous_idempotency = projection.idempotency.get(event.idempotency_key)
    if previous_idempotency is not None:
        if previous_idempotency.event_id == event.event_id and previous_idempotency.record_hash == event.record_hash:
            return
        raise IdempotencyConflict("duplicate control idempotency key in action log")

    action = event.action
    if action == "schedule_claimed":
        expected_payload_keys = ("record", "claimed_for")
    elif action == "approval_resolved":
        expected_payload_keys = ("approval_record", "attempt_record", "expected_attempt_revision")
    else:
        expected_payload_keys = ("record",)
    _exact_keys(event.payload, expected_payload_keys, "control event payload")
    if action == "task_created":
        record = TaskRecord.from_dict(_record_payload(event))
        _require_event_target(event, "task", record.task_id)
        if event.expected_revision != 0 or record.revision != 1 or record.task_id in projection.tasks:
            raise RevisionConflict("task create requires an absent task at revision 0")
        projection.tasks[record.task_id] = record
    elif action == "task_merged":
        record = TaskRecord.from_dict(_record_payload(event))
        _require_event_target(event, "task", record.task_id)
        current = projection.tasks.get(record.task_id)
        if current is None:
            raise RecordNotFound("task does not exist")
        if record.merged_into_task_id == record.task_id:
            raise InvalidTransition("task cannot be merged into itself")
        target = projection.tasks.get(record.merged_into_task_id or "")
        if target is None or target.merged_into_task_id is not None:
            raise ControlPlaneError("merge target must be an active task")
        _require_revision(current.revision, event.expected_revision, record.task_id)
        if current.merged_into_task_id is not None or record.revision != current.revision + 1:
            raise InvalidTransition("task is already merged or merge revision is invalid")
        cursor = target
        seen = {record.task_id}
        while cursor.merged_into_task_id is not None:
            if cursor.task_id in seen:
                raise InvalidTransition("task merge would create a cycle")
            seen.add(cursor.task_id)
            cursor = projection.tasks[cursor.merged_into_task_id]
        projection.tasks[record.task_id] = record
    elif action == "contract_created":
        record = TaskContract.from_dict(_record_payload(event))
        _require_event_target(event, "task_contract", record.task_id)
        task = projection.tasks.get(record.task_id)
        if task is None or task.merged_into_task_id is not None:
            raise RecordNotFound("active task does not exist")
        if record.workspace_id not in projection.workspaces:
            raise RecordNotFound("workspace does not exist")
        current = projection.contracts.get(record.task_id)
        current_revision = current.revision if current else 0
        _require_revision(current_revision, event.expected_revision, f"contract for {record.task_id}")
        if record.revision != current_revision + 1:
            raise RevisionConflict("contract revision must increase by one")
        missing_policies = [policy_id for policy_id in record.budget_policy_ids if policy_id not in projection.budget_policies]
        if missing_policies:
            raise RecordNotFound("contract references an unknown budget policy")
        projection.contracts[record.task_id] = record
    elif action == "agent_registered":
        record = AgentSpec.from_dict(_record_payload(event))
        _require_event_target(event, "agent", record.agent_id)
        current = projection.agents.get(record.agent_id)
        current_revision = current.revision if current else 0
        _require_revision(current_revision, event.expected_revision, record.agent_id)
        if record.revision != current_revision + 1:
            raise RevisionConflict("agent revision must increase by one")
        projection.agents[record.agent_id] = record
    elif action == "workspace_registered":
        record = Workspace.from_dict(_record_payload(event))
        _require_event_target(event, "workspace", record.workspace_id)
        current = projection.workspaces.get(record.workspace_id)
        current_revision = current.revision if current else 0
        _require_revision(current_revision, event.expected_revision, record.workspace_id)
        if record.revision != current_revision + 1:
            raise RevisionConflict("workspace revision must increase by one")
        if current is not None and (record.canonical_root, record.store_dir) != (
            current.canonical_root,
            current.store_dir,
        ):
            raise InvalidTransition("workspace identity is immutable; register a new workspace_id")
        projection.workspaces[record.workspace_id] = record
    elif action == "attempt_created":
        record = RunAttempt.from_dict(_record_payload(event))
        _require_event_target(event, "attempt", record.attempt_id)
        if event.expected_revision != 0 or record.revision != 1 or record.attempt_id in projection.attempts:
            raise RevisionConflict("attempt create requires an absent attempt at revision 0")
        task = projection.tasks.get(record.task_id)
        contract = projection.contracts.get(record.task_id)
        agent = projection.agents.get(record.agent_id)
        workspace = projection.workspaces.get(record.workspace_id)
        if task is None or task.merged_into_task_id is not None:
            raise RecordNotFound("active task does not exist")
        if contract is None or contract.revision != record.contract_revision:
            raise RevisionConflict("attempt must freeze the current task contract revision")
        if agent is None or not agent.enabled:
            raise RecordNotFound("enabled agent does not exist")
        if agent.revision != record.agent_revision:
            raise RevisionConflict("attempt must freeze the current agent revision")
        if workspace is None or not workspace.enabled or contract.workspace_id != workspace.workspace_id:
            raise RecordNotFound("enabled contract workspace does not exist")
        allowed_initial_control = {"ready", "awaiting_approval"}
        if (
            record.execution_state != "pending"
            or record.outcome_state != "unknown"
            or record.control_state not in allowed_initial_control
        ):
            raise InvalidTransition("new attempt must begin pending/unknown and ready or awaiting_approval")
        if contract_requires_launch_approval(contract.permission_envelope) and record.control_state != "awaiting_approval":
            raise InvalidTransition("approval-required attempt must begin awaiting_approval")
        projection.attempts[record.attempt_id] = record
    elif action == "attempt_transitioned":
        record = RunAttempt.from_dict(_record_payload(event))
        _require_event_target(event, "attempt", record.attempt_id)
        current = projection.attempts.get(record.attempt_id)
        if current is None:
            raise RecordNotFound("attempt does not exist")
        _require_revision(current.revision, event.expected_revision, record.attempt_id)
        if record.revision != current.revision + 1:
            raise RevisionConflict("attempt revision must increase by one")
        if (
            record.task_id,
            record.contract_revision,
            record.agent_id,
            record.agent_revision,
            record.workspace_id,
        ) != (
            current.task_id,
            current.contract_revision,
            current.agent_id,
            current.agent_revision,
            current.workspace_id,
        ):
            raise InvalidTransition("attempt immutable identity cannot change")
        if current.manifest_id is not None and record.manifest_id != current.manifest_id:
            raise InvalidTransition("attempt manifest identity cannot change")
        if current.manifest_id is None and record.manifest_id is not None and not (
            current.execution_state == "pending" and record.execution_state == "launching"
        ):
            raise InvalidTransition("attempt manifest may only be frozen while launching")
        proof_fields = (
            "pid",
            "process_group_id",
            "process_birth_time",
            "process_executable",
            "process_cwd",
            "ownership_nonce_hash",
            "started_at",
        )
        current_proof = tuple(getattr(current, name) for name in proof_fields)
        record_proof = tuple(getattr(record, name) for name in proof_fields)
        if any(value is not None for value in current_proof):
            if record_proof != current_proof:
                raise InvalidTransition("attempt process ownership proof cannot change")
        elif any(value is not None for value in record_proof):
            if not (
                current.execution_state == "launching"
                and record.execution_state == "running"
                and all(value is not None for value in record_proof)
            ):
                raise InvalidTransition("attempt process ownership proof may only be frozen on running")
        _same_or_transition(current.execution_state, record.execution_state, _EXECUTION_TRANSITIONS, "execution")
        _same_or_transition(current.outcome_state, record.outcome_state, _OUTCOME_TRANSITIONS, "outcome")
        _same_or_transition(current.control_state, record.control_state, _CONTROL_TRANSITIONS, "control")
        if record.execution_state in {"launching", "running", "cancel_requested"} and not record.manifest_id:
            raise InvalidTransition("live attempt transition requires an immutable manifest")
        if record.execution_state in {"running", "cancel_requested"} and not record.has_complete_process_proof:
            raise InvalidTransition("live attempt transition requires complete process ownership proof")
        if record.execution_state == "running" and record.started_at is None:
            raise InvalidTransition("running attempt requires started_at")
        if record.execution_state in {"succeeded", "failed", "cancelled", "lost"} and record.ended_at is None:
            raise InvalidTransition("terminal attempt requires ended_at")
        if event.prior_state not in {None, current.execution_state} or event.next_state not in {None, record.execution_state}:
            raise InvalidTransition("attempt event state labels do not match its record")
        projection.attempts[record.attempt_id] = record
    elif action == "approval_requested":
        record = ApprovalRequest.from_dict(_record_payload(event))
        _require_event_target(event, "approval", record.approval_id)
        if event.expected_revision != 0 or record.revision != 1 or record.approval_id in projection.approvals:
            raise RevisionConflict("approval create requires an absent approval at revision 0")
        if record.task_id not in projection.tasks or (record.attempt_id and record.attempt_id not in projection.attempts):
            raise RecordNotFound("approval target does not exist")
        if record.state != "pending":
            raise InvalidTransition("new approval must be pending")
        if any(value is not None for value in (record.decided_by, record.decided_at, record.consumed_at)):
            raise InvalidTransition("new approval cannot contain decision or consumption data")
        if record.expires_at <= event.occurred_at:
            raise InvalidTransition("new approval must expire after it is requested")
        projection.approvals[record.approval_id] = record
    elif action == "approval_resolved":
        approval_payload = event.payload.get("approval_record")
        attempt_payload = event.payload.get("attempt_record")
        if not isinstance(approval_payload, Mapping) or not isinstance(attempt_payload, Mapping):
            raise ControlPlaneError("approval resolution must contain approval and attempt records")
        record = ApprovalRequest.from_dict(approval_payload)
        attempt_record = RunAttempt.from_dict(attempt_payload)
        _require_event_target(event, "approval", record.approval_id)
        current = projection.approvals.get(record.approval_id)
        if current is None:
            raise RecordNotFound("approval does not exist")
        _require_revision(current.revision, event.expected_revision, record.approval_id)
        if current.state != "pending" or record.revision != current.revision + 1:
            raise InvalidTransition("linked approval resolution requires one pending approval revision")
        if (
            record.task_id,
            record.attempt_id,
            record.kind,
            record.requested_action,
            record.requested_by,
            record.expires_at,
        ) != (
            current.task_id,
            current.attempt_id,
            current.kind,
            current.requested_action,
            current.requested_by,
            current.expires_at,
        ):
            raise InvalidTransition("approval request identity cannot change")
        if current.attempt_id is None or record.attempt_id != current.attempt_id:
            raise InvalidTransition("linked approval resolution requires an attempt-scoped approval")
        if current.requested_action != "launch":
            raise InvalidTransition("only a launch approval may release an attempt")
        if not record.decided_by or record.decided_at is None or record.decided_at >= record.expires_at:
            raise InvalidTransition("approval resolution must freeze a valid decision")
        if current.decided_by is not None or current.decided_at is not None or current.consumed_at is not None:
            raise InvalidTransition("approval decision history is invalid")

        current_attempt = projection.attempts.get(current.attempt_id)
        if current_attempt is None:
            raise RecordNotFound("approval attempt does not exist")
        expected_attempt_revision = _positive_revision(
            event.payload.get("expected_attempt_revision"),
            "expected_attempt_revision",
        )
        _require_revision(current_attempt.revision, expected_attempt_revision, current_attempt.attempt_id)
        if attempt_record.revision != current_attempt.revision + 1:
            raise RevisionConflict("approval attempt revision must increase by one")
        if attempt_record.attempt_id != current_attempt.attempt_id or attempt_record.task_id != current.task_id:
            raise InvalidTransition("approval and attempt identities do not match")
        unchanged_attempt_fields = (
            "contract_revision",
            "agent_id",
            "agent_revision",
            "workspace_id",
            "execution_state",
            "outcome_state",
            "manifest_id",
            "pid",
            "process_group_id",
            "process_birth_time",
            "process_executable",
            "process_cwd",
            "ownership_nonce_hash",
            "started_at",
            "ended_at",
            "exit_code",
        )
        if any(
            getattr(attempt_record, name) != getattr(current_attempt, name)
            for name in unchanged_attempt_fields
        ):
            raise InvalidTransition("approval resolution may only change attempt control state")
        if current_attempt.execution_state != "pending" or current_attempt.control_state != "awaiting_approval":
            raise InvalidTransition("approval attempt must be pending and awaiting approval")

        if record.state == "consumed":
            if (
                record.consumed_at is None
                or record.consumed_at < record.decided_at
                or record.consumed_at >= record.expires_at
                or attempt_record.control_state != "ready"
            ):
                raise InvalidTransition("approved resolution must consume once and release the attempt")
        elif record.state == "rejected":
            if record.consumed_at is not None or attempt_record.control_state != "policy_hold":
                raise InvalidTransition("rejected resolution must keep the attempt held")
        else:
            raise InvalidTransition("linked approval resolution must consume or reject")
        _same_or_transition(
            current_attempt.control_state,
            attempt_record.control_state,
            _CONTROL_TRANSITIONS,
            "control",
        )
        if event.prior_state not in {None, current.state} or event.next_state not in {None, record.state}:
            raise InvalidTransition("approval event state labels do not match its record")
        projection.approvals[record.approval_id] = record
        projection.attempts[attempt_record.attempt_id] = attempt_record
    elif action in {"approval_decided", "approval_consumed", "approval_expired", "approval_cancelled"}:
        record = ApprovalRequest.from_dict(_record_payload(event))
        _require_event_target(event, "approval", record.approval_id)
        current = projection.approvals.get(record.approval_id)
        if current is None:
            raise RecordNotFound("approval does not exist")
        _require_revision(current.revision, event.expected_revision, record.approval_id)
        if record.revision != current.revision + 1:
            raise RevisionConflict("approval revision must increase by one")
        if (
            record.task_id,
            record.attempt_id,
            record.kind,
            record.requested_action,
            record.requested_by,
            record.expires_at,
        ) != (
            current.task_id,
            current.attempt_id,
            current.kind,
            current.requested_action,
            current.requested_by,
            current.expires_at,
        ):
            raise InvalidTransition("approval request identity cannot change")
        allowed = {
            "pending": {"approved", "rejected", "expired", "cancelled"},
            "approved": {"consumed", "expired", "cancelled"},
        }.get(current.state, set())
        if record.state not in allowed:
            raise InvalidTransition(f"invalid approval transition: {current.state} -> {record.state}")
        if action == "approval_decided":
            if record.state not in {"approved", "rejected"} or not record.decided_by or record.decided_at is None:
                raise InvalidTransition("approval decision must freeze its actor and timestamp")
            if record.decided_at >= record.expires_at:
                raise InvalidTransition("expired approval cannot be decided")
            if current.decided_by is not None or current.decided_at is not None or record.consumed_at is not None:
                raise InvalidTransition("approval decision history is invalid")
        else:
            if (record.decided_by, record.decided_at) != (current.decided_by, current.decided_at):
                raise InvalidTransition("approval decision identity cannot change")
            if action == "approval_consumed":
                if record.state != "consumed" or record.consumed_at is None:
                    raise InvalidTransition("approval consumption must freeze its timestamp")
                if record.consumed_at >= record.expires_at:
                    raise InvalidTransition("expired approval cannot be consumed")
            elif record.consumed_at != current.consumed_at:
                raise InvalidTransition("approval consumption history cannot change")
            if action == "approval_expired" and record.state != "expired":
                raise InvalidTransition("approval_expired event must expire the approval")
            if action == "approval_cancelled" and record.state != "cancelled":
                raise InvalidTransition("approval_cancelled event must cancel the approval")
        if event.prior_state not in {None, current.state} or event.next_state not in {None, record.state}:
            raise InvalidTransition("approval event state labels do not match its record")
        projection.approvals[record.approval_id] = record
    elif action == "budget_registered":
        record = BudgetPolicyRecord.from_dict(_record_payload(event))
        _require_event_target(event, "budget_policy", record.policy_id)
        current = projection.budget_policies.get(record.policy_id)
        current_revision = current.revision if current else 0
        _require_revision(current_revision, event.expected_revision, record.policy_id)
        if record.revision != current_revision + 1:
            raise RevisionConflict("budget policy revision must increase by one")
        if record.action in {"cancel", "block"} and record.basis not in {
            "provider_billed",
            "conservative_approved",
        }:
            raise ControlPlaneError("hard budget action requires provider_billed or conservative_approved basis")
        projection.budget_policies[record.policy_id] = record
    elif action in {"schedule_registered", "schedule_updated", "schedule_claimed"}:
        record = ScheduleSpec.from_dict(_record_payload(event))
        _require_event_target(event, "schedule", record.schedule_id)
        current = projection.schedules.get(record.schedule_id)
        current_revision = current.revision if current else 0
        _require_revision(current_revision, event.expected_revision, record.schedule_id)
        if record.revision != current_revision + 1:
            raise RevisionConflict("schedule revision must increase by one")
        if record.task_id not in projection.tasks:
            raise RecordNotFound("schedule task does not exist")
        if action == "schedule_registered" and current is not None:
            raise InvalidTransition("schedule already exists")
        if action == "schedule_registered" and (record.claim_count != 0 or record.last_claimed_at is not None):
            raise InvalidTransition("new schedule cannot contain claim history")
        if current is not None and (record.task_id, record.cadence, record.interval_seconds) != (
            current.task_id,
            current.cadence,
            current.interval_seconds,
        ):
            raise InvalidTransition("schedule identity and cadence cannot change")
        if action == "schedule_updated" and current is not None and (
            record.claim_count,
            record.last_claimed_at,
        ) != (
            current.claim_count,
            current.last_claimed_at,
        ):
            raise InvalidTransition("schedule update cannot rewrite claim history")
        if action == "schedule_claimed":
            if current is None or not current.enabled or current.next_run_at is None:
                raise InvalidTransition("schedule is not claimable")
            claimed_for = _timestamp(event.payload.get("claimed_for"), "claimed_for")
            if claimed_for != current.next_run_at:
                raise InvalidTransition("schedule claim does not match the due tick")
            if (
                record.claim_count != current.claim_count + 1
                or record.last_claimed_at is None
                or record.last_claimed_at < claimed_for
                or record.updated_at != record.last_claimed_at
            ):
                raise InvalidTransition("schedule claim counters are invalid")
            if current.cadence == "one_shot":
                if record.enabled or record.next_run_at is not None:
                    raise InvalidTransition("one-shot schedule must disable after its claim")
            else:
                assert current.interval_seconds is not None
                elapsed = max(0.0, record.last_claimed_at - claimed_for)
                intervals = int(elapsed // current.interval_seconds) + 1
                expected_next_run = claimed_for + intervals * current.interval_seconds
                if not record.enabled or record.next_run_at != expected_next_run:
                    raise InvalidTransition("fixed schedule claim did not coalesce to the next future tick")
        projection.schedules[record.schedule_id] = record
    else:
        raise ControlPlaneError(f"unsupported control action: {action}")

    projection.events.append(event)
    projection.idempotency[event.idempotency_key] = event


class ControlStore:
    """Append-only control-event store with a deterministic in-memory projection."""

    def __init__(self, root: Path | str) -> None:
        if root is None:
            raise ValueError("control store root is required")
        self.root = Path(root).expanduser()
        self.control_root = self.root / CONTROL_STORE_DIRNAME
        self.actions_path = self.control_root / CONTROL_ACTIONS_FILENAME
        self.lock_path = self.control_root / ".actions.lock"
        self.manifests_root = self.control_root / "manifests"

    def _ensure_root(self) -> None:
        self.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.control_root, 0o700)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self._ensure_root()
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_unlocked(self, event: ControlEvent) -> None:
        serialized = _canonical_json_bytes(event.to_dict()) + b"\n"
        existed = self.actions_path.exists()
        with self.actions_path.open("a+b") as handle:
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                pass
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.seek(0, os.SEEK_END)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            self._fsync_directory(self.control_root)

    def _project_unlocked(self) -> ControlProjection:
        projection = ControlProjection()
        if not self.actions_path.exists():
            return projection
        try:
            lines = self.actions_path.read_bytes().splitlines()
        except FileNotFoundError:
            return projection
        for line_number, raw in enumerate(lines, start=1):
            if not raw.strip():
                continue
            event_id: str | None = None
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, Mapping) and isinstance(decoded.get("event_id"), str):
                    event_id = decoded["event_id"]
                event = ControlEvent.from_dict(decoded)
                _apply_event(projection, event)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                projection.issues.append(
                    ProjectionIssue(
                        line_number=line_number,
                        code="invalid_record",
                        message=f"{type(exc).__name__}: {exc}"[:1000],
                        event_id=event_id,
                    )
                )
        return projection

    def project(self) -> ControlProjection:
        if not self.actions_path.exists():
            return ControlProjection()
        with self._locked(exclusive=False):
            return self._project_unlocked()

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise ControlPlaneError("idempotency_key must contain 1-240 safe characters")
        return value

    def _mutate(
        self,
        *,
        action: str,
        target_type: str,
        target_id: str,
        expected_revision: int,
        payload: Mapping[str, Any],
        idempotency_key: str,
        actor_kind: str,
        actor_id: str,
        prior_state: str | None = None,
        next_state: str | None = None,
        request_id: str | None = None,
        causal_parent_id: str | None = None,
        operation_payload: Mapping[str, Any] | None = None,
    ) -> tuple[ControlProjection, ControlEvent]:
        key = self._validate_idempotency_key(idempotency_key)
        expected = _revision(expected_revision, "expected_revision")
        intent = {
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "expected_revision": expected,
            "actor_kind": actor_kind,
            "actor_id": actor_id,
            "prior_state": prior_state,
            "next_state": next_state,
            "payload": dict(operation_payload if operation_payload is not None else payload),
        }
        operation_digest = _digest(intent)
        with self._locked(exclusive=True):
            projection = self._project_unlocked()
            previous = projection.idempotency.get(key)
            if previous is not None:
                if previous.operation_digest != operation_digest:
                    raise IdempotencyConflict("idempotency key was already used for a different control operation")
                return projection, previous
            event = ControlEvent.create(
                actor_kind=actor_kind,
                actor_id=actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                expected_revision=expected,
                prior_state=prior_state,
                next_state=next_state,
                request_id=request_id,
                causal_parent_id=causal_parent_id,
                idempotency_key=key,
                operation_digest=operation_digest,
                payload=payload,
            )
            _apply_event(projection, event)
            self._append_unlocked(event)
            return projection, event

    def _retry_projection(
        self,
        idempotency_key: str,
        action: str,
        *,
        operation_payload: Mapping[str, Any] | None = None,
        target_id: str | None = None,
        expected_revision: int | None = None,
        actor_kind: str | None = None,
        actor_id: str | None = None,
    ) -> ControlProjection | None:
        key = self._validate_idempotency_key(idempotency_key)
        projection = self.project()
        event = projection.idempotency.get(key)
        if event is None:
            return None
        if event.action != action:
            raise IdempotencyConflict("idempotency key was already used for a different control operation")
        if target_id is not None and event.target_id != target_id:
            raise IdempotencyConflict("idempotency key was already used for a different control target")
        if expected_revision is not None and event.expected_revision != expected_revision:
            raise IdempotencyConflict("idempotency key was already used at a different control revision")
        if actor_kind is not None and event.actor_kind != actor_kind:
            raise IdempotencyConflict("idempotency key was already used by a different actor kind")
        if actor_id is not None and event.actor_id != actor_id:
            raise IdempotencyConflict("idempotency key was already used by a different actor")
        if operation_payload is not None:
            intent = {
                "action": event.action,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "expected_revision": event.expected_revision,
                "actor_kind": event.actor_kind,
                "actor_id": event.actor_id,
                "prior_state": event.prior_state,
                "next_state": event.next_state,
                "payload": dict(operation_payload),
            }
            if _digest(intent) != event.operation_digest:
                raise IdempotencyConflict("idempotency key was already used for different control inputs")
        return projection

    @staticmethod
    def _opaque_id(prefix: str, idempotency_key: str) -> str:
        material = f"agent-chronicle:{prefix}:{idempotency_key}".encode("utf-8")
        return f"{prefix}_{hashlib.sha256(material).hexdigest()[:32]}"

    def create_task(
        self,
        *,
        origin: str,
        idempotency_key: str,
        task_id: str | None = None,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> TaskRecord:
        identifier = task_id or self._opaque_id("task", idempotency_key)
        operation = {"origin": origin, "task_id": identifier}
        retry = self._retry_projection(
            idempotency_key,
            "task_created",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=0,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return TaskRecord.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        record = TaskRecord.from_dict(
            {"task_id": identifier, "origin": origin, "created_at": _now(), "revision": 1, "merged_into_task_id": None}
        )
        projection, _ = self._mutate(
            action="task_created",
            target_type="task",
            target_id=record.task_id,
            expected_revision=0,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return projection.tasks[record.task_id]

    def merge_task(
        self,
        task_id: str,
        into_task_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> TaskRecord:
        identifier = _id(task_id, "task_id", task=True)
        target = _id(into_task_id, "into_task_id", task=True)
        if identifier == target:
            raise InvalidTransition("task cannot be merged into itself")
        operation = {"into_task_id": target}
        retry = self._retry_projection(
            idempotency_key,
            "task_merged",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return TaskRecord.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        projection = self.project()
        current = projection.tasks.get(identifier)
        if current is None:
            raise RecordNotFound("task does not exist")
        record = replace(current, revision=current.revision + 1, merged_into_task_id=target)
        updated, _ = self._mutate(
            action="task_merged",
            target_type="task",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state="active",
            next_state="merged",
            operation_payload=operation,
        )
        return updated.tasks[identifier]

    def register_workspace(
        self,
        root: Path | str,
        *,
        idempotency_key: str,
        workspace_id: str | None = None,
        store_dir: Path | str | None = None,
        enabled: bool = True,
        expected_revision: int = 0,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> Workspace:
        canonical = Path(root).expanduser().resolve(strict=True)
        if not canonical.is_dir():
            raise ControlPlaneError("workspace root must be an existing directory")
        identifier = workspace_id or self._opaque_id("workspace", idempotency_key)
        effective_store = str(Path(store_dir).expanduser().resolve()) if store_dir is not None else None
        operation = {
            "canonical_root": str(canonical),
            "store_dir": effective_store,
            "enabled": enabled,
        }
        retry = self._retry_projection(
            idempotency_key,
            "workspace_registered",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return Workspace.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        previous = self.project().workspaces.get(identifier)
        current_revision = previous.revision if previous else 0
        if current_revision != expected_revision:
            raise RevisionConflict(f"workspace revision is {current_revision}, expected {expected_revision}")
        record = Workspace.from_dict(
            {
                "workspace_id": identifier,
                "canonical_root": str(canonical),
                "store_dir": effective_store,
                "enabled": enabled,
                "revision": current_revision + 1,
                "updated_at": _now(),
            }
        )
        updated, _ = self._mutate(
            action="workspace_registered",
            target_type="workspace",
            target_id=record.workspace_id,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return updated.workspaces[record.workspace_id]

    def register_agent(
        self,
        agent_id: str,
        *,
        display_name: str,
        adapter: str,
        execution_backend: str,
        argv_template: Sequence[str],
        capabilities: Sequence[str] = (),
        enabled: bool = True,
        expected_revision: int = 0,
        idempotency_key: str,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> AgentSpec:
        identifier = _id(agent_id, "agent_id")
        operation = {
            "display_name": display_name,
            "adapter": adapter,
            "execution_backend": execution_backend,
            "argv_template": list(argv_template),
            "capabilities": list(capabilities),
            "enabled": enabled,
        }
        retry = self._retry_projection(
            idempotency_key,
            "agent_registered",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return AgentSpec.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        previous = self.project().agents.get(identifier)
        current_revision = previous.revision if previous else 0
        if current_revision != expected_revision:
            raise RevisionConflict(f"agent revision is {current_revision}, expected {expected_revision}")
        record = AgentSpec.from_dict(
            {
                "agent_id": identifier,
                "display_name": display_name,
                "adapter": adapter,
                "execution_backend": execution_backend,
                "argv_template": list(argv_template),
                "capabilities": list(capabilities),
                "enabled": enabled,
                "revision": current_revision + 1,
                "updated_at": _now(),
            }
        )
        updated, _ = self._mutate(
            action="agent_registered",
            target_type="agent",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return updated.agents[identifier]

    def register_budget_policy(
        self,
        policy_id: str,
        *,
        scope: str,
        metric: str,
        limit: float,
        basis: str,
        action: str,
        enabled: bool = True,
        expected_revision: int = 0,
        idempotency_key: str,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> BudgetPolicyRecord:
        identifier = _id(policy_id, "policy_id")
        operation = {
            "scope": scope,
            "metric": metric,
            "limit": limit,
            "basis": basis,
            "action": action,
            "enabled": enabled,
        }
        retry = self._retry_projection(
            idempotency_key,
            "budget_registered",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return BudgetPolicyRecord.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        previous = self.project().budget_policies.get(identifier)
        current_revision = previous.revision if previous else 0
        if current_revision != expected_revision:
            raise RevisionConflict(f"budget policy revision is {current_revision}, expected {expected_revision}")
        if action in {"cancel", "block"} and basis not in {"provider_billed", "conservative_approved"}:
            raise ControlPlaneError("hard budget action requires provider_billed or conservative_approved basis")
        record = BudgetPolicyRecord.from_dict(
            {
                "policy_id": identifier,
                "scope": scope,
                "metric": metric,
                "limit": limit,
                "basis": basis,
                "action": action,
                "enabled": enabled,
                "revision": current_revision + 1,
                "updated_at": _now(),
            }
        )
        updated, _ = self._mutate(
            action="budget_registered",
            target_type="budget_policy",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return updated.budget_policies[identifier]

    def create_contract(
        self,
        task_id: str,
        *,
        objective: str,
        workspace_id: str,
        permission_envelope: Mapping[str, Any] | None = None,
        budget_policy_ids: Sequence[str] = (),
        success_checks: Sequence[str] = (),
        expected_revision: int = 0,
        idempotency_key: str,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> TaskContract:
        identifier = _id(task_id, "task_id", task=True)
        operation = {
            "objective": objective,
            "workspace_id": workspace_id,
            "permission_envelope": dict(permission_envelope or {}),
            "budget_policy_ids": list(budget_policy_ids),
            "success_checks": list(success_checks),
        }
        retry = self._retry_projection(
            idempotency_key,
            "contract_created",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return TaskContract.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        record = TaskContract.from_dict(
            {
                "task_id": identifier,
                "revision": expected_revision + 1,
                "objective": objective,
                "workspace_id": workspace_id,
                "permission_envelope": dict(permission_envelope or {}),
                "budget_policy_ids": list(budget_policy_ids),
                "success_checks": list(success_checks),
                "created_at": _now(),
            }
        )
        updated, _ = self._mutate(
            action="contract_created",
            target_type="task_contract",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return updated.contracts[identifier]

    def create_attempt(
        self,
        task_id: str,
        *,
        agent_id: str,
        workspace_id: str,
        contract_revision: int,
        idempotency_key: str,
        attempt_id: str | None = None,
        initial_control_state: str | None = None,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> RunAttempt:
        identifier = attempt_id or self._opaque_id("attempt", idempotency_key)
        projection = self.project()
        previous = projection.idempotency.get(idempotency_key)
        if previous is not None:
            # A committed attempt freezes the contract revision it was born
            # with. Exact retries must remain replayable even when the task's
            # current contract has since advanced.
            retry_initial_state = initial_control_state
            retry_agent_revision: int | None = None
            if retry_initial_state is None and previous.action == "attempt_created":
                previous_attempt = RunAttempt.from_dict(_record_payload(previous))
                retry_initial_state = previous_attempt.control_state
                retry_agent_revision = previous_attempt.agent_revision
            elif previous.action == "attempt_created":
                retry_agent_revision = RunAttempt.from_dict(_record_payload(previous)).agent_revision
            retry_operation = {
                "task_id": task_id,
                "agent_id": agent_id,
                "agent_revision": retry_agent_revision,
                "workspace_id": workspace_id,
                "contract_revision": contract_revision,
                "attempt_id": identifier,
                "initial_control_state": retry_initial_state,
            }
            retry = self._retry_projection(
                idempotency_key,
                "attempt_created",
                operation_payload=retry_operation,
                target_id=identifier,
                expected_revision=0,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
            assert retry is not None
            return RunAttempt.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        contract = projection.contracts.get(task_id)
        if contract is None or contract.revision != contract_revision:
            raise RevisionConflict("attempt must freeze the current task contract revision")
        agent = projection.agents.get(agent_id)
        if agent is None or not agent.enabled:
            raise RecordNotFound("enabled agent does not exist")
        agent_revision = agent.revision
        required = contract_requires_launch_approval(contract.permission_envelope)
        if initial_control_state is None:
            initial_control_state = "awaiting_approval" if required else "ready"
        if initial_control_state not in {"ready", "awaiting_approval"}:
            raise ControlPlaneError("initial_control_state must be ready or awaiting_approval")
        if required and initial_control_state != "awaiting_approval":
            raise InvalidTransition("approval-required attempt must begin awaiting_approval")
        operation = {
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_revision": agent_revision,
            "workspace_id": workspace_id,
            "contract_revision": contract_revision,
            "attempt_id": identifier,
            "initial_control_state": initial_control_state,
        }
        record = RunAttempt.from_dict(
            {
                "attempt_id": identifier,
                "task_id": task_id,
                "contract_revision": contract_revision,
                "agent_id": agent_id,
                "agent_revision": agent_revision,
                "workspace_id": workspace_id,
                "execution_state": "pending",
                "outcome_state": "unknown",
                "control_state": initial_control_state,
                "manifest_id": None,
                "pid": None,
                "process_group_id": None,
                "process_birth_time": None,
                "process_executable": None,
                "process_cwd": None,
                "ownership_nonce_hash": None,
                "started_at": None,
                "ended_at": None,
                "exit_code": None,
                "revision": 1,
                "reason": None,
            }
        )
        updated, _ = self._mutate(
            action="attempt_created",
            target_type="attempt",
            target_id=identifier,
            expected_revision=0,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            next_state="pending",
            operation_payload=operation,
        )
        return updated.attempts[identifier]

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        execution_state: str | None = None,
        outcome_state: str | None = None,
        control_state: str | None = None,
        manifest_id: str | None = None,
        pid: int | None = None,
        process_group_id: int | None = None,
        process_birth_time: float | None = None,
        process_executable: str | None = None,
        process_cwd: str | None = None,
        ownership_nonce_hash: str | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        exit_code: int | None = None,
        reason: str | None = None,
        actor_kind: str = "supervisor",
        actor_id: str = "local-supervisor",
        request_id: str | None = None,
        causal_parent_id: str | None = None,
    ) -> RunAttempt:
        identifier = _id(attempt_id, "attempt_id")
        operation = {
            "execution_state": execution_state,
            "outcome_state": outcome_state,
            "control_state": control_state,
            "manifest_id": manifest_id,
            "pid": pid,
            "process_group_id": process_group_id,
            "process_birth_time": process_birth_time,
            "process_executable": process_executable,
            "process_cwd": process_cwd,
            "ownership_nonce_hash": ownership_nonce_hash,
            "started_at": started_at,
            "ended_at": ended_at,
            "exit_code": exit_code,
            "reason": reason,
        }
        retry = self._retry_projection(
            idempotency_key,
            "attempt_transitioned",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return RunAttempt.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        projection = self.project()
        current = projection.attempts.get(identifier)
        if current is None:
            raise RecordNotFound("attempt does not exist")
        next_execution = execution_state or current.execution_state
        next_outcome = outcome_state or current.outcome_state
        next_control = control_state or current.control_state
        terminal = next_execution in {"succeeded", "failed", "cancelled", "lost"}
        record = replace(
            current,
            execution_state=next_execution,
            outcome_state=next_outcome,
            control_state=next_control,
            manifest_id=manifest_id if manifest_id is not None else current.manifest_id,
            pid=pid if pid is not None else current.pid,
            process_group_id=process_group_id if process_group_id is not None else current.process_group_id,
            process_birth_time=(
                process_birth_time if process_birth_time is not None else current.process_birth_time
            ),
            process_executable=process_executable if process_executable is not None else current.process_executable,
            process_cwd=process_cwd if process_cwd is not None else current.process_cwd,
            ownership_nonce_hash=(
                ownership_nonce_hash if ownership_nonce_hash is not None else current.ownership_nonce_hash
            ),
            started_at=started_at if started_at is not None else current.started_at,
            ended_at=(ended_at if ended_at is not None else (_now() if terminal else current.ended_at)),
            exit_code=exit_code if exit_code is not None else current.exit_code,
            revision=current.revision + 1,
            reason=reason if reason is not None else current.reason,
        )
        updated, _ = self._mutate(
            action="attempt_transitioned",
            target_type="attempt",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state=current.execution_state,
            next_state=next_execution,
            request_id=request_id,
            causal_parent_id=causal_parent_id,
            operation_payload=operation,
        )
        return updated.attempts[identifier]

    def request_approval(
        self,
        task_id: str,
        *,
        kind: str,
        requested_action: str,
        requested_by: str,
        expires_at: float,
        idempotency_key: str,
        attempt_id: str | None = None,
        approval_id: str | None = None,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> ApprovalRequest:
        identifier = approval_id or self._opaque_id("approval", idempotency_key)
        operation = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "requested_action": requested_action,
            "requested_by": requested_by,
            "expires_at": expires_at,
            "approval_id": identifier,
        }
        retry = self._retry_projection(
            idempotency_key,
            "approval_requested",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=0,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return ApprovalRequest.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        expiry = _timestamp(expires_at, "expires_at")
        if expiry <= _now():
            raise ControlPlaneError("approval expiry must be in the future")
        record = ApprovalRequest.from_dict(
            {
                "approval_id": identifier,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "kind": kind,
                "requested_action": requested_action,
                "state": "pending",
                "requested_by": requested_by,
                "decided_by": None,
                "expires_at": expiry,
                "decided_at": None,
                "consumed_at": None,
                "revision": 1,
            }
        )
        updated, _ = self._mutate(
            action="approval_requested",
            target_type="approval",
            target_id=identifier,
            expected_revision=0,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            next_state="pending",
            operation_payload=operation,
        )
        return updated.approvals[identifier]

    def resolve_approval_for_attempt(
        self,
        approval_id: str,
        *,
        approve: bool,
        decided_by: str,
        expected_revision: int,
        idempotency_key: str,
        now: float | None = None,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> tuple[ApprovalRequest, RunAttempt]:
        """Resolve, consume when approved, and release/hold one attempt atomically."""

        if not isinstance(approve, bool):
            raise ControlPlaneError("approve must be boolean")
        identifier = _id(approval_id, "approval_id")
        operation = {"approve": approve, "decided_by": decided_by}
        retry = self._retry_projection(
            idempotency_key,
            "approval_resolved",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            event = retry.idempotency[idempotency_key]
            approval_payload = event.payload.get("approval_record")
            attempt_payload = event.payload.get("attempt_record")
            if not isinstance(approval_payload, Mapping) or not isinstance(attempt_payload, Mapping):
                raise ControlPlaneError("approval resolution retry payload is invalid")
            return ApprovalRequest.from_dict(approval_payload), RunAttempt.from_dict(attempt_payload)

        timestamp = _now() if now is None else _timestamp(now, "now")
        projection = self.project()
        current = projection.approvals.get(identifier)
        if current is None:
            raise RecordNotFound("approval does not exist")
        if current.state != "pending":
            raise InvalidTransition(f"approval is already {current.state}")
        if timestamp >= current.expires_at:
            raise InvalidTransition("approval is expired")
        if current.attempt_id is None:
            raise InvalidTransition("only an attempt-scoped approval can release an attempt")
        if current.requested_action != "launch":
            raise InvalidTransition("only a launch approval may release an attempt")
        attempt = projection.attempts.get(current.attempt_id)
        if attempt is None:
            raise RecordNotFound("approval attempt does not exist")
        if attempt.task_id != current.task_id:
            raise InvalidTransition("approval task and attempt do not match")
        if attempt.execution_state != "pending" or attempt.control_state != "awaiting_approval":
            raise InvalidTransition("approval attempt must be pending and awaiting approval")

        decided = replace(
            current,
            state="consumed" if approve else "rejected",
            decided_by=_text(decided_by, "decided_by", maximum=120),
            decided_at=timestamp,
            consumed_at=timestamp if approve else None,
            revision=current.revision + 1,
        )
        released = replace(
            attempt,
            control_state="ready" if approve else "policy_hold",
            revision=attempt.revision + 1,
            reason="single-use approval consumed" if approve else "launch approval rejected",
        )
        updated, _ = self._mutate(
            action="approval_resolved",
            target_type="approval",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={
                "approval_record": decided.to_dict(),
                "attempt_record": released.to_dict(),
                "expected_attempt_revision": attempt.revision,
            },
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state=current.state,
            next_state=decided.state,
            operation_payload=operation,
        )
        return updated.approvals[identifier], updated.attempts[attempt.attempt_id]

    def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        decided_by: str,
        expected_revision: int,
        idempotency_key: str,
        now: float | None = None,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> ApprovalRequest:
        if not isinstance(approve, bool):
            raise ControlPlaneError("approve must be boolean")
        identifier = _id(approval_id, "approval_id")
        operation = {"approve": approve, "decided_by": decided_by}
        retry = self._retry_projection(
            idempotency_key,
            "approval_decided",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return ApprovalRequest.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        timestamp = _now() if now is None else _timestamp(now, "now")
        current = self.project().approvals.get(identifier)
        if current is None:
            raise RecordNotFound("approval does not exist")
        if current.state != "pending":
            raise InvalidTransition(f"approval is already {current.state}")
        if timestamp >= current.expires_at:
            raise InvalidTransition("approval is expired")
        next_state = "approved" if approve else "rejected"
        record = replace(
            current,
            state=next_state,
            decided_by=_text(decided_by, "decided_by", maximum=120),
            decided_at=timestamp,
            revision=current.revision + 1,
        )
        updated, _ = self._mutate(
            action="approval_decided",
            target_type="approval",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state=current.state,
            next_state=next_state,
            operation_payload=operation,
        )
        return updated.approvals[identifier]

    def consume_approval(
        self,
        approval_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        now: float | None = None,
        actor_kind: str = "supervisor",
        actor_id: str = "local-supervisor",
    ) -> ApprovalRequest:
        identifier = _id(approval_id, "approval_id")
        operation = {"approval_id": identifier}
        retry = self._retry_projection(
            idempotency_key,
            "approval_consumed",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return ApprovalRequest.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        timestamp = _now() if now is None else _timestamp(now, "now")
        current = self.project().approvals.get(identifier)
        if current is None:
            raise RecordNotFound("approval does not exist")
        if current.state != "approved":
            raise InvalidTransition("only one approved decision may be consumed")
        if timestamp >= current.expires_at:
            raise InvalidTransition("approval is expired")
        record = replace(current, state="consumed", consumed_at=timestamp, revision=current.revision + 1)
        updated, _ = self._mutate(
            action="approval_consumed",
            target_type="approval",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state=current.state,
            next_state="consumed",
            operation_payload=operation,
        )
        return updated.approvals[identifier]

    def expire_approval(
        self,
        approval_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        now: float | None = None,
        actor_kind: str = "supervisor",
        actor_id: str = "local-supervisor",
    ) -> ApprovalRequest:
        identifier = _id(approval_id, "approval_id")
        operation = {"approval_id": identifier}
        retry = self._retry_projection(
            idempotency_key,
            "approval_expired",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return ApprovalRequest.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        timestamp = _now() if now is None else _timestamp(now, "now")
        current = self.project().approvals.get(identifier)
        if current is None:
            raise RecordNotFound("approval does not exist")
        if current.state not in {"pending", "approved"} or timestamp < current.expires_at:
            raise InvalidTransition("approval is not eligible to expire")
        record = replace(current, state="expired", revision=current.revision + 1)
        updated, _ = self._mutate(
            action="approval_expired",
            target_type="approval",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state=current.state,
            next_state="expired",
            operation_payload=operation,
        )
        return updated.approvals[identifier]

    def register_schedule(
        self,
        task_id: str,
        *,
        cadence: str,
        next_run_at: float,
        idempotency_key: str,
        interval_seconds: float | None = None,
        schedule_id: str | None = None,
        enabled: bool = True,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> ScheduleSpec:
        identifier = schedule_id or self._opaque_id("schedule", idempotency_key)
        operation = {
            "task_id": task_id,
            "cadence": cadence,
            "interval_seconds": interval_seconds,
            "next_run_at": next_run_at,
            "enabled": enabled,
            "schedule_id": identifier,
        }
        retry = self._retry_projection(
            idempotency_key,
            "schedule_registered",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=0,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return ScheduleSpec.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        record = ScheduleSpec.from_dict(
            {
                "schedule_id": identifier,
                "task_id": task_id,
                "cadence": cadence,
                "interval_seconds": interval_seconds,
                "next_run_at": next_run_at,
                "enabled": enabled,
                "claim_count": 0,
                "last_claimed_at": None,
                "revision": 1,
                "updated_at": _now(),
            }
        )
        updated, _ = self._mutate(
            action="schedule_registered",
            target_type="schedule",
            target_id=identifier,
            expected_revision=0,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return updated.schedules[identifier]

    def update_schedule(
        self,
        schedule_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        enabled: bool | None = None,
        next_run_at: float | None = None,
        actor_kind: str = "local_user",
        actor_id: str = "local-user",
    ) -> ScheduleSpec:
        identifier = _id(schedule_id, "schedule_id")
        operation = {"enabled": enabled, "next_run_at": next_run_at}
        retry = self._retry_projection(
            idempotency_key,
            "schedule_updated",
            operation_payload=operation,
            target_id=identifier,
            expected_revision=expected_revision,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        if retry is not None:
            return ScheduleSpec.from_dict(_record_payload(retry.idempotency[idempotency_key]))
        current = self.project().schedules.get(identifier)
        if current is None:
            raise RecordNotFound("schedule does not exist")
        record = replace(
            current,
            enabled=current.enabled if enabled is None else enabled,
            next_run_at=current.next_run_at if next_run_at is None else _timestamp(next_run_at, "next_run_at"),
            revision=current.revision + 1,
            updated_at=_now(),
        )
        # Re-run the complete schedule validator after replace.
        record = ScheduleSpec.from_dict(record.to_dict())
        updated, _ = self._mutate(
            action="schedule_updated",
            target_type="schedule",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict()},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            operation_payload=operation,
        )
        return updated.schedules[identifier]

    def due_schedules(self, *, now: float | None = None) -> tuple[ScheduleSpec, ...]:
        timestamp = _now() if now is None else _timestamp(now, "now")
        return tuple(
            sorted(
                (
                    schedule
                    for schedule in self.project().schedules.values()
                    if schedule.enabled
                    and schedule.next_run_at is not None
                    and schedule.next_run_at <= timestamp
                ),
                key=lambda item: (item.next_run_at or 0.0, item.schedule_id),
            )
        )

    def claim_schedule(
        self,
        schedule_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        now: float | None = None,
        actor_kind: str = "supervisor",
        actor_id: str = "local-supervisor",
    ) -> ScheduleSpec:
        identifier = _id(schedule_id, "schedule_id")
        # The scheduled timestamp, not wall-clock claim latency, is the
        # idempotent unit of work. It is filled after loading the current row.
        if now is not None:
            _timestamp(now, "now")
        retry_projection = self.project()
        retry_event = retry_projection.idempotency.get(idempotency_key)
        if retry_event is not None:
            claimed_for = retry_event.payload.get("claimed_for")
            retry = self._retry_projection(
                idempotency_key,
                "schedule_claimed",
                operation_payload={"schedule_id": identifier, "claimed_for": claimed_for},
                target_id=identifier,
                expected_revision=expected_revision,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
            assert retry is not None
            return ScheduleSpec.from_dict(_record_payload(retry_event))
        timestamp = _now() if now is None else _timestamp(now, "now")
        current = retry_projection.schedules.get(identifier)
        if current is None:
            raise RecordNotFound("schedule does not exist")
        if not current.enabled or current.next_run_at is None or current.next_run_at > timestamp:
            raise InvalidTransition("schedule is not due")
        if current.cadence == "one_shot":
            next_run_at = None
            enabled = False
        else:
            assert current.interval_seconds is not None
            elapsed = max(0.0, timestamp - current.next_run_at)
            intervals = int(elapsed // current.interval_seconds) + 1
            next_run_at = current.next_run_at + intervals * current.interval_seconds
            enabled = True
        record = replace(
            current,
            next_run_at=next_run_at,
            enabled=enabled,
            claim_count=current.claim_count + 1,
            last_claimed_at=timestamp,
            revision=current.revision + 1,
            updated_at=timestamp,
        )
        updated, _ = self._mutate(
            action="schedule_claimed",
            target_type="schedule",
            target_id=identifier,
            expected_revision=expected_revision,
            payload={"record": record.to_dict(), "claimed_for": current.next_run_at},
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_id=actor_id,
            prior_state="due",
            next_state="disabled" if not enabled else "scheduled",
            operation_payload={"schedule_id": identifier, "claimed_for": current.next_run_at},
        )
        return updated.schedules[identifier]

    def write_manifest(self, manifest_id: str, payload: Mapping[str, Any]) -> Path:
        identifier = _id(manifest_id, "manifest_id")
        body = {
            "schema_version": "agent-chronicle.attempt-manifest.v1",
            "manifest_id": identifier,
            "created_at": _now(),
            "payload": _mapping(payload, "manifest payload"),
        }
        envelope = {**body, "record_hash": _digest(body)}
        serialized = _canonical_json_bytes(envelope) + b"\n"
        with self._locked(exclusive=True):
            manifests_existed = self.manifests_root.exists()
            self.manifests_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _owner_only(self.manifests_root, 0o700)
            if not manifests_existed:
                self._fsync_directory(self.control_root)
            path = self.manifests_root / f"{identifier}.json"
            if path.exists():
                existing = self.read_manifest(identifier)
                if existing.get("payload") != body["payload"]:
                    raise IdempotencyConflict("manifest id already exists with different content")
                return path
            tmp = self.manifests_root / f".{identifier}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    existing = self.read_manifest(identifier)
                    if existing.get("payload") != body["payload"]:
                        raise IdempotencyConflict("manifest id already exists with different content")
                self._fsync_directory(self.manifests_root)
                return path
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass

    def read_manifest(self, manifest_id: str) -> dict[str, Any]:
        identifier = _id(manifest_id, "manifest_id")
        path = self.manifests_root / f"{identifier}.json"
        if not path.is_file():
            raise FileNotFoundError(f"unknown manifest_id: {identifier}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("attempt manifest is invalid") from exc
        _exact_keys(
            value,
            ("schema_version", "manifest_id", "created_at", "payload", "record_hash"),
            "attempt manifest",
        )
        if value.get("schema_version") != "agent-chronicle.attempt-manifest.v1":
            raise ControlPlaneError("attempt manifest is invalid")
        embedded_id = _id(value.get("manifest_id"), "manifest_id")
        if embedded_id != identifier:
            raise ControlPlaneError("attempt manifest id does not match its filename")
        created_at = _timestamp(value.get("created_at"), "created_at")
        normalized_payload = _mapping(value.get("payload"), "manifest payload")
        record_hash = value.get("record_hash")
        if not isinstance(record_hash, str) or not _HASH.fullmatch(record_hash):
            raise ControlPlaneError("attempt manifest record_hash is invalid")
        content = {
            "schema_version": value["schema_version"],
            "manifest_id": embedded_id,
            "created_at": created_at,
            "payload": normalized_payload,
        }
        if record_hash != _digest(content):
            raise ControlPlaneError("attempt manifest record_hash mismatch")
        return {**content, "record_hash": record_hash}


__all__ = [
    "APPROVAL_STATES",
    "CONTROL_STATES",
    "EXECUTION_STATES",
    "OUTCOME_STATES",
    "AgentSpec",
    "ApprovalRequest",
    "BudgetPolicyRecord",
    "ControlEvent",
    "ControlPlaneError",
    "ControlProjection",
    "ControlStore",
    "IdempotencyConflict",
    "InvalidTransition",
    "RecordNotFound",
    "RevisionConflict",
    "RunAttempt",
    "ScheduleSpec",
    "TaskContract",
    "TaskRecord",
    "Workspace",
    "contract_requires_launch_approval",
]
