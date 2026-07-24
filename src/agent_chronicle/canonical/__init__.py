"""Canonical SQLite truth model.

Offline writers (importer, recovery, benchmarks) still require an explicit
candidate path or a verified offline snapshot and refuse live state roots.
Since migration phase 3 the runtime additionally imports this package through
exactly one sanctioned seam — ``agent_chronicle.canonical_live``, the
flag-gated live shadow writer targeting ``CanonicalStore.open_live`` — and
that import is lazy: with the flag off, nothing here loads or runs.
"""

from .repository import (
    CandidateQueries,
    FactRepository,
    SessionRepository,
    SourceRepository,
    TaskAnchorRepository,
    UsageRepository,
)
from .snapshot import (
    CandidateTargetError,
    ManifestValidationError,
    SnapshotManifest,
    SnapshotSafetyError,
    SnapshotVerificationError,
    VerifiedSnapshot,
    validate_candidate_target,
)
from .safe_scratch import (
    AnchoredRunDirectory,
    ScratchSafetyError,
    create_anchored_run_directory,
)
from .sqlite import (
    APPLICATION_ID,
    LIVE_STORE_FILENAME,
    SCHEMA_VERSION,
    CanonicalRepository,
    CanonicalStore,
    canonical_hash,
)
from .types import (
    CostCalculationInput,
    FactInput,
    FactSessionLinkInput,
    RECOVERY_FACT_TRANSPORT,
    RECOVERY_LINK_METHOD,
    RECOVERY_LINK_RULE_VERSION,
    RecoveredWorkClaimInput,
    SessionEdgeInput,
    SessionInput,
    SourceInstanceInput,
    TaskCursor,
    UsageDayQuery,
    UsageMeasurementInput,
    WorkClaimInput,
)
from .unsupported_legacy_routing import (
    STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS,
    STATIC_UNSUPPORTED_LEGACY_ROW_COUNT,
    UNSUPPORTED_LEGACY_CURRENT_PRODUCT_STATE,
    UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE,
    UNSUPPORTED_LEGACY_MIGRATION_DISPOSITION,
    UNSUPPORTED_LEGACY_ROUTING_POLICY_ID,
    UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION,
    UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST,
    UnsupportedLegacyProductLane,
    UnsupportedLegacyRoute,
    UnsupportedLegacyRoutingError,
    UnsupportedLegacyRoutingSummary,
    route_unsupported_legacy_event_type,
    summarize_unsupported_legacy_event_types,
    verify_static_unsupported_legacy_classification,
)

__all__ = [
    "APPLICATION_ID",
    "AnchoredRunDirectory",
    "CandidateQueries",
    "CandidateTargetError",
    "CanonicalRepository",
    "CanonicalStore",
    "CostCalculationInput",
    "FactInput",
    "FactSessionLinkInput",
    "RECOVERY_FACT_TRANSPORT",
    "RECOVERY_LINK_METHOD",
    "RECOVERY_LINK_RULE_VERSION",
    "RecoveredWorkClaimInput",
    "FactRepository",
    "LIVE_STORE_FILENAME",
    "ManifestValidationError",
    "SCHEMA_VERSION",
    "ScratchSafetyError",
    "STATIC_UNSUPPORTED_LEGACY_EVENT_TYPE_COUNTS",
    "STATIC_UNSUPPORTED_LEGACY_ROW_COUNT",
    "SessionEdgeInput",
    "SessionInput",
    "SessionRepository",
    "SnapshotManifest",
    "SnapshotSafetyError",
    "SnapshotVerificationError",
    "SourceInstanceInput",
    "SourceRepository",
    "TaskAnchorRepository",
    "TaskCursor",
    "UsageDayQuery",
    "UsageMeasurementInput",
    "UsageRepository",
    "UNSUPPORTED_LEGACY_CURRENT_PRODUCT_STATE",
    "UNSUPPORTED_LEGACY_EVENT_TYPE_TO_FUTURE_PRODUCT_LANE",
    "UNSUPPORTED_LEGACY_MIGRATION_DISPOSITION",
    "UNSUPPORTED_LEGACY_ROUTING_POLICY_ID",
    "UNSUPPORTED_LEGACY_ROUTING_POLICY_VERSION",
    "UNSUPPORTED_LEGACY_ROUTING_RULES_DIGEST",
    "UnsupportedLegacyProductLane",
    "UnsupportedLegacyRoute",
    "UnsupportedLegacyRoutingError",
    "UnsupportedLegacyRoutingSummary",
    "VerifiedSnapshot",
    "WorkClaimInput",
    "canonical_hash",
    "create_anchored_run_directory",
    "route_unsupported_legacy_event_type",
    "summarize_unsupported_legacy_event_types",
    "validate_candidate_target",
    "verify_static_unsupported_legacy_classification",
]
