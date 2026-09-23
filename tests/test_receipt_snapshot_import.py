"""Actual import refreshes should not turn every stored view into first-build UI."""
import json
import time

import pytest
from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.receipt_snapshot_builder import snapshot_input_state
from agentacct.receipt_snapshot_runtime import ReceiptSnapshotManager
from agentacct.service import SentinelService
from tests.test_client_usage import (
    _make_claude_home, _claude_import_args, _append_claude_same_model_growth_row,
    _make_codex_home, _codex_token_count, _codex_counters,
)
from tests.test_receipt_snapshot_api import HEADERS, client, publish


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_real_usage_import_refresh_serves_previous_receipt_while_updating(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(ReceiptSnapshotManager, "start", lambda self: None)
    store = tmp_path / "state"
    runner = CliRunner()
    if provider == "claude-code":
        home = _make_claude_home(tmp_path)
        args = _claude_import_args(store, home)
    else:
        home = _make_codex_home(tmp_path)
        args = ["usage", "import-local", "--store-dir", str(store), "--client", "codex", "--codex-home", str(home), "--json"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    service = SentinelService(store)
    publish(store)
    before = snapshot_input_state(store, service)
    http = client(store)
    first_tasks = http.get("/v1/tasks", headers=HEADERS)
    assert first_tasks.status_code == 200
    if provider == "claude-code":
        _append_claude_same_model_growth_row(home)
    else:
        rollout = next((home / "sessions").rglob("*.jsonl"))
        with rollout.open("a") as handle:
            handle.write(json.dumps(_codex_token_count("2026-06-27T00:01:00Z", _codex_counters(3000, 1000, 200, 30),
                last=_codex_counters(500, 100, 75, 8))) + "\n")
    refreshed = runner.invoke(app, [*args, "--refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    assert json.loads(refreshed.output)["refreshed_events"] > 0
    after = snapshot_input_state(store, service)
    assert after[0] != before[0]
    assert after[1] == before[1], "ordinary usage refresh incorrectly revoked safe stored work"
    response = http.get("/v1/tasks", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["projection"]["state"] == "updating"
    assert response.json()["tasks"] == first_tasks.json()["tasks"]
    publish(store)
    assert http.get("/v1/tasks", headers=HEADERS).json()["projection"]["state"] == "current"
