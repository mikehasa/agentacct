"""Metadata-only hook adapters for supported coding-agent hosts.

The adapters may inspect body-bearing host fields transiently to classify an
objective machine result, but only allowlisted scalars, relative paths, and
digests can enter a :class:`CaptureObservation`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from .base import (
    AdapterCapabilities,
    CaptureCompleteness,
    CaptureContext,
    CaptureEventType,
    CaptureObservation,
    CaptureResult,
    parse_observed_at,
    safe_digest,
    safe_relative_path,
    safe_token,
    source_event_id,
    utc_now,
)


_COMPOUND_SHELL = re.compile(r"(?:&&|\|\||[;|\n\r])")
_CURSOR_ENV_MARKERS = (
    "CURSOR_AGENT",
    "CURSOR_SESSION_ID",
    "CURSOR_TRACE_ID",
    "CURSOR_PROJECT_PATH",
    "CURSOR_VERSION",
)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _opaque_id(payload: Mapping[str, Any], *keys: str) -> str:
    return safe_token(_first(payload, *keys))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and 1 < len(value) <= 262_144 and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _result_mappings(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = [payload]
    for key in (
        "tool_response",
        "execution_result",
        "result",
        "response",
    ):
        child = _as_mapping(payload.get(key))
        if child:
            result.append(child)
    # A string-valued tool_output can be command stdout that merely happens
    # to contain JSON; only an actual host object is objective result metadata.
    tool_output = payload.get("tool_output")
    if isinstance(tool_output, Mapping):
        result.append(tool_output)
    return tuple(result)


def _input_mappings(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = [payload]
    for key in (
        "tool_input",
        "input",
        "arguments",
        "tool_args",
    ):
        child = _as_mapping(payload.get(key))
        if child:
            result.append(child)
    return tuple(result)


def _integer(value: Any, *, minimum: int = -(2**31), maximum: int = 2**31 - 1) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if minimum <= value <= maximum else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        converted = int(value)
        return converted if minimum <= converted <= maximum else None
    return None


def _bounded_number(value: Any, *, maximum: float) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return int(number) if number.is_integer() else number


def _exit_code(payload: Mapping[str, Any]) -> int | None:
    for candidate in _result_mappings(payload):
        for key in ("exit_code", "exitCode"):
            value = _integer(candidate.get(key))
            if value is not None:
                return value
    return None


def _command(payload: Mapping[str, Any]) -> str:
    for candidate in _input_mappings(payload):
        for key in ("command", "cmd", "shell_command"):
            value = candidate.get(key)
            if isinstance(value, str) and 0 < len(value) <= 131_072:
                return value
    return ""


def _command_kind(command: str) -> tuple[str, str] | None:
    """Conservatively classify a single recognized check command."""

    if not command.strip() or _COMPOUND_SHELL.search(command):
        return None
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not words:
        return None

    executable = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    args = [word.lower() for word in words[1:]]
    if executable in {"uv", "poetry", "pipenv"} and args[:1] == ["run"] and len(args) >= 2:
        executable, args = args[1], args[2:]
    elif executable in {"npx", "pnpm"} and args[:1] in (["exec"], ["dlx"]) and len(args) >= 2:
        executable, args = args[1], args[2:]
    elif executable == "npx" and args:
        executable, args = args[0], args[1:]

    if executable in {"python", "python3", "python.exe", "python3.exe"} and args[:2] in (
        ["-m", "pytest"],
        ["-m", "unittest"],
    ):
        return "test", executable
    if executable in {"pytest", "py.test", "unittest", "vitest", "jest", "rspec", "tox"}:
        return "test", executable
    if executable == "nox" and any(value in {"test", "tests"} for value in args):
        return "test", executable

    if executable in {"npm", "pnpm", "yarn", "bun"}:
        target = ""
        if args:
            if args[0] in {"test", "build", "lint", "typecheck"}:
                target = args[0]
            elif len(args) > 1 and args[0] in {"run", "run-script"}:
                target = args[1]
        if target in {"test", "build", "lint", "typecheck"}:
            return target, executable

    if executable == "go" and args:
        if args[0] == "test":
            return "test", executable
        if args[0] == "build":
            return "build", executable
        if args[0] == "vet":
            return "lint", executable
    if executable == "cargo" and args:
        category = {
            "test": "test",
            "build": "build",
            "check": "typecheck",
            "clippy": "lint",
        }.get(args[0])
        if category:
            return category, executable
    if executable in {"mvn", "mvnw"}:
        if "test" in args:
            return "test", executable
        if "package" in args or "verify" in args:
            return "build", executable
    if executable in {"gradle", "gradlew"}:
        if "test" in args:
            return "test", executable
        if "build" in args or "assemble" in args:
            return "build", executable
    if executable == "cmake" and "--build" in args:
        return "build", executable
    if executable == "make" and args:
        target = args[0]
        if target in {"test", "tests", "check"}:
            return "test", executable
        if target in {"build", "all"}:
            return "build", executable
        if target == "lint":
            return "lint", executable
        if target in {"typecheck", "type-check"}:
            return "typecheck", executable

    if executable in {"ruff", "flake8", "pylint", "eslint", "golangci-lint", "biome", "shellcheck"}:
        return "lint", executable
    if executable in {"mypy", "pyright", "pyre", "tsc"}:
        return "typecheck", executable
    return None


def _machine_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    exit_code = _exit_code(payload)
    if exit_code is None:
        return None
    command = _command(payload)
    classified = _command_kind(command)
    if classified is None:
        return None
    check_kind, runner = classified
    return {
        "check_kind": check_kind,
        "runner": safe_token(runner),
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "command_digest": f"sha256:{hashlib.sha256(command.encode('utf-8')).hexdigest()}",
    }


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    """Opaque retry discriminator used only when a host event id is absent."""

    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        encoded = repr(sorted(str(key) for key in payload)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _observed_times(
    payload: Mapping[str, Any],
    context: CaptureContext,
) -> tuple[datetime, datetime | None, bool]:
    value = _first(payload, "timestamp", "event_timestamp", "created_at", "occurred_at", "time")
    fallback = parse_observed_at(context.observed_at, fallback=utc_now()) if context.observed_at else utc_now()
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = parse_observed_at(value, fallback=fallback)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                if abs(seconds) > 10_000_000_000:
                    seconds /= 1000.0
                parsed = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            parsed = None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
        except ValueError:
            parsed = None
    if parsed is None:
        # Host occurrence time is unknown, so use the one capture timestamp
        # stamped on the registry context for observation ordering only.  It
        # must not enter retry identity: the same hook retried later would
        # otherwise become a different source event.
        return fallback, None, True
    return parsed, parsed, False


def _canonical_event(payload: Mapping[str, Any], context: CaptureContext, aliases: Mapping[str, str]) -> str:
    candidate = context.host_event or _first(payload, "hook_event_name", "event_name", "event") or ""
    if not isinstance(candidate, str):
        return ""
    return aliases.get(candidate.strip().lower(), "")


def _paths(payload: Mapping[str, Any], context: CaptureContext) -> tuple[str, ...]:
    candidates: list[Any] = []
    for key in ("file_path", "path"):
        if payload.get(key) is not None:
            candidates.append(payload[key])
    for key in ("modified_files", "files"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            candidates.extend(value[:50])
    attachments = payload.get("attachments")
    if isinstance(attachments, Sequence) and not isinstance(attachments, (str, bytes, bytearray)):
        for attachment in attachments[:50]:
            if isinstance(attachment, Mapping):
                candidates.append(attachment.get("file_path"))
    result: list[str] = []
    for candidate in candidates:
        projected = safe_relative_path(candidate, roots=context.workspace_roots)
        if projected and projected not in result:
            result.append(projected)
    return tuple(result[:50])


def _digests(payload: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for key in ("sha256", "content_digest", "file_digest", "artifact_digest"):
        digest = safe_digest(payload.get(key))
        if digest and digest not in result:
            result.append(digest)
    return tuple(result)


def _safe_attributes(
    payload: Mapping[str, Any],
    context: CaptureContext,
    *,
    phase: str,
    tool_name: str = "",
) -> dict[str, Any]:
    attributes: dict[str, Any] = {"phase": phase}
    token_fields = {
        "model": ("model",),
        "status": ("status", "final_status"),
        "permission_mode": ("permission_mode", "approval_mode", "composer_mode"),
        "client_version": ("cursor_version", "client_version"),
        "source": ("source",),
        "failure_type": ("failure_type",),
        "subagent_type": ("subagent_type",),
    }
    for output_key, input_keys in token_fields.items():
        value = safe_token(_first(payload, *input_keys))
        if value:
            attributes[output_key] = value
    if tool_name:
        attributes["tool_name"] = tool_name
    for output_key, input_key in (
        ("sandboxed", "sandbox"),
        ("background", "is_background_agent"),
        ("parallel_worker", "is_parallel_worker"),
        ("interrupted", "is_interrupt"),
        ("first_compaction", "is_first_compaction"),
    ):
        if isinstance(payload.get(input_key), bool):
            attributes[output_key] = payload[input_key]
    for output_key, input_keys, maximum in (
        ("duration_ms", ("duration_ms", "tool_duration_ms", "duration"), 86_400_000.0),
        ("loop_count", ("loop_count",), 1_000_000.0),
        ("message_count", ("message_count", "message_count_subagent"), 10_000_000.0),
        ("tool_call_count", ("tool_call_count",), 10_000_000.0),
        ("context_tokens", ("context_tokens",), 100_000_000.0),
        ("context_window_size", ("context_window_size",), 100_000_000.0),
        ("context_usage_percent", ("context_usage_percent",), 100.0),
    ):
        value = _bounded_number(_first(payload, *input_keys), maximum=maximum)
        if value is not None:
            attributes[output_key] = value
    files = _paths(payload, context)
    if files:
        attributes["files"] = list(files)
    digests = _digests(payload)
    if digests:
        attributes["digests"] = list(digests)
    return attributes


def _completeness(
    required: Mapping[str, str],
    *,
    timestamp_missing: bool,
    limitations: Sequence[str] = (),
) -> CaptureCompleteness:
    missing = [name for name, value in required.items() if not value]
    if timestamp_missing:
        missing.append("host_timestamp")
    return CaptureCompleteness(
        status="complete" if not missing and not limitations else "partial",
        missing_fields=tuple(sorted(set(missing))),
        limitations=tuple(sorted(set(limitations))),
    )


class _HookAdapter:
    vendor: str
    capabilities: AdapterCapabilities
    _aliases: Mapping[str, str]

    def _observation(
        self,
        *,
        payload: Mapping[str, Any],
        context: CaptureContext,
        host_event: str,
        event_type: CaptureEventType,
        session_id: str = "",
        turn_id: str = "",
        tool_call_id: str = "",
        parent_session_id: str = "",
        linked_session_id: str = "",
        attributes: Mapping[str, Any] | None = None,
        required: Mapping[str, str] | None = None,
        limitations: Sequence[str] = (),
    ) -> tuple[CaptureObservation, bool]:
        observed_at, identity_at, timestamp_missing = _observed_times(payload, context)
        raw_fingerprint = _payload_fingerprint(payload)
        discriminator = _opaque_id(payload, "event_id", "hook_id", "invocation_id", "uuid")
        if not discriminator:
            discriminator = raw_fingerprint if timestamp_missing else "host-timestamp"
        safe_attributes = dict(attributes or {})
        if timestamp_missing:
            safe_attributes["time_basis"] = "capture_observed"
            safe_attributes["identity_basis"] = "payload_fingerprint"
        observation = CaptureObservation(
            vendor=self.vendor,
            event_type=event_type,
            source_event_id=source_event_id(
                vendor=self.vendor,
                host_event=host_event,
                event_type=event_type,
                observed_at=identity_at,
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                parent_session_id=parent_session_id,
                linked_session_id=linked_session_id,
                stable_discriminator=discriminator,
            ),
            observed_at=observed_at,
            host_event=host_event,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            parent_session_id=parent_session_id,
            linked_session_id=linked_session_id,
            raw_digest=f"sha256:{raw_fingerprint}",
            attributes=safe_attributes,
            completeness=_completeness(
                required or {"session_id": session_id},
                timestamp_missing=timestamp_missing,
                limitations=limitations,
            ),
        )
        return observation, timestamp_missing

    def _parent_link(
        self,
        *,
        payload: Mapping[str, Any],
        context: CaptureContext,
        host_event: str,
        session_id: str,
        parent_session_id: str,
    ) -> CaptureObservation | None:
        if not parent_session_id or not session_id or parent_session_id == session_id:
            return None
        link, _ = self._observation(
            payload=payload,
            context=context,
            host_event=host_event,
            event_type="session_link_observed",
            session_id=session_id,
            parent_session_id=parent_session_id,
            linked_session_id=session_id,
            attributes={"relationship": "parent_session"},
            required={"parent_session_id": parent_session_id, "linked_session_id": session_id},
        )
        return link


class ClaudeCodeAdapter(_HookAdapter):
    vendor = "claude-code"
    _events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SubagentStop", "SessionEnd")
    _aliases = {value.lower(): value for value in _events}
    capabilities = AdapterCapabilities(
        vendor=vendor,
        supported_host_events=_events,
        emits=("session_observed", "turn_observed", "tool_observed", "machine_check_observed", "session_link_observed"),
        session_lifecycle=True,
        turns=True,
        tools=True,
        edits_artifacts=True,
        subagents=True,
        machine_results=True,
        usage=False,
        cost=False,
        native_otlp=True,
        stable_event_identity="derived",
        session_identity="host",
        turn_identity="unavailable",
        usage_basis="not_captured",
        limitations=(
            "hook_payloads_do_not_report_usage_or_cost",
            "subagent_task_id_is_not_a_client_session_id",
            "turn_id_is_not_consistently_exposed",
        ),
    )

    def normalize(self, payload: Mapping[str, Any], context: CaptureContext) -> CaptureResult:
        cursor_marker = any(context.environment.get(key) for key in _CURSOR_ENV_MARKERS)
        if cursor_marker and context.environment.get("CLAUDECODE") != "1":
            return CaptureResult(ignored_reason="host_mismatch_cursor_environment")
        event = _canonical_event(payload, context, self._aliases)
        if not event:
            return CaptureResult(ignored_reason="unsupported_host_event")

        session_id = _opaque_id(payload, "session_id", "conversation_id")
        turn_id = _opaque_id(payload, "turn_id", "prompt_id", "message_id")
        tool_call_id = _opaque_id(payload, "tool_use_id", "tool_call_id", "call_id")
        tool_name = safe_token(_first(payload, "tool_name"))
        observations: list[CaptureObservation] = []
        missing_timestamp = False

        if event in {"SessionStart", "SessionEnd"}:
            phase = "started" if event == "SessionStart" else "ended"
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="session_observed",
                session_id=session_id,
                attributes=_safe_attributes(payload, context, phase=phase),
                required={"session_id": session_id},
            )
            observations.append(observation)
        elif event in {"UserPromptSubmit", "Stop"}:
            phase = "submitted" if event == "UserPromptSubmit" else "completed"
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="turn_observed",
                session_id=session_id,
                turn_id=turn_id,
                attributes=_safe_attributes(payload, context, phase=phase),
                required={"session_id": session_id, "turn_id": turn_id},
                limitations=("turn_id_not_exposed_by_host",) if not turn_id else (),
            )
            observations.append(observation)
        elif event in {"PreToolUse", "PostToolUse"}:
            phase = "requested" if event == "PreToolUse" else "completed"
            attributes = _safe_attributes(payload, context, phase=phase, tool_name=tool_name)
            exit_code = _exit_code(payload) if event == "PostToolUse" else None
            if exit_code is not None:
                attributes["exit_code"] = exit_code
                attributes["errored"] = exit_code != 0
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="tool_observed",
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                attributes=attributes,
                required={"session_id": session_id, "tool_identity": tool_call_id or tool_name},
            )
            observations.append(observation)
            machine = _machine_metadata(payload) if event == "PostToolUse" else None
            if machine is not None:
                check, _ = self._observation(
                    payload=payload,
                    context=context,
                    host_event=event,
                    event_type="machine_check_observed",
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    attributes=machine,
                    required={"session_id": session_id, "objective_exit_status": str(machine["exit_code"])},
                )
                observations.append(check)
        else:  # SubagentStop
            linked_id = _opaque_id(payload, "task_id", "subagent_id", "agent_id")
            attributes = _safe_attributes(payload, context, phase="completed", tool_name=tool_name)
            attributes["relationship"] = "spawned_subagent"
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="session_link_observed",
                session_id=session_id,
                tool_call_id=tool_call_id,
                parent_session_id=session_id,
                linked_session_id=linked_id,
                attributes=attributes,
                required={"parent_session_id": session_id, "linked_session_id": linked_id},
                limitations=("linked_id_is_task_not_client_session",),
            )
            observations.append(observation)

        warnings = ("host_timestamp_missing",) if missing_timestamp else ()
        return CaptureResult(observations=tuple(observations), warnings=warnings)


class CodexAdapter(_HookAdapter):
    vendor = "codex"
    _events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
    _aliases = {value.lower(): value for value in _events}
    capabilities = AdapterCapabilities(
        vendor=vendor,
        supported_host_events=_events,
        emits=("session_observed", "turn_observed", "tool_observed", "machine_check_observed", "session_link_observed"),
        session_lifecycle=True,
        turns=True,
        tools=True,
        edits_artifacts=True,
        subagents=True,
        machine_results=True,
        usage=False,
        cost=False,
        native_otlp=False,
        stable_event_identity="mixed",
        session_identity="host",
        turn_identity="host",
        usage_basis="not_captured",
        limitations=(
            "codex_hook_protocol_has_no_session_end_event",
            "usage_requires_separate_rollout_import",
            "parent_link_may_require_rollout_metadata",
        ),
    )

    def normalize(self, payload: Mapping[str, Any], context: CaptureContext) -> CaptureResult:
        event = _canonical_event(payload, context, self._aliases)
        if not event:
            return CaptureResult(ignored_reason="unsupported_host_event")
        session_id = _opaque_id(payload, "session_id", "thread_id", "conversation_id")
        parent_id = _opaque_id(payload, "parent_session_id", "parent_thread_id")
        turn_id = _opaque_id(payload, "turn_id", "message_id", "request_id")
        tool_call_id = _opaque_id(payload, "tool_use_id", "tool_call_id", "call_id")
        tool_name = safe_token(_first(payload, "tool_name"))
        observations: list[CaptureObservation] = []
        missing_timestamp = False

        if event == "SessionStart":
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="session_observed",
                session_id=session_id,
                parent_session_id=parent_id,
                attributes=_safe_attributes(payload, context, phase="started"),
                required={"session_id": session_id},
                limitations=("session_end_not_exposed_by_host",),
            )
            observations.append(observation)
        elif event in {"UserPromptSubmit", "Stop"}:
            phase = "submitted" if event == "UserPromptSubmit" else "completed"
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="turn_observed",
                session_id=session_id,
                turn_id=turn_id,
                parent_session_id=parent_id,
                attributes=_safe_attributes(payload, context, phase=phase),
                required={"session_id": session_id, "turn_id": turn_id},
            )
            observations.append(observation)
        else:
            phase = "requested" if event == "PreToolUse" else "completed"
            attributes = _safe_attributes(payload, context, phase=phase, tool_name=tool_name)
            exit_code = _exit_code(payload) if event == "PostToolUse" else None
            if exit_code is not None:
                attributes["exit_code"] = exit_code
                attributes["errored"] = exit_code != 0
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="tool_observed",
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                parent_session_id=parent_id,
                attributes=attributes,
                required={"session_id": session_id, "tool_identity": tool_call_id or tool_name},
            )
            observations.append(observation)
            machine = _machine_metadata(payload) if event == "PostToolUse" else None
            if machine is not None:
                check, _ = self._observation(
                    payload=payload,
                    context=context,
                    host_event=event,
                    event_type="machine_check_observed",
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    parent_session_id=parent_id,
                    attributes=machine,
                    required={"session_id": session_id, "objective_exit_status": str(machine["exit_code"])},
                )
                observations.append(check)

        link = self._parent_link(
            payload=payload,
            context=context,
            host_event=event,
            session_id=session_id,
            parent_session_id=parent_id,
        )
        if link is not None:
            observations.append(link)
        warnings = ("host_timestamp_missing",) if missing_timestamp else ()
        return CaptureResult(observations=tuple(observations), warnings=warnings)


class CursorAdapter(_HookAdapter):
    vendor = "cursor"
    _events = (
        "sessionStart",
        "sessionEnd",
        "beforeSubmitPrompt",
        "afterAgentResponse",
        "afterAgentThought",
        "preToolUse",
        "postToolUse",
        "postToolUseFailure",
        "subagentStart",
        "subagentStop",
        "beforeShellExecution",
        "afterShellExecution",
        "beforeMCPExecution",
        "afterMCPExecution",
        "beforeReadFile",
        "afterFileEdit",
        "preCompact",
        "stop",
    )
    _aliases = {value.lower(): value for value in _events}
    capabilities = AdapterCapabilities(
        vendor=vendor,
        supported_host_events=_events,
        emits=("session_observed", "turn_observed", "tool_observed", "machine_check_observed", "session_link_observed"),
        session_lifecycle=True,
        turns=True,
        tools=True,
        edits_artifacts=True,
        subagents=True,
        machine_results=True,
        usage=False,
        cost=False,
        native_otlp=False,
        stable_event_identity="derived",
        session_identity="mixed",
        turn_identity="host",
        usage_basis="not_captured",
        limitations=(
            "conversation_id_is_preferred_over_process_session_id",
            "hook_payloads_do_not_report_usage_or_cost",
            "machine_results_require_a_host_exit_code_extension",
        ),
    )

    def normalize(self, payload: Mapping[str, Any], context: CaptureContext) -> CaptureResult:
        event = _canonical_event(payload, context, self._aliases)
        if not event:
            return CaptureResult(ignored_reason="unsupported_host_event")
        session_id = _opaque_id(payload, "conversation_id", "session_id")
        turn_id = _opaque_id(payload, "generation_id", "turn_id", "message_id")
        tool_call_id = _opaque_id(payload, "tool_use_id", "tool_call_id", "call_id")
        tool_name = safe_token(_first(payload, "tool_name"))
        observations: list[CaptureObservation] = []
        missing_timestamp = False

        if event in {"sessionStart", "sessionEnd", "preCompact"}:
            phase = {"sessionStart": "started", "sessionEnd": "ended", "preCompact": "compacting"}[event]
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="session_observed",
                session_id=session_id,
                attributes=_safe_attributes(payload, context, phase=phase),
                required={"session_id": session_id},
            )
            observations.append(observation)
        elif event in {"beforeSubmitPrompt", "afterAgentResponse", "afterAgentThought", "stop"}:
            phase = {
                "beforeSubmitPrompt": "submitted",
                "afterAgentResponse": "responded",
                "afterAgentThought": "thought_completed",
                "stop": "completed",
            }[event]
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="turn_observed",
                session_id=session_id,
                turn_id=turn_id,
                attributes=_safe_attributes(payload, context, phase=phase),
                required={"session_id": session_id, "turn_id": turn_id},
            )
            observations.append(observation)
        elif event in {"subagentStart", "subagentStop"}:
            linked_id = _opaque_id(payload, "subagent_id", "agent_id")
            parent_id = _opaque_id(payload, "parent_conversation_id", "parent_session_id") or session_id
            attributes = _safe_attributes(
                payload,
                context,
                phase="started" if event == "subagentStart" else "completed",
                tool_name=tool_name,
            )
            attributes["relationship"] = "spawned_subagent"
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="session_link_observed",
                session_id=session_id,
                tool_call_id=tool_call_id,
                parent_session_id=parent_id,
                linked_session_id=linked_id,
                attributes=attributes,
                required={"parent_session_id": parent_id, "linked_session_id": linked_id},
            )
            observations.append(observation)
        else:
            shell_event = event in {"beforeShellExecution", "afterShellExecution"}
            if event.startswith(("pre", "before")):
                phase = "requested"
            elif event == "postToolUseFailure":
                phase = "failed"
            else:
                phase = "completed"
            if shell_event:
                effective_tool_name = "shell"
            elif event == "beforeReadFile":
                effective_tool_name = "read_file"
            elif event == "afterFileEdit":
                effective_tool_name = "file_edit"
            else:
                effective_tool_name = tool_name
            attributes = _safe_attributes(payload, context, phase=phase, tool_name=effective_tool_name)
            exit_code = _exit_code(payload) if event.startswith(("post", "after")) else None
            if exit_code is not None:
                attributes["exit_code"] = exit_code
                attributes["errored"] = exit_code != 0
            elif event == "postToolUseFailure":
                attributes["errored"] = True
            observation, missing_timestamp = self._observation(
                payload=payload,
                context=context,
                host_event=event,
                event_type="tool_observed",
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                attributes=attributes,
                required={"session_id": session_id, "tool_identity": tool_call_id or effective_tool_name},
            )
            observations.append(observation)
            machine = _machine_metadata(payload) if event.startswith(("post", "after")) else None
            if machine is not None:
                check, _ = self._observation(
                    payload=payload,
                    context=context,
                    host_event=event,
                    event_type="machine_check_observed",
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    attributes=machine,
                    required={"session_id": session_id, "objective_exit_status": str(machine["exit_code"])},
                )
                observations.append(check)

        warnings = ("host_timestamp_missing",) if missing_timestamp else ()
        return CaptureResult(observations=tuple(observations), warnings=warnings)


__all__ = ["ClaudeCodeAdapter", "CodexAdapter", "CursorAdapter"]
