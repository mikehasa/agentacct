"""The TUI Receipts screen + Receipt detail (Textual, headless via run_test)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import DataTable, Static

from agentacct.receipt import build_receipt
from agentacct.service import SentinelService
from agentacct.tui import (
    AgentAcctTUI,
    ReceiptDetailScreen,
    ReceiptsScreen,
    _render_receipt_markup,
)

NS = "sha256:receipt-tui-ns"


def _run(coro) -> None:
    asyncio.run(coro)


def _seed(store: Path) -> None:
    service = SentinelService(store)
    service.record_event(
        {
            "event_id": "evt_usage_s1",
            "created_at": 100.0,
            "source": "claude-code-local-session-import",
            "event_type": "model_usage",
            "run_id": None,
            "provider": "claude-code",
            "model": "claude-opus-4-8",
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 25,
            "estimated_cost_usd": 0.5,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "cost_basis": "pricing_table",
            "metadata": {
                "usage_source": "local_client_session_store",
                "usage_provenance": "agent_sentinel_local_usage_import",
                "client": "claude-code",
                "client_session_id": "s1",
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "project_dir": "/tmp/project",
                "started_at": 100.0,
                "updated_at": 100.0,
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "source_namespace_fingerprint": NS,
            },
        },
        trusted_usage_import=True,
    )
    service.record_event(
        {
            "event_id": "evt_section_s1",
            "created_at": 100.0,
            "source": "claude-code",
            "event_type": "section_completed",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": "s1",
                "client_context_keys_authored": ["client_session_id"],
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": "sec-1",
                "section_status": "completed",
                "section_title": "Add rate limit to login",
                "kind": "implementation",
            },
        }
    )


def test_render_receipt_markup_contains_axes_and_dimensions() -> None:
    task = {
        "task_id": "task_x",
        "primary_root": {"client": "claude-code", "client_session_id": "s1"},
        "root_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "session_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "sessions": [
            {"client": "claude-code", "client_session_id": "s1", "project": "acme", "identity_scope_state": "explicit", "last_activity_at": 100.0, "usage": {}}
        ],
        "session_count": 1,
        "supporting_count": 0,
        "child_count": 0,
        "internal_count": 0,
        "last_activity_at": 100.0,
        "work_items": [{"work_id": "w", "latest_status": "completed", "updated_at": 100.0, "objective": "add rate limit"}],
        "work_associations": [],
        "usage": {"rows": 1, "estimated_cost_usd": 0.5, "cost_complete": True, "cost_basis": "pricing_table", "total_tokens": 1000},
        "models": ["claude-opus-4-8"],
        "actions": {"tool_category_counts": {}, "tool_category_total": 0, "touched_files": [], "touched_file_count": 0},
    }
    markup = _render_receipt_markup(build_receipt(task, public_task_id="task_x", title="Add rate limit"))
    for marker in ("Decision status", "Evidence coverage", "[b]Task[/]", "[b]Cost[/]", "Provenance"):
        assert marker in markup
    # The evidence line is a coverage RATIO, not a bare axis phrase. Assert the
    # actual content (one completed no-check step reads "1 unchecked") so a
    # regression that empties the headline cannot hide behind the orthogonality
    # note, which also contains the phrase "Evidence coverage".
    assert "unchecked" in markup


def _handoff_task(items: list[dict]) -> dict:
    return {
        "task_id": "task_x",
        "primary_root": {"client": "claude-code", "client_session_id": "s1"},
        "root_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "session_keys": [{"client": "claude-code", "client_session_id": "s1"}],
        "sessions": [
            {"client": "claude-code", "client_session_id": "s1", "project": "acme", "identity_scope_state": "explicit", "last_activity_at": 100.0, "usage": {}}
        ],
        "session_count": 1,
        "supporting_count": 0,
        "child_count": 0,
        "internal_count": 0,
        "last_activity_at": 100.0,
        "work_items": items,
        "work_associations": [],
        "usage": {"rows": 1, "estimated_cost_usd": 0.5, "cost_complete": True, "cost_basis": "pricing_table", "total_tokens": 1000},
        "models": ["claude-opus-4-8"],
        "actions": {"tool_category_counts": {}, "tool_category_total": 0, "touched_files": [], "touched_file_count": 0},
    }


def test_render_receipt_markup_shows_handoff_marker_beside_a_hard_problem() -> None:
    # A blocked headline must still surface the parallel handoff marker.
    task = _handoff_task(
        [
            {"work_id": "a", "latest_status": "blocked", "updated_at": 100.0, "objective": "x"},
            {"work_id": "b", "latest_status": "handed_off", "updated_at": 200.0, "objective": "y"},
        ]
    )
    markup = _render_receipt_markup(build_receipt(task, public_task_id="task_x", title="t"))
    assert "Lifecycle" in markup
    assert "Handed off" in markup


def test_render_receipt_markup_no_double_marker_for_pure_handoff() -> None:
    # Invariant #4: when the decision word IS "handed_off", the parallel marker
    # must be suppressed — the handoff must never be stated twice. The data flag
    # is on here (handoff_current True); only the decision-key guard suppresses
    # the marker, so this pins that guard (a resumed-task test cannot — its flag
    # is already off).
    task = _handoff_task(
        [
            {"work_id": "a", "latest_status": "completed", "updated_at": 100.0, "objective": "x"},
            {"work_id": "b", "latest_status": "handed_off", "updated_at": 200.0, "objective": "y"},
        ]
    )
    receipt = build_receipt(task, public_task_id="task_x", title="t")
    assert receipt["axes"]["decision_status"]["key"] == "handed_off"
    assert receipt["axes"]["handoff"]["handed_off"] is True  # data flag on...
    markup = _render_receipt_markup(receipt)
    assert "Lifecycle" not in markup  # ...but the marker line is suppressed


def test_render_receipt_markup_hides_marker_when_task_resumed() -> None:
    # A later open step postdates the handoff: no stale marker is shown.
    task = _handoff_task(
        [
            {"work_id": "a", "latest_status": "handed_off", "updated_at": 100.0, "objective": "x"},
            {"work_id": "b", "latest_status": "started", "updated_at": 200.0, "objective": "y"},
        ]
    )
    markup = _render_receipt_markup(build_receipt(task, public_task_id="task_x", title="t"))
    assert "Lifecycle" not in markup


def test_receipts_screen_lists_a_task_and_opens_its_detail(tmp_path: Path) -> None:
    _seed(tmp_path)

    async def scenario() -> None:
        app = AgentAcctTUI(store_dir=tmp_path, refresh_seconds=3600)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")  # open the Receipts screen
            await pilot.pause()
            assert isinstance(app.screen, ReceiptsScreen)
            table = app.screen.query_one("#receipts", DataTable)
            # The list is built on a thread worker; give it real time to land.
            for _ in range(60):
                if table.row_count >= 1:
                    break
                await asyncio.sleep(0.05)
                await pilot.pause()
            assert table.row_count >= 1
            # Open the first row's full Receipt.
            table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ReceiptDetailScreen)
            assert app.screen.query_one("#receipt-body", Static) is not None
            assert "Decision status" in app.screen._body_text
            assert "Evidence coverage" in app.screen._body_text
            # Assert the ratio content, not just the axis phrase (which the
            # orthogonality note also contains).
            assert "unchecked" in app.screen._body_text

    _run(scenario())
