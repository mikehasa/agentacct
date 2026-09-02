"""The TUI Work Receipt detail builder (``_build_receipt_markup``).

The receipt detail is now a laid-out object inside the Work pane rather than a
standalone screen; these tests exercise the shared markup builder against a real
``build_receipt`` output so the terminal detail can never disagree with the CLI /
app vocabulary, and so a stray ``[/]`` in any field can never crash the view.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from agentacct.api import _task_title, build_store_task_projection
from agentacct.receipt import (
    build_receipt,
    latest_store_activity,
    session_start_index,
)
from agentacct.service import SentinelService
from agentacct.tui import _DARK, _LIGHT, _build_receipt_parts


def _seed(store: Path, *, now: float = 1_700_000_000.0) -> None:
    svc = SentinelService(store)
    base = now - 3 * 3600
    svc.record_event({
        "event_id": "evt_usage_s1", "created_at": base + 1800, "source": "claude-code",
        "event_type": "model_usage",
        "metadata": {
            "sentinel_semantic_kind": "usage", "client": "claude-code", "client_session_id": "s1",
            "model": "claude-opus-4-8", "input_tokens": 250_000_000, "output_tokens": 0,
            "total_tokens": 250_000_000, "estimated_cost_usd": 4.82, "cost_confidence": "estimated_from_tokens",
        },
    })
    svc.record_event({
        "event_id": "evt_section_s1_1", "created_at": base + 1800, "source": "claude-code",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section", "client": "claude-code", "client_session_id": "s1",
            "client_transcript_id": "s1", "project_dir": "/tmp/proj", "section_id": "s1-1",
            "section_status": "completed", "section_title": "Build the snapshot harness",
            "summary": "Implemented and tested.", "kind": "implementation", "files": ["src/mod.py"],
        },
    })
    svc.record_event({
        "event_id": "evt_ev_s1_1", "created_at": base + 1900, "source": "claude-code",
        "event_type": "machine_check",
        "metadata": {
            "sentinel_semantic_kind": "evidence", "client": "claude-code", "client_session_id": "s1",
            "section_id": "s1-1", "evidence_type": "test", "result": "passed", "name": "pytest",
            "summary": "12 passed", "command": "pytest -q", "exit_code": 0,
        },
    })


def _first_receipt(store: Path) -> dict:
    projection = build_store_task_projection(store)
    tasks = [t for t in projection.get("tasks", []) if isinstance(t, dict) and t.get("public_task_id")]
    task = tasks[0]
    tid = str(task["public_task_id"])
    return build_receipt(
        task,
        public_task_id=tid,
        title=_task_title(task),
        latest_store_activity_at=latest_store_activity(tasks),
        session_starts=session_start_index(tasks),
    )


def test_receipt_markup_is_a_laid_out_object(tmp_path):
    _seed(tmp_path)
    receipt = _first_receipt(tmp_path)
    for pal in (_DARK, _LIGHT):
        parts = _build_receipt_parts(receipt, pal)
        text = "\n".join(parts.values())
        Text.from_markup(text)  # must be valid markup
        plain = Text.from_markup(text).plain
        assert "All receipts" in plain          # breadcrumb (head)
        assert "CURRENT OUTCOME" in plain        # the verdict card title
        assert "RECEIPT DIMENSIONS" in plain     # the ledger card title
        assert "Build the snapshot harness" in plain


def test_receipt_markup_survives_hostile_fields(tmp_path):
    svc = SentinelService(tmp_path)
    now = 1_700_000_000.0
    svc.record_event({
        "event_id": "evt_usage_x", "created_at": now - 120, "source": "claude-code",
        "event_type": "model_usage",
        "metadata": {
            "sentinel_semantic_kind": "usage", "client": "claude-code", "client_session_id": "sx",
            "model": "gpt[/]4", "input_tokens": 1000, "output_tokens": 0, "total_tokens": 1000,
        },
    })
    svc.record_event({
        "event_id": "evt_section_x", "created_at": now - 120, "source": "claude-code",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section", "client": "claude-code", "client_session_id": "sx",
            "client_transcript_id": "sx", "project_dir": "/tmp/p", "section_id": "sx-1",
            "section_status": "completed", "section_title": "pwn[/]step", "summary": "[/]boom",
        },
    })
    receipt = _first_receipt(tmp_path)
    Text.from_markup("\n".join(_build_receipt_parts(receipt, _DARK).values()))  # no MarkupError
