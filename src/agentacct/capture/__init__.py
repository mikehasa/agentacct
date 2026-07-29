"""Mechanical, metadata-only capture for coding-agent host hooks."""

from .adapters import ClaudeCodeAdapter, CodexAdapter, CursorAdapter
from .base import (
    CAPTURE_SCHEMA_VERSION,
    AdapterCapabilities,
    CaptureAdapter,
    CaptureCompleteness,
    CaptureContext,
    CaptureObservation,
    CaptureResult,
)
from .manifests import RenderedHookManifest, merge_cursor_hooks, merge_hook_manifest, render_hook_manifest
from .registry import DEFAULT_CAPTURE_REGISTRY, CaptureRegistry, build_default_registry
from .service import CaptureService, CaptureWriteResult, EvidenceSink

__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "DEFAULT_CAPTURE_REGISTRY",
    "AdapterCapabilities",
    "CaptureAdapter",
    "CaptureCompleteness",
    "CaptureContext",
    "CaptureObservation",
    "CaptureRegistry",
    "CaptureResult",
    "CaptureService",
    "CaptureWriteResult",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "EvidenceSink",
    "RenderedHookManifest",
    "build_default_registry",
    "merge_cursor_hooks",
    "merge_hook_manifest",
    "render_hook_manifest",
]
