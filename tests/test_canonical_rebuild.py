"""Rebuild a live canonical store from a store directory's events.jsonl.

Covers the one-shot dev/CLI bridge (``rebuild_live_store_from_events``): a
store dir with only an append-only ledger becomes a role='live' chronicle
store whose read models the fast read runtime can serve, idempotently and
without the production promote ceremony.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentacct.canonical.rebuild import (
    EVENTS_FILENAME,
    RebuildError,
    rebuild_live_store_from_events,
)
from agentacct.canonical.sqlite import LIVE_STORE_FILENAME
from agentacct.canonical_read import CanonicalReadRuntime


def _usage_event(
    *,
    event_id: str,
    session_id: str = "sess-1",
    created_at: float = 1_700_000_000.0,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict[str, Any]:
    """A minimal local-import model_usage event that imports cleanly.

    Carries ``source_namespace_fingerprint`` (the importer excludes events
    without it) and cumulative-snapshot semantics.
    """

    return {
        "event_id": event_id,
        "event_type": "model_usage",
        "source": "codex-usage-import",
        "created_at": created_at,
        "provider": "openai",
        "model": "gpt-test",
        "usage_confidence": "client_reported_total",
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": 0.01,
        "metadata": {
            "client": "codex",
            "client_session_id": session_id,
            "client_session_kind": "root",
            "source_namespace_fingerprint": "fp-rebuild",
            "usage_update_semantics": "cumulative_snapshot",
            "updated_at": created_at,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _write_store_dir(tmp_path: Path, events: list[dict[str, Any]], *, name: str = "store") -> Path:
    store_dir = tmp_path / name
    store_dir.mkdir(mode=0o700)
    ledger = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    (store_dir / EVENTS_FILENAME).write_text(ledger, encoding="utf-8")
    return store_dir


def _store_role(store_path: Path) -> str:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT store_role FROM store_metadata WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    return str(row[0])


def test_rebuild_produces_a_live_store_the_read_runtime_can_serve(tmp_path: Path) -> None:
    store_dir = _write_store_dir(
        tmp_path,
        [
            _usage_event(event_id="u1", session_id="s1", input_tokens=100, output_tokens=50),
            # A later cumulative snapshot for the same session supersedes the first.
            _usage_event(event_id="u2", session_id="s1", input_tokens=140, output_tokens=70),
            _usage_event(event_id="u3", session_id="s2", input_tokens=10, output_tokens=5),
        ],
    )

    report = rebuild_live_store_from_events(store_dir)

    target = store_dir / LIVE_STORE_FILENAME
    assert report.store_path == target
    assert target.exists()
    assert _store_role(target) == "live"
    # 0600 identity open_live demands.
    assert (target.stat().st_mode & 0o777) == 0o600
    assert report.session_count == 2
    assert report.usage_day_count >= 1
    assert report.parsed_events >= 3

    # The fast read runtime serves the rebuilt store (flag forced on).
    runtime = CanonicalReadRuntime(store_dir, enabled=True)
    health = runtime.health_probe()
    assert health["available"] is True
    assert health["store"]["store_role"] == "live"
    assert health["projections"]["rm_usage_day"]["state"] == "current"

    usage = runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    assert usage.rows, "expected at least one dated usage day"
    sessions = runtime.session_list_read()
    assert len(sessions.sessions) == 2


def test_rebuild_is_idempotent_and_replaces_an_existing_store(tmp_path: Path) -> None:
    store_dir = _write_store_dir(tmp_path, [_usage_event(event_id="u1")])

    first = rebuild_live_store_from_events(store_dir)
    first_uuid = _store_uuid(first.store_path)

    second = rebuild_live_store_from_events(store_dir)
    second_uuid = _store_uuid(second.store_path)

    # A fresh store each time (new uuid), but the same shape and still live.
    assert first_uuid != second_uuid
    assert _store_role(second.store_path) == "live"
    assert second.session_count == first.session_count
    assert second.usage_day_count == first.usage_day_count

    runtime = CanonicalReadRuntime(store_dir, enabled=True)
    assert runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31").rows


def test_rebuild_absorbs_new_events_appended_to_the_ledger(tmp_path: Path) -> None:
    store_dir = _write_store_dir(tmp_path, [_usage_event(event_id="u1", session_id="s1")])
    rebuild_live_store_from_events(store_dir)

    with (store_dir / EVENTS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_event(event_id="u2", session_id="s2"), sort_keys=True) + "\n")

    report = rebuild_live_store_from_events(store_dir)
    assert report.session_count == 2


def test_rebuild_reads_the_authoritative_log_not_a_frozen_events_jsonl_backup(
    tmp_path: Path, monkeypatch
) -> None:
    # An adopted store keeps events.jsonl as a FROZEN pre-cutover backup while the
    # SQLite log keeps growing. rebuild-store must reconstruct from the
    # authoritative log, not the stale flat file, or every event recorded after
    # adoption is silently dropped from the rebuilt index.
    from agentacct.service import SentinelService

    store_dir = tmp_path / "adopted"
    store_dir.mkdir(mode=0o700)
    # Seed an existing events.jsonl store in mirror mode, then adopt it by opening
    # under the authoritative default; the flat file is kept as a frozen backup.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    seeder = SentinelService(store_dir)
    seeder.append_events_preserving_identity([_usage_event(event_id="u1", session_id="s1")])
    seeder.list_all_events()

    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)  # authoritative default
    adopted = SentinelService(store_dir)
    assert adopted._authoritative()
    # events.jsonl stays a frozen 1-session backup; the second session goes only
    # to the log.
    adopted.append_events_preserving_identity([_usage_event(event_id="u2", session_id="s2")])
    assert len((store_dir / EVENTS_FILENAME).read_text(encoding="utf-8").splitlines()) == 1

    report = rebuild_live_store_from_events(store_dir)
    # Both sessions present => the rebuild read the authoritative log, not the
    # frozen 1-session flat backup.
    assert report.session_count == 2


def test_rebuild_leaves_no_stale_wal_sidecars_from_a_prior_store(tmp_path: Path) -> None:
    # A prior live store can leave an uncheckpointed chronicle.sqlite3-wal. A
    # rebuild replaces only the main file, so the fresh store must not inherit
    # (or replay) a stale sidecar.
    store_dir = _write_store_dir(tmp_path, [_usage_event(event_id="u1", session_id="s1")])
    rebuild_live_store_from_events(store_dir)
    store = store_dir / LIVE_STORE_FILENAME

    # Simulate a prior store's dirty WAL carrying a sentinel value.
    connection = sqlite3.connect(store)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE store_metadata SET canonical_sequence = 999999 WHERE singleton = 1")
        connection.commit()
    finally:
        connection.close()

    with (store_dir / EVENTS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_event(event_id="u2", session_id="s2"), sort_keys=True) + "\n")
    report = rebuild_live_store_from_events(store_dir)

    # No stale sidecars, and the sentinel from the dirty WAL did not survive.
    assert not (store_dir / f"{LIVE_STORE_FILENAME}-wal").exists()
    assert not (store_dir / f"{LIVE_STORE_FILENAME}-shm").exists()
    connection = sqlite3.connect(store)
    try:
        sequence = connection.execute(
            "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert sequence != 999999
    assert report.session_count == 2


def test_rebuild_without_a_ledger_raises(tmp_path: Path) -> None:
    store_dir = tmp_path / "empty"
    store_dir.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        rebuild_live_store_from_events(store_dir)


def test_rebuild_refuses_to_install_an_empty_store(tmp_path: Path) -> None:
    # A ledger whose lines all lack a source_namespace_fingerprint imports
    # nothing. Rather than stand up a valid-but-empty role='live' store that a
    # later read-flag flip would serve, rebuild must refuse and leave no store.
    fingerprintless = {
        "event_type": "section_started",
        "metadata": {"client": "claude-code", "client_session_id": "s1"},
        "created_at": 1_700_000_000.0,
    }
    store_dir = _write_store_dir(tmp_path, [fingerprintless])
    with pytest.raises(RebuildError):
        rebuild_live_store_from_events(store_dir)
    assert not (store_dir / LIVE_STORE_FILENAME).exists()


def _store_uuid(store_path: Path) -> str:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT store_uuid FROM store_metadata WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    return str(row[0])
