"""Read-only advisory control-signal evaluation.

The control-signal evaluator scores a single advisory signal against locally
stored evidence and never dispatches an external action.
"""

from .control import (
    ControlDecision,
    ControlSignal,
    HardEnforcementRefused,
    SupportingEvidenceValidation,
    evaluate_control_signal,
    normalize_supporting_evidence_ids,
    require_hard_enforcement,
    validate_supporting_evidence,
)

__all__ = [
    "ControlDecision",
    "ControlSignal",
    "HardEnforcementRefused",
    "SupportingEvidenceValidation",
    "evaluate_control_signal",
    "normalize_supporting_evidence_ids",
    "require_hard_enforcement",
    "validate_supporting_evidence",
]
