"""The `agentacct receipt` / `agentacct receipts` CLI — the acceptance vehicle."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.receipt import RECEIPT_SCHEMA_VERSION
from agentacct.service import SentinelService

runner = CliRunner()
NS = "sha256:receipt-cli-ns"


def _seed(store: Path, *, title: str = "Add rate limit to login") -> None:
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
                "section_title": title,
                "objective": title,
                "kind": "implementation",
                "files": ["src/login.py"],
            },
        }
    )


def _add_section(store: Path, *, section_id: str, status: str, at: float) -> None:
    SentinelService(store).record_event(
        {
            "event_id": f"evt_{section_id}",
            "created_at": at,
            "source": "claude-code",
            "event_type": f"section_{status}",
            "run_id": None,
            "metadata": {
                "sentinel_semantic_kind": "section",
                "client": "claude-code",
                "client_session_id": "s1",
                "client_context_keys_authored": ["client_session_id"],
                "project_dir": "/tmp/project",
                "session_namespace_fingerprint": NS,
                "identity_scope_state": "explicit",
                "section_id": section_id,
                "section_status": status,
                "section_title": "handoff task",
                "objective": "handoff task",
                "kind": "implementation",
            },
        }
    )


def test_receipts_show_handoff_marker_beside_a_hard_problem(tmp_path: Path) -> None:
    # End-to-end: a blocked step + a later handoff → the decision word is the hard
    # problem (BLOCKED) while the deliberate stop rides a parallel marker in both
    # the list and the detail. This is the dogfood bug the change fixes.
    _seed(tmp_path)  # session s1 + one completed section
    _add_section(tmp_path, section_id="sec-blocked", status="blocked", at=150.0)
    _add_section(tmp_path, section_id="sec-handoff", status="handed_off", at=200.0)

    listing = runner.invoke(app, ["receipts", "--store-dir", str(tmp_path)])
    assert listing.exit_code == 0, listing.output
    assert "handed off" in listing.output.lower()

    task_id = json.loads(
        runner.invoke(app, ["receipts", "--json", "--store-dir", str(tmp_path)]).output
    )["tasks"][0]["task_id"]
    detail = runner.invoke(app, ["receipt", task_id, "--store-dir", str(tmp_path)])
    assert detail.exit_code == 0, detail.output
    assert "BLOCKED" in detail.output  # the hard problem is the decision word
    assert "Handed off" in detail.output  # the parallel marker is still shown


def test_receipts_pure_handoff_shows_no_duplicate_marker(tmp_path: Path) -> None:
    # Invariant #4 end-to-end: a cleanly handed-off task's decision word IS
    # handed_off, so the parallel "↗ handed off" marker must be suppressed — the
    # handoff is never stated twice. (Only the completed + handed_off sections;
    # no open successor, so the decision word is handed_off.)
    _seed(tmp_path)  # session s1 + one completed section
    _add_section(tmp_path, section_id="sec-handoff", status="handed_off", at=200.0)

    listing = runner.invoke(app, ["receipts", "--store-dir", str(tmp_path)])
    assert listing.exit_code == 0, listing.output
    assert "handed_off" in listing.output  # the decision word itself
    assert "↗" not in listing.output  # but NOT the parallel marker glyph

    task_id = json.loads(
        runner.invoke(app, ["receipts", "--json", "--store-dir", str(tmp_path)]).output
    )["tasks"][0]["task_id"]
    detail = runner.invoke(app, ["receipt", task_id, "--store-dir", str(tmp_path)])
    assert detail.exit_code == 0, detail.output
    assert "HANDED_OFF" in detail.output
    assert "Lifecycle" not in detail.output  # no duplicate marker row


def test_receipts_list_hides_marker_for_a_resumed_task(tmp_path: Path) -> None:
    # Invariant #3 end-to-end on the list surface: a task handed off then resumed
    # (a later open step) must show NO handoff marker anywhere — the exact stale-
    # marker failure the recency guard prevents.
    _seed(tmp_path)  # session s1 + one completed section
    _add_section(tmp_path, section_id="sec-handoff", status="handed_off", at=150.0)
    _add_section(tmp_path, section_id="sec-open", status="started", at=200.0)

    listing = runner.invoke(app, ["receipts", "--store-dir", str(tmp_path)])
    assert listing.exit_code == 0, listing.output
    assert "↗" not in listing.output  # no stale handoff marker


def test_receipts_list_json(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipts", "--json", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == RECEIPT_SCHEMA_VERSION
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["task_id"].startswith("task_")


def test_receipt_detail_json_has_all_eight_dimensions(tmp_path: Path) -> None:
    _seed(tmp_path)
    listing = json.loads(
        runner.invoke(app, ["receipts", "--json", "--store-dir", str(tmp_path)]).output
    )
    task_id = listing["tasks"][0]["task_id"]
    result = runner.invoke(app, ["receipt", task_id, "--json", "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert set(receipt["dimensions"]) == {
        "task",
        "actors",
        "actions",
        "cost",
        "evidence",
        "outcome",
        "gaps",
        "provenance",
    }
    assert receipt["dimensions"]["cost"]["cost_basis"] == "pricing_table"


def test_receipt_text_render_is_scannable(tmp_path: Path) -> None:
    _seed(tmp_path)
    task_id = json.loads(
        runner.invoke(app, ["receipts", "--json", "--store-dir", str(tmp_path)]).output
    )["tasks"][0]["task_id"]
    result = runner.invoke(app, ["receipt", task_id, "--store-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for marker in ("Work Receipt", "Decision status", "Evidence coverage", "Provenance"):
        assert marker in result.output
    # The evidence line is a coverage RATIO, not a categorical grade word.
    assert "unchecked" in result.output or "checked" in result.output


def test_receipt_text_survives_rich_markup_in_recorded_titles(tmp_path: Path) -> None:
    # Agent-recorded titles/objectives are arbitrary text; a bracket-tag title
    # must never crash the render (MarkupError) or be silently reinterpreted.
    _seed(tmp_path, title="cleanup [/] wip [red]danger[/red]")
    listing = runner.invoke(app, ["receipts", "--store-dir", str(tmp_path)])
    assert listing.exit_code == 0, listing.output
    task_id = json.loads(
        runner.invoke(app, ["receipts", "--json", "--store-dir", str(tmp_path)]).output
    )["tasks"][0]["task_id"]
    detail = runner.invoke(app, ["receipt", task_id, "--store-dir", str(tmp_path)])
    assert detail.exit_code == 0, detail.output
    # The literal bracket text is preserved (escaped), not interpreted away.
    assert "[/]" in detail.output


def test_receipt_unknown_task_exits_nonzero(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", "task_deadbeef", "--store-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "No Task matches" in result.output
