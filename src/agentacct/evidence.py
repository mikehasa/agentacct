"""Immutable, source-neutral evidence primitives.

Evidence v2 is deliberately additive.  It does not know about the historical
``events.jsonl`` ledger and it does not assign trust merely because a writer
was able to submit an event.  An envelope says either that a source *observed*
something or that a claimant *claimed* something; the two states are mutually
exclusive and downstream authority is decided per evidence dimension.

The model also makes retries and source corrections distinguishable:

* ``idempotency_key`` identifies one logical event in one source instance;
* ``integrity_hash`` identifies the exact immutable envelope content;
* ``evidence_id`` is derived from that exact content.

Consequently an exact retry is a duplicate, while the same source event id
with changed content is a conflict version that a store can preserve.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


EVIDENCE_SCHEMA_VERSION = "agent-chronicle.evidence.v2"
CLAIMED_LINK_SCHEMA_VERSION = "agent-chronicle.claimed-link.v1"

_ASSERTIONS = frozenset({"observed", "claimed"})
_COMPLETENESS_STATES = frozenset({"complete", "partial", "unknown"})
_PRIVACY_CLASSES = frozenset({"public", "internal", "sensitive", "restricted"})
_CONTENT_CAPTURE_STATES = frozenset({"none", "metadata_only", "redacted_excerpt", "full"})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")
_EVIDENCE_ID_RE = re.compile(r"^evd_[0-9a-f]{64}\Z")
_LINK_ID_RE = re.compile(r"^clm_[0-9a-f]{64}\Z")
_IDEMPOTENCY_RE = re.compile(r"^idem_[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})\Z")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

# Prompt and tool bodies are not evidence metadata.  They require an explicit
# opt-in at envelope creation and a privacy classification that acknowledges
# the content capture.  Generic keys such as ``result`` and ``input_tokens``
# are intentionally absent: machine outcomes and usage counters remain safe.
_BODY_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "system_prompt",
        "user_prompt",
        "raw_prompt",
        "completion",
        "raw_completion",
        "request_body",
        "response_body",
        "message_body",
        "raw_request",
        "raw_response",
        "raw_content",
        "body",
        "content",
        "arguments",
        "args",
        "command",
        "input",
        "output",
        "query",
        "thought",
        "reasoning_content",
        "tool_input",
        "tool_output",
        "tool_arguments",
        "tool_result",
        "transcript",
        "input_text",
        "output_text",
    }
)
_CONTENT_PARENT_KEYS = frozenset({"message", "messages", "request", "response", "tool", "assistant", "user"})
_ARGUMENT_PARENT_KEYS = frozenset({"tool", "tool_call", "call", "function"})
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "auth_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "access_token",
        "refresh_token",
        "session_token",
        "token",
        "cookie",
        "set_cookie",
        "private_key",
        "credential",
        "credentials",
        "headers",
        "http_headers",
        "request_headers",
        "response_headers",
        "proxy_authorization",
        "aws_access_key_id",
        "aws_secret_access_key",
    }
)
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_KEY_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _canonical_jsonable(value: Any) -> Any:
    """Return a plain, deterministic JSON value from frozen model values."""

    if isinstance(value, Mapping):
        return {str(key): _canonical_jsonable(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical_jsonable(child) for child in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence values cannot contain NaN or infinity")
        return value
    raise TypeError(f"evidence values must be JSON-compatible, got {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON encoding used by every v2 digest."""

    return json.dumps(
        _canonical_jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def raw_digest_for(value: bytes | str | Mapping[str, Any] | Sequence[Any]) -> str:
    """Hash raw source material without retaining the material itself."""

    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_json_bytes(value)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("evidence mapping keys must be strings")
            frozen[key] = _deep_freeze(child)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(child) for child in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence values cannot contain NaN or infinity")
        return value
    raise TypeError(f"evidence values must be JSON-compatible, got {type(value).__name__}")


def _required_token(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a non-empty stable token")
    return value


def _optional_ref(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512 or "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must be a non-empty single-line string")
    return value


def normalize_timestamp(value: str | int | float | datetime, field_name: str = "timestamp") -> str:
    """Normalize an aware timestamp to stable UTC RFC3339 microseconds."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an aware timestamp")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{field_name} must be finite")
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be an aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    match = _DIGEST_RE.fullmatch(value)
    if not match:
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return f"sha256:{match.group(1).lower()}"


def canonical_metadata_key(key: Any) -> str:
    """Normalize snake/kebab/camelCase keys for privacy classification."""

    split = _CAMEL_ACRONYM_BOUNDARY_RE.sub(r"\1_\2", str(key).strip())
    split = _CAMEL_CASE_BOUNDARY_RE.sub("_", split)
    return _NON_KEY_TOKEN_RE.sub("_", split.lower()).strip("_")


def is_sensitive_metadata_key(key: Any) -> bool:
    """Recognize common credential/header names without matching token counts."""

    normalized = canonical_metadata_key(key)
    compact = normalized.replace("_", "")
    segments = {segment for segment in normalized.split("_") if segment}
    if normalized in _SECRET_KEYS or compact in {"apikey", "clientsecret", "privatekey", "accesskey"}:
        return True
    if segments & {"secret", "password", "passwd", "credential", "credentials", "authorization"}:
        return True
    if normalized.endswith(("_api_key", "_token")):
        return True
    if normalized.endswith(("_cookie", "_private_key", "_access_key")):
        return True
    return normalized in {"token", "cookie", "set_cookie", "bearer", "headers"}


def _is_content_key(key: str, parents: tuple[str, ...]) -> bool:
    normalized = canonical_metadata_key(key)
    if normalized in _BODY_KEYS:
        return True
    # List indexes are structural, not semantic parents (for example
    # messages.0.content is still message content).
    parent = next((value for value in reversed(parents) if not value.isdigit()), "")
    if normalized == "content" and parent in _CONTENT_PARENT_KEYS:
        return True
    if normalized in {"arguments", "args"} and parent in _ARGUMENT_PARENT_KEYS:
        return True
    return normalized.endswith("_prompt") or normalized.endswith("_tool_body")


def _matching_paths(
    value: Any,
    predicate: Callable[[str, tuple[str, ...]], bool],
    parents: tuple[str, ...] = (),
) -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if predicate(str(key), parents):
                paths.append(".".join((*parents, str(key))))
            else:
                paths.extend(
                    _matching_paths(
                        child,
                        predicate,
                        (*parents, canonical_metadata_key(key)),
                    )
                )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            paths.extend(_matching_paths(child, predicate, (*parents, str(index))))
    return tuple(paths)


def _secret_paths(value: Any) -> tuple[str, ...]:
    return _matching_paths(value, lambda key, _parents: is_sensitive_metadata_key(key))


def _content_paths(value: Any) -> tuple[str, ...]:
    return _matching_paths(value, _is_content_key)


def sanitize_evidence_payload(payload: Mapping[str, Any], *, include_content: bool = False) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    """Deep-freeze payload metadata and omit prompt/tool bodies by default."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    redacted: list[str] = []

    def walk(value: Any, parents: tuple[str, ...]) -> Any:
        if isinstance(value, Mapping):
            safe: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError("evidence mapping keys must be strings")
                normalized = canonical_metadata_key(key)
                path = ".".join((*parents, key))
                if is_sensitive_metadata_key(key) or (not include_content and _is_content_key(key, parents)):
                    redacted.append(path)
                    continue
                safe[key] = walk(child, (*parents, normalized))
            return MappingProxyType(safe)
        if isinstance(value, (list, tuple)):
            return tuple(walk(child, (*parents, str(index))) for index, child in enumerate(value))
        return _deep_freeze(value)

    return walk(payload, ()), tuple(sorted(set(redacted)))


def _redact_raw_ref(raw_ref: str | None) -> tuple[str | None, bool]:
    if raw_ref is None:
        return None, False
    raw_ref = _optional_ref(raw_ref, "raw_ref")
    assert raw_ref is not None
    if raw_ref.startswith("/") or _WINDOWS_ABSOLUTE_PATH_RE.match(raw_ref):
        leaf = raw_ref.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "source"
        leaf = re.sub(r"[^A-Za-z0-9._-]", "_", leaf)[:96]
        path_hash = hashlib.sha256(raw_ref.encode("utf-8")).hexdigest()[:16]
        return f"local-redacted://{leaf}#{path_hash}", True
    return raw_ref, False


@dataclass(frozen=True)
class Completeness:
    status: str = "unknown"
    covered_dimensions: tuple[str, ...] = ()
    missing_dimensions: tuple[str, ...] = ()
    note_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _COMPLETENESS_STATES:
            raise ValueError(f"unsupported completeness status: {self.status!r}")
        covered = tuple(sorted({_required_token(value, "covered_dimensions") for value in self.covered_dimensions}))
        missing = tuple(sorted({_required_token(value, "missing_dimensions") for value in self.missing_dimensions}))
        notes = tuple(sorted({_required_token(value, "note_codes") for value in self.note_codes}))
        if set(covered) & set(missing):
            raise ValueError("a dimension cannot be both covered and missing")
        object.__setattr__(self, "covered_dimensions", covered)
        object.__setattr__(self, "missing_dimensions", missing)
        object.__setattr__(self, "note_codes", notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "covered_dimensions": list(self.covered_dimensions),
            "missing_dimensions": list(self.missing_dimensions),
            "note_codes": list(self.note_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Completeness":
        return cls(
            status=str(value.get("status") or "unknown"),
            covered_dimensions=tuple(value.get("covered_dimensions") or ()),
            missing_dimensions=tuple(value.get("missing_dimensions") or ()),
            note_codes=tuple(value.get("note_codes") or ()),
        )


@dataclass(frozen=True)
class Truncation:
    truncated: bool = False
    reason_code: str | None = None
    original_count: int | None = None
    retained_count: int | None = None

    def __post_init__(self) -> None:
        if self.truncated and not self.reason_code:
            raise ValueError("truncated evidence requires a reason_code")
        if not self.truncated and any(value is not None for value in (self.reason_code, self.original_count, self.retained_count)):
            raise ValueError("non-truncated evidence cannot carry truncation details")
        if self.reason_code is not None:
            _required_token(self.reason_code, "reason_code")
        for name, value in (("original_count", self.original_count), ("retained_count", self.retained_count)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.original_count is not None and self.retained_count is not None and self.retained_count > self.original_count:
            raise ValueError("retained_count cannot exceed original_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "truncated": self.truncated,
            "reason_code": self.reason_code,
            "original_count": self.original_count,
            "retained_count": self.retained_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Truncation":
        return cls(
            truncated=value.get("truncated") is True,
            reason_code=value.get("reason_code"),
            original_count=value.get("original_count"),
            retained_count=value.get("retained_count"),
        )


@dataclass(frozen=True)
class PrivacyMetadata:
    classification: str = "internal"
    content_capture: str = "metadata_only"
    redacted: bool = False
    redacted_fields: tuple[str, ...] = ()
    redaction_methods: tuple[str, ...] = ()
    raw_content_included: bool = False

    def __post_init__(self) -> None:
        if self.classification not in _PRIVACY_CLASSES:
            raise ValueError(f"unsupported privacy classification: {self.classification!r}")
        if self.content_capture not in _CONTENT_CAPTURE_STATES:
            raise ValueError(f"unsupported content_capture: {self.content_capture!r}")
        fields = tuple(sorted({str(value) for value in self.redacted_fields if str(value)}))
        methods = tuple(sorted({_required_token(value, "redaction_methods") for value in self.redaction_methods}))
        if fields and not self.redacted:
            raise ValueError("redacted_fields require redacted=True")
        if self.raw_content_included:
            if self.content_capture != "full":
                raise ValueError("raw_content_included requires content_capture='full'")
            if self.classification not in {"sensitive", "restricted"}:
                raise ValueError("raw content requires sensitive or restricted classification")
        object.__setattr__(self, "redacted_fields", fields)
        object.__setattr__(self, "redaction_methods", methods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "content_capture": self.content_capture,
            "redacted": self.redacted,
            "redacted_fields": list(self.redacted_fields),
            "redaction_methods": list(self.redaction_methods),
            "raw_content_included": self.raw_content_included,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PrivacyMetadata":
        return cls(
            classification=str(value.get("classification") or "internal"),
            content_capture=str(value.get("content_capture") or "metadata_only"),
            redacted=value.get("redacted") is True,
            redacted_fields=tuple(value.get("redacted_fields") or ()),
            redaction_methods=tuple(value.get("redaction_methods") or ()),
            raw_content_included=value.get("raw_content_included") is True,
        )


@dataclass(frozen=True)
class SubjectRefs:
    project_id: str | None = None
    run_id: str | None = None
    client_session_id: str | None = None
    parent_client_session_id: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    request_id: str | None = None
    work_id: str | None = None
    section_id: str | None = None
    tool_call_id: str | None = None
    agent_id: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "run_id",
            "client_session_id",
            "parent_client_session_id",
            "turn_id",
            "message_id",
            "request_id",
            "work_id",
            "section_id",
            "tool_call_id",
            "agent_id",
        ):
            object.__setattr__(self, name, _optional_ref(getattr(self, name), name))
        if not isinstance(self.extra, Mapping):
            raise TypeError("subject extra refs must be a mapping")
        extra: dict[str, str] = {}
        for key, value in self.extra.items():
            _required_token(key, "subject extra key")
            normalized = _optional_ref(value, f"subject extra {key}")
            assert normalized is not None
            extra[key] = normalized
        object.__setattr__(self, "extra", MappingProxyType(dict(sorted(extra.items()))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "client_session_id": self.client_session_id,
            "parent_client_session_id": self.parent_client_session_id,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "request_id": self.request_id,
            "work_id": self.work_id,
            "section_id": self.section_id,
            "tool_call_id": self.tool_call_id,
            "agent_id": self.agent_id,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubjectRefs":
        aliases = {
            "project": "project_id",
            "run": "run_id",
            "execution": "run_id",
            "client_session": "client_session_id",
            "session": "client_session_id",
            "parent_client_session": "parent_client_session_id",
            "parent_session": "parent_client_session_id",
            "turn": "turn_id",
            "message": "message_id",
            "request": "request_id",
            "work": "work_id",
            "work_item": "work_id",
            "section": "section_id",
            "tool_call": "tool_call_id",
            "agent": "agent_id",
            "principal": "agent_id",
        }
        known = {
            "project_id",
            "run_id",
            "client_session_id",
            "parent_client_session_id",
            "turn_id",
            "message_id",
            "request_id",
            "work_id",
            "section_id",
            "tool_call_id",
            "agent_id",
        }
        normalized = dict(value)
        for alias, canonical in aliases.items():
            if not normalized.get(canonical) and normalized.get(alias) is not None:
                normalized[canonical] = normalized[alias]
        extra = dict(normalized.get("extra") or {})
        extra.update(
            {
                str(key): str(child)
                for key, child in normalized.items()
                if key not in known | set(aliases) | {"extra"} and child is not None
            }
        )
        return cls(**{key: normalized.get(key) for key in known}, extra=extra)


@dataclass(frozen=True)
class EvidenceEnvelope:
    schema_version: str
    assertion: str
    event_type: str
    source_type: str
    source_system: str
    source_instance: str
    source_schema: str
    adapter: str
    event_timestamp: str
    observed_at: str
    dimensions: tuple[str, ...]
    measurement_basis: Mapping[str, str]
    completeness: Completeness
    truncation: Truncation
    privacy: PrivacyMetadata
    subjects: SubjectRefs
    payload: Mapping[str, Any]
    source_event_id: str | None = None
    event_end_timestamp: str | None = None
    emitted_at: str | None = None
    raw_digest: str | None = None
    raw_ref: str | None = None
    claimant: str | None = None
    tags: tuple[str, ...] = ()
    idempotency_key: str = ""
    integrity_hash: str = ""
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EVIDENCE_SCHEMA_VERSION!r}")
        if self.assertion not in _ASSERTIONS:
            raise ValueError("assertion must be 'observed' or 'claimed'")
        for name in ("event_type", "source_type", "source_system", "source_instance", "source_schema", "adapter"):
            _required_token(getattr(self, name), name)
        claimant = _optional_ref(self.claimant, "claimant")
        if self.assertion == "claimed" and claimant is None:
            raise ValueError("claimed evidence requires claimant")
        if self.assertion == "observed" and claimant is not None:
            raise ValueError("observed evidence cannot carry claimant")
        object.__setattr__(self, "claimant", claimant)
        object.__setattr__(self, "source_event_id", _optional_ref(self.source_event_id, "source_event_id"))

        event_timestamp = normalize_timestamp(self.event_timestamp, "event_timestamp")
        observed_at = normalize_timestamp(self.observed_at, "observed_at")
        end_timestamp = normalize_timestamp(self.event_end_timestamp, "event_end_timestamp") if self.event_end_timestamp else None
        emitted_at = normalize_timestamp(self.emitted_at, "emitted_at") if self.emitted_at else None
        if end_timestamp is not None and end_timestamp < event_timestamp:
            raise ValueError("event_end_timestamp cannot precede event_timestamp")
        object.__setattr__(self, "event_timestamp", event_timestamp)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "event_end_timestamp", end_timestamp)
        object.__setattr__(self, "emitted_at", emitted_at)

        dimensions = tuple(sorted({_required_token(value, "dimension") for value in self.dimensions}))
        if not dimensions:
            raise ValueError("evidence requires at least one dimension")
        object.__setattr__(self, "dimensions", dimensions)
        if not isinstance(self.measurement_basis, Mapping):
            raise TypeError("measurement_basis must be a mapping")
        bases = {_required_token(key, "measurement dimension"): _required_token(value, "measurement basis") for key, value in self.measurement_basis.items()}
        if set(bases) != set(dimensions):
            raise ValueError("measurement_basis must define exactly every evidence dimension")
        object.__setattr__(self, "measurement_basis", MappingProxyType(dict(sorted(bases.items()))))

        if not isinstance(self.completeness, Completeness):
            raise TypeError("completeness must be Completeness")
        if not isinstance(self.truncation, Truncation):
            raise TypeError("truncation must be Truncation")
        if self.truncation.truncated and self.completeness.status == "complete":
            raise ValueError("truncated evidence cannot claim complete coverage")
        if self.completeness.status == "complete":
            if self.completeness.missing_dimensions:
                raise ValueError("complete evidence cannot list missing dimensions")
            if set(self.completeness.covered_dimensions) != set(dimensions):
                raise ValueError("complete evidence must cover exactly every evidence dimension")
        if not isinstance(self.privacy, PrivacyMetadata):
            raise TypeError("privacy must be PrivacyMetadata")
        if not isinstance(self.subjects, SubjectRefs):
            raise TypeError("subjects must be SubjectRefs")
        frozen_payload = _deep_freeze(self.payload)
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("payload must be a mapping")
        secret_paths = _secret_paths(frozen_payload)
        if secret_paths:
            raise ValueError(f"secret fields are never permitted in evidence payloads: {', '.join(secret_paths)}")
        if not self.privacy.raw_content_included:
            content_paths = _content_paths(frozen_payload)
            if content_paths:
                raise ValueError(f"prompt/tool body fields require explicit raw-content opt-in: {', '.join(content_paths)}")
        if len(canonical_json_bytes(frozen_payload)) > 262_144:
            raise ValueError("evidence payload exceeds 256 KiB; truncate at the adapter and record truncation metadata")
        object.__setattr__(self, "payload", frozen_payload)
        object.__setattr__(self, "raw_digest", _normalize_digest(self.raw_digest, "raw_digest"))
        object.__setattr__(self, "raw_ref", _optional_ref(self.raw_ref, "raw_ref"))
        object.__setattr__(self, "tags", tuple(sorted({_required_token(value, "tag") for value in self.tags})))

        expected_idempotency = self._derived_idempotency_key()
        if self.idempotency_key and self.idempotency_key != expected_idempotency:
            raise ValueError("idempotency_key does not match deterministic envelope identity")
        if self.idempotency_key and not _IDEMPOTENCY_RE.fullmatch(self.idempotency_key):
            raise ValueError("invalid idempotency_key")
        object.__setattr__(self, "idempotency_key", expected_idempotency)

        expected_integrity = canonical_digest(self._integrity_material())
        if self.integrity_hash and self.integrity_hash != expected_integrity:
            raise ValueError("integrity_hash does not match envelope content")
        object.__setattr__(self, "integrity_hash", expected_integrity)
        expected_evidence_id = f"evd_{expected_integrity.removeprefix('sha256:')}"
        if self.evidence_id and self.evidence_id != expected_evidence_id:
            raise ValueError("evidence_id does not match integrity_hash")
        if self.evidence_id and not _EVIDENCE_ID_RE.fullmatch(self.evidence_id):
            raise ValueError("invalid evidence_id")
        object.__setattr__(self, "evidence_id", expected_evidence_id)

    @classmethod
    def create(
        cls,
        *,
        assertion: str,
        event_type: str,
        source_type: str,
        source_system: str,
        event_timestamp: str | int | float | datetime,
        dimensions: Sequence[str],
        measurement_basis: str | Mapping[str, str],
        source_instance: str = "default",
        source_schema: str = "unknown",
        adapter: str = "unknown",
        observed_at: str | int | float | datetime | None = None,
        event_end_timestamp: str | int | float | datetime | None = None,
        emitted_at: str | int | float | datetime | None = None,
        completeness: Completeness | str | None = None,
        truncation: Truncation | None = None,
        privacy: PrivacyMetadata | None = None,
        subjects: SubjectRefs | Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        source_event_id: str | None = None,
        raw_digest: str | None = None,
        raw_ref: str | None = None,
        claimant: str | None = None,
        tags: Sequence[str] = (),
        include_raw_content: bool = False,
    ) -> "EvidenceEnvelope":
        normalized_dimensions = tuple(dimensions)
        if isinstance(measurement_basis, str):
            bases = {dimension: measurement_basis for dimension in normalized_dimensions}
        else:
            bases = dict(measurement_basis)
        if completeness is None:
            completeness_value = Completeness(status="unknown")
        elif isinstance(completeness, str):
            completeness_value = Completeness(status=completeness)
        else:
            completeness_value = completeness
        privacy_value = privacy or PrivacyMetadata()
        if include_raw_content:
            if not privacy_value.raw_content_included:
                raise ValueError("include_raw_content=True requires PrivacyMetadata(raw_content_included=True)")
        safe_payload, redacted_fields = sanitize_evidence_payload(payload or {}, include_content=include_raw_content)
        safe_raw_ref, raw_ref_redacted = _redact_raw_ref(raw_ref)
        all_redactions = set(privacy_value.redacted_fields) | set(redacted_fields)
        if raw_ref_redacted:
            all_redactions.add("raw_ref")
        if all_redactions:
            methods = set(privacy_value.redaction_methods)
            methods.add("field_omission" if redacted_fields else "path_pseudonymization")
            if raw_ref_redacted:
                methods.add("path_pseudonymization")
            privacy_value = replace(
                privacy_value,
                redacted=True,
                redacted_fields=tuple(sorted(all_redactions)),
                redaction_methods=tuple(sorted(methods)),
            )
        if subjects is None:
            subject_value = SubjectRefs()
        elif isinstance(subjects, SubjectRefs):
            subject_value = subjects
        else:
            subject_value = SubjectRefs.from_dict(subjects)
        event_time = normalize_timestamp(event_timestamp, "event_timestamp")
        observation_time = normalize_timestamp(observed_at if observed_at is not None else event_timestamp, "observed_at")
        return cls(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            assertion=assertion,
            event_type=event_type,
            source_type=source_type,
            source_system=source_system,
            source_instance=source_instance,
            source_schema=source_schema,
            adapter=adapter,
            event_timestamp=event_time,
            observed_at=observation_time,
            event_end_timestamp=normalize_timestamp(event_end_timestamp, "event_end_timestamp") if event_end_timestamp is not None else None,
            emitted_at=normalize_timestamp(emitted_at, "emitted_at") if emitted_at is not None else None,
            dimensions=normalized_dimensions,
            measurement_basis=bases,
            completeness=completeness_value,
            truncation=truncation or Truncation(),
            privacy=privacy_value,
            subjects=subject_value,
            payload=safe_payload,
            source_event_id=source_event_id,
            raw_digest=raw_digest,
            raw_ref=safe_raw_ref,
            claimant=claimant,
            tags=tuple(tags),
        )

    @property
    def occurred_at(self) -> str:
        return self.event_timestamp

    def __hash__(self) -> int:
        # Frozen nested mappings are intentionally read-only but mappingproxy
        # itself is not hashable.  The content-derived id is the exact stable
        # identity of an envelope version.
        return hash(self.evidence_id)

    def _identity_material(self) -> dict[str, Any]:
        source = {
            "source_type": self.source_type,
            "source_system": self.source_system,
            "source_instance": self.source_instance,
            "source_schema": self.source_schema,
        }
        if self.source_event_id is not None:
            return {"source": source, "source_event_id": self.source_event_id}
        return {
            "source": source,
            "event_type": self.event_type,
            "event_timestamp": self.event_timestamp,
            "assertion": self.assertion,
            "claimant": self.claimant,
            "dimensions": list(self.dimensions),
            "subjects": self.subjects.to_dict(),
            "payload_digest": canonical_digest(self.payload),
        }

    def _derived_idempotency_key(self) -> str:
        digest = hashlib.sha256(canonical_json_bytes(self._identity_material())).hexdigest()
        return f"idem_{digest}"

    def _content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assertion": self.assertion,
            "event_type": self.event_type,
            "source_type": self.source_type,
            "source_system": self.source_system,
            "source_instance": self.source_instance,
            "source_schema": self.source_schema,
            "adapter": self.adapter,
            "source_event_id": self.source_event_id,
            "event_timestamp": self.event_timestamp,
            "event_end_timestamp": self.event_end_timestamp,
            "observed_at": self.observed_at,
            "emitted_at": self.emitted_at,
            "dimensions": list(self.dimensions),
            "measurement_basis": dict(self.measurement_basis),
            "completeness": self.completeness.to_dict(),
            "truncation": self.truncation.to_dict(),
            "privacy": self.privacy.to_dict(),
            "subjects": self.subjects.to_dict(),
            "raw_digest": self.raw_digest,
            "raw_ref": self.raw_ref,
            "claimant": self.claimant,
            "tags": list(self.tags),
            "payload": _canonical_jsonable(self.payload),
        }

    def _integrity_material(self) -> dict[str, Any]:
        return {**self._content_dict(), "idempotency_key": self.idempotency_key}

    def verify_integrity(self) -> bool:
        return self.integrity_hash == canonical_digest(self._integrity_material()) and self.evidence_id == f"evd_{self.integrity_hash.removeprefix('sha256:')}"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_dict(),
            "idempotency_key": self.idempotency_key,
            "integrity_hash": self.integrity_hash,
            "evidence_id": self.evidence_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceEnvelope":
        if not isinstance(value, Mapping):
            raise TypeError("evidence envelope must be a mapping")
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            assertion=str(value.get("assertion") or ""),
            event_type=str(value.get("event_type") or ""),
            source_type=str(value.get("source_type") or ""),
            source_system=str(value.get("source_system") or ""),
            source_instance=str(value.get("source_instance") or ""),
            source_schema=str(value.get("source_schema") or ""),
            adapter=str(value.get("adapter") or ""),
            source_event_id=value.get("source_event_id"),
            event_timestamp=value.get("event_timestamp"),
            event_end_timestamp=value.get("event_end_timestamp"),
            observed_at=value.get("observed_at"),
            emitted_at=value.get("emitted_at"),
            dimensions=tuple(value.get("dimensions") or ()),
            measurement_basis=dict(value.get("measurement_basis") or {}),
            completeness=Completeness.from_dict(value.get("completeness") or {}),
            truncation=Truncation.from_dict(value.get("truncation") or {}),
            privacy=PrivacyMetadata.from_dict(value.get("privacy") or {}),
            subjects=SubjectRefs.from_dict(value.get("subjects") or {}),
            raw_digest=value.get("raw_digest"),
            raw_ref=value.get("raw_ref"),
            claimant=value.get("claimant"),
            tags=tuple(value.get("tags") or ()),
            payload=dict(value.get("payload") or {}),
            idempotency_key=str(value.get("idempotency_key") or ""),
            integrity_hash=str(value.get("integrity_hash") or ""),
            evidence_id=str(value.get("evidence_id") or ""),
        )


@dataclass(frozen=True)
class ClaimedLink:
    """Immutable link from one claimed envelope to one observed envelope.

    The model fixes direction syntactically.  A store can additionally verify
    that the referenced envelopes exist and have the required assertions; it
    may retain a pending link when the envelopes arrive out of order.
    """

    schema_version: str
    claimed_evidence_id: str
    observed_evidence_id: str
    relationship: str
    dimensions: tuple[str, ...]
    created_at: str
    created_by: str
    rationale_code: str | None = None
    idempotency_key: str = ""
    integrity_hash: str = ""
    link_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != CLAIMED_LINK_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CLAIMED_LINK_SCHEMA_VERSION!r}")
        if not _EVIDENCE_ID_RE.fullmatch(self.claimed_evidence_id):
            raise ValueError("claimed_evidence_id must be an Evidence v2 id")
        if not _EVIDENCE_ID_RE.fullmatch(self.observed_evidence_id):
            raise ValueError("observed_evidence_id must be an Evidence v2 id")
        if self.claimed_evidence_id == self.observed_evidence_id:
            raise ValueError("a claimed link cannot point to itself")
        _required_token(self.relationship, "relationship")
        _required_token(self.created_by, "created_by")
        if self.rationale_code is not None:
            _required_token(self.rationale_code, "rationale_code")
        dimensions = tuple(sorted({_required_token(value, "dimension") for value in self.dimensions}))
        if not dimensions:
            raise ValueError("claimed link requires at least one dimension")
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "created_at", normalize_timestamp(self.created_at, "created_at"))

        identity = {
            "claimed_evidence_id": self.claimed_evidence_id,
            "observed_evidence_id": self.observed_evidence_id,
            "relationship": self.relationship,
            "dimensions": list(dimensions),
            "created_by": self.created_by,
        }
        expected_idempotency = f"idem_{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()}"
        if self.idempotency_key and self.idempotency_key != expected_idempotency:
            raise ValueError("claimed-link idempotency_key does not match content")
        object.__setattr__(self, "idempotency_key", expected_idempotency)
        expected_integrity = canonical_digest({**self._content_dict(), "idempotency_key": expected_idempotency})
        if self.integrity_hash and self.integrity_hash != expected_integrity:
            raise ValueError("claimed-link integrity_hash does not match content")
        object.__setattr__(self, "integrity_hash", expected_integrity)
        expected_link_id = f"clm_{expected_integrity.removeprefix('sha256:')}"
        if self.link_id and self.link_id != expected_link_id:
            raise ValueError("link_id does not match integrity_hash")
        if self.link_id and not _LINK_ID_RE.fullmatch(self.link_id):
            raise ValueError("invalid link_id")
        object.__setattr__(self, "link_id", expected_link_id)

    @classmethod
    def create(
        cls,
        *,
        claimed_evidence_id: str,
        observed_evidence_id: str,
        relationship: str,
        dimensions: Sequence[str],
        created_at: str | int | float | datetime,
        created_by: str,
        rationale_code: str | None = None,
    ) -> "ClaimedLink":
        return cls(
            schema_version=CLAIMED_LINK_SCHEMA_VERSION,
            claimed_evidence_id=claimed_evidence_id,
            observed_evidence_id=observed_evidence_id,
            relationship=relationship,
            dimensions=tuple(dimensions),
            created_at=normalize_timestamp(created_at, "created_at"),
            created_by=created_by,
            rationale_code=rationale_code,
        )

    def _content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claimed_evidence_id": self.claimed_evidence_id,
            "observed_evidence_id": self.observed_evidence_id,
            "relationship": self.relationship,
            "dimensions": list(self.dimensions),
            "created_at": self.created_at,
            "created_by": self.created_by,
            "rationale_code": self.rationale_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_dict(),
            "idempotency_key": self.idempotency_key,
            "integrity_hash": self.integrity_hash,
            "link_id": self.link_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ClaimedLink":
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            claimed_evidence_id=str(value.get("claimed_evidence_id") or ""),
            observed_evidence_id=str(value.get("observed_evidence_id") or ""),
            relationship=str(value.get("relationship") or ""),
            dimensions=tuple(value.get("dimensions") or ()),
            created_at=value.get("created_at"),
            created_by=str(value.get("created_by") or ""),
            rationale_code=value.get("rationale_code"),
            idempotency_key=str(value.get("idempotency_key") or ""),
            integrity_hash=str(value.get("integrity_hash") or ""),
            link_id=str(value.get("link_id") or ""),
        )


def _legacy_source_type(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> tuple[str, str]:
    source = str(event.get("source") or "legacy")
    event_type = str(event.get("event_type") or "legacy_event")
    provenance = str(metadata.get("usage_provenance") or "")
    usage_source = str(metadata.get("usage_source") or "")
    observation_provenance = str(metadata.get("observation_provenance") or "")
    observation_source = str(metadata.get("observation_source") or "")
    context_source = str(metadata.get("client_context_source") or "")
    if provenance == "agent_sentinel_local_usage_import" or usage_source == "local_client_session_store":
        return "local_client_log", "observed"
    if (
        observation_provenance
        == "agent_chronicle_local_session_observation_import"
        or observation_source == "local_client_session_store_observation"
    ):
        return "local_client_log", "observed"
    if "hook" in context_source or source in {"claude_code_hook", "codex_hook"}:
        return "client_hook", "observed"
    if source in {"proxy", "provider_proxy", "openai_proxy", "openrouter_proxy"}:
        return "provider_proxy", "observed"
    if source in {"runner", "process_wrapper", "agent-chronicle-run"}:
        return "process_wrapper", "observed"
    if source in {"ci", "github_actions"}:
        return "ci", "observed"
    if event_type in {"run_started", "run_completed", "run_failed", "process_exit"}:
        return "process_wrapper", "observed"
    return "mcp_agent_reported", "claimed"


def _legacy_dimensions(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> tuple[str, ...]:
    event_type = str(event.get("event_type") or "legacy_event").lower()
    dimensions: set[str] = set()
    if "usage" in event_type or any(key in event for key in ("estimated_input_tokens", "estimated_output_tokens")):
        dimensions.add("usage")
    if "cost" in event_type or event.get("estimated_cost_usd") is not None:
        dimensions.add("cost")
    if "machine_check" in event_type or any(key in metadata for key in ("exit_code", "after_exit_code", "before_exit_code")):
        dimensions.add("machine_check")
    if event_type.startswith("section_") or event_type.startswith("task_") or metadata.get("section_id"):
        dimensions.add("task_semantics")
    if "context" in event_type or any(key in metadata for key in ("client_session_id", "client_transcript_id")):
        dimensions.add("session_identity")
    if event_type.startswith("run_") or "tool" in event_type:
        dimensions.add("activity")
    if "outcome" in event_type or event_type in {"task_completed", "task_blocked"}:
        dimensions.add("outcome")
    return tuple(sorted(dimensions or {"activity"}))


def _legacy_project_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/") or _WINDOWS_ABSOLUTE_PATH_RE.match(value):
        leaf = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "project"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"project:{re.sub(r'[^A-Za-z0-9._-]', '_', leaf)[:96]}:{digest}"
    return value[:512]


def _redact_legacy_absolute_paths(value: Any, parents: tuple[str, ...] = ()) -> tuple[Any, tuple[str, ...]]:
    """Pseudonymize absolute paths copied from the broad v1 metadata bag."""

    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        paths: list[str] = []
        for key, child in value.items():
            redacted_child, child_paths = _redact_legacy_absolute_paths(child, (*parents, str(key)))
            safe[str(key)] = redacted_child
            paths.extend(child_paths)
        return safe, tuple(paths)
    if isinstance(value, (list, tuple)):
        safe_items: list[Any] = []
        paths: list[str] = []
        for index, child in enumerate(value):
            redacted_child, child_paths = _redact_legacy_absolute_paths(child, (*parents, str(index)))
            safe_items.append(redacted_child)
            paths.extend(child_paths)
        return safe_items, tuple(paths)
    if isinstance(value, str) and (value.startswith("/") or _WINDOWS_ABSOLUTE_PATH_RE.match(value)):
        leaf = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "path"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        safe_leaf = re.sub(r"[^A-Za-z0-9._-]", "_", leaf)[:96]
        return f"local-ref:{safe_leaf}:{digest}", (".".join(parents),)
    return value, ()


def adapt_v1_event(
    event: Mapping[str, Any],
    *,
    source_instance: str = "legacy-events-jsonl",
    adapter: str = "chronicle-v1-event-adapter",
) -> EvidenceEnvelope:
    """Convert one historical ledger event without mutating either ledger.

    The adapter is intentionally conservative: known mechanical sources become
    observations; everything else becomes an explicit legacy claim.  Prompt,
    message, request, response, and tool bodies are omitted by the normal v2
    privacy path, while a digest of the complete input preserves provenance.
    """

    if not isinstance(event, Mapping):
        raise TypeError("v1 event must be a mapping")
    metadata_value = event.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
    source_type, assertion = _legacy_source_type(event, metadata)
    dimensions = _legacy_dimensions(event, metadata)
    event_type = str(event.get("event_type") or "legacy_event")
    source = str(event.get("source") or "legacy")
    source_system = str(metadata.get("client") or event.get("provider") or source).replace(" ", "_")
    if not _TOKEN_RE.fullmatch(source_system):
        source_system = "legacy"

    created = event.get("created_at")
    if created is None:
        created = metadata.get("client_event_timestamp") or "1970-01-01T00:00:00Z"
        completeness = Completeness(status="partial", missing_dimensions=("event_time",), note_codes=("legacy_timestamp_missing",))
    else:
        completeness = Completeness(status="unknown", note_codes=("legacy_event",))

    bases: dict[str, str] = {}
    for dimension in dimensions:
        if assertion == "claimed":
            bases[dimension] = "agent_claimed"
        elif dimension == "usage":
            bases[dimension] = str(event.get("usage_confidence") or metadata.get("usage_confidence") or "client_reported")
        elif dimension == "cost":
            bases[dimension] = str(event.get("cost_confidence") or metadata.get("cost_confidence") or "client_reported")
        elif source_type == "client_hook":
            bases[dimension] = "client_hook_observed"
        elif source_type == "process_wrapper":
            bases[dimension] = "process_observed"
        elif source_type == "ci":
            bases[dimension] = "ci_observed"
        else:
            bases[dimension] = "source_observed"

    subject_data = {
        "project_id": _legacy_project_ref(metadata.get("project_dir") or event.get("project_dir")),
        "run_id": metadata.get("run_id") or event.get("run_id"),
        "client_session_id": metadata.get("client_session_id") or event.get("client_session_id"),
        "parent_client_session_id": metadata.get("parent_client_session_id") or event.get("parent_client_session_id"),
        "turn_id": metadata.get("turn_id") or event.get("turn_id"),
        "message_id": metadata.get("message_id") or event.get("message_id"),
        "request_id": metadata.get("request_id") or event.get("request_id"),
        "work_id": metadata.get("work_id") or event.get("work_id"),
        "section_id": metadata.get("section_id") or event.get("section_id"),
        "tool_call_id": metadata.get("tool_call_id") or event.get("tool_call_id"),
        "agent_id": metadata.get("agent_id") or event.get("agent_id"),
        "extra": (
            {
                "source_namespace_fingerprint": str(
                    metadata.get("source_namespace_fingerprint")
                )
            }
            if metadata.get("source_namespace_fingerprint")
            else {}
        ),
    }
    safe_metadata, path_redactions = _redact_legacy_absolute_paths(metadata)
    legacy_payload = {
        "legacy": {
            "metadata": safe_metadata,
            "provider": event.get("provider"),
            "model": event.get("model"),
            "usage_confidence": event.get("usage_confidence"),
            "cost_confidence": event.get("cost_confidence"),
            "estimated_input_tokens": event.get("estimated_input_tokens"),
            "estimated_output_tokens": event.get("estimated_output_tokens"),
            "estimated_cost_usd": event.get("estimated_cost_usd"),
        }
    }
    source_event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else raw_digest_for(event)
    return EvidenceEnvelope.create(
        assertion=assertion,
        event_type=event_type if _TOKEN_RE.fullmatch(event_type) else "legacy_event",
        source_type=source_type,
        source_system=source_system,
        source_instance=source_instance,
        source_schema=str(event.get("schema_version") or "agent-chronicle.event.v1").replace(" ", "_"),
        adapter=adapter,
        source_event_id=source_event_id,
        event_timestamp=created,
        observed_at=created,
        dimensions=dimensions,
        measurement_basis=bases,
        completeness=completeness,
        privacy=PrivacyMetadata(
            classification="internal",
            content_capture="metadata_only",
            redacted=bool(path_redactions),
            redacted_fields=tuple(f"legacy.metadata.{path}" for path in path_redactions),
            redaction_methods=("path_pseudonymization",) if path_redactions else (),
        ),
        subjects=SubjectRefs.from_dict(subject_data),
        payload=legacy_payload,
        raw_digest=raw_digest_for(event),
        raw_ref=f"legacy-event:{event.get('event_id') or raw_digest_for(event).removeprefix('sha256:')[:16]}",
        claimant=source if assertion == "claimed" else None,
        tags=("v1_compat",),
    )


# Readable aliases for callers that prefer nouns over implementation names.
Privacy = PrivacyMetadata
from_v1_event = adapt_v1_event


__all__ = [
    "CLAIMED_LINK_SCHEMA_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "ClaimedLink",
    "Completeness",
    "EvidenceEnvelope",
    "Privacy",
    "PrivacyMetadata",
    "SubjectRefs",
    "Truncation",
    "adapt_v1_event",
    "canonical_digest",
    "canonical_json_bytes",
    "from_v1_event",
    "normalize_timestamp",
    "raw_digest_for",
    "sanitize_evidence_payload",
]
