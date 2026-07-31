"""CLI cutover: verify-log parity gate + safe flat-ledger deletion."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.service import SentinelService


def _seed(store_dir: Path, n: int) -> None:
    service = SentinelService(store_dir)
    for i in range(n):
        service.record_event({"event_type": "note", "metadata": {"client": "cc", "client_session_id": "s1"}, "note": f"n{i}"})
    service.list_all_events()  # sync the mirror


def test_verify_log_reports_parity(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    _seed(store_dir, 3)
    result = CliRunner().invoke(app, ["event", "verify-log", "--store-dir", str(store_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["matches"] is True
    assert payload["log_lines"] == 3


def test_drop_flat_ledger_refuses_without_the_authoritative_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    _seed(store_dir, 2)
    result = CliRunner().invoke(app, ["event", "drop-flat-ledger", "--store-dir", str(store_dir), "--confirm"])
    assert result.exit_code == 2
    assert (store_dir / "events.jsonl").exists()  # not deleted


def test_drop_flat_ledger_deletes_when_authoritative_and_synced(tmp_path: Path, monkeypatch) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    _seed(store_dir, 4)
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")

    # Dry run keeps the file.
    dry = CliRunner().invoke(app, ["event", "drop-flat-ledger", "--store-dir", str(store_dir)])
    assert dry.exit_code == 0, dry.output
    assert (store_dir / "events.jsonl").exists()

    # Confirmed deletion removes the flat file; reads still work from SQLite.
    done = CliRunner().invoke(app, ["event", "drop-flat-ledger", "--store-dir", str(store_dir), "--confirm"])
    assert done.exit_code == 0, done.output
    assert not (store_dir / "events.jsonl").exists()
    assert (store_dir / "events.sqlite3").exists()

    listing = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir), "--json"])
    assert listing.exit_code == 0, listing.output
    assert len(json.loads(listing.output)["events"]) == 4
