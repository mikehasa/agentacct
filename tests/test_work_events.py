from __future__ import annotations

from agentacct.work_events import WORK_EVENT_SCHEMA_VERSION, WorkEvent


def test_mcp_section_normalizes_to_transport_neutral_work_event() -> None:
    event = {
        "event_id": "evt_section",
        "created_at": 123.0,
        "source": "codex",
        "event_type": "section_completed",
        "run_id": "run-1",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "section_id": "phase-1",
            "section_title": "Build evidence core",
            "summary": "Implemented the immutable evidence layer.",
            "client": "codex",
            "client_session_id": "session-1",
            "files": ["src/agentacct/evidence.py"],
        },
    }

    normalized = WorkEvent.from_v1_event(event, transport="mcp")

    assert normalized.event_kind == "section"
    assert normalized.status == "completed"
    assert normalized.transport == "mcp"
    assert normalized.client_session_id == "session-1"
    assert normalized.files == ("src/agentacct/evidence.py",)
    assert normalized.to_dict()["schema_version"] == WORK_EVENT_SCHEMA_VERSION


def test_work_event_drops_content_and_unsafe_paths_from_v1_metadata() -> None:
    event = {
        "event_id": "evt_private",
        "created_at": 123.0,
        "source": "claude-code",
        "event_type": "section_started",
        "metadata": {
            "prompt": "PROMPT_CANARY",
            "response": "RESPONSE_CANARY",
            "thought": "THOUGHT_CANARY",
            "tool_input": {"secret": "SECRET_CANARY"},
            "tool_result": "RESULT_CANARY",
            "files": ["safe/path.py", "../private.txt", "/absolute/private.txt", "~/.ssh/id_rsa"],
        },
    }

    normalized = WorkEvent.from_v1_event(event, transport="mcp")
    encoded = str(normalized.to_dict())

    assert normalized.files == ("safe/path.py",)
    for canary in ("PROMPT_CANARY", "RESPONSE_CANARY", "THOUGHT_CANARY", "SECRET_CANARY", "RESULT_CANARY"):
        assert canary not in encoded


def test_work_event_round_trips_through_v1_compatibility_shape() -> None:
    original = WorkEvent(
        event_kind="task",
        source="paperclip",
        transport="orchestrator",
        status="started",
        occurred_at=456.0,
        source_event_id="task-run-1",
        run_id="run-1",
        work_id="issue-1",
        objective="Implement the evidence product.",
        original_event_type="task_started",
    )

    v1 = original.to_v1_event()
    restored = WorkEvent.from_v1_event(
        {**v1, "event_id": "evt_server_receipt", "created_at": 999.0},
        transport="orchestrator",
    )

    assert v1["created_at"] == 456.0
    assert v1["metadata"]["client_event_timestamp"] == 456.0
    assert v1["metadata"]["source_event_id"] == "task-run-1"
    assert v1["metadata"]["idempotency_key"].startswith("work-event:orchestrator:")
    assert restored.event_kind == "task"
    assert restored.status == "started"
    assert restored.occurred_at == 456.0
    assert restored.source_event_id == "task-run-1"
    assert restored.work_id == "issue-1"
    assert restored.objective == "Implement the evidence product."


def test_work_event_idempotency_is_stable_and_transport_scoped() -> None:
    shared = {
        "event_kind": "task",
        "source": "paperclip",
        "status": "started",
        "occurred_at": 456.0,
        "source_event_id": "task-run-1",
        "original_event_type": "task_started",
    }

    first = WorkEvent(transport="http", **shared).to_v1_event()
    retry = WorkEvent(transport="http", **shared).to_v1_event()
    other_transport = WorkEvent(transport="cli", **shared).to_v1_event()

    assert first["metadata"]["idempotency_key"] == retry["metadata"]["idempotency_key"]
    assert first["metadata"]["idempotency_key"] != other_transport["metadata"]["idempotency_key"]
    assert "idempotency_key" not in WorkEvent(
        event_kind="note",
        source="codex",
        transport="http",
    ).to_v1_event()["metadata"]


def test_invalid_transport_cannot_gain_provenance() -> None:
    normalized = WorkEvent.from_v1_event(
        {"source": "custom", "event_type": "task_started", "metadata": {}},
        transport="provider_billed",
    )

    assert normalized.transport == "unknown"
