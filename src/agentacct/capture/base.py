"""Vendor-neutral contracts for mechanical coding-agent capture.

The capture layer deliberately produces metadata-only observations.  It is a
mechanical source (host hooks, local client records), not a semantic work
ledger: prompts, responses, thoughts, tool arguments, and tool results never
cross this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Mapping, Protocol, TypeAlias


CAPTURE_SCHEMA_VERSION = "agent-chronicle.capture-observation.v1"

CaptureEventType: TypeAlias = Literal[
    "session_observed",
    "turn_observed",
    "tool_observed",
    "machine_check_observed",
    "session_link_observed",
]
CompletenessStatus: TypeAlias = Literal["complete", "partial", "unknown"]

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$")
_SHA256 = re.compile(r"^(?:sha256:)?[a-fA-F0-9]{64}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_observed_at(value: Any, *, fallback: datetime | None = None) -> datetime:
    """Parse a host timestamp without ever failing the hook path."""

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            # Vendors have shipped both seconds and milliseconds.
            seconds = float(value)
            if abs(seconds) > 10_000_000_000:
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback or utc_now()


def safe_token(value: Any, *, fallback: str = "") -> str:
    """Return a short non-body identifier suitable for metadata storage."""

    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if not candidate or not _SAFE_TOKEN.fullmatch(candidate):
        return fallback
    return candidate


def safe_digest(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not _SHA256.fullmatch(candidate):
        return ""
    digest = candidate.split(":", 1)[-1].lower()
    return f"sha256:{digest}"


def safe_relative_path(value: Any, *, roots: tuple[str, ...] = ()) -> str:
    """Project only an allowlisted relative path; reject absolute leakage.

    Absolute vendor paths are accepted only when they are descendants of a
    supplied workspace root, in which case only the relative suffix survives.
    Parent traversal, home-relative paths, drive-only paths, and empty paths are
    rejected.
    """

    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or raw.startswith("~") or len(raw) > 2048:
        return ""

    normalized = raw.replace("\\", "/")
    candidate: str | None = None
    path = Path(raw).expanduser()
    is_windows_absolute = PureWindowsPath(raw).is_absolute()
    if is_windows_absolute:
        windows_path = PureWindowsPath(raw)
        for root_text in roots:
            try:
                if not isinstance(root_text, str) or not root_text.strip():
                    continue
                windows_root = PureWindowsPath(root_text)
                if not windows_root.is_absolute() or windows_root == PureWindowsPath(windows_root.anchor):
                    continue
                candidate = windows_path.relative_to(windows_root).as_posix()
                break
            except (TypeError, ValueError):
                continue
        if candidate is None:
            return ""
    elif path.is_absolute():
        for root_text in roots:
            try:
                if not isinstance(root_text, str) or not root_text.strip():
                    continue
                root_path = Path(root_text).expanduser()
                if not root_path.is_absolute():
                    continue
                root = root_path.resolve(strict=False)
                if root == Path(root.anchor):
                    continue
                resolved = path.resolve(strict=False)
                candidate = resolved.relative_to(root).as_posix()
                break
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
        if candidate is None:
            return ""
    else:
        candidate = normalized

    pure = PurePosixPath(candidate)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        return ""
    rendered = pure.as_posix()
    return rendered if len(rendered) <= 512 else ""


def normalized_sha256(payload: Mapping[str, Any]) -> str:
    """Digest normalized metadata only, never a raw vendor payload."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CaptureCompleteness:
    status: CompletenessStatus
    basis: str = "host_hook"
    missing_fields: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "basis": self.basis,
            "missing_fields": list(self.missing_fields),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class CaptureObservation:
    vendor: str
    event_type: CaptureEventType
    source_event_id: str
    observed_at: datetime
    host_event: str
    session_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    parent_session_id: str = ""
    linked_session_id: str = ""
    raw_digest: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    completeness: CaptureCompleteness = field(
        default_factory=lambda: CaptureCompleteness(status="unknown")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "source_event_id": self.source_event_id,
            "source_kind": "host_hook",
            "vendor": self.vendor,
            "event_type": self.event_type,
            "observed_at": iso_utc(self.observed_at),
            "host_event": self.host_event,
            "raw_digest": self.raw_digest or None,
            "subjects": {
                "session_id": self.session_id or None,
                "turn_id": self.turn_id or None,
                "tool_call_id": self.tool_call_id or None,
                "parent_session_id": self.parent_session_id or None,
                "linked_session_id": self.linked_session_id or None,
            },
            "attributes": dict(self.attributes),
            "completeness": self.completeness.to_dict(),
            "privacy": {
                "capture_mode": "metadata_only",
                "contains_prompt": False,
                "contains_response": False,
                "contains_thought": False,
                "contains_tool_arguments": False,
                "contains_tool_result": False,
            },
        }


@dataclass(frozen=True)
class CaptureContext:
    observed_at: datetime | None = None
    host_event: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    workspace_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterCapabilities:
    vendor: str
    supported_host_events: tuple[str, ...]
    emits: tuple[CaptureEventType, ...]
    session_lifecycle: bool
    turns: bool
    tools: bool
    edits_artifacts: bool
    subagents: bool
    machine_results: bool
    usage: bool
    cost: bool
    native_otlp: bool
    stable_event_identity: Literal["host", "derived", "mixed", "unavailable"]
    session_identity: Literal["host", "derived", "mixed", "unavailable"]
    turn_identity: Literal["host", "derived", "mixed", "unavailable"]
    usage_basis: Literal["not_captured", "host_reported", "transcript", "estimated"]
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "supported_host_events": list(self.supported_host_events),
            "emits": list(self.emits),
            "session_lifecycle": self.session_lifecycle,
            "turns": self.turns,
            "tools": self.tools,
            "edits_artifacts": self.edits_artifacts,
            "subagents": self.subagents,
            "machine_results": self.machine_results,
            "usage": self.usage,
            "cost": self.cost,
            "native_otlp": self.native_otlp,
            "stable_event_identity": self.stable_event_identity,
            "session_identity": self.session_identity,
            "turn_identity": self.turn_identity,
            "usage_basis": self.usage_basis,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class CaptureResult:
    observations: tuple[CaptureObservation, ...] = ()
    ignored_reason: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def ignored(self) -> bool:
        return bool(self.ignored_reason)


class CaptureAdapter(Protocol):
    vendor: str
    capabilities: AdapterCapabilities

    def normalize(self, payload: Mapping[str, Any], context: CaptureContext) -> CaptureResult: ...


def source_event_id(
    *,
    vendor: str,
    host_event: str,
    event_type: CaptureEventType,
    observed_at: datetime | None,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    parent_session_id: str = "",
    linked_session_id: str = "",
    stable_discriminator: str = "",
) -> str:
    """Build an idempotency key from body-free source identity fields.

    ``observed_at`` must be the host occurrence time, not the local capture
    time.  Hosts that do not expose an occurrence timestamp pass ``None`` and
    rely on their stable discriminator (or the adapter's opaque payload
    fingerprint), so retry identity does not change merely because a hook was
    retried later.
    """

    identity = {
        "schema": CAPTURE_SCHEMA_VERSION,
        "vendor": vendor,
        "host_event": host_event,
        "event_type": event_type,
        "observed_at": iso_utc(observed_at) if observed_at is not None else None,
        "session_id": session_id,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "parent_session_id": parent_session_id,
        "linked_session_id": linked_session_id,
        "stable_discriminator": stable_discriminator,
    }
    return f"capture:{vendor}:{normalized_sha256(identity)}"
