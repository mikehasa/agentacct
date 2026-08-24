"""Compatibility boundary for Codex rollout records.

Codex rollout JSONL is a persistence format, not agentacct's domain model. This
module converts the historical carrier variants into logical MCP-call fragments,
reconciles duplicate fragments deterministically, and selects model identity only
from known Codex-owned paths. Token accounting and product projection remain in
``client_usage`` and consume these normalized facts.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping

from .log_evidence import (
    EVIDENCED_EVENT_ID_RE,
    LogEvidenceAccumulator,
    classify_codex_function_call,
    classify_codex_mcp_call_identity,
    classify_refused_recording,
    extract_created_event_id,
    extract_created_event_id_from_payload,
)


_CodexRefusal = tuple[str | None, str | None, str]
_CodexOutcomeClassification = Literal[
    "success",
    "failure",
    "unknown",
    "malformed",
]
_CodexOutcomeCarrier = Literal["function_output", "result", "error"]


@dataclass(frozen=True)
class CodexEvidenceOutcome:
    """Privacy-bounded semantic outcome decoded from one Codex carrier."""

    outcome_classification: _CodexOutcomeClassification
    event_id: str | None = None
    refusal: _CodexRefusal | None = None

    def __post_init__(self) -> None:
        if self.outcome_classification not in {
            "success",
            "failure",
            "unknown",
            "malformed",
        }:
            raise ValueError(
                "unsupported Codex outcome classification: "
                f"{self.outcome_classification!r}"
            )
        if self.event_id is not None and self.outcome_classification != "success":
            raise ValueError("only a successful Codex outcome may carry an event_id")
        if self.outcome_classification == "success" and self.event_id is None:
            raise ValueError("a successful Codex outcome requires an event_id")
        if self.event_id is not None and not EVIDENCED_EVENT_ID_RE.match(
            self.event_id
        ):
            raise ValueError("Codex outcome event_id is malformed")
        if self.refusal is not None and self.outcome_classification != "failure":
            raise ValueError("only a failed Codex outcome may carry a refusal")


@dataclass(frozen=True)
class CodexEvidenceFragment:
    """One privacy-bounded fact about a logical Codex MCP call.

    Raw output and error text are decoded and discarded per JSONL line. Only a
    created-event id, a frozen refusal classification, and structural flags may
    survive until reconciliation at end of file.
    """

    call_id: str | None
    role: Literal["descriptor", "output", "terminal"]
    creation_tool_verdict: Literal["accepted", "rejected"] | None = None
    creation_tool_name: str | None = None
    outcome: CodexEvidenceOutcome | None = None
    counts_as_evidence_skip: bool = False
    has_schema_drift: bool = False

    def __post_init__(self) -> None:
        if (
            self.call_id is not None
            and validated_codex_identifier(self.call_id) != self.call_id
        ):
            raise ValueError("Codex evidence fragment call_id is malformed")
        if self.role not in {"descriptor", "output", "terminal"}:
            raise ValueError(f"unsupported Codex fragment role: {self.role!r}")
        if self.creation_tool_verdict not in {None, "accepted", "rejected"}:
            raise ValueError(
                "unsupported Codex fragment creation-tool verdict: "
                f"{self.creation_tool_verdict!r}"
            )
        if self.outcome is not None and not isinstance(
            self.outcome,
            CodexEvidenceOutcome,
        ):
            raise TypeError("Codex fragment outcome is invalid")
        if not isinstance(self.counts_as_evidence_skip, bool) or not isinstance(
            self.has_schema_drift,
            bool,
        ):
            raise TypeError("Codex fragment flags must be bools")


_CODEX_IDENTITY_TEXT_MAX = 240
_CODEX_ACTION_NAME_TEXT_MAX = 120
_CODEX_MODEL_TEXT_MAX = 120
_CODEX_RESULT_TEXT_MAX = 1_000_000


def validated_codex_identifier(
    value: Any,
    *,
    max_length: int = _CODEX_IDENTITY_TEXT_MAX,
) -> str | None:
    """Return an exact bounded identifier, or ``None`` when it is unsafe."""

    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        return None
    # Never truncate an identity: truncation could merge distinct logical calls.
    return value


def codex_mcp_action_name(server: Any, tool: Any) -> str | None:
    """Canonical Actions identity for an MCP server/tool pair."""

    validated_server = validated_codex_identifier(server)
    validated_tool = validated_codex_identifier(tool)
    if validated_server is None or validated_tool is None:
        return None
    normalized_server = validated_server.removeprefix("mcp__").replace("-", "_")
    if not normalized_server:
        return None
    action_name = f"mcp__{normalized_server}__{validated_tool}"
    return validated_codex_identifier(
        action_name,
        max_length=_CODEX_ACTION_NAME_TEXT_MAX,
    )


def codex_function_action_name(namespace: Any, tool: Any) -> str | None:
    """Action identity for a Codex ``function_call`` without namespace drift.

    A namespace is not synonymous with MCP: Codex uses values such as
    ``collaboration`` for built-in agent tools. Canonicalize only explicit MCP
    namespaces and the historical agentacct registration namespaces on an
    allowlisted creation call; otherwise preserve the bare built-in name.
    """

    normalized_tool = validated_codex_identifier(
        tool,
        max_length=_CODEX_ACTION_NAME_TEXT_MAX,
    )
    if normalized_tool is None:
        return None
    if isinstance(namespace, str) and namespace.startswith("mcp__"):
        return codex_mcp_action_name(namespace, normalized_tool)
    if (
        namespace is not None
        and classify_codex_function_call(normalized_tool, namespace) == "accepted"
    ):
        return codex_mcp_action_name(namespace, normalized_tool)
    return normalized_tool


_MALFORMED_CODEX_OUTCOME = CodexEvidenceOutcome(
    outcome_classification="malformed"
)


def _single_codex_text_block(content: Any) -> str | None:
    """Return the one non-empty text block emitted by a creation tool."""

    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, Mapping) or block.get("type") != "text":
        return None
    text = block.get("text")
    return text if isinstance(text, str) and text else None


def _codex_success_outcome(text: str) -> CodexEvidenceOutcome:
    """Reduce a structurally successful result without retaining its text."""

    if len(text) > _CODEX_RESULT_TEXT_MAX:
        return _MALFORMED_CODEX_OUTCOME
    try:
        event_id = extract_created_event_id(text)
    except RecursionError:
        return _MALFORMED_CODEX_OUTCOME
    if event_id is None:
        return _MALFORMED_CODEX_OUTCOME
    return CodexEvidenceOutcome(
        outcome_classification="success",
        event_id=event_id,
    )


def _codex_success_outcome_from_payload(
    payload: Mapping[str, Any],
) -> CodexEvidenceOutcome:
    """Reduce an already-decoded success payload without parsing it again."""

    event_id = extract_created_event_id_from_payload(payload)
    if event_id is None:
        return _MALFORMED_CODEX_OUTCOME
    return CodexEvidenceOutcome(
        outcome_classification="success",
        event_id=event_id,
    )


def _codex_failure_outcome_from_refusal(
    refusal: _CodexRefusal,
    *,
    creation_tool_name: str | None,
) -> CodexEvidenceOutcome:
    embedded_tool, field, reason = refusal
    if (
        embedded_tool is not None
        and creation_tool_name is not None
        and embedded_tool != creation_tool_name
    ):
        return _MALFORMED_CODEX_OUTCOME
    return CodexEvidenceOutcome(
        outcome_classification="failure",
        refusal=(embedded_tool or creation_tool_name, field, reason),
    )


def _codex_failure_outcome(
    text: Any,
    *,
    creation_tool_name: str | None,
) -> CodexEvidenceOutcome:
    """Classify and discard one explicit Codex failure message."""

    if not isinstance(text, str) or not text:
        return _MALFORMED_CODEX_OUTCOME
    if len(text) > _CODEX_RESULT_TEXT_MAX:
        return CodexEvidenceOutcome(outcome_classification="failure")
    refusal = classify_refused_recording(text)
    if refusal is None:
        return CodexEvidenceOutcome(outcome_classification="failure")
    return _codex_failure_outcome_from_refusal(
        refusal,
        creation_tool_name=creation_tool_name,
    )


def _decode_codex_call_tool_result(
    result: Mapping[str, Any],
    *,
    creation_tool_name: str | None,
) -> CodexEvidenceOutcome:
    """Decode one direct MCP ``CallToolResult`` mapping."""

    if any(
        discriminator in result
        for discriminator in ("Ok", "Err", "error", "message")
    ):
        return _MALFORMED_CODEX_OUTCOME
    is_error = result.get("isError", False)
    if not isinstance(is_error, bool):
        return _MALFORMED_CODEX_OUTCOME
    text = _single_codex_text_block(result.get("content"))
    if text is None:
        return _MALFORMED_CODEX_OUTCOME
    if is_error:
        return _codex_failure_outcome(
            text,
            creation_tool_name=creation_tool_name,
        )
    return _codex_success_outcome(text)


def _decode_codex_result_envelope(
    value: Any,
    *,
    creation_tool_name: str | None,
) -> CodexEvidenceOutcome:
    """Decode legacy ``Ok``/``Err`` or direct MCP result envelopes."""

    if not isinstance(value, Mapping):
        return _MALFORMED_CODEX_OUTCOME
    has_ok = "Ok" in value
    has_err = "Err" in value
    has_content = "content" in value
    has_is_error = "isError" in value
    has_message = "message" in value
    has_error = "error" in value
    has_direct_result_fields = (
        has_content or has_is_error or has_message or has_error
    )

    if has_ok:
        if has_err or has_direct_result_fields:
            return _MALFORMED_CODEX_OUTCOME
        nested = value.get("Ok")
        if not isinstance(nested, Mapping):
            return _MALFORMED_CODEX_OUTCOME
        return _decode_codex_call_tool_result(
            nested,
            creation_tool_name=creation_tool_name,
        )
    if has_err:
        if has_direct_result_fields:
            return _MALFORMED_CODEX_OUTCOME
        return _codex_failure_outcome(
            value.get("Err"),
            creation_tool_name=creation_tool_name,
        )
    if has_message or has_error:
        # Current paginated failures carry message under the outer ``error``
        # field. A message in a result envelope is an unsupported discriminator,
        # not an alternate success/failure branch.
        return _MALFORMED_CODEX_OUTCOME
    if has_content or has_is_error:
        return _decode_codex_call_tool_result(
            value,
            creation_tool_name=creation_tool_name,
        )
    return _MALFORMED_CODEX_OUTCOME


def _decode_codex_function_output(
    value: Any,
    *,
    creation_tool_name: str | None,
) -> CodexEvidenceOutcome:
    """Decode the historical split ``function_call_output`` value."""

    if isinstance(value, Mapping):
        return _decode_codex_result_envelope(
            value,
            creation_tool_name=creation_tool_name,
        )
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _CODEX_RESULT_TEXT_MAX
    ):
        return _MALFORMED_CODEX_OUTCOME

    candidates = [value]
    output_marker = "Output:\n"
    if output_marker in value:
        candidates.insert(0, value.split(output_marker, 1)[1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except RecursionError:
            return _MALFORMED_CODEX_OUTCOME
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            text = _single_codex_text_block(parsed)
            if text is None:
                return _MALFORMED_CODEX_OUTCOME
            refusal = classify_refused_recording(text)
            if refusal is not None:
                return _codex_failure_outcome_from_refusal(
                    refusal,
                    creation_tool_name=creation_tool_name,
                )
            return _codex_success_outcome(text)
        if isinstance(parsed, Mapping):
            transport_discriminators = ("Ok", "Err", "content", "isError")
            if any(key in parsed for key in transport_discriminators):
                if "event" in parsed:
                    return _MALFORMED_CODEX_OUTCOME
                return _decode_codex_result_envelope(
                    parsed,
                    creation_tool_name=creation_tool_name,
                )
            creation_outcome = _codex_success_outcome_from_payload(parsed)
            if creation_outcome.outcome_classification == "success":
                return creation_outcome
            if "error" in parsed or "message" in parsed:
                return _decode_codex_result_envelope(
                    parsed,
                    creation_tool_name=creation_tool_name,
                )
            return creation_outcome
        return _MALFORMED_CODEX_OUTCOME

    refusal = classify_refused_recording(value)
    if refusal is not None:
        return _codex_failure_outcome_from_refusal(
            refusal,
            creation_tool_name=creation_tool_name,
        )
    return CodexEvidenceOutcome(outcome_classification="unknown")


def _decode_codex_call_outcome(
    value: Any,
    *,
    carrier: _CodexOutcomeCarrier,
    creation_tool_name: str | None,
) -> CodexEvidenceOutcome:
    """Decode one Codex outcome into an explicit, privacy-bounded state."""

    if carrier == "function_output":
        return _decode_codex_function_output(
            value,
            creation_tool_name=creation_tool_name,
        )
    if carrier == "result":
        return _decode_codex_result_envelope(
            value,
            creation_tool_name=creation_tool_name,
        )
    if carrier == "error":
        if not isinstance(value, Mapping):
            return _MALFORMED_CODEX_OUTCOME
        if any(
            discriminator in value
            for discriminator in ("Ok", "Err", "content", "isError")
        ):
            return _MALFORMED_CODEX_OUTCOME
        return _codex_failure_outcome(
            value.get("message"),
            creation_tool_name=creation_tool_name,
        )
    raise ValueError(f"unsupported Codex outcome carrier: {carrier!r}")


def _codex_function_descriptor_fragment(
    payload: Mapping[str, Any],
) -> CodexEvidenceFragment | None:
    """Decode the call descriptor from Codex's split function-call carrier."""

    creation_tool_verdict = classify_codex_function_call(
        payload.get("name"), payload.get("namespace")
    )
    if creation_tool_verdict is None:
        return None
    call_id = validated_codex_identifier(payload.get("call_id"))
    return CodexEvidenceFragment(
        call_id=call_id,
        role="descriptor",
        creation_tool_verdict=creation_tool_verdict,
        creation_tool_name=validated_codex_identifier(payload.get("name")),
        counts_as_evidence_skip=call_id is None,
        has_schema_drift=call_id is None,
    )


def _codex_function_output_fragment(
    payload: Mapping[str, Any],
) -> CodexEvidenceFragment | None:
    """Decode the output half of Codex's split function-call carrier."""

    call_id = validated_codex_identifier(payload.get("call_id"))
    if call_id is None:
        return None
    outcome = _decode_codex_call_outcome(
        payload.get("output"),
        carrier="function_output",
        creation_tool_name=None,
    )
    return CodexEvidenceFragment(
        call_id=call_id,
        role="output",
        outcome=outcome,
        has_schema_drift=outcome.outcome_classification == "malformed",
    )


def _codex_completed_terminal_fragment(
    *,
    call_id: str | None,
    creation_tool_name: str,
    result: Any,
    has_identity_drift: bool,
) -> CodexEvidenceFragment:
    """Normalize one accepted terminal carrier with a completed result."""

    outcome = _decode_codex_call_outcome(
        result,
        carrier="result",
        creation_tool_name=creation_tool_name,
    )
    return CodexEvidenceFragment(
        call_id=call_id,
        role="terminal",
        creation_tool_verdict="accepted",
        creation_tool_name=creation_tool_name,
        outcome=outcome,
        counts_as_evidence_skip=call_id is None or outcome.event_id is None,
        has_schema_drift=(
            has_identity_drift or outcome.outcome_classification == "malformed"
        ),
    )


def _codex_failed_terminal_fragment(
    *,
    call_id: str | None,
    creation_tool_name: str,
    error: Any,
    result: Any,
    has_identity_drift: bool,
) -> CodexEvidenceFragment:
    """Normalize one accepted paginated carrier whose call failed."""

    has_error = error is not None
    has_result = result is not None
    if has_error == has_result:
        outcome = _MALFORMED_CODEX_OUTCOME
    else:
        outcome = _decode_codex_call_outcome(
            error if has_error else result,
            carrier="error" if has_error else "result",
            creation_tool_name=creation_tool_name,
        )
        if outcome.outcome_classification != "failure":
            outcome = _MALFORMED_CODEX_OUTCOME
    return CodexEvidenceFragment(
        call_id=call_id,
        role="terminal",
        creation_tool_verdict="accepted",
        creation_tool_name=creation_tool_name,
        outcome=outcome,
        counts_as_evidence_skip=True,
        has_schema_drift=(
            has_identity_drift or outcome.outcome_classification == "malformed"
        ),
    )


def _codex_conflicted_paginated_outcome_fragment(
    *,
    call_id: str | None,
    creation_tool_name: str,
) -> CodexEvidenceFragment:
    """Fail closed when mutually exclusive paginated outcomes coexist."""

    return CodexEvidenceFragment(
        call_id=call_id,
        role="terminal",
        creation_tool_verdict="accepted",
        creation_tool_name=creation_tool_name,
        outcome=_MALFORMED_CODEX_OUTCOME,
        counts_as_evidence_skip=True,
        has_schema_drift=True,
    )


def _codex_legacy_terminal_fragment(
    payload: Mapping[str, Any],
) -> CodexEvidenceFragment | None:
    """Decode the historical ``mcp_tool_call_end`` terminal carrier."""

    call_id = validated_codex_identifier(payload.get("call_id"))
    invocation = payload.get("invocation")
    if not isinstance(invocation, Mapping):
        return CodexEvidenceFragment(
            call_id=call_id,
            role="terminal",
            counts_as_evidence_skip=True,
            has_schema_drift=True,
        )
    creation_tool_name = validated_codex_identifier(invocation.get("tool"))
    if creation_tool_name is None:
        return CodexEvidenceFragment(
            call_id=call_id,
            role="terminal",
            counts_as_evidence_skip=True,
            has_schema_drift=True,
        )
    server = invocation.get("server")
    creation_tool_verdict = classify_codex_mcp_call_identity(
        server,
        creation_tool_name,
    )
    if creation_tool_verdict is None:
        return None
    has_identity_drift = (
        call_id is None or not isinstance(server, str) or not server
    )
    if creation_tool_verdict == "rejected":
        return CodexEvidenceFragment(
            call_id=call_id,
            role="terminal",
            creation_tool_verdict=creation_tool_verdict,
            creation_tool_name=creation_tool_name,
            counts_as_evidence_skip=True,
            has_schema_drift=has_identity_drift,
        )
    return _codex_completed_terminal_fragment(
        call_id=call_id,
        creation_tool_name=creation_tool_name,
        result=payload.get("result"),
        has_identity_drift=has_identity_drift,
    )


def _codex_paginated_terminal_fragment(
    payload: Mapping[str, Any],
) -> CodexEvidenceFragment | None:
    """Decode Codex's current ``item_completed`` / ``McpToolCall`` carrier."""

    item = payload.get("item")
    if not isinstance(item, Mapping):
        return CodexEvidenceFragment(
            call_id=None,
            role="terminal",
            counts_as_evidence_skip=True,
            has_schema_drift=True,
        )
    if item.get("type") != "McpToolCall":
        # Other known TurnItems are irrelevant. An unknown wrapper carrying a
        # creation-tool identity is evidence drift, not a safe reason to ignore
        # a call that may otherwise have been agentacct evidence.
        creation_tool_name = validated_codex_identifier(item.get("tool"))
        creation_tool_verdict = classify_codex_mcp_call_identity(
            item.get("server"),
            creation_tool_name,
        )
        if creation_tool_verdict is None:
            return None
        return CodexEvidenceFragment(
            call_id=validated_codex_identifier(item.get("id")),
            role="terminal",
            creation_tool_verdict=creation_tool_verdict,
            creation_tool_name=creation_tool_name,
            counts_as_evidence_skip=True,
            has_schema_drift=True,
        )

    call_id = validated_codex_identifier(item.get("id"))
    creation_tool_name = validated_codex_identifier(item.get("tool"))
    if creation_tool_name is None:
        return CodexEvidenceFragment(
            call_id=call_id,
            role="terminal",
            counts_as_evidence_skip=True,
            has_schema_drift=True,
        )
    server = item.get("server")
    creation_tool_verdict = classify_codex_mcp_call_identity(
        server,
        creation_tool_name,
    )
    if creation_tool_verdict is None:
        return None
    has_identity_drift = (
        call_id is None or not isinstance(server, str) or not server
    )
    if creation_tool_verdict == "rejected":
        return CodexEvidenceFragment(
            call_id=call_id,
            role="terminal",
            creation_tool_verdict=creation_tool_verdict,
            creation_tool_name=creation_tool_name,
            counts_as_evidence_skip=True,
            has_schema_drift=has_identity_drift,
        )

    status = item.get("status")
    result = item.get("result")
    error = item.get("error")
    if status == "completed":
        if error is not None:
            return _codex_conflicted_paginated_outcome_fragment(
                call_id=call_id,
                creation_tool_name=creation_tool_name,
            )
        return _codex_completed_terminal_fragment(
            call_id=call_id,
            creation_tool_name=creation_tool_name,
            result=result,
            has_identity_drift=has_identity_drift,
        )
    if status == "failed":
        if error is not None and result is not None:
            return _codex_conflicted_paginated_outcome_fragment(
                call_id=call_id,
                creation_tool_name=creation_tool_name,
            )
        return _codex_failed_terminal_fragment(
            call_id=call_id,
            creation_tool_name=creation_tool_name,
            error=error,
            result=result,
            has_identity_drift=has_identity_drift,
        )
    return CodexEvidenceFragment(
        call_id=call_id,
        role="terminal",
        creation_tool_verdict=creation_tool_verdict,
        creation_tool_name=creation_tool_name,
        outcome=_MALFORMED_CODEX_OUTCOME,
        counts_as_evidence_skip=True,
        has_schema_drift=True,
    )


def decode_codex_evidence_fragment(
    record: Mapping[str, Any],
) -> CodexEvidenceFragment | None:
    """Decode one complete rollout record into a bounded evidence fragment.

    Non-creation calls return None for evidence purposes. Malformed creation
    carriers remain visible as bounded skips plus evidence-schema drift. The
    outer record type is validated here so a payload under the wrong Codex
    channel cannot acquire evidence authority.
    """

    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    payload_type = payload.get("type")
    fragment: CodexEvidenceFragment | None
    expected_record_type: str | None = None
    if payload_type == "function_call":
        fragment = _codex_function_descriptor_fragment(payload)
        expected_record_type = "response_item"
    elif payload_type == "function_call_output":
        fragment = _codex_function_output_fragment(payload)
        expected_record_type = "response_item"
    elif payload_type == "mcp_tool_call_end":
        fragment = _codex_legacy_terminal_fragment(payload)
        expected_record_type = "event_msg"
    elif payload_type == "item_completed":
        fragment = _codex_paginated_terminal_fragment(payload)
        expected_record_type = "event_msg"
    else:
        return None

    if fragment is None or record.get("type") == expected_record_type:
        return fragment
    return replace(
        fragment,
        counts_as_evidence_skip=(
            fragment.counts_as_evidence_skip or fragment.role != "output"
        ),
        has_schema_drift=True,
    )


def codex_record_may_contain_evidence(record: Mapping[str, Any]) -> bool:
    """Cheap relevance check used only after the fragment scan cap is full."""

    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return False
    payload_type = payload.get("type")
    if payload_type == "function_call":
        return (
            classify_codex_function_call(
                payload.get("name"), payload.get("namespace")
            )
            is not None
        )
    if payload_type == "function_call_output":
        return validated_codex_identifier(payload.get("call_id")) is not None
    if payload_type == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        if not isinstance(invocation, Mapping):
            return True
        creation_tool_name = validated_codex_identifier(invocation.get("tool"))
        if creation_tool_name is None:
            return True
        return (
            classify_codex_mcp_call_identity(
                invocation.get("server"),
                creation_tool_name,
            )
            is not None
        )
    if payload_type != "item_completed":
        return False
    item = payload.get("item")
    if not isinstance(item, Mapping):
        return True
    if item.get("type") == "McpToolCall":
        creation_tool_name = validated_codex_identifier(item.get("tool"))
        if creation_tool_name is None:
            return True
        return (
            classify_codex_mcp_call_identity(
                item.get("server"),
                creation_tool_name,
            )
            is not None
        )
    return (
        classify_codex_mcp_call_identity(
            item.get("server"),
            validated_codex_identifier(item.get("tool")),
        )
        is not None
    )


def _bind_codex_refusal_to_tool(
    refusal: _CodexRefusal | None,
    creation_tool_name: str | None,
) -> tuple[_CodexRefusal | None, bool]:
    """Bind a split refusal to its descriptor and expose identity conflicts."""

    if refusal is None:
        return None, False
    refusal_tool, field, reason = refusal
    if (
        refusal_tool is not None
        and creation_tool_name is not None
        and refusal_tool != creation_tool_name
    ):
        return None, True
    if refusal_tool is None and creation_tool_name is not None:
        return (creation_tool_name, field, reason), False
    return refusal, False


def _codex_refusal_sort_key(
    refusal: _CodexRefusal | None,
) -> tuple[bool, tuple[str, str, str]]:
    """Prefer classifiable refusals, then make diagnostic choice stable."""

    normalized_refusal = (
        tuple(part or "" for part in refusal)
        if refusal is not None
        else ("", "", "")
    )
    return refusal is None, normalized_refusal


def reconcile_codex_evidence_fragments(
    fragments: list[CodexEvidenceFragment],
    evidence: LogEvidenceAccumulator,
) -> bool:
    """Reduce carrier fragments to deterministic logical-call evidence.

    Returns whether semantic evidence schema drift or an integrity conflict was
    observed. A call can donate at most once and can contribute at most one
    non-donating diagnostic, independent of fragment order.
    """

    fragments_by_call_id: dict[str, list[CodexEvidenceFragment]] = {}
    evidence_parse_incomplete = False
    for fragment in fragments:
        if fragment.role != "output":
            evidence_parse_incomplete = (
                evidence_parse_incomplete or fragment.has_schema_drift
            )
        if fragment.call_id is None:
            if fragment.counts_as_evidence_skip:
                refusal = (
                    fragment.outcome.refusal
                    if fragment.outcome is not None
                    else None
                )
                evidence.record_classified_skip(refusal)
            continue
        fragments_by_call_id.setdefault(fragment.call_id, []).append(fragment)

    for call_fragments in fragments_by_call_id.values():
        descriptor_fragments = [
            fragment
            for fragment in call_fragments
            if fragment.role == "descriptor"
        ]
        output_fragments = [
            fragment for fragment in call_fragments if fragment.role == "output"
        ]
        terminal_fragments = [
            fragment
            for fragment in call_fragments
            if fragment.role == "terminal"
        ]
        accepted_descriptor_fragments = [
            fragment
            for fragment in descriptor_fragments
            if fragment.creation_tool_verdict == "accepted"
        ]
        rejected_descriptor_fragments = [
            fragment
            for fragment in descriptor_fragments
            if fragment.creation_tool_verdict == "rejected"
        ]
        accepted_terminal_fragments = [
            fragment
            for fragment in terminal_fragments
            if fragment.creation_tool_verdict == "accepted"
        ]
        accepted_creation_tool_fragments = [
            *accepted_descriptor_fragments,
            *accepted_terminal_fragments,
        ]
        accepted_creation_tool_names = {
            fragment.creation_tool_name
            for fragment in accepted_creation_tool_fragments
            if fragment.creation_tool_name is not None
        }
        outcomes_with_creation_tool: list[
            tuple[CodexEvidenceOutcome, str | None]
        ] = []
        created_event_ids: list[str] = []
        skip_refusals: list[_CodexRefusal | None] = []
        has_refusal_tool_conflict = False

        if accepted_descriptor_fragments:
            bound_creation_tool_name = min(
                (
                    fragment.creation_tool_name
                    for fragment in accepted_descriptor_fragments
                    if fragment.creation_tool_name is not None
                ),
                default=None,
            )
            for output_fragment in output_fragments:
                evidence_parse_incomplete = (
                    evidence_parse_incomplete
                    or output_fragment.has_schema_drift
                )
                outcome = output_fragment.outcome or _MALFORMED_CODEX_OUTCOME
                outcomes_with_creation_tool.append(
                    (outcome, bound_creation_tool_name)
                )
        if rejected_descriptor_fragments and output_fragments:
            skip_refusals.append(None)

        for terminal_fragment in terminal_fragments:
            if terminal_fragment.creation_tool_verdict == "accepted":
                outcomes_with_creation_tool.append(
                    (
                        terminal_fragment.outcome or _MALFORMED_CODEX_OUTCOME,
                        terminal_fragment.creation_tool_name,
                    )
                )
            elif (
                terminal_fragment.counts_as_evidence_skip
                or terminal_fragment.creation_tool_verdict == "rejected"
            ):
                skip_refusals.append(None)

        for outcome, creation_tool_name in outcomes_with_creation_tool:
            if (
                outcome.outcome_classification == "success"
                and outcome.event_id is not None
            ):
                created_event_ids.append(outcome.event_id)
                continue
            refusal, has_tool_identity_conflict = _bind_codex_refusal_to_tool(
                outcome.refusal,
                creation_tool_name,
            )
            has_refusal_tool_conflict = (
                has_refusal_tool_conflict or has_tool_identity_conflict
            )
            skip_refusals.append(refusal)

        distinct_created_event_ids = set(created_event_ids)
        has_relevant_schema_drift = any(
            fragment.has_schema_drift
            for fragment in [*descriptor_fragments, *terminal_fragments]
        ) or (
            bool(accepted_descriptor_fragments)
            and any(fragment.has_schema_drift for fragment in output_fragments)
        )
        has_integrity_conflict = (
            len(distinct_created_event_ids) > 1
            or len(accepted_creation_tool_names) > 1
            or has_refusal_tool_conflict
            or has_relevant_schema_drift
        )
        if has_integrity_conflict:
            # Never choose between distinct successful ids, accepted tool
            # identities, mismatched refusal identities, or malformed carriers.
            evidence_parse_incomplete = True
            created_event_ids = []
            skip_refusals = [None]

        if created_event_ids:
            evidence.add_event_id(created_event_ids[0])
        if skip_refusals:
            # Prefer a refusal agentacct can actually classify, then choose
            # deterministically so carrier reversal cannot change the bounded
            # diagnostic row. Generic client/transport failures remain skips
            # but must not hide a real server refusal for the same logical call.
            selected_refusal = min(
                skip_refusals,
                key=_codex_refusal_sort_key,
            )
            evidence.record_classified_skip(selected_refusal)

    return evidence_parse_incomplete


def codex_model_from_record(record: Mapping[str, Any]) -> str | None:
    """Read model identity only from exact Codex-owned record paths."""

    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None

    def model_name_from_mapping(value: Any) -> str | None:
        if not isinstance(value, Mapping):
            return None
        return validated_codex_identifier(
            value.get("model"),
            max_length=_CODEX_MODEL_TEXT_MAX,
        )

    outer_type = record.get("type")
    payload_type = payload.get("type")
    info = payload.get("info")
    if outer_type == "session_meta":
        return model_name_from_mapping(payload)
    if outer_type == "turn_context":
        return model_name_from_mapping(payload)
    if outer_type == "world_state":
        return model_name_from_mapping(payload.get("state"))
    if outer_type == "event_msg" and payload_type == "thread_settings_applied":
        return model_name_from_mapping(payload.get("thread_settings"))
    if (
        outer_type == "event_msg"
        and payload_type in (None, "token_count")
        and isinstance(info, Mapping)
        and isinstance(info.get("total_token_usage"), Mapping)
    ):
        # Legacy/compatibility carrier used by persisted rollouts and fixtures.
        return model_name_from_mapping(payload)
    return None


__all__ = [
    "CodexEvidenceFragment",
    "CodexEvidenceOutcome",
    "codex_function_action_name",
    "codex_mcp_action_name",
    "codex_model_from_record",
    "codex_record_may_contain_evidence",
    "decode_codex_evidence_fragment",
    "reconcile_codex_evidence_fragments",
    "validated_codex_identifier",
]
