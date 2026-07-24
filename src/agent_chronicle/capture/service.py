"""Fail-open local write service for normalized hook observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .base import CAPTURE_SCHEMA_VERSION, CaptureContext, CaptureObservation, CaptureResult, safe_token
from .registry import DEFAULT_CAPTURE_REGISTRY, CaptureRegistry


class EvidenceSink(Protocol):
    """Narrow seam implemented by the local evidence spool/store."""

    def append(self, envelope: Any) -> Any: ...


EnvelopeFactory = Callable[[CaptureObservation], Any]


@dataclass(frozen=True)
class CaptureWriteResult:
    capture: CaptureResult
    attempted_count: int = 0
    stored_count: int = 0
    write_errors: tuple[str, ...] = ()
    fail_open: bool = True

    @property
    def ok(self) -> bool:
        return not self.write_errors and not self.capture.ignored


def _dimensions_for(observation: CaptureObservation) -> tuple[str, ...]:
    if observation.event_type == "machine_check_observed":
        return ("activity", "machine_check")
    if observation.event_type == "session_link_observed":
        if observation.attributes.get("relationship") == "spawned_subagent":
            return ("session_identity", "subagent_activity")
        return ("session_identity",)
    if observation.event_type == "session_observed":
        return ("activity", "lifecycle", "session_identity")
    if observation.event_type == "tool_observed":
        dimensions = ["activity", "session_identity", "tool_activity"]
        if observation.attributes.get("files"):
            dimensions.append("file_change")
        return tuple(dimensions)
    return ("activity", "session_identity")


def _default_envelope_factory(
    observation: CaptureObservation,
    *,
    source_instance: str,
) -> Any:
    # Lazy import keeps capture parsing independent of the shadow evidence
    # kernel and makes the hook return cleanly when v2 is disabled/unavailable.
    from agent_chronicle.evidence import (
        Completeness,
        EvidenceEnvelope,
        PrivacyMetadata,
        SubjectRefs,
    )

    dimensions = _dimensions_for(observation)
    missing_fields = set(observation.completeness.missing_fields)
    missing: set[str] = set()
    if missing_fields & {"session_id", "turn_id", "parent_session_id", "linked_session_id"}:
        missing.add("session_identity")
    if "tool_identity" in missing_fields:
        missing.add("tool_activity")
    if missing_fields & {"parent_session_id", "linked_session_id"}:
        missing.add("subagent_activity")
    missing_dimensions = tuple(value for value in dimensions if value in missing)
    covered_dimensions = tuple(value for value in dimensions if value not in missing_dimensions)
    note_codes = tuple(
        sorted(
            set(observation.completeness.missing_fields)
            | set(observation.completeness.limitations)
        )
    )
    subjects = SubjectRefs(
        client_session_id=observation.session_id or None,
        parent_client_session_id=observation.parent_session_id or None,
        turn_id=observation.turn_id or None,
        tool_call_id=observation.tool_call_id or None,
        extra={
            key: value
            for key, value in {"linked_session_id": observation.linked_session_id}.items()
            if value
        },
    )
    return EvidenceEnvelope.create(
        assertion="observed",
        event_type=observation.event_type,
        source_type="client_hook",
        source_system=observation.vendor,
        source_instance=source_instance,
        source_schema=CAPTURE_SCHEMA_VERSION,
        adapter=f"capture.{observation.vendor}.v1",
        source_event_id=observation.source_event_id,
        event_timestamp=observation.observed_at,
        observed_at=observation.observed_at,
        dimensions=dimensions,
        measurement_basis={
            dimension: "hook_exit_code" if dimension == "machine_check" else "client_hook_observed"
            for dimension in dimensions
        },
        completeness=Completeness(
            status=observation.completeness.status,
            covered_dimensions=covered_dimensions,
            missing_dimensions=missing_dimensions,
            note_codes=note_codes,
        ),
        privacy=PrivacyMetadata(classification="internal", content_capture="metadata_only"),
        subjects=subjects,
        payload={
            "host_event": observation.host_event,
            "attributes": dict(observation.attributes),
            "capture_basis": observation.completeness.basis,
        },
        raw_digest=observation.raw_digest or None,
        tags=("host_hook", "mechanical_capture"),
    )


class CaptureService:
    """Normalize one hook payload and append only to a caller-supplied local sink.

    All input, adapter, envelope, and sink failures become structured results;
    none escape into the coding agent's hook process.
    """

    def __init__(
        self,
        sink: EvidenceSink,
        *,
        registry: CaptureRegistry = DEFAULT_CAPTURE_REGISTRY,
        envelope_factory: EnvelopeFactory | None = None,
        source_instance: str = "local-hooks",
    ) -> None:
        normalized_instance = safe_token(source_instance)
        if not normalized_instance:
            raise ValueError("source_instance must be a stable token")
        self.sink = sink
        self.registry = registry
        self.source_instance = normalized_instance
        self.envelope_factory = envelope_factory or (
            lambda observation: _default_envelope_factory(
                observation,
                source_instance=self.source_instance,
            )
        )

    def capture(
        self,
        vendor: str,
        payload: Mapping[str, Any] | str | bytes | bytearray,
        *,
        context: CaptureContext | None = None,
        host_event: str = "",
    ) -> CaptureWriteResult:
        try:
            normalized = self.registry.normalize(
                vendor,
                payload,
                context=context,
                host_event=host_event,
            )
        except Exception as exc:  # Defensive: custom registries remain fail-open.
            return CaptureWriteResult(
                capture=CaptureResult(
                    ignored_reason="registry_failed",
                    warnings=(f"registry_error:{type(exc).__name__}",),
                ),
                write_errors=("registry_failed",),
            )
        if normalized.ignored or not normalized.observations:
            return CaptureWriteResult(capture=normalized)

        envelopes: list[Any] = []
        errors: list[str] = []
        for observation in normalized.observations:
            try:
                envelopes.append(self.envelope_factory(observation))
            except Exception as exc:
                errors.append(f"envelope_error:{type(exc).__name__}")
        if not envelopes:
            return CaptureWriteResult(
                capture=normalized,
                attempted_count=len(normalized.observations),
                write_errors=tuple(errors or ("envelope_failed",)),
            )

        stored = 0
        append_one = getattr(self.sink, "append", None)
        if callable(append_one):
            # The evidence spool commits each envelope independently.  Append
            # in order so a failure after a durable prefix can be reported as
            # exactly N stored records instead of the false all-or-nothing 0.
            for envelope in envelopes:
                try:
                    append_one(envelope)
                    stored += 1
                except Exception as exc:
                    errors.append(f"sink_error:{type(exc).__name__}")
                    break
        else:
            # Compatibility for narrow test/custom sinks that only implement
            # the older batch seam.  A returned receipt sequence gives a
            # truthful count; ``None`` retains the legacy success contract.
            try:
                append_many = getattr(self.sink, "append_many", None)
                if not callable(append_many):
                    raise TypeError("sink does not provide append or append_many")
                batch_result = append_many(tuple(envelopes))
                if isinstance(batch_result, (tuple, list)):
                    stored = min(len(batch_result), len(envelopes))
                    if stored != len(envelopes):
                        errors.append("sink_error:PartialWrite")
                else:
                    stored = len(envelopes)
            except Exception as exc:
                errors.append(f"sink_error:{type(exc).__name__}")
        return CaptureWriteResult(
            capture=normalized,
            attempted_count=len(normalized.observations),
            stored_count=stored,
            write_errors=tuple(errors),
        )


__all__ = ["CaptureService", "CaptureWriteResult", "EnvelopeFactory", "EvidenceSink"]
