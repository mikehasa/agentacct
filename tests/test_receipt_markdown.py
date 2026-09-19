"""The Markdown Work Receipt renderer + the `agentacct receipt --markdown` flag."""

from __future__ import annotations

from pathlib import Path

import json
import re

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
                "summary": "Recorded outcome for this fixture section.",
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
    assert "**Decision — Reported**" in md
    assert "**Coverage —" in md
    # Row labels come from the shared receipt field glossary.
    assert "| Agents |" in md and "| Decision |" in md
    # The agent's own outcome words are shown, labeled as the agent's report.
    assert "Outcome (agent-reported): Recorded outcome for this fixture section." in md
    # The dimension table and the shared provenance legend both render.
    assert "| Dimension | Summary | Source |" in md
    # The cost wears the shared grammar and its spelled-out basis, never a raw key.
    assert "| Cost |" in md and "pricing estimate" in md and "pricing_table" not in md
    assert "| Checks |" in md
    assert "**Provenance**" in md
    # The orthogonality note (decision vs evidence) is preserved verbatim.
    assert "never counts as machine verification" in md


def test_markdown_render_includes_the_timeline(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", _task_id(tmp_path), "--markdown", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "**Timeline**" in result.output
    assert "| When | Session | Event | Status | Source |" in result.output
    # Relative-time offsets, never wall-clock, so the render is reproducible.
    assert "+0s" in result.output


def test_a_rendered_receipt_carries_no_unfilled_placeholder(tmp_path: Path) -> None:
    """A receipt is pasted into PRs and read as evidence, so a template variable
    that never got filled reads as a broken receipt. The coverage definition used
    to print a literal "X of Y checkable steps"; it states the rule in words now,
    and nothing else may reintroduce that shape."""
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", _task_id(tmp_path), "--markdown", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    # The definition must still render, or this guard would pass vacuously.
    assert "checkable steps" in result.output
    placeholders = re.findall(r"\b[a-zA-Z] of [a-zA-Z]\b|\bTODO\b|\bFIXME\b", result.output)
    assert placeholders == [], f"unfilled placeholder(s) in a rendered receipt: {placeholders}"


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


def _minimal_receipt(**overrides) -> dict:
    receipt = {
        "title": "x",
        "task_id": "task_abc",
        "axes": {
            "decision_status": {"key": "finding", "asserted_by": "machine", "statement": "s"},
            "evidence_strength": {"gradeable": False},
            "handoff": {"handed_off": False},
        },
        "dimensions": {
            "task": {"objectives": ["a"], "boundary": {}, "provenance": ["mcp"]},
            "actors": {"provenance": ["client_log"]},
            "actions": {"tool_category_counts": {}, "touched_file_count": 0, "provenance": ["none"]},
            "cost": {"provenance": ["none"]},
            "evidence": {"checks_total": 0, "provenance": ["none"]},
            "outcome": {"decision_status": "finding", "asserted_by": "machine", "provenance": ["hook"]},
            "gaps": {"items": [], "count": 0},
            "provenance": {"legend": {}},
        },
        "timeline": {"events": [], "shown": 0, "total": 0},
    }
    receipt.update(overrides)
    return receipt


def test_markdown_attention_block_states_reason_label_next_step_and_disposition() -> None:
    receipt = _minimal_receipt(
        attention={
            "kind": "failed_check",
            "reason_label": "Failed check",
            "label": "Failed build check · make release · exit 0",
            "summary": "Could not reproduce the build.",
            "next_step": "Rerun on a clean tree",
            "disposition_state": "reviewed",
            "disposition_note": "looking into it",
        }
    )
    md = render_receipt_markdown(receipt)
    assert "**Attention**" in md
    # The label's lead segment already carries the reason words, so it leads
    # alone: never "Failed check · Failed build check".
    assert "- **Failed build check** · make release · exit 0" in md
    assert "Failed check · Failed build check" not in md
    assert "  Could not reproduce the build." in md
    assert "  Recorded next step: Rerun on a clean tree" in md
    assert "Disposition: reviewed by you; still needs resolving. Note: looking into it" in md
    # No attention -> no block.
    assert "**Attention**" not in render_receipt_markdown(_minimal_receipt())


def test_markdown_gap_uses_the_reducer_gap_label_and_never_a_dash() -> None:
    receipt = _minimal_receipt(
        verdict={
            "headline": "Reported — 1/1 self-checked",
            "gap": "1 step blocked · no usage recorded",
            "gap_text": "1 step blocked",
            "gap_label": None,
        }
    )
    md = render_receipt_markdown(receipt)
    assert "  Gap: 1 step blocked" in md
    # Cost absence is not an evidence gap line.
    assert "Gap: 1 step blocked · no usage recorded" not in md
    assert "Not yet proven" not in md
    unproven = _minimal_receipt(
        verdict={"headline": "Reported — 0/1 checked", "gap_text": "1 completed step unchecked", "gap_label": "Not yet proven"}
    )
    assert "  Not yet proven: 1 completed step unchecked" in render_receipt_markdown(unproven)
    # Named absences only: the rendered table never shows a bare dash value.
    assert "| — |" not in md and " — |" not in md.replace("Work Receipt — x", "")


def test_markdown_timeline_names_supersession_revision_and_subsecond_offsets() -> None:
    receipt = _minimal_receipt(
        timeline={
            "events": [
                {"occurred_at": 100.0, "lane": "evidence", "kind": "check", "title": "pytest", "status": "failed",
                 "superseded": True, "source": "mcp"},
                {"occurred_at": 100.3, "lane": "evidence", "kind": "check", "title": "pytest", "status": "passed",
                 "superseded": False, "source": "mcp", "revision_label": "at 8a4e024 · main · uncommitted changes"},
                {"occurred_at": 145.0, "lane": "primary", "kind": "work", "title": "ship", "status": "completed",
                 "source": "claude-code"},
            ],
            "shown": 3,
            "total": 3,
        }
    )
    md = render_receipt_markdown(receipt)
    # Printed words only: an event without a session title names the absence,
    # statuses use the shared result/step labels, sources the provenance label.
    assert "| +0s | Check evidence | pytest | Failed · superseded | Agent-reported |" in md
    assert "| +0.3s | Check evidence | pytest · at 8a4e024 · main · uncommitted changes | Passed | Agent-reported |" in md
    assert "| +45s | Primary session | ship | Reported completed | Agent-reported |" in md


_RAW_TIMELINE_KEYS = {
    "primary", "supporting", "evidence", "control", "handed_off", "completed", "started", "checkpoint",
    "blocked", "failed", "passed", "error", "skipped", "unknown", "recorded", "mcp", "hook", "ci",
    "client_log", "none",
}


def test_no_markdown_table_cell_is_a_raw_key(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", _task_id(tmp_path), "--markdown", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    rows = [line for line in result.output.splitlines() if line.startswith("| ") and not line.startswith("| ---")]
    assert any("Reported completed" in row for row in rows)
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert not (set(cells) & _RAW_TIMELINE_KEYS), row
    # Gap groups print their dimension label, never the key.
    assert "- **actors**" not in result.output and "- **actions**" not in result.output


def test_actions_row_names_its_absence_instead_of_a_counted_zero(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", _task_id(tmp_path), "--markdown", "--store-dir", str(tmp_path)])
    md = result.output
    assert "touched 0 files" not in md
    assert "| Tool calls | not instrumented · 1 related path |" in md


def test_terminal_receipt_keeps_its_hanging_indent_when_prose_wraps(tmp_path: Path) -> None:
    _seed(tmp_path, title="A deliberately long section title that forces the terminal receipt to wrap its lines")
    result = runner.invoke(
        app, ["receipt", _task_id(tmp_path), "--store-dir", str(tmp_path)], env={"COLUMNS": "60"}
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.lstrip().startswith("Decision"))
    end = next(i for i, line in enumerate(lines) if line.startswith("Dimension"))
    headings = {"Attention"}
    for line in lines[start:end]:
        # Every continuation stays indented: nothing but a section heading
        # starts at column 0 inside the lead block.
        assert not line or line.startswith(" ") or line.strip() in headings, line
    assert "[actors]" not in result.output and "[actions]" not in result.output
