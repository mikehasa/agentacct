from __future__ import annotations

import json

import pytest

from agentacct.codex_rollout_adapter import (
    CodexEvidenceFragment,
    CodexEvidenceOutcome,
    _CODEX_RESULT_TEXT_MAX,
    _CodexOutcomeCarrier,
    _decode_codex_call_outcome,
    codex_mcp_action_name,
    codex_model_from_record,
    codex_record_may_contain_evidence,
    decode_codex_evidence_fragment,
    reconcile_codex_evidence_fragments,
    validated_codex_identifier,
)
from agentacct.log_evidence import LogEvidenceAccumulator


TOOL = "agentacct_record_section"
EVENT_ID = "evt_abc123abc123"


def _created_event_text(event_id: str = EVENT_ID) -> str:
    return json.dumps({"event": {"event_id": event_id}})


def _call_tool_result(text: str, *, is_error: bool | None = None) -> dict:
    result = {"content": [{"type": "text", "text": text}]}
    if is_error is not None:
        result["isError"] = is_error
    return result


@pytest.mark.parametrize(
    (
        "carrier",
        "value",
        "expected_outcome_classification",
        "expected_event_id",
    ),
    [
        ("result", _call_tool_result(_created_event_text()), "success", EVENT_ID),
        (
            "result",
            _call_tool_result(_created_event_text(), is_error=False),
            "success",
            EVENT_ID,
        ),
        (
            "result",
            {"Ok": _call_tool_result(_created_event_text())},
            "success",
            EVENT_ID,
        ),
        ("result", _call_tool_result("tool failed", is_error=True), "failure", None),
        (
            "result",
            {"Ok": _call_tool_result("tool failed", is_error=True)},
            "failure",
            None,
        ),
        ("result", {"Err": "tool failed"}, "failure", None),
        ("error", {"message": "tool failed", "code": -32000}, "failure", None),
        (
            "function_output",
            "Wall time: 0.1 seconds\nOutput:\n"
            + json.dumps([{"type": "text", "text": _created_event_text()}]),
            "success",
            EVENT_ID,
        ),
        (
            "function_output",
            json.dumps([{"type": "text", "text": _created_event_text()}]),
            "success",
            EVENT_ID,
        ),
        ("function_output", _created_event_text(), "success", EVENT_ID),
    ],
)
def test_decode_codex_call_outcome_accepts_supported_carriers(
    carrier: _CodexOutcomeCarrier,
    value: object,
    expected_outcome_classification: str,
    expected_event_id: str | None,
) -> None:
    outcome = _decode_codex_call_outcome(
        value,
        carrier=carrier,
        creation_tool_name=TOOL,
    )

    assert outcome.outcome_classification == expected_outcome_classification
    assert outcome.event_id == expected_event_id


@pytest.mark.parametrize("extra_key", ["message", "error"])
def test_function_output_creation_payload_ignores_non_identity_fields(
    extra_key: str,
) -> None:
    payload = {
        "event": {"event_id": EVENT_ID},
        extra_key: "informational",
    }

    outcome = _decode_codex_call_outcome(
        json.dumps(payload),
        carrier="function_output",
        creation_tool_name=TOOL,
    )

    assert outcome == CodexEvidenceOutcome(
        outcome_classification="success",
        event_id=EVENT_ID,
    )


@pytest.mark.parametrize(
    "transport_fields",
    [
        {"Ok": _call_tool_result(_created_event_text())},
        {"Err": "failed"},
        {"content": []},
        {"isError": False},
    ],
)
def test_function_output_rejects_creation_payload_with_transport_discriminators(
    transport_fields: dict,
) -> None:
    payload = {
        "event": {"event_id": EVENT_ID},
        **transport_fields,
    }

    outcome = _decode_codex_call_outcome(
        json.dumps(payload),
        carrier="function_output",
        creation_tool_name=TOOL,
    )

    assert outcome == CodexEvidenceOutcome(outcome_classification="malformed")


@pytest.mark.parametrize(
    "value",
    [
        {"Ok": _call_tool_result(_created_event_text()), "Err": "failed"},
        {"Ok": {**_call_tool_result(_created_event_text()), "Err": "failed"}},
        {
            "Ok": {
                **_call_tool_result(_created_event_text()),
                "Ok": _call_tool_result(_created_event_text()),
            }
        },
        {"Ok": _call_tool_result(_created_event_text()), "content": []},
        {"Err": "failed", "content": []},
        {
            **_call_tool_result(_created_event_text()),
            "error": {"message": "failed"},
        },
        {
            "Ok": {
                **_call_tool_result(_created_event_text()),
                "error": {"message": "failed"},
            }
        },
        {
            "Ok": _call_tool_result(_created_event_text()),
            "error": {"message": "failed"},
        },
        {**_call_tool_result(_created_event_text()), "message": "failed"},
        {**_call_tool_result(_created_event_text()), "isError": "false"},
        {"Ok": {**_call_tool_result(_created_event_text()), "isError": "true"}},
        {"message": "failed", "isError": False},
        {"message": "failed"},
        {"Ok": {}},
        {"Err": ""},
        {"message": ""},
        {},
        {"content": "not-a-list"},
        {"content": []},
        {
            "content": [
                {"type": "text", "text": _created_event_text()},
                {"type": "text", "text": _created_event_text()},
            ]
        },
        {
            "content": [
                {"type": "text", "text": _created_event_text()},
                {"type": "image", "data": "private"},
            ]
        },
    ],
)
def test_decode_codex_call_outcome_rejects_malformed_or_ambiguous_shapes(
    value: object,
) -> None:
    outcome = _decode_codex_call_outcome(
        value,
        carrier="result",
        creation_tool_name=TOOL,
    )

    assert outcome == CodexEvidenceOutcome(outcome_classification="malformed")


@pytest.mark.parametrize(
    ("carrier", "value"),
    [
        (
            "result",
            _call_tool_result(json.dumps({"unexpected_response": True})),
        ),
        (
            "function_output",
            json.dumps({"unexpected_response": True}),
        ),
    ],
)
def test_success_without_created_event_id_is_schema_drift(
    carrier: _CodexOutcomeCarrier,
    value: object,
) -> None:
    outcome = _decode_codex_call_outcome(
        value,
        carrier=carrier,
        creation_tool_name=TOOL,
    )
    accumulator, drifted = _reconcile_same_call([outcome])

    assert accumulator.evidenced_event_ids == []
    assert accumulator.evidenced_outputs_skipped == 1
    assert drifted is True


@pytest.mark.parametrize(
    (
        "call_tool_result",
        "expected_outcome",
        "expected_refused_attempts",
        "expected_schema_drift",
    ),
    [
        (
            _call_tool_result(
                "Mcp error: -32602: summary must be <= 1200 characters",
                is_error=True,
            ),
            CodexEvidenceOutcome(
                outcome_classification="failure",
                refusal=(TOOL, "summary", "narrative_over_limit"),
            ),
            [
                {
                    "tool": TOOL,
                    "field": "summary",
                    "reason_code": "narrative_over_limit",
                    "count": 1,
                }
            ],
            False,
        ),
        (
            _call_tool_result(_created_event_text()),
            CodexEvidenceOutcome(outcome_classification="malformed"),
            [],
            True,
        ),
    ],
)
def test_failed_paginated_result_only_accepts_only_explicit_tool_failure(
    call_tool_result: dict[str, object],
    expected_outcome: CodexEvidenceOutcome,
    expected_refused_attempts: list[dict[str, object]],
    expected_schema_drift: bool,
) -> None:
    paginated_completion_record = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "McpToolCall",
                "id": "call-1",
                "server": "agentacct",
                "tool": TOOL,
                "status": "failed",
                "result": call_tool_result,
            },
        },
    }

    terminal_fragment = decode_codex_evidence_fragment(
        paginated_completion_record
    )

    assert terminal_fragment is not None
    assert terminal_fragment.outcome == expected_outcome

    evidence = LogEvidenceAccumulator()
    has_schema_drift = reconcile_codex_evidence_fragments(
        [terminal_fragment], evidence
    )

    assert evidence.evidenced_event_ids == []
    assert evidence.evidenced_outputs_skipped == 1
    assert evidence.refused_recording_attempts() == expected_refused_attempts
    assert has_schema_drift is expected_schema_drift


def _reconcile_same_call(
    outcomes: list[CodexEvidenceOutcome],
) -> tuple[LogEvidenceAccumulator, bool]:
    accumulator = LogEvidenceAccumulator()
    fragments = [
        CodexEvidenceFragment(
            call_id="call-1",
            role="terminal",
            creation_tool_verdict="accepted",
            creation_tool_name=TOOL,
            outcome=outcome,
            counts_as_evidence_skip=outcome.event_id is None,
            has_schema_drift=outcome.outcome_classification == "malformed",
        )
        for outcome in outcomes
    ]
    drifted = reconcile_codex_evidence_fragments(fragments, accumulator)
    return accumulator, drifted


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    (
        "outcomes",
        "expected_ids",
        "expected_skips",
        "expected_drift",
    ),
    [
        (
            [
                CodexEvidenceOutcome(
                    outcome_classification="success", event_id=EVENT_ID
                ),
                CodexEvidenceOutcome(
                    outcome_classification="success", event_id=EVENT_ID
                ),
            ],
            [EVENT_ID],
            0,
            False,
        ),
        (
            [
                CodexEvidenceOutcome(
                    outcome_classification="success", event_id=EVENT_ID
                ),
                CodexEvidenceOutcome(
                    outcome_classification="success",
                    event_id="evt_def456def456",
                ),
            ],
            [],
            1,
            True,
        ),
        (
            [
                CodexEvidenceOutcome(
                    outcome_classification="success", event_id=EVENT_ID
                ),
                CodexEvidenceOutcome(outcome_classification="failure"),
            ],
            [EVENT_ID],
            1,
            False,
        ),
        (
            [
                CodexEvidenceOutcome(
                    outcome_classification="success", event_id=EVENT_ID
                ),
                CodexEvidenceOutcome(outcome_classification="unknown"),
            ],
            [EVENT_ID],
            1,
            False,
        ),
        (
            [
                CodexEvidenceOutcome(outcome_classification="failure"),
                CodexEvidenceOutcome(
                    outcome_classification="failure",
                    refusal=(TOOL, "summary", "narrative_over_limit"),
                ),
            ],
            [],
            1,
            False,
        ),
    ],
)
def test_same_call_outcomes_reconcile_order_independently(
    outcomes: list[CodexEvidenceOutcome],
    expected_ids: list[str],
    expected_skips: int,
    expected_drift: bool,
    reverse: bool,
) -> None:
    ordered = list(reversed(outcomes)) if reverse else outcomes

    accumulator, drifted = _reconcile_same_call(ordered)

    assert accumulator.evidenced_event_ids == expected_ids
    assert accumulator.evidenced_outputs_skipped == expected_skips
    assert drifted is expected_drift


def test_distinct_call_ids_do_not_cross_conflict() -> None:
    accumulator = LogEvidenceAccumulator()
    fragments = [
        CodexEvidenceFragment(
            call_id="success-call",
            role="terminal",
            creation_tool_verdict="accepted",
            creation_tool_name=TOOL,
            outcome=CodexEvidenceOutcome(
                outcome_classification="success",
                event_id=EVENT_ID,
            ),
        ),
        CodexEvidenceFragment(
            call_id="failed-call",
            role="terminal",
            creation_tool_verdict="accepted",
            creation_tool_name=TOOL,
            outcome=CodexEvidenceOutcome(outcome_classification="failure"),
            counts_as_evidence_skip=True,
        ),
    ]

    drifted = reconcile_codex_evidence_fragments(fragments, accumulator)

    assert accumulator.evidenced_event_ids == [EVENT_ID]
    assert accumulator.evidenced_outputs_skipped == 1
    assert drifted is False


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    (
        "output_outcome",
        "output_has_schema_drift",
        "terminal_outcome",
        "expected_ids",
    ),
    [
        (
            CodexEvidenceOutcome(
                outcome_classification="success",
                event_id="evt_badbadbadbad",
            ),
            False,
            CodexEvidenceOutcome(outcome_classification="failure"),
            [],
        ),
        (
            CodexEvidenceOutcome(outcome_classification="malformed"),
            True,
            CodexEvidenceOutcome(
                outcome_classification="success",
                event_id=EVENT_ID,
            ),
            [EVENT_ID],
        ),
    ],
)
def test_rejected_descriptor_cannot_lend_or_override_terminal_identity(
    output_outcome: CodexEvidenceOutcome,
    output_has_schema_drift: bool,
    terminal_outcome: CodexEvidenceOutcome,
    expected_ids: list[str],
    reverse: bool,
) -> None:
    fragments = [
        CodexEvidenceFragment(
            call_id="call-1",
            role="descriptor",
            creation_tool_verdict="rejected",
            creation_tool_name=TOOL,
        ),
        CodexEvidenceFragment(
            call_id="call-1",
            role="output",
            outcome=output_outcome,
            has_schema_drift=output_has_schema_drift,
        ),
        CodexEvidenceFragment(
            call_id="call-1",
            role="terminal",
            creation_tool_verdict="accepted",
            creation_tool_name=TOOL,
            outcome=terminal_outcome,
            counts_as_evidence_skip=terminal_outcome.event_id is None,
        ),
    ]
    if reverse:
        fragments.reverse()
    accumulator = LogEvidenceAccumulator()

    drifted = reconcile_codex_evidence_fragments(fragments, accumulator)

    assert accumulator.evidenced_event_ids == expected_ids
    assert accumulator.evidenced_outputs_skipped == 1
    assert drifted is False


@pytest.mark.parametrize("reverse", [False, True])
def test_orphan_output_cannot_borrow_terminal_identity(reverse: bool) -> None:
    fragments = [
        CodexEvidenceFragment(
            call_id="call-1",
            role="terminal",
            creation_tool_verdict="accepted",
            creation_tool_name=TOOL,
            outcome=CodexEvidenceOutcome(outcome_classification="failure"),
            counts_as_evidence_skip=True,
        ),
        CodexEvidenceFragment(
            call_id="call-1",
            role="output",
            outcome=CodexEvidenceOutcome(
                outcome_classification="success",
                event_id="evt_badbadbadbad",
            ),
        ),
    ]
    if reverse:
        fragments.reverse()
    accumulator = LogEvidenceAccumulator()

    drifted = reconcile_codex_evidence_fragments(fragments, accumulator)

    assert accumulator.evidenced_event_ids == []
    assert accumulator.evidenced_outputs_skipped == 1
    assert drifted is False


def test_wrong_outer_carrier_is_visible_only_for_relevant_creation_calls() -> None:
    creation_record = {
        "type": "event_msg",
        "payload": {
            "type": "function_call",
            "name": TOOL,
            "call_id": "call-1",
        },
    }
    unrelated_record = {
        "type": "event_msg",
        "payload": {
            "type": "function_call",
            "name": "read_file",
            "call_id": "call-2",
        },
    }

    fragment = decode_codex_evidence_fragment(creation_record)

    assert fragment is not None
    assert fragment.has_schema_drift is True
    assert decode_codex_evidence_fragment(unrelated_record) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_wrong_outer_descriptor_cannot_donate_through_a_valid_output(
    reverse: bool,
) -> None:
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "function_call",
                "name": TOOL,
                "call_id": "call-1",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": _created_event_text(),
            },
        },
    ]
    if reverse:
        records.reverse()
    fragments = [
        fragment
        for record in records
        if (fragment := decode_codex_evidence_fragment(record)) is not None
    ]
    accumulator = LogEvidenceAccumulator()

    drifted = reconcile_codex_evidence_fragments(fragments, accumulator)

    assert accumulator.evidenced_event_ids == []
    assert accumulator.evidenced_outputs_skipped == 1
    assert drifted is True


@pytest.mark.parametrize(
    "call_id",
    [
        "",
        "   ",
        " call-1",
        "call-1 ",
        "call\n1",
        "call\u200b1",
        "x" * 241,
    ],
)
def test_codex_identifier_rejects_ambiguous_or_unbounded_text(
    call_id: str,
) -> None:
    assert validated_codex_identifier(call_id) is None


@pytest.mark.parametrize(
    ("server", "tool"),
    [
        ("agentacct\u200b", TOOL),
        ("agentacct", f"{TOOL}\nprivate"),
        ("s" * 121, TOOL),
        ("agentacct", "t" * 121),
    ],
)
def test_codex_mcp_action_name_rejects_unsafe_or_oversized_components(
    server: str,
    tool: str,
) -> None:
    assert codex_mcp_action_name(server, tool) is None


def test_codex_mcp_action_name_canonicalizes_a_valid_server() -> None:
    assert codex_mcp_action_name("mcp__agent-acct", TOOL) == (
        f"mcp__agent_acct__{TOOL}"
    )


@pytest.mark.parametrize(
    ("carrier", "value"),
    [
        ("function_output", "x" * (_CODEX_RESULT_TEXT_MAX + 1)),
        (
            "result",
            _call_tool_result("x" * (_CODEX_RESULT_TEXT_MAX + 1)),
        ),
    ],
    ids=["function-output", "call-tool-result"],
)
def test_codex_success_text_is_bounded_before_reparsing(
    carrier: _CodexOutcomeCarrier,
    value: object,
) -> None:
    outcome = _decode_codex_call_outcome(
        value,
        carrier=carrier,
        creation_tool_name=TOOL,
    )

    assert outcome == CodexEvidenceOutcome(outcome_classification="malformed")


@pytest.mark.parametrize(
    ("carrier", "value"),
    [
        ("function_output", "[" * 10_000 + "0" + "]" * 10_000),
        (
            "result",
            _call_tool_result("[" * 10_000 + "0" + "]" * 10_000),
        ),
    ],
    ids=["function-output", "call-tool-result"],
)
def test_codex_deeply_nested_result_text_is_malformed(
    carrier: _CodexOutcomeCarrier,
    value: object,
) -> None:
    outcome = _decode_codex_call_outcome(
        value,
        carrier=carrier,
        creation_tool_name=TOOL,
    )

    assert outcome == CodexEvidenceOutcome(outcome_classification="malformed")


@pytest.mark.parametrize(
    ("carrier", "value"),
    [
        (
            "result",
            _call_tool_result(
                "x" * (_CODEX_RESULT_TEXT_MAX + 1),
                is_error=True,
            ),
        ),
        ("error", {"message": "x" * (_CODEX_RESULT_TEXT_MAX + 1)}),
    ],
    ids=["call-tool-result", "error"],
)
def test_codex_failure_text_is_not_classified_beyond_bound(
    carrier: _CodexOutcomeCarrier,
    value: object,
) -> None:
    outcome = _decode_codex_call_outcome(
        value,
        carrier=carrier,
        creation_tool_name=TOOL,
    )

    assert outcome == CodexEvidenceOutcome(outcome_classification="failure")


def test_codex_model_selection_uses_only_authoritative_record_paths() -> None:
    accepted = [
        {"type": "session_meta", "payload": {"model": "gpt-meta"}},
        {"type": "turn_context", "payload": {"model": "gpt-turn"}},
        {
            "type": "world_state",
            "payload": {"state": {"model": "gpt-world"}},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {"model": "gpt-settings"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "model": "gpt-token",
                "info": {"total_token_usage": {}},
            },
        },
    ]
    assert [codex_model_from_record(record) for record in accepted] == [
        "gpt-meta",
        "gpt-turn",
        "gpt-world",
        "gpt-settings",
        "gpt-token",
    ]

    rejected = [
        {
            "type": "session_meta",
            "payload": {"base_instructions": {"model": "poison"}},
        },
        {
            "type": "world_state",
            "payload": {"state": {"personality": {"model": "poison"}}},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"arguments": {"model": "poison"}},
            },
        },
        {"type": "session_meta", "payload": {"model": "x" * 121}},
        {"type": "session_meta", "payload": {"model": "gpt\u200bpoison"}},
        {"type": "session_meta", "payload": "not-an-object"},
    ]
    assert all(codex_model_from_record(record) is None for record in rejected)


def test_post_cap_relevance_ignores_known_read_tools() -> None:
    legacy_read = {
        "payload": {
            "type": "mcp_tool_call_end",
            "invocation": {
                "server": "agentacct",
                "tool": "agentacct_list_events",
            },
        }
    }
    paginated_read = {
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "McpToolCall",
                "id": "call-1",
                "server": "agentacct",
                "tool": "agentacct_list_events",
            },
        }
    }
    malformed_terminal = {
        "payload": {"type": "mcp_tool_call_end"},
    }

    assert codex_record_may_contain_evidence(legacy_read) is False
    assert codex_record_may_contain_evidence(paginated_read) is False
    assert codex_record_may_contain_evidence(malformed_terminal) is True
