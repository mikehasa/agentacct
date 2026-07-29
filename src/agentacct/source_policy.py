"""Source-by-dimension authority for Evidence v2.

Authority is intentionally not a property of a source in the abstract.  A
Claude/Codex client log can be primary evidence for client-reported usage but
only corroborating evidence for task meaning; an MCP event can carry useful
task meaning while remaining an explicit claim about machine execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from .evidence import EvidenceEnvelope


class EvidenceDimension(str, Enum):
    SESSION_IDENTITY = "session_identity"
    ACTIVITY = "activity"
    TOOL_ACTIVITY = "tool_activity"
    FILE_CHANGE = "file_change"
    SUBAGENT_ACTIVITY = "subagent_activity"
    USAGE = "usage"
    COST = "cost"
    TASK_SEMANTICS = "task_semantics"
    DECISION = "decision"
    BLOCKER = "blocker"
    MACHINE_CHECK = "machine_check"
    PROCESS = "process"
    OUTCOME = "outcome"
    LIFECYCLE = "lifecycle"
    ARTIFACT = "artifact"
    WORK_MEANING = "work_meaning"


class Authority(str, Enum):
    NONE = "none"
    CLAIMED = "claimed"
    CORROBORATING = "corroborating"
    AUTHORITATIVE = "authoritative"


AUTHORITY_RANK = {
    Authority.NONE: 0,
    Authority.CLAIMED: 1,
    Authority.CORROBORATING: 2,
    Authority.AUTHORITATIVE: 3,
}


def _value(value: str | Enum) -> str:
    return str(value.value if isinstance(value, Enum) else value)


@dataclass(frozen=True)
class SourceAuthorityRule:
    source_type: str
    dimension: str
    authority: Authority
    reason: str
    source_system: str | None = None
    assertion: str = "observed"
    measurement_bases: tuple[str, ...] = ()
    priority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", _value(self.source_type))
        object.__setattr__(self, "dimension", _value(self.dimension))
        object.__setattr__(self, "authority", Authority(self.authority))
        object.__setattr__(self, "measurement_bases", tuple(sorted({_value(value) for value in self.measurement_bases})))
        if self.assertion not in {"observed", "claimed", "*"}:
            raise ValueError("rule assertion must be observed, claimed, or *")
        if not self.source_type or not self.dimension or not self.reason:
            raise ValueError("authority rules require source_type, dimension, and reason")
        if not isinstance(self.priority, int):
            raise TypeError("authority rule priority must be an integer")

    def matches(
        self,
        *,
        source_type: str,
        source_system: str,
        assertion: str,
        dimension: str,
        measurement_basis: str,
    ) -> bool:
        return (
            self.source_type in {"*", source_type}
            and self.dimension in {"*", dimension}
            and (self.source_system is None or self.source_system == source_system)
            and self.assertion in {"*", assertion}
            and (not self.measurement_bases or measurement_basis in self.measurement_bases)
        )

    @property
    def specificity(self) -> int:
        return (
            int(self.source_type != "*")
            + int(self.dimension != "*")
            + int(self.source_system is not None)
            + int(self.assertion != "*")
            + int(bool(self.measurement_bases))
        )


@dataclass(frozen=True)
class AuthorityDecision:
    dimension: str
    authority: Authority
    reason: str
    source_type: str
    source_system: str
    measurement_basis: str | None
    matched_rule: SourceAuthorityRule | None = None

    @property
    def is_authoritative(self) -> bool:
        return self.authority is Authority.AUTHORITATIVE

    @property
    def rank(self) -> int:
        return AUTHORITY_RANK[self.authority]


class SourceAuthorityPolicy:
    """Immutable ordered rule set with a deny-by-default fallback."""

    def __init__(self, rules: Iterable[SourceAuthorityRule]) -> None:
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[SourceAuthorityRule, ...]:
        return self._rules

    def evaluate(self, envelope: EvidenceEnvelope, dimension: str | EvidenceDimension) -> AuthorityDecision:
        dimension_value = _value(dimension)
        if dimension_value not in envelope.dimensions:
            return AuthorityDecision(
                dimension=dimension_value,
                authority=Authority.NONE,
                reason="envelope_does_not_cover_dimension",
                source_type=envelope.source_type,
                source_system=envelope.source_system,
                measurement_basis=None,
            )
        basis = envelope.measurement_basis[dimension_value]
        # Claimed facts never become machine observations merely because their
        # writer uses a source type that is authoritative for observed facts.
        if envelope.assertion == "claimed":
            return AuthorityDecision(
                dimension=dimension_value,
                authority=Authority.CLAIMED,
                reason="explicit_claim_not_observation",
                source_type=envelope.source_type,
                source_system=envelope.source_system,
                measurement_basis=basis,
            )
        return self.evaluate_source(
            source_type=envelope.source_type,
            source_system=envelope.source_system,
            assertion=envelope.assertion,
            dimension=dimension_value,
            measurement_basis=basis,
        )

    def evaluate_source(
        self,
        *,
        source_type: str,
        source_system: str,
        assertion: str,
        dimension: str | EvidenceDimension,
        measurement_basis: str,
    ) -> AuthorityDecision:
        dimension_value = _value(dimension)
        if assertion == "claimed":
            return AuthorityDecision(
                dimension=dimension_value,
                authority=Authority.CLAIMED,
                reason="explicit_claim_not_observation",
                source_type=source_type,
                source_system=source_system,
                measurement_basis=measurement_basis,
            )
        candidates = [
            rule
            for rule in self._rules
            if rule.matches(
                source_type=source_type,
                source_system=source_system,
                assertion=assertion,
                dimension=dimension_value,
                measurement_basis=measurement_basis,
            )
        ]
        if not candidates:
            return AuthorityDecision(
                dimension=dimension_value,
                authority=Authority.NONE,
                reason="no_source_dimension_authority_rule",
                source_type=source_type,
                source_system=source_system,
                measurement_basis=measurement_basis,
            )
        selected = max(candidates, key=lambda rule: (rule.priority, rule.specificity))
        return AuthorityDecision(
            dimension=dimension_value,
            authority=selected.authority,
            reason=selected.reason,
            source_type=source_type,
            source_system=source_system,
            measurement_basis=measurement_basis,
            matched_rule=selected,
        )

    # Friendly aliases for integration code and policy-focused tests.
    authority_for = evaluate

    def decisions(self, envelope: EvidenceEnvelope) -> tuple[AuthorityDecision, ...]:
        return tuple(self.evaluate(envelope, dimension) for dimension in envelope.dimensions)

    def authoritative_dimensions(self, envelope: EvidenceEnvelope) -> tuple[str, ...]:
        return tuple(decision.dimension for decision in self.decisions(envelope) if decision.is_authoritative)

    def prefer(
        self,
        left: EvidenceEnvelope,
        right: EvidenceEnvelope,
        dimension: str | EvidenceDimension,
    ) -> EvidenceEnvelope | None:
        """Select the higher-authority envelope, refusing an authority tie."""

        left_decision = self.evaluate(left, dimension)
        right_decision = self.evaluate(right, dimension)
        if left_decision.rank == right_decision.rank:
            return None
        return left if left_decision.rank > right_decision.rank else right


def _rules_for_sources(
    source_types: Sequence[str],
    dimension: str | EvidenceDimension,
    authority: Authority,
    reason: str,
    *,
    bases: Sequence[str] = (),
    priority: int = 0,
) -> list[SourceAuthorityRule]:
    return [
        SourceAuthorityRule(
            source_type=source_type,
            dimension=_value(dimension),
            authority=authority,
            reason=reason,
            measurement_bases=tuple(bases),
            priority=priority,
        )
        for source_type in source_types
    ]


def default_source_authority_policy() -> SourceAuthorityPolicy:
    """Product-default authority without vendor-specific hard coding.

    Adapters should use these stable source types.  Vendor/system identity is
    retained in the envelope for filtering and future overrides, while the
    default policy describes *how* a fact was captured.
    """

    rules: list[SourceAuthorityRule] = []
    rules += _rules_for_sources(
        ("provider_invoice",),
        EvidenceDimension.COST,
        Authority.AUTHORITATIVE,
        "provider_billing_record",
    )
    rules += _rules_for_sources(
        ("provider_api", "provider_proxy"),
        EvidenceDimension.USAGE,
        Authority.AUTHORITATIVE,
        "provider_response_usage",
        bases=("provider_reported", "provider_billed"),
        priority=10,
    )
    rules += _rules_for_sources(
        ("provider_api", "provider_proxy"),
        EvidenceDimension.USAGE,
        Authority.CORROBORATING,
        "provider_transport_usage_with_unclassified_measurement_basis",
    )
    rules += _rules_for_sources(
        ("provider_api", "provider_proxy"),
        EvidenceDimension.COST,
        Authority.AUTHORITATIVE,
        "provider_billed_cost",
        bases=("provider_billed",),
        priority=10,
    )
    rules += _rules_for_sources(
        ("provider_api", "provider_proxy"),
        EvidenceDimension.COST,
        Authority.CORROBORATING,
        "provider_or_proxy_cost_estimate",
    )

    local_logs = (
        "local_client_log",
        "local_import",
        "log_import",
        "client_log",
        "client_transcript",
        "client_usage_store",
    )
    rules += _rules_for_sources(
        local_logs,
        EvidenceDimension.USAGE,
        Authority.AUTHORITATIVE,
        "client_reported_usage_from_client_store",
        bases=("client_reported", "local_client_reported"),
        priority=10,
    )
    rules += _rules_for_sources(
        local_logs,
        EvidenceDimension.USAGE,
        Authority.CORROBORATING,
        "client_store_usage_with_unclassified_measurement_basis",
    )
    rules += _rules_for_sources(
        local_logs,
        EvidenceDimension.COST,
        Authority.CORROBORATING,
        "client_reported_or_estimated_cost_not_invoice",
    )
    rules += _rules_for_sources(
        local_logs,
        EvidenceDimension.SESSION_IDENTITY,
        Authority.AUTHORITATIVE,
        "client_store_session_identity",
    )
    rules += _rules_for_sources(
        local_logs,
        EvidenceDimension.ACTIVITY,
        Authority.CORROBORATING,
        "client_log_activity_is_partial",
    )

    hooks = ("client_hook", "runtime_hook", "native_hook", "official_hook")
    for dimension in (
        EvidenceDimension.SESSION_IDENTITY,
        EvidenceDimension.ACTIVITY,
        EvidenceDimension.TOOL_ACTIVITY,
        EvidenceDimension.FILE_CHANGE,
        EvidenceDimension.SUBAGENT_ACTIVITY,
        EvidenceDimension.LIFECYCLE,
    ):
        rules += _rules_for_sources(hooks, dimension, Authority.AUTHORITATIVE, "official_runtime_hook_observation")
    rules += _rules_for_sources(
        hooks,
        EvidenceDimension.MACHINE_CHECK,
        Authority.AUTHORITATIVE,
        "hook_observed_process_exit",
        bases=("process_exit_code", "hook_exit_code"),
        priority=10,
    )
    rules += _rules_for_sources(
        hooks,
        EvidenceDimension.MACHINE_CHECK,
        Authority.CORROBORATING,
        "hook_activity_without_verified_exit",
    )

    wrappers = ("process_wrapper", "runner")
    for dimension in (
        EvidenceDimension.PROCESS,
        EvidenceDimension.ACTIVITY,
        EvidenceDimension.MACHINE_CHECK,
        EvidenceDimension.OUTCOME,
    ):
        rules += _rules_for_sources(wrappers, dimension, Authority.AUTHORITATIVE, "chronicle_owned_process_observation")
    for dimension in (EvidenceDimension.MACHINE_CHECK, EvidenceDimension.OUTCOME):
        rules += _rules_for_sources(("ci",), dimension, Authority.AUTHORITATIVE, "ci_machine_result")

    # OpenLIT's OTLP export is a transport for runtime observations, not a
    # provider invoice.  Lifecycle/tool facts are primary mechanical evidence;
    # token/cost fields remain corroborating unless a separate provider source
    # establishes stronger billing authority.
    for dimension in (
        EvidenceDimension.LIFECYCLE,
        EvidenceDimension.SESSION_IDENTITY,
        EvidenceDimension.ACTIVITY,
        EvidenceDimension.TOOL_ACTIVITY,
    ):
        rules += _rules_for_sources(
            ("otlp_http_json", "otlp"),
            dimension,
            Authority.AUTHORITATIVE,
            "runtime_telemetry_observation",
        )
    for dimension in (EvidenceDimension.USAGE, EvidenceDimension.COST):
        rules += _rules_for_sources(
            ("otlp_http_json", "otlp"),
            dimension,
            Authority.CORROBORATING,
            "telemetry_value_not_provider_invoice",
        )

    # Entire/Git checkpoint metadata proves that a commit/checkpoint artifact
    # exists.  It does not by itself prove task completion or provider usage.
    rules += _rules_for_sources(
        ("git_checkpoint",),
        EvidenceDimension.ARTIFACT,
        Authority.AUTHORITATIVE,
        "git_object_and_checkpoint_metadata_observed",
    )
    for dimension in (EvidenceDimension.LIFECYCLE, EvidenceDimension.USAGE, EvidenceDimension.COST):
        rules += _rules_for_sources(
            ("git_checkpoint",),
            dimension,
            Authority.CORROBORATING,
            "checkpoint_metadata_is_partial_context",
        )

    # Explicitly describe MCP/manual observed rows as non-authoritative.  Their
    # normal representation is ``assertion=claimed``, which is short-circuited
    # to Authority.CLAIMED above; these rules safely handle malformed legacy
    # rows that say observed.
    for source_type in ("mcp", "mcp_agent_reported", "manual", "generic_http"):
        rules.append(
            SourceAuthorityRule(
                source_type=source_type,
                dimension="*",
                authority=Authority.NONE,
                reason="self_report_cannot_be_upgraded_to_observation",
                assertion="observed",
            )
        )
    return SourceAuthorityPolicy(rules)


DEFAULT_SOURCE_AUTHORITY_POLICY = default_source_authority_policy()
AuthorityLevel = Authority


__all__ = [
    "AUTHORITY_RANK",
    "DEFAULT_SOURCE_AUTHORITY_POLICY",
    "Authority",
    "AuthorityDecision",
    "AuthorityLevel",
    "EvidenceDimension",
    "SourceAuthorityPolicy",
    "SourceAuthorityRule",
    "default_source_authority_policy",
]
