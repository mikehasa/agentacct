from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentacct.confidence import COST_PROVIDER_BILLED
from agentacct.connectors import (
    ControlSignal,
    HardEnforcementRefused,
    evaluate_control_signal,
    require_hard_enforcement,
)
from agentacct.connectors.control import validate_supporting_evidence
from agentacct.evidence import EvidenceEnvelope
from agentacct.evidence_runtime import EvidenceRuntime


def _append_control_evidence(
    runtime: EvidenceRuntime,
    *,
    run_id: str,
    source_event_id: str,
    basis: str = COST_PROVIDER_BILLED,
    amount: float = 1.0,
    assertion: str = "observed",
) -> EvidenceEnvelope:
    envelope = EvidenceEnvelope.create(
        assertion=assertion,
        event_type="provider.cost.observed",
        source_type="provider_invoice",
        source_system="fixture-provider",
        source_instance="fixture-account",
        source_schema="fixture.v1",
        adapter="fixture.v1",
        source_event_id=source_event_id,
        event_timestamp="2026-07-13T00:00:00Z",
        dimensions=("cost",),
        measurement_basis={"cost": basis},
        subjects={"run_id": run_id},
        payload={"amount_usd": amount},
        claimant="fixture-provider" if assertion == "claimed" else None,
    )
    runtime.append(envelope)
    return envelope


def _hard_control_signal(
    *,
    run_id: str,
    evidence_ids: tuple[str, ...] = (),
    approval: bool = False,
    expires_at: str | None = "2030-01-01T00:00:00Z",
) -> ControlSignal:
    return ControlSignal(
        action="pause",
        target_type="execution",
        target_id=run_id,
        recommendation="pause after validated threshold",
        requested_mode="hard",
        evidence_basis=COST_PROVIDER_BILLED,
        cost_confidence=COST_PROVIDER_BILLED,
        supporting_evidence_ids=evidence_ids,
        explicit_conservative_approval=approval,
        controller_owns_execution=True,
        idempotency_key=f"pause-{run_id}-v1",
        expires_at=expires_at,
    )


def test_control_policy_is_store_bound_fail_closed_and_never_dispatches(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path / "state", enabled=True)
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)

    empty = _hard_control_signal(run_id="run-empty")
    empty_validation = validate_supporting_evidence(empty, runtime)
    empty_decision = evaluate_control_signal(empty, now=now, supporting_evidence=empty_validation)
    assert empty_decision.hard_enforcement_allowed is False
    assert "no supporting evidence" in empty_decision.reason
    with pytest.raises(HardEnforcementRefused):
        require_hard_enforcement(empty, now=now, supporting_evidence=empty_validation)

    with pytest.raises(ValueError, match="64 lowercase hex"):
        _hard_control_signal(run_id="run-malformed", evidence_ids=("not-evidence",))

    # Initialize a real local projection, then prove an unknown id cannot be
    # satisfied by merely looking syntactically valid.
    _append_control_evidence(runtime, run_id="run-seed", source_event_id="seed")
    missing = _hard_control_signal(run_id="run-missing", evidence_ids=("evd_" + ("0" * 64),))
    missing_validation = validate_supporting_evidence(missing, runtime)
    assert missing_validation.missing_ids == missing.supporting_evidence_ids
    assert evaluate_control_signal(
        missing,
        now=now,
        supporting_evidence=missing_validation,
    ).hard_enforcement_allowed is False

    wrong_target_evidence = _append_control_evidence(
        runtime,
        run_id="run-other",
        source_event_id="wrong-target",
    )
    wrong_target = _hard_control_signal(
        run_id="run-target",
        evidence_ids=(wrong_target_evidence.evidence_id,),
    )
    wrong_target_validation = validate_supporting_evidence(wrong_target, runtime)
    assert wrong_target_validation.target_mismatch_ids == (wrong_target_evidence.evidence_id,)
    assert evaluate_control_signal(
        wrong_target,
        now=now,
        supporting_evidence=wrong_target_validation,
    ).hard_enforcement_allowed is False

    billed_evidence = _append_control_evidence(
        runtime,
        run_id="run-billed",
        source_event_id="billed",
    )
    billed = _hard_control_signal(
        run_id="run-billed",
        evidence_ids=(billed_evidence.evidence_id,),
    )
    billed_validation = validate_supporting_evidence(billed, runtime)
    allowed = require_hard_enforcement(
        billed,
        now=now,
        supporting_evidence=billed_validation,
    )
    assert billed_validation.valid is True
    assert billed_validation.provider_billed_ids == (billed_evidence.evidence_id,)
    assert allowed.effective_mode == "hard"
    assert evaluate_control_signal(
        wrong_target,
        now=now,
        supporting_evidence=billed_validation,
    ).hard_enforcement_allowed is False

    estimated_evidence = _append_control_evidence(
        runtime,
        run_id="run-estimated",
        source_event_id="estimated",
        basis="estimated_from_tokens",
    )
    estimated = _hard_control_signal(
        run_id="run-estimated",
        evidence_ids=(estimated_evidence.evidence_id,),
    )
    estimated_validation = validate_supporting_evidence(estimated, runtime)
    assert evaluate_control_signal(
        estimated,
        now=now,
        supporting_evidence=estimated_validation,
    ).hard_enforcement_allowed is False
    conservative = _hard_control_signal(
        run_id="run-estimated",
        evidence_ids=(estimated_evidence.evidence_id,),
        approval=True,
    )
    assert evaluate_control_signal(
        conservative,
        now=now,
        supporting_evidence=validate_supporting_evidence(conservative, runtime),
    ).hard_enforcement_allowed is True

    claimed_billed_evidence = _append_control_evidence(
        runtime,
        run_id="run-claimed-billed",
        source_event_id="claimed-billed",
        assertion="claimed",
    )
    claimed_billed = _hard_control_signal(
        run_id="run-claimed-billed",
        evidence_ids=(claimed_billed_evidence.evidence_id,),
    )
    claimed_billed_validation = validate_supporting_evidence(claimed_billed, runtime)
    assert claimed_billed_validation.valid is True
    assert claimed_billed_validation.provider_billed_ids == ()
    assert evaluate_control_signal(
        claimed_billed,
        now=now,
        supporting_evidence=claimed_billed_validation,
    ).hard_enforcement_allowed is False

    no_expiry = _hard_control_signal(
        run_id="run-billed",
        evidence_ids=(billed_evidence.evidence_id,),
        expires_at=None,
    )
    no_expiry_decision = evaluate_control_signal(
        no_expiry,
        now=now,
        supporting_evidence=validate_supporting_evidence(no_expiry, runtime),
    )
    assert no_expiry_decision.hard_enforcement_allowed is False
    assert "freshness is unproven" in no_expiry_decision.reason

    original = _append_control_evidence(
        runtime,
        run_id="run-conflict",
        source_event_id="conflicting-cost",
        amount=1.0,
    )
    _append_control_evidence(
        runtime,
        run_id="run-conflict",
        source_event_id="conflicting-cost",
        amount=2.0,
    )
    conflict_signal = _hard_control_signal(
        run_id="run-conflict",
        evidence_ids=(original.evidence_id,),
    )
    conflict_validation = validate_supporting_evidence(conflict_signal, runtime)
    assert conflict_validation.conflicting_ids == (original.evidence_id,)
    assert evaluate_control_signal(
        conflict_signal,
        now=now,
        supporting_evidence=conflict_validation,
    ).hard_enforcement_allowed is False

    disabled = EvidenceRuntime(tmp_path / "disabled", enabled=False)
    disabled_validation = validate_supporting_evidence(billed, disabled)
    assert disabled_validation.store_available is False
    assert evaluate_control_signal(
        billed,
        now=now,
        supporting_evidence=disabled_validation,
    ).hard_enforcement_allowed is False

    assert not hasattr(billed, "dispatch")
    assert not hasattr(billed, "execute")
