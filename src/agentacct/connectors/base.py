"""Shared, vendor-neutral primitives for read-only evidence connectors.

Connectors deliberately stop at :class:`ConnectorRecord`.  The record is a
small, deterministic normalization seam that can be inspected and tested
without giving an adapter access to agentacct's stores or to an upstream
system.  ``to_evidence_envelope`` is the only dependency on the evidence-v2
kernel and is imported lazily so the connector package remains independently
testable during the shadow-kernel rollout.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

ADAPTER_VERSION = "1"

_COMPLETENESS = frozenset({"complete", "partial", "unknown"})
_EVIDENCE_TYPES = frozenset({"observation", "claim", "derived"})
_ATTRIBUTION = frozenset({"direct", "heuristic", "unknown"})
_CORE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")


class ConnectorError(ValueError):
    """Base class for bounded connector parsing errors."""


class EvidenceCoreUnavailable(RuntimeError):
    """Raised when callers request an envelope before evidence v2 is present."""


def _json_safe(value: Any) -> JsonValue:
    """Return a canonical JSON-safe copy, rejecting non-finite numbers.

    Connector outputs must never depend on ``repr`` of vendor objects.  The
    accepted surface is intentionally limited to ordinary decoded JSON.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConnectorError("connector records cannot contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise ConnectorError(f"unsupported connector JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Canonical JSON used for hashes, ordering, and replay identity."""

    return json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stable_digest(value: Any) -> str:
    """SHA-256 of decoded input without retaining the input itself."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json_document(source: Mapping[str, Any] | Sequence[Any] | str | bytes | Path) -> Any:
    """Load an exported JSON document without network or upstream writes."""

    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
        return list(source)
    if isinstance(source, Path):
        return json.loads(source.read_text(encoding="utf-8"))
    if isinstance(source, bytes):
        return json.loads(source.decode("utf-8"))
    if isinstance(source, str):
        stripped = source.lstrip()
        if stripped.startswith(("{", "[")):
            return json.loads(source)
        return json.loads(Path(source).read_text(encoding="utf-8"))
    raise ConnectorError(f"unsupported JSON source: {type(source).__name__}")


@dataclass(frozen=True, slots=True)
class ConnectorRecord:
    """A deterministic connector result awaiting evidence-kernel ingestion."""

    connector: str
    source_type: str
    event_kind: str
    source_event_id: str
    evidence_type: str
    measurement_basis: str
    completeness: str
    subjects: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    occurred_at: str | None = None
    observed_at: str | None = None
    source_instance_id: str = "local"
    vendor_schema_version: str | None = None
    adapter_version: str = ADAPTER_VERSION
    usage_confidence: str = "unknown"
    cost_confidence: str = "unknown"
    capture_level: str = "metadata_only"
    redaction_profile: str = "connector_metadata_allowlist_v1"
    attribution: str = "unknown"
    truncation_reason: str | None = None
    raw_digest: str | None = None
    upstream_sha: str | None = None
    license_id: str | None = None

    def __post_init__(self) -> None:
        required = {
            "connector": self.connector,
            "source_type": self.source_type,
            "event_kind": self.event_kind,
            "source_event_id": self.source_event_id,
            "measurement_basis": self.measurement_basis,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ConnectorError("missing connector record fields: " + ", ".join(missing))
        if self.evidence_type not in _EVIDENCE_TYPES:
            raise ConnectorError(f"invalid evidence_type: {self.evidence_type}")
        if self.completeness not in _COMPLETENESS:
            raise ConnectorError(f"invalid completeness: {self.completeness}")
        if self.attribution not in _ATTRIBUTION:
            raise ConnectorError(f"invalid attribution: {self.attribution}")
        object.__setattr__(
            self,
            "subjects",
            {
                str(key): str(value)
                for key, value in sorted(self.subjects.items())
                if value is not None and str(value).strip()
            },
        )
        safe_attributes = _json_safe(dict(self.attributes))
        if not isinstance(safe_attributes, dict):  # pragma: no cover - construction invariant
            raise ConnectorError("attributes must be an object")
        object.__setattr__(self, "attributes", safe_attributes)

    @property
    def idempotency_key(self) -> str:
        material = {
            "adapter_version": self.adapter_version,
            "connector": self.connector,
            "event_kind": self.event_kind,
            "source_event_id": self.source_event_id,
            "source_instance_id": self.source_instance_id,
            "source_type": self.source_type,
        }
        return f"connector:{stable_digest(material)}"

    @property
    def record_id(self) -> str:
        return f"ev_{stable_digest(self.idempotency_key)[:32]}"

    def to_dict(self) -> dict[str, JsonValue]:
        """Serializable connector form used by fixtures and dry-run tooling."""

        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "connector": self.connector,
            "source_type": self.source_type,
            "source_instance_id": self.source_instance_id,
            "source_event_id": self.source_event_id,
            "vendor_schema_version": self.vendor_schema_version,
            "adapter_version": self.adapter_version,
            "event_kind": self.event_kind,
            "evidence_type": self.evidence_type,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "measurement_basis": self.measurement_basis,
            "completeness": self.completeness,
            "truncation_reason": self.truncation_reason,
            "usage_confidence": self.usage_confidence,
            "cost_confidence": self.cost_confidence,
            "capture_level": self.capture_level,
            "redaction_profile": self.redaction_profile,
            "sensitive_fields_present": False,
            "attribution": self.attribution,
            "subjects": dict(self.subjects),
            "attributes": dict(self.attributes),
            "raw_digest": self.raw_digest,
            "upstream_sha": self.upstream_sha,
            "license_id": self.license_id,
        }

    def to_evidence_dict(self, *, observed_at: str | None = None) -> dict[str, Any]:
        """Return the evidence kernel's canonical, integrity-checked form."""

        envelope = self.to_evidence_envelope(observed_at=observed_at)
        to_dict = getattr(envelope, "to_dict", None)
        if not callable(to_dict):  # pragma: no cover - evidence-kernel invariant
            raise EvidenceCoreUnavailable("EvidenceEnvelope exposes no to_dict API")
        return to_dict()

    def _evidence_dimensions(self) -> tuple[str, ...]:
        """Project a connector event onto evidence-v2 authority dimensions."""

        dimensions: set[str] = set()
        kind = self.event_kind
        if ".cost." in kind or "cost_usd" in self.attributes or "amount_usd" in self.attributes:
            dimensions.add("cost")
        if ".usage." in kind or any(
            key in self.attributes
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        ):
            dimensions.add("usage")
        if any(token in kind for token in ("work_product", "checkpoint")):
            dimensions.add("artifact")
        if any(token in kind for token in ("session", "turn", "tool", "span", "execution", "agent", "company")):
            dimensions.add("lifecycle")
        if any(token in kind for token in ("work_item", "execution")):
            dimensions.add("work_meaning")
        if "execution" in kind and any(key in self.attributes for key in ("status", "exit_code")):
            dimensions.add("outcome")
        return tuple(sorted(dimensions or {"lifecycle"}))

    @staticmethod
    def _core_token(value: str, prefix: str) -> str:
        if _CORE_TOKEN.fullmatch(value):
            return value
        return f"{prefix}_{stable_digest(value)[:24]}"

    def to_evidence_envelope(self, *, observed_at: str | None = None) -> Any:
        """Construct the core ``EvidenceEnvelope`` through its public seam.

        Export formats do not always carry a timestamp.  In that case callers
        must provide the import observation time explicitly; the adapter never
        invents a historical event time or makes replay identity depend on the
        current clock.
        """

        try:
            from agentacct.evidence import (
                Completeness,
                EvidenceEnvelope,
                PrivacyMetadata,
                Truncation,
            )
        except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - rollout seam
            raise EvidenceCoreUnavailable(
                "agentacct.evidence is not installed; use to_evidence_dict() during shadow rollout"
            ) from exc

        create = getattr(EvidenceEnvelope, "create", None)
        if not callable(create):  # pragma: no cover - defensive compatibility
            raise EvidenceCoreUnavailable(
                "EvidenceEnvelope exposes no compatible create API"
            )

        if self.evidence_type == "derived":
            raise ConnectorError(
                "the evidence kernel does not accept connector-minted derived evidence"
            )

        event_timestamp = self.occurred_at or self.observed_at or observed_at
        observation_timestamp = self.observed_at or observed_at or self.occurred_at
        if event_timestamp is None or observation_timestamp is None:
            raise ConnectorError(
                "connector source has no timestamp; pass observed_at explicitly when creating the envelope"
            )
        dimensions = self._evidence_dimensions()
        notes = (self.truncation_reason,) if self.truncation_reason else ()
        completeness = Completeness(
            status=self.completeness,
            covered_dimensions=dimensions if self.completeness == "complete" else (),
            note_codes=notes,
        )
        truncation = (
            Truncation(truncated=True, reason_code=self.truncation_reason)
            if self.truncation_reason
            else Truncation()
        )
        privacy = PrivacyMetadata(
            classification="internal",
            content_capture=self.capture_level,
            redacted=True,
            redaction_methods=(self.redaction_profile,),
            raw_content_included=False,
        )
        payload = {
            "attributes": dict(self.attributes),
            "attribution": self.attribution,
            "confidence": {
                "usage": self.usage_confidence,
                "cost": self.cost_confidence,
            },
            "connector_provenance": {
                "upstream_commit": self.upstream_sha,
                "license": self.license_id,
            },
        }
        return create(
            assertion="claimed" if self.evidence_type == "claim" else "observed",
            event_type=self.event_kind,
            source_type=self.source_type,
            source_system=self.connector,
            source_instance=self._core_token(self.source_instance_id, "instance"),
            source_schema=self._core_token(self.vendor_schema_version or "unknown", "schema"),
            adapter=self._core_token(f"{self.connector}.connector.v{self.adapter_version}", "adapter"),
            source_event_id=self.source_event_id,
            event_timestamp=event_timestamp,
            observed_at=observation_timestamp,
            dimensions=dimensions,
            measurement_basis=self.measurement_basis,
            completeness=completeness,
            truncation=truncation,
            privacy=privacy,
            subjects=dict(self.subjects),
            payload=payload,
            raw_digest=self.raw_digest,
            claimant=self.connector if self.evidence_type == "claim" else None,
            tags=("connector", self.connector, f"license:{self.license_id or 'unknown'}"),
        )


class ReadOnlyConnector(ABC):
    """Connector interface intentionally exposing no mutation operation."""

    name: str
    source_type: str
    upstream_sha: str
    license_id: str

    @abstractmethod
    def read(self, source: Any = None) -> tuple[ConnectorRecord, ...]:
        """Parse current upstream evidence without changing upstream state."""
