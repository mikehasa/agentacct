from __future__ import annotations

import json

from typer.testing import CliRunner

from agent_chronicle.cli import app
from agent_chronicle.service import SentinelService


runner = CliRunner()


def test_evidence_work_event_preserves_v1_and_adds_v2(tmp_path) -> None:
    store = tmp_path / "state"

    args = [
        "evidence",
        "work-event",
        "--store-dir",
        str(store),
        "--source",
        "codex",
        "--kind",
        "section",
        "--status",
        "completed",
        "--occurred-at",
        "1783900123.5",
        "--source-event-id",
        "codex-section-implementation-1",
        "--run-id",
        "run-1",
        "--section-id",
        "implementation",
        "--client",
        "codex",
        "--client-session-id",
        "session-1",
        "--summary",
        "Implemented the Evidence v2 shadow layer.",
        "--json",
    ]
    result = runner.invoke(app, args)
    retry = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert retry.exit_code == 0, retry.output
    payload = json.loads(result.output)
    assert payload["work_event"]["transport"] == "cli"
    assert payload["work_event"]["event_kind"] == "section"
    assert payload["work_event"]["occurred_at"] == 1_783_900_123.5
    assert payload["work_event"]["source_event_id"] == "codex-section-implementation-1"
    assert payload["v1_event"]["event_type"] == "section_completed"
    assert payload["v1_event"]["metadata"]["client_event_timestamp"] == 1_783_900_123.5
    assert json.loads(retry.output)["v1_event"]["event_id"] == payload["v1_event"]["event_id"]
    assert (store / "events.jsonl").is_file()
    assert len((store / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (store / "evidence-v2" / "spool.jsonl").is_file()

    listing = runner.invoke(app, ["evidence", "list", "--store-dir", str(store), "--json"])
    assert listing.exit_code == 0, listing.output
    envelopes = json.loads(listing.output)["evidence"]
    assert len(envelopes) == 1
    assert envelopes[0]["receipt_count"] == 2
    assert envelopes[0]["duplicate_receipt_count"] == 1
    assert envelopes[0]["envelope"]["source_instance"] == "v1-cli"


def test_evidence_status_product_and_idempotent_v1_replay(tmp_path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    service.record_event(
        {
            "source": "codex",
            "event_type": "task_started",
            "run_id": "run-1",
            "metadata": {"summary": "Start", "client_session_id": "session-1"},
        },
        transport="mcp",
    )

    status = runner.invoke(app, ["evidence", "status", "--store-dir", str(store), "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["stats"]["evidence_versions"] == 1

    replay = runner.invoke(app, ["evidence", "replay-v1", "--store-dir", str(store), "--json"])
    assert replay.exit_code == 0, replay.output
    replay_payload = json.loads(replay.output)
    # The explicit replay transport is a distinct source instance, so the
    # first migration adds one compatibility version; repeating is idempotent.
    assert replay_payload["inserted_count"] == 1
    replay_again = runner.invoke(app, ["evidence", "replay-v1", "--store-dir", str(store), "--json"])
    assert json.loads(replay_again.output)["duplicate_count"] == 1

    product = runner.invoke(app, ["evidence", "product", "--store-dir", str(store), "--json"])
    assert product.exit_code == 0, product.output
    product_payload = json.loads(product.output)
    assert product_payload["summary"]["evidence_count"] == 2
    assert set(product_payload) >= {
        "work_graph",
        "evidence_matrix",
        "discrepancies",
        "cost_outcome_basis",
    }


def test_evidence_commands_honor_kill_switch(tmp_path) -> None:
    store = tmp_path / "state"
    store.mkdir()

    status = runner.invoke(
        app,
        ["evidence", "status", "--store-dir", str(store), "--json"],
        env={"AGENT_CHRONICLE_EVIDENCE_V2": "0"},
    )

    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["enabled"] is False
    assert not (store / "evidence-v2").exists()
