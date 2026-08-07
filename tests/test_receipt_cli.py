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
                "files": ["src/login.py"],
            },
        }
    )


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
    for marker in ("Work Receipt", "Decision status", "Evidence strength", "Provenance"):
        assert marker in result.output


def test_receipt_unknown_task_exits_nonzero(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["receipt", "task_deadbeef", "--store-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "No Task matches" in result.output
