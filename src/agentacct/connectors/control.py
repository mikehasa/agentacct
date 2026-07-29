"""Advisory-only control signals and conservative hard-enforcement policy.

This module has no controller or HTTP client by design.  It can recommend or
refuse an action, but it cannot pause/cancel an external run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

from agentacct.confidence import COST_PROVIDER_BILLED
from agentacct.evidence_runtime import EvidenceRuntime
from agentacct.source_policy import DEFAULT_SOURCE_AUTHORITY_POLICY

from .base import stable_digest


ControlMode = Literal["advisory", "hard"]
ControlAction = Literal["warn", "pause", "cancel", "block"]
_EVIDENCE_ID_RE = re.compile(r"^evd_[0-9a-f]{64}\Z")
_MAX_SUPPORTING_EVIDENCE = 100
_TARGET_SUBJECT_FIELDS = {
    "agent": "agent_id",
    "client_session": "client_session_id",
    "execution": "run_id",
    "project": "project_id",
    "run": "run_id",
    "section": "section_id",
    "session": "client_session_id",
    "tool_call": "tool_call_id",
    "work": "work_id",
    "work_item": "work_id",
}


class HardEnforcementRefused(PermissionError):
    """A signal did not satisfy the hard-enforcement evidence contract."""


@dataclass(frozen=True, slots=True)
class ControlSignal:
    action: ControlAction
    target_type: str
    target_id: str
    recommendation: str
    requested_mode: ControlMode = "advisory"
    evidence_basis: str = "unknown"
    cost_confidence: str = "unknown"
    supporting_evidence_ids: tuple[str, ...] = ()
    explicit_conservative_approval: bool = False
    controller_owns_execution: bool = False
    conflicting: bool = False
    expires_at: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supporting_evidence_ids",
            normalize_supporting_evidence_ids(self.supporting_evidence_ids),
        )

    @property
    def signal_id(self) -> str:
        material = {
            "action": self.action,
            "basis": self.evidence_basis,
            "evidence": sorted(set(self.supporting_evidence_ids)),
            "idempotency_key": self.idempotency_key,
            "target_id": self.target_id,
            "target_type": self.target_type,
        }
        return f"ctrl_{stable_digest(material)[:24]}"


@dataclass(frozen=True, slots=True)
class ControlDecision:
    signal_id: str
    requested_mode: ControlMode
    effective_mode: ControlMode
    hard_enforcement_allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SupportingEvidenceValidation:
    """Store-bound facts used by the pure hard-enforcement policy gate."""

    signal_id: str
    store_available: bool
    requested_ids: tuple[str, ...]
    found_ids: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()
    integrity_failed_ids: tuple[str, ...] = ()
    conflicting_ids: tuple[str, ...] = ()
    target_mismatch_ids: tuple[str, ...] = ()
    provider_billed_ids: tuple[str, ...] = ()
    lookup_failed: bool = False

    @property
    def valid(self) -> bool:
        return bool(self.requested_ids) and self.store_available and not any(
            (
                self.missing_ids,
                self.integrity_failed_ids,
                self.conflicting_ids,
                self.target_mismatch_ids,
                self.lookup_failed,
            )
        ) and len(self.found_ids) == len(self.requested_ids)

    @property
    def has_provider_billed_basis(self) -> bool:
        return bool(self.provider_billed_ids)

    def refusal_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.requested_ids:
            reasons.append("hard action has no supporting evidence")
        if not self.store_available:
            reasons.append("local Evidence Store is unavailable")
        if self.lookup_failed:
            reasons.append("supporting evidence lookup failed")
        if self.missing_ids:
            reasons.append(f"{len(self.missing_ids)} supporting evidence id(s) do not exist in this store")
        if self.integrity_failed_ids:
            reasons.append(
                f"{len(self.integrity_failed_ids)} supporting evidence envelope(s) failed integrity validation"
            )
        if self.conflicting_ids:
            reasons.append(f"{len(self.conflicting_ids)} supporting evidence envelope(s) belong to a conflict group")
        if self.target_mismatch_ids:
            reasons.append(f"{len(self.target_mismatch_ids)} supporting evidence envelope(s) do not match the target")
        return tuple(reasons)

    def to_dict(self) -> dict[str, object]:
        state = "valid" if self.valid else ("unavailable" if not self.store_available else "invalid")
        return {
            "state": state,
            "signal_id": self.signal_id,
            "store_available": self.store_available,
            "requested_ids": list(self.requested_ids),
            "found_ids": list(self.found_ids),
            "missing_ids": list(self.missing_ids),
            "integrity_failed_ids": list(self.integrity_failed_ids),
            "conflicting_ids": list(self.conflicting_ids),
            "target_mismatch_ids": list(self.target_mismatch_ids),
            "provider_billed_ids": list(self.provider_billed_ids),
            "lookup_failed": self.lookup_failed,
        }


def normalize_supporting_evidence_ids(values: Iterable[str]) -> tuple[str, ...]:
    ids = tuple(values)
    if len(ids) > _MAX_SUPPORTING_EVIDENCE:
        raise ValueError(f"at most {_MAX_SUPPORTING_EVIDENCE} supporting evidence ids are allowed")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in ids:
        if not isinstance(value, str) or not _EVIDENCE_ID_RE.fullmatch(value):
            raise ValueError("supporting evidence ids must match evd_ followed by 64 lowercase hex characters")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return tuple(normalized)


def _target_matches(signal: ControlSignal, envelope: object) -> bool:
    subject_field = _TARGET_SUBJECT_FIELDS.get(signal.target_type.strip().lower().replace("-", "_"))
    subjects = getattr(envelope, "subjects", None)
    if subject_field is None or subjects is None:
        return False
    return getattr(subjects, subject_field, None) == signal.target_id


def validate_supporting_evidence(
    signal: ControlSignal,
    runtime: EvidenceRuntime | None,
) -> SupportingEvidenceValidation:
    """Resolve every evidence id against exactly one local Evidence Store.

    Validation is read-only with respect to external systems. Any local store
    error is converted to a fail-closed result rather than authorizing a hard
    action or exposing exception details in the policy reason.
    """

    requested = signal.supporting_evidence_ids
    if runtime is None or not runtime.enabled:
        return SupportingEvidenceValidation(
            signal_id=signal.signal_id,
            store_available=False,
            requested_ids=requested,
        )
    if not requested:
        # An empty hard signal is invalid on its face; do not create/open the
        # local evidence projection merely to prove that it supplied nothing.
        return SupportingEvidenceValidation(
            signal_id=signal.signal_id,
            store_available=True,
            requested_ids=(),
        )

    found: list[str] = []
    missing: list[str] = []
    integrity_failed: list[str] = []
    conflicting: list[str] = []
    target_mismatch: list[str] = []
    provider_billed: list[str] = []
    try:
        store = runtime.store
        for evidence_id in requested:
            envelope = store.get(evidence_id)
            if envelope is None:
                missing.append(evidence_id)
                continue
            found.append(evidence_id)
            if not envelope.verify_integrity():
                integrity_failed.append(evidence_id)
                continue
            if store.conflicts(idempotency_key=envelope.idempotency_key):
                conflicting.append(evidence_id)
            if not _target_matches(signal, envelope):
                target_mismatch.append(evidence_id)
            if (
                envelope.assertion == "observed"
                and envelope.measurement_basis.get("cost") == COST_PROVIDER_BILLED
                and DEFAULT_SOURCE_AUTHORITY_POLICY.evaluate(envelope, "cost").is_authoritative
            ):
                provider_billed.append(evidence_id)
    except Exception:  # noqa: BLE001 - a policy gate must fail closed on local corruption/I/O errors.
        return SupportingEvidenceValidation(
            signal_id=signal.signal_id,
            store_available=True,
            requested_ids=requested,
            found_ids=tuple(found),
            missing_ids=tuple(missing),
            integrity_failed_ids=tuple(integrity_failed),
            conflicting_ids=tuple(conflicting),
            target_mismatch_ids=tuple(target_mismatch),
            provider_billed_ids=tuple(provider_billed),
            lookup_failed=True,
        )
    return SupportingEvidenceValidation(
        signal_id=signal.signal_id,
        store_available=True,
        requested_ids=requested,
        found_ids=tuple(found),
        missing_ids=tuple(missing),
        integrity_failed_ids=tuple(integrity_failed),
        conflicting_ids=tuple(conflicting),
        target_mismatch_ids=tuple(target_mismatch),
        provider_billed_ids=tuple(provider_billed),
    )


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_control_signal(
    signal: ControlSignal,
    *,
    now: datetime | None = None,
    supporting_evidence: SupportingEvidenceValidation | None = None,
) -> ControlDecision:
    """Evaluate eligibility; never dispatch an external control operation."""

    if signal.requested_mode == "advisory":
        return ControlDecision(
            signal_id=signal.signal_id,
            requested_mode="advisory",
            effective_mode="advisory",
            hard_enforcement_allowed=False,
            reason="advisory signal; no external mutation is authorized",
        )

    reasons: list[str] = []
    if supporting_evidence is None:
        reasons.append("supporting evidence was not validated against the local Evidence Store")
        provider_billed = False
    elif supporting_evidence.signal_id != signal.signal_id:
        reasons.append("supporting evidence validation does not match this control signal")
        provider_billed = False
    else:
        reasons.extend(supporting_evidence.refusal_reasons())
        provider_billed = supporting_evidence.valid and supporting_evidence.has_provider_billed_basis
    if not (provider_billed or signal.explicit_conservative_approval):
        reasons.append("basis is neither provider_billed nor explicitly approved as conservative")
    if not signal.controller_owns_execution:
        reasons.append("target controller ownership is unproven")
    if signal.conflicting:
        reasons.append("supporting evidence is conflicting")
    if not signal.idempotency_key:
        reasons.append("hard action has no caller-supplied idempotency key")
    if not signal.expires_at:
        reasons.append("hard action has no expiry, so freshness is unproven")
    else:
        expiry = _parse_time(signal.expires_at)
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if expiry is None:
            reasons.append("signal expiry is invalid")
        elif expiry <= reference:
            reasons.append("signal is stale")

    if reasons:
        return ControlDecision(
            signal_id=signal.signal_id,
            requested_mode="hard",
            effective_mode="advisory",
            hard_enforcement_allowed=False,
            reason="; ".join(reasons),
        )
    return ControlDecision(
        signal_id=signal.signal_id,
        requested_mode="hard",
        effective_mode="hard",
        hard_enforcement_allowed=True,
        reason="hard-enforcement evidence gate passed; dispatch remains external to agentacct",
    )


def require_hard_enforcement(
    signal: ControlSignal,
    *,
    now: datetime | None = None,
    supporting_evidence: SupportingEvidenceValidation | None = None,
) -> ControlDecision:
    decision = evaluate_control_signal(
        signal,
        now=now,
        supporting_evidence=supporting_evidence,
    )
    if not decision.hard_enforcement_allowed:
        raise HardEnforcementRefused(decision.reason)
    return decision
