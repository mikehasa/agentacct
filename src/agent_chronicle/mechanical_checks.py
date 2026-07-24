"""Body-free Task evidence projected from trusted mechanical hook checks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping


_CHECK_KINDS = {"test", "build", "lint", "typecheck"}
_SAFE_RUNNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$")
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plain(value: Any) -> dict[str, Any]:
    arrival_sequence = getattr(value, "first_receipt_sequence", None)
    value = getattr(value, "envelope", value)
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    result = dict(value) if isinstance(value, Mapping) else {}
    if isinstance(arrival_sequence, int) and not isinstance(arrival_sequence, bool):
        result["_arrival_sequence"] = arrival_sequence
    return result


def _text(value: Any, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized if 0 < len(normalized) <= maximum else ""


def _timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return result if result > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        result = parsed.astimezone(UTC).timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return result if result > 0 else None


def _namespace_fingerprint(client: str, subjects: Mapping[str, Any]) -> str | None:
    extra = _mapping(subjects.get("extra"))
    organization = _text(subjects.get("organization") or extra.get("organization")) or "unscoped"
    project_id = _text(subjects.get("project_id")) or "unscoped"
    if organization == "unscoped" and project_id == "unscoped":
        return None
    material = json.dumps(
        {"client": client, "organization": organization, "project_id": project_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"ns:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def build_mechanical_check_events(envelopes: Iterable[Any]) -> list[dict[str, Any]]:
    """Translate valid hook exit metadata into existing Task-check rows.

    Commands, outputs, digests, prompts, and response bodies are never copied
    into this projection.  Invalid or internally inconsistent observations are
    omitted rather than repaired or guessed.
    """

    events: list[dict[str, Any]] = []
    for raw in envelopes:
        envelope = _plain(raw)
        if (
            envelope.get("assertion") != "observed"
            or envelope.get("source_type") != "client_hook"
            or envelope.get("event_type") != "machine_check_observed"
        ):
            continue
        dimensions = envelope.get("dimensions")
        basis = _mapping(envelope.get("measurement_basis"))
        tags = envelope.get("tags")
        if (
            not isinstance(dimensions, (list, tuple))
            or "machine_check" not in dimensions
            or basis.get("machine_check") != "hook_exit_code"
            or not isinstance(tags, (list, tuple))
            or "mechanical_capture" not in tags
        ):
            continue
        subjects = _mapping(envelope.get("subjects"))
        client = _text(envelope.get("source_system"), maximum=256)
        session_id = _text(subjects.get("client_session_id"))
        evidence_id = _text(envelope.get("evidence_id"), maximum=160)
        payload = _mapping(envelope.get("payload"))
        attributes = _mapping(payload.get("attributes"))
        if payload.get("capture_basis") != "host_hook" or not client or not session_id or not evidence_id:
            continue
        namespace_fingerprint = _namespace_fingerprint(client, subjects)
        check_kind = _text(attributes.get("check_kind"), maximum=32)
        runner = _text(attributes.get("runner"), maximum=160)
        command_digest = _text(attributes.get("command_digest"), maximum=80).lower()
        exit_code = attributes.get("exit_code")
        passed = attributes.get("passed")
        if (
            check_kind not in _CHECK_KINDS
            or not _SAFE_RUNNER.fullmatch(runner)
            or not _SHA256.fullmatch(command_digest)
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not -65_535 <= exit_code <= 65_535
            or not isinstance(passed, bool)
            or passed != (exit_code == 0)
        ):
            continue
        created_at = _timestamp(envelope.get("event_timestamp") or envelope.get("observed_at"))
        if created_at is None:
            continue
        identity_material = "\0".join((client, check_kind, runner, command_digest))
        check_identity = f"client-hook:{hashlib.sha256(identity_material.encode('utf-8')).hexdigest()[:20]}"
        name = f"{runner} {check_kind}"
        events.append(
            {
                "event_id": f"evidence:{evidence_id}",
                "created_at": created_at,
                "source": client,
                "source_type": "client_hook",
                "run_id": None,
                "work_id": None,
                "section_id": None,
                "client": client,
                "client_session_id": session_id,
                "namespace_fingerprint": namespace_fingerprint,
                "identity_scope_state": "explicit" if namespace_fingerprint else "unscoped",
                "project_dir": None,
                "project_source": None,
                "evidence_type": check_kind,
                "check_identity": check_identity,
                "name": name,
                "result": "passed" if exit_code == 0 else "failed",
                "summary": f"{name} exited with code {exit_code}.",
                "command": None,
                "command_redacted": True,
                "exit_code": exit_code,
                "artifact_ref": None,
                "artifact_path": None,
                "artifact_url": None,
                "artifact_path_redacted": False,
                "artifact_url_redacted": False,
                "files": [],
                "measurement_basis": "hook_exit_code",
                "arrival_sequence": (
                    envelope.get("_arrival_sequence")
                    if isinstance(envelope.get("_arrival_sequence"), int)
                    and not isinstance(envelope.get("_arrival_sequence"), bool)
                    else None
                ),
            }
        )
    return sorted(
        events,
        key=lambda event: (
            float(event.get("created_at") or 0.0),
            int(event.get("arrival_sequence") or 0),
        ),
        reverse=True,
    )


__all__ = ["build_mechanical_check_events"]
