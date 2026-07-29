"""Capability registry and bounded parsing boundary for hook capture."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping

from .adapters import ClaudeCodeAdapter, CodexAdapter, CursorAdapter
from .base import AdapterCapabilities, CaptureAdapter, CaptureContext, CaptureResult, utc_now


DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


class CaptureRegistry:
    def __init__(self, *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> None:
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self.max_payload_bytes = max_payload_bytes
        self._adapters: dict[str, CaptureAdapter] = {}
        self._aliases: dict[str, str] = {}

    def register(self, adapter: CaptureAdapter, *, aliases: tuple[str, ...] = ()) -> None:
        vendor = adapter.vendor.strip().lower()
        if not vendor or vendor in self._adapters:
            raise ValueError(f"capture adapter already registered or invalid: {vendor!r}")
        self._adapters[vendor] = adapter
        self._aliases[vendor] = vendor
        for alias in aliases:
            normalized = alias.strip().lower()
            if not normalized or normalized in self._aliases:
                raise ValueError(f"capture adapter alias already registered or invalid: {alias!r}")
            self._aliases[normalized] = vendor

    def vendors(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def get(self, vendor: str) -> CaptureAdapter | None:
        normalized = vendor.strip().lower() if isinstance(vendor, str) else ""
        canonical = self._aliases.get(normalized, normalized)
        return self._adapters.get(canonical)

    def capabilities(self, vendor: str | None = None) -> Mapping[str, AdapterCapabilities] | AdapterCapabilities | None:
        if vendor is not None:
            adapter = self.get(vendor)
            return adapter.capabilities if adapter is not None else None
        return {name: self._adapters[name].capabilities for name in sorted(self._adapters)}

    def normalize(
        self,
        vendor: str,
        payload: Mapping[str, Any] | str | bytes | bytearray,
        *,
        context: CaptureContext | None = None,
        host_event: str = "",
    ) -> CaptureResult:
        adapter = self.get(vendor)
        if adapter is None:
            return CaptureResult(ignored_reason="unsupported_vendor")
        try:
            parsed, failure = self._bounded_mapping(payload)
        except Exception as exc:
            return CaptureResult(
                ignored_reason="invalid_payload",
                warnings=(f"payload_error:{type(exc).__name__}",),
            )
        if failure:
            return CaptureResult(ignored_reason=failure)
        capture_context = context or CaptureContext()
        if capture_context.observed_at is None:
            capture_context = replace(capture_context, observed_at=utc_now())
        if host_event:
            capture_context = replace(capture_context, host_event=host_event)
        try:
            return adapter.normalize(parsed, capture_context)
        except Exception as exc:  # A host hook must remain fail-open.
            return CaptureResult(
                ignored_reason="adapter_failed",
                warnings=(f"adapter_error:{type(exc).__name__}",),
            )

    def _bounded_mapping(
        self,
        payload: Mapping[str, Any] | str | bytes | bytearray,
    ) -> tuple[Mapping[str, Any], str]:
        if isinstance(payload, Mapping):
            try:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError):
                return {}, "invalid_payload"
            if len(encoded) > self.max_payload_bytes:
                return {}, "payload_too_large"
            return payload, ""

        if isinstance(payload, str):
            encoded = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray)):
            encoded = bytes(payload)
        else:
            return {}, "invalid_payload_type"
        if len(encoded) > self.max_payload_bytes:
            return {}, "payload_too_large"
        try:
            decoded = encoded.decode("utf-8")
            parsed = json.loads(decoded, parse_constant=_reject_non_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return {}, "malformed_json"
        if not isinstance(parsed, Mapping):
            return {}, "payload_must_be_object"
        return parsed, ""


def build_default_registry(*, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> CaptureRegistry:
    registry = CaptureRegistry(max_payload_bytes=max_payload_bytes)
    registry.register(ClaudeCodeAdapter(), aliases=("cc", "claudecode", "claude_code", "claude"))
    registry.register(CodexAdapter(), aliases=("openai-codex", "openai_codex"))
    registry.register(CursorAdapter())
    return registry


DEFAULT_CAPTURE_REGISTRY = build_default_registry()


__all__ = [
    "DEFAULT_CAPTURE_REGISTRY",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "CaptureRegistry",
    "build_default_registry",
]
