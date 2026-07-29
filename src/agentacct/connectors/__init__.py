"""Read-only third-party evidence adapters.

No connector in this package owns upstream scheduling, tracing storage, Git
refs, or external control.  They normalize bounded snapshots into
``ConnectorRecord`` objects for the evidence kernel.
"""

from .base import (
    ConnectorError,
    ConnectorRecord,
    EvidenceCoreUnavailable,
    ReadOnlyConnector,
)
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
from .entire import EntireGitConnector
from .openlit import OpenLITOTLPConnector
from .paperclip import PaperclipSnapshotConnector
from .registry import ConnectorRegistry, build_default_registry

__all__ = [
    "ConnectorError",
    "ConnectorRecord",
    "ConnectorRegistry",
    "ControlDecision",
    "ControlSignal",
    "EntireGitConnector",
    "EvidenceCoreUnavailable",
    "HardEnforcementRefused",
    "OpenLITOTLPConnector",
    "PaperclipSnapshotConnector",
    "ReadOnlyConnector",
    "SupportingEvidenceValidation",
    "build_default_registry",
    "evaluate_control_signal",
    "normalize_supporting_evidence_ids",
    "require_hard_enforcement",
    "validate_supporting_evidence",
]
