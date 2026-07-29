"""Runtime seam shared by hook CLI and trusted-localhost capture routes."""

from __future__ import annotations

from typing import Any

from .capture import CaptureContext, CaptureService, CaptureWriteResult
from .evidence_runtime import EVIDENCE_V2_ENV, EvidenceRuntime


def capture_write_result_payload(
    result: CaptureWriteResult,
    *,
    vendor: str,
    host_event: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Return a body-safe receipt; raw hook input is never echoed."""

    return {
        "schema_version": "agent-chronicle.capture-receipt.v1",
        "enabled": enabled,
        "vendor": vendor,
        "host_event": host_event,
        "ok": result.ok,
        "fail_open": result.fail_open,
        "attempted_count": result.attempted_count,
        "stored_count": result.stored_count,
        "ignored_reason": result.capture.ignored_reason or None,
        "warnings": list(result.capture.warnings),
        "write_errors": list(result.write_errors),
        "observations": [observation.to_dict() for observation in result.capture.observations],
        "privacy": {
            "capture_mode": "metadata_only",
            "raw_payload_returned": False,
            "hook_exit_policy": "always_zero",
        },
    }


def capture_hook_payload(
    runtime: EvidenceRuntime,
    *,
    vendor: str,
    host_event: str,
    payload: Any,
    context: CaptureContext | None = None,
) -> dict[str, Any]:
    """Normalize and durably append one payload without breaking a host hook."""

    normalized_vendor = vendor.strip().lower() if isinstance(vendor, str) else ""
    normalized_event = host_event.strip() if isinstance(host_event, str) else ""
    if not runtime.enabled:
        return {
            "schema_version": "agent-chronicle.capture-receipt.v1",
            "enabled": False,
            "vendor": normalized_vendor,
            "host_event": normalized_event,
            "ok": True,
            "fail_open": True,
            "attempted_count": 0,
            "stored_count": 0,
            "ignored_reason": "evidence_v2_disabled",
            "warnings": [f"disabled_by:{EVIDENCE_V2_ENV}"],
            "write_errors": [],
            "observations": [],
            "privacy": {
                "capture_mode": "metadata_only",
                "raw_payload_returned": False,
                "hook_exit_policy": "always_zero",
            },
        }
    try:
        result = CaptureService(runtime.store).capture(
            normalized_vendor,
            payload,
            context=context,
            host_event=normalized_event,
        )
        return capture_write_result_payload(
            result,
            vendor=normalized_vendor,
            host_event=normalized_event,
        )
    except Exception as exc:  # noqa: BLE001 - host hooks are deliberately fail-open.
        return {
            "schema_version": "agent-chronicle.capture-receipt.v1",
            "enabled": True,
            "vendor": normalized_vendor,
            "host_event": normalized_event,
            "ok": False,
            "fail_open": True,
            "attempted_count": 0,
            "stored_count": 0,
            "ignored_reason": "capture_runtime_failed",
            "warnings": [f"runtime_error:{type(exc).__name__}"],
            "write_errors": ["capture_runtime_failed"],
            "observations": [],
            "privacy": {
                "capture_mode": "metadata_only",
                "raw_payload_returned": False,
                "hook_exit_policy": "always_zero",
            },
        }


__all__ = ["capture_hook_payload", "capture_write_result_payload"]
