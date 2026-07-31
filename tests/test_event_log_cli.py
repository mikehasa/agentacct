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


def test_verify_log_reports_parity(tmp_path: Path, monkeypatch) -> None:
    # Mirror mode: verify-log genuinely compares the flat file to the log (in the
    # default authoritative mode there is no flat file left to prove parity against).
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    _seed(store_dir, 3)
    result = CliRunner().invoke(app, ["event", "verify-log", "--store-dir", str(store_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["matches"] is True
    assert payload["log_lines"] == 3


def test_drop_flat_ledger_cutover_survives_reopen_without_env(tmp_path: Path, monkeypatch) -> None:
    # The blocker fix: the cutover writes a PERSISTENT marker so that even a later
    # open in explicit mirror mode (env=0) keeps reading the log and does NOT wipe
    # it or resurrect events.jsonl — the marker, not the env, is the authority
    # signal. Mirror mode is pinned throughout so the store starts flat-file-
    # authoritative, giving drop-flat-ledger a real events.jsonl to cut over.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    _seed(store_dir, 4)

    # Dry run keeps the file.
    dry = CliRunner().invoke(app, ["event", "drop-flat-ledger", "--store-dir", str(store_dir)])
    assert dry.exit_code == 0, dry.output
    assert (store_dir / "events.jsonl").exists()

    # Confirmed cutover: marker written, flat file deleted, lock file KEPT.
    done = CliRunner().invoke(app, ["event", "drop-flat-ledger", "--store-dir", str(store_dir), "--confirm"])
    assert done.exit_code == 0, done.output
    assert not (store_dir / "events.jsonl").exists()
    assert (store_dir / "events.authoritative").exists()
    assert (store_dir / "events.jsonl.lock").exists()

    # A fresh service in explicit mirror mode still sees all 4 events (no wipe)
    # and a new write goes to the log, not a resurrected flat file — the marker
    # overrides the mirror-mode env.
    reopened = SentinelService(store_dir)
    assert reopened._authoritative()
    assert len(reopened.list_all_events()) == 4
    reopened.record_event({"event_type": "note", "metadata": {"client": "cc", "client_session_id": "s1"}, "note": "post"})
    assert len(reopened.list_all_events()) == 5
    assert not (store_dir / "events.jsonl").exists()

    listing = CliRunner().invoke(app, ["event", "list", "--store-dir", str(store_dir), "--json"])
    assert listing.exit_code == 0, listing.output
    assert len(json.loads(listing.output)["events"]) == 5
