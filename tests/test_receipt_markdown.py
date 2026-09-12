"""The Markdown Work Receipt renderer + the `agentacct receipt --markdown` flag."""

from __future__ import annotations

from pathlib import Path

import json

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.receipt_markdown import render_receipt_markdown
from agentacct.service import SentinelService

runner = CliRunner()
NS = "sha256:receipt-md-ns"


def _seed(store: Path, *, title: str = "Add a token-bucket rate limiter") -> None:
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
            "estimated_cost_usd": 12.0,
            "usage_confidence": "client_reported",
            "cost_confidence": "estimated_from_tokens",
            "cost_basis": "pricing_table",
            "metadata": {
                "usage_source": "local_client_session_store",
                "client": "claude-code",
                "client_session_id": "s1",
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
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": "sec-1",
                "section_status": "completed",
                "section_title": title,
                "objective": title,
                "kind": "implementation",
                "files": ["src/login.py"],
            },
        }
    )
    service.record_event(
        {
            "event_id": "evt_check_s1",
            "created_at": 130.0,
            "source": "claude-code",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "claude-code",
                "client_session_id": "s1",
                "section_id": "sec-1",
                "evidence_type": "test",
                "result": "passed",
                "name": "pytest",
                "summary": "12 passed",
                "command": "pytest -q",
                "exit_code": 0,
            },
        }
    )


def _task_id(store: Path) -> str:
    out = runner.invoke(app, ["receipts", "--json", "--store-dir", str(store)]).output
    return json.loads(out)["tasks"][0]["task_id"]


def test_markdown_render_has_the_receipt_skeleton(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", _task_id(tmp_path), "--markdown", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    md = result.output
    assert "Work Receipt — Add a token-bucket rate limiter" in md
    assert "**Decision status —" in md
    assert "**Evidence coverage —" in md
    # The dimension table and the shared provenance legend both render.
    assert "| Dimension | Summary | Source |" in md
    assert "| Cost |" in md and "pricing_table" in md
    assert "**Provenance**" in md
    # The orthogonality note (decision vs evidence) is preserved verbatim.
    assert "never counts as machine verification" in md


def test_markdown_render_includes_the_timeline(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", _task_id(tmp_path), "--markdown", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "**Timeline**" in result.output
    assert "| When | Lane | Event | Status | Source |" in result.output
    # Relative-time offsets, never wall-clock, so the render is reproducible.
    assert "+0s" in result.output


def test_markdown_and_json_are_mutually_exclusive(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(
        app, ["receipt", _task_id(tmp_path), "--markdown", "--json", "--store-dir", str(tmp_path)]
    )
    assert result.exit_code == 2, result.output
    assert "not both" in result.output


def test_markdown_escapes_table_breaking_pipes() -> None:
    # A tool name / title carrying a pipe must not break the Markdown table.
    receipt = {
        "title": "x",
        "task_id": "task_abc",
        "axes": {
            "decision_status": {"key": "reported", "asserted_by": "agent_report", "statement": "s"},
            "evidence_strength": {"gradeable": False},
            "handoff": {"handed_off": False},
            "orthogonality_note": "note",
        },
        "dimensions": {
            "task": {"objectives": ["a | b"], "boundary": {}, "provenance": ["mcp"]},
            "actors": {"provenance": ["client_log"]},
            "actions": {
                "tool_category_counts": {"execute": 1},
                "touched_file_count": 0,
                "command_count": 0,
                "tool_names_preview": [{"name": "a|b", "count": 1}],
                "provenance": ["hook"],
            },
            "cost": {"provenance": ["none"]},
            "evidence": {"checks_total": 0, "checks_passed": 0, "checks_failed": 0, "provenance": ["none"]},
            "outcome": {"decision_status": "reported", "asserted_by": "agent_report", "provenance": ["mcp"]},
            "gaps": {"items": [], "count": 0},
            "provenance": {"legend": {"mcp": "desc"}},
        },
        "timeline": {"events": [], "shown": 0, "total": 0},
    }
    md = render_receipt_markdown(receipt)
    # The raw pipe never appears unescaped inside a rendered cell.
    assert "a \\| b" in md
    assert "`a\\|b`" in md
