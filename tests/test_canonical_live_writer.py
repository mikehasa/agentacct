from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_chronicle.activation import RUNTIME_ENV_ALLOWLIST
from agent_chronicle.canonical.legacy_import import LegacyImporter
from agent_chronicle.canonical.sqlite import CanonicalStore, LIVE_STORE_FILENAME
from agent_chronicle.canonical_live import (
    CANONICAL_LIVE_ENV,
    CanonicalAuthoritativeNotReady,
    CanonicalLiveRuntime,
    CanonicalWriteMode,
    canonical_live_write_enabled,
    canonical_live_write_mode,
    require_supported_write_mode,
)
from agent_chronicle.service import SentinelService

from test_canonical_legacy_import import _verified_events_snapshot


def _section_event(
    *,
    session_id: str = "sess-1",
    section_id: str = "sec-a",
    status: str = "started",
    namespace: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "client": "claude-code",
        "client_session_id": session_id,
        "section_id": section_id,
        "section_status": status,
        "section_title": "live shadow test",
        "sentinel_semantic_kind": "section",
    }
    if namespace is not None:
        metadata["source_namespace_fingerprint"] = namespace
    return {
        "source": "test-suite",
        "event_type": f"section_{status}",
        "metadata": metadata,
    }


def _live_db(store_dir: Path) -> Path:
    return store_dir / LIVE_STORE_FILENAME


def _counts(db: Path) -> dict[str, int]:
    connection = sqlite3.connect(db)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("source_instances", "sessions", "facts", "fact_session_links")
        }
    finally:
        connection.close()


def _canonical_sequence(db: Path) -> int:
    connection = sqlite3.connect(db)
    try:
        return int(
            connection.execute(
                "SELECT canonical_sequence FROM store_metadata WHERE singleton = 1"
            ).fetchone()[0]
        )
    finally:
        connection.close()


# --- flag semantics ---------------------------------------------------------


def test_flag_defaults_off_and_parses_truthy_values(monkeypatch):
    monkeypatch.delenv(CANONICAL_LIVE_ENV, raising=False)
    monkeypatch.delenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", raising=False)
    assert canonical_live_write_enabled() is False
    for value in ("1", "true", "on", "shadow", "YES"):
        monkeypatch.setenv(CANONICAL_LIVE_ENV, value)
        assert canonical_live_write_enabled() is True
    for value in ("", "0", "off", "false", "disabled"):
        monkeypatch.setenv(CANONICAL_LIVE_ENV, value)
        assert canonical_live_write_enabled() is False


def test_flag_honors_legacy_alias(monkeypatch):
    monkeypatch.delenv(CANONICAL_LIVE_ENV, raising=False)
    monkeypatch.setenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", "shadow")
    assert canonical_live_write_enabled() is True


def test_flag_pair_is_forwarded_to_managed_runtime_children():
    assert "AGENT_CHRONICLE_CANONICAL_LIVE_WRITE" in RUNTIME_ENV_ALLOWLIST
    assert "AGENT_SENTINEL_CANONICAL_LIVE_WRITE" in RUNTIME_ENV_ALLOWLIST


# --- I0: tri-state write mode (off | shadow | authoritative) ----------------


def test_write_mode_parses_tri_state(monkeypatch):
    monkeypatch.delenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", raising=False)
    monkeypatch.delenv(CANONICAL_LIVE_ENV, raising=False)
    assert canonical_live_write_mode() is CanonicalWriteMode.OFF
    # Every historical truthy value stays exactly "shadow" — no silent drift.
    for value in ("1", "true", "on", "yes", "shadow", "SHADOW"):
        monkeypatch.setenv(CANONICAL_LIVE_ENV, value)
        assert canonical_live_write_mode() is CanonicalWriteMode.SHADOW
        assert canonical_live_write_enabled() is True
    # Off / unknown tokens fail closed to OFF (canonical writes nothing).
    for value in ("", "0", "off", "false", "disabled", "nonsense"):
        monkeypatch.setenv(CANONICAL_LIVE_ENV, value)
        assert canonical_live_write_mode() is CanonicalWriteMode.OFF
        assert canonical_live_write_enabled() is False
    # The new tier parses, and still counts as "writes on".
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "authoritative")
    assert canonical_live_write_mode() is CanonicalWriteMode.AUTHORITATIVE
    assert canonical_live_write_enabled() is True


def test_write_mode_honors_legacy_alias_for_authoritative(monkeypatch):
    monkeypatch.delenv(CANONICAL_LIVE_ENV, raising=False)
    monkeypatch.setenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", "authoritative")
    assert canonical_live_write_mode() is CanonicalWriteMode.AUTHORITATIVE


def test_require_supported_write_mode_gates_only_authoritative():
    assert require_supported_write_mode(CanonicalWriteMode.OFF) is CanonicalWriteMode.OFF
    assert (
        require_supported_write_mode(CanonicalWriteMode.SHADOW)
        is CanonicalWriteMode.SHADOW
    )
    with pytest.raises(CanonicalAuthoritativeNotReady):
        require_supported_write_mode(CanonicalWriteMode.AUTHORITATIVE)


def test_authoritative_env_fails_loud_at_runtime_construction(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", raising=False)
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "authoritative")
    with pytest.raises(CanonicalAuthoritativeNotReady):
        CanonicalLiveRuntime(tmp_path / "store")


def test_authoritative_env_fails_loud_at_service_construction(monkeypatch, tmp_path):
    # A whole service refuses to start in the unwired authoritative mode — v1
    # is never silently demoted from the record.
    monkeypatch.delenv("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", raising=False)
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "authoritative")
    with pytest.raises(CanonicalAuthoritativeNotReady):
        SentinelService(tmp_path / "store")


def test_explicit_mode_overrides_and_gates(tmp_path):
    shadow = CanonicalLiveRuntime(tmp_path / "s", mode=CanonicalWriteMode.SHADOW)
    assert shadow.mode is CanonicalWriteMode.SHADOW and shadow.enabled is True
    off = CanonicalLiveRuntime(tmp_path / "o", mode=CanonicalWriteMode.OFF)
    assert off.mode is CanonicalWriteMode.OFF and off.enabled is False
    with pytest.raises(CanonicalAuthoritativeNotReady):
        CanonicalLiveRuntime(tmp_path / "a", mode=CanonicalWriteMode.AUTHORITATIVE)


def test_bool_enabled_override_maps_to_shadow_or_off(monkeypatch, tmp_path):
    # A bool can never select authoritative, even with the env set to it: the
    # explicit construction argument wins and stays shadow/off.
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "authoritative")
    assert CanonicalLiveRuntime(tmp_path / "t", enabled=True).mode is CanonicalWriteMode.SHADOW
    assert CanonicalLiveRuntime(tmp_path / "f", enabled=False).mode is CanonicalWriteMode.OFF


def test_status_reports_mode(tmp_path):
    runtime = CanonicalLiveRuntime(tmp_path / "store", mode=CanonicalWriteMode.SHADOW)
    status = runtime.status()
    assert status["mode"] == "shadow"
    assert status["enabled"] is True


# --- flag off: byte-identical live behavior ---------------------------------


def test_flag_off_record_event_creates_no_canonical_store(tmp_path):
    service = SentinelService(tmp_path / "store")
    recorded = service.record_event(_section_event())
    assert recorded["event_type"] == "section_started"
    assert not _live_db(tmp_path / "store").exists()


# --- flag on: the record_event lane -----------------------------------------


def test_shadow_writes_source_session_fact_and_link(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    service.record_event(_section_event())
    db = _live_db(store_dir)
    assert db.exists()
    assert _counts(db) == {
        "source_instances": 1,
        "sessions": 1,
        "facts": 1,
        "fact_session_links": 1,
    }
    connection = sqlite3.connect(db)
    try:
        connection.row_factory = sqlite3.Row
        source = connection.execute("SELECT * FROM source_instances").fetchone()
        assert source["client"] == "claude-code"
        assert source["adapter"] == "chronicle-v1-live-writer"
        assert source["representation"] == "legacy-v1"
        assert source["namespace_scheme"] == "live-unresolved-store-namespace-v1"
        role = connection.execute(
            "SELECT store_role FROM store_metadata WHERE singleton = 1"
        ).fetchone()
        assert role["store_role"] == "live"
        claim = connection.execute(
            "SELECT event_kind, status FROM work_claims JOIN facts USING (fact_id)"
        ).fetchone()
        assert (claim["event_kind"], claim["status"]) == ("section", "started")
        fact = connection.execute("SELECT source_event_id, transport FROM facts").fetchone()
        assert str(fact["source_event_id"]).startswith("evt_")
        assert str(fact["transport"]).startswith("live-")
    finally:
        connection.close()


def test_shadow_replay_of_same_recorded_event_is_all_noop(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    recorded = service.record_event(_section_event())
    db = _live_db(store_dir)
    counts = _counts(db)
    sequence = _canonical_sequence(db)
    result = service.canonical_live.shadow_v1_event(recorded)
    assert result.error is None
    assert result.written is False
    assert result.dispositions == {
        "session": "noop",
        "fact": "noop",
        "link": "noop",
        "task_anchor": "present",
    }
    assert _counts(db) == counts
    assert _canonical_sequence(db) == sequence


def test_idempotent_record_event_replay_does_not_duplicate_facts(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    event = _section_event()
    event["metadata"]["idempotency_key"] = "idem-1"
    first = service.record_event(event)
    second = service.record_event(json.loads(json.dumps(event)))
    assert second["event_id"] == first["event_id"]
    assert _counts(_live_db(store_dir))["facts"] == 1


def test_non_work_claim_events_are_skipped_like_the_importer(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    recorded = service.record_event({"source": "t", "event_type": "note", "metadata": {}})
    result = service.canonical_live.shadow_v1_event(recorded)
    assert result.skipped_reason == "not_represented_in_canonical_model"
    # The skip happened before any store was needed for the note itself, but
    # record_event already ran once for it; only work-claim rows may exist.
    if _live_db(store_dir).exists():
        assert _counts(_live_db(store_dir))["facts"] == 0


def test_unknown_superseded_event_is_skipped_not_written(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    event = _section_event()
    event["metadata"]["supersedes_event_id"] = "evt_nonexistent"
    service.record_event(event)
    assert _counts(_live_db(store_dir))["facts"] == 0


def test_missing_created_at_falls_back_to_epoch_zero_visibly(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    event = _section_event()
    event["event_id"] = "evt_manual"
    result = runtime.shadow_v1_event(event)
    assert result.error is None
    assert result.dispositions.get("occurred_fallback") == "epoch_zero"


def test_shadow_failure_never_fails_the_v1_write(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    corrupt = _live_db(store_dir)
    corrupt.write_bytes(b"not a sqlite database")
    os.chmod(corrupt, 0o600)
    recorded = service.record_event(_section_event())
    assert recorded["event_type"] == "section_started"
    result = service.canonical_live.shadow_v1_event(recorded)
    assert result.error is not None


# --- live store opening guards ----------------------------------------------


def test_open_live_refuses_candidate_role_store(tmp_path):
    candidate_dir = tmp_path / "scratch"
    candidate_dir.mkdir(mode=0o700)
    candidate = CanonicalStore.create(candidate_dir / "candidate.sqlite3")
    candidate.close()
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    target = _live_db(store_dir)
    target.write_bytes((candidate_dir / "candidate.sqlite3").read_bytes())
    os.chmod(target, 0o600)
    with pytest.raises(ValueError, match="live store metadata is missing or incompatible"):
        CanonicalStore.open_live(store_dir)


def test_candidate_open_refuses_live_store_file(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    live = CanonicalStore.create_live(store_dir)
    live.close()
    with pytest.raises(ValueError, match="legacy live-store file"):
        CanonicalStore.open(_live_db(store_dir))


def test_open_live_requires_owner_only_file(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    CanonicalStore.create_live(store_dir).close()
    os.chmod(_live_db(store_dir), 0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        CanonicalStore.open_live(store_dir)


def test_open_live_refuses_symlinked_store(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    other = tmp_path / "elsewhere.sqlite3"
    CanonicalStore.create_live(store_dir).close()
    os.rename(_live_db(store_dir), other)
    os.symlink(other, _live_db(store_dir))
    with pytest.raises(ValueError, match="regular non-symlink"):
        CanonicalStore.open_live(store_dir)


def test_open_or_create_live_is_reentrant(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    first = CanonicalStore.open_or_create_live(store_dir)
    uuid_one = first.connection.execute(
        "SELECT store_uuid FROM store_metadata"
    ).fetchone()[0]
    first.close()
    second = CanonicalStore.open_or_create_live(store_dir)
    uuid_two = second.connection.execute(
        "SELECT store_uuid FROM store_metadata"
    ).fetchone()[0]
    second.close()
    assert uuid_one == uuid_two


# --- live/import normalization parity ---------------------------------------


def test_live_shadow_and_importer_agree_on_hashes_for_the_same_event(tmp_path):
    """The keystone: identical content hash, observation hash, namespace
    digest, and source_event_id for the same v1 event through both writers.

    Same-identity-different-content across the two writers would quarantine
    healthy rows as conflicts at the cutover seam; this test pins the shared
    normalization that prevents it.
    """

    namespace = "fp-abc123"
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    event = _section_event(namespace=namespace)
    event["metadata"]["updated_at"] = 1_700_000_000
    recorded = service.record_event(event)

    live_connection = sqlite3.connect(_live_db(store_dir))
    live_connection.row_factory = sqlite3.Row
    live_fact = live_connection.execute(
        "SELECT source_event_id, content_hash, source_instance_id FROM facts"
    ).fetchone()
    live_session = live_connection.execute(
        "SELECT observation_hash FROM sessions"
    ).fetchone()
    live_namespace = live_connection.execute(
        "SELECT namespace_digest, namespace_scheme FROM source_instances"
    ).fetchone()
    live_connection.close()

    ledger_line = json.dumps(recorded, sort_keys=True) + "\n"
    snapshot = _verified_events_snapshot(tmp_path, ledger_line.encode("utf-8"))
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    candidate = CanonicalStore.create(scratch / "candidate.sqlite3")
    importer = LegacyImporter(
        snapshot=snapshot,
        store=candidate,
        scratch_root=scratch,
    )
    importer.import_events()
    imported_fact = candidate.connection.execute(
        "SELECT source_event_id, content_hash FROM facts"
    ).fetchone()
    imported_session = candidate.connection.execute(
        "SELECT observation_hash FROM sessions"
    ).fetchone()
    imported_namespace = candidate.connection.execute(
        "SELECT namespace_digest, namespace_scheme FROM source_instances"
    ).fetchone()
    candidate.close()

    assert imported_fact is not None and live_fact is not None
    assert imported_fact["source_event_id"] == live_fact["source_event_id"]
    assert bytes(imported_fact["content_hash"]) == bytes(live_fact["content_hash"])
    assert bytes(imported_session["observation_hash"]) == bytes(
        live_session["observation_hash"]
    )
    assert bytes(imported_namespace["namespace_digest"]) == bytes(
        live_namespace["namespace_digest"]
    )
    assert imported_namespace["namespace_scheme"] == live_namespace["namespace_scheme"]


# --- review-round regressions ------------------------------------------------


def test_replaying_an_older_event_neither_conflicts_nor_regresses(tmp_path):
    """Idempotent MCP retries re-shadow the ORIGINAL recorded event after the
    session has moved on; that must not mint a source_conflict."""

    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    first = service.record_event(_section_event(status="started"))
    service.record_event(_section_event(status="completed"))

    result = service.canonical_live.shadow_v1_event(first)
    assert result.error is None
    assert result.dispositions["session"] == "noop"
    assert result.dispositions["fact"] == "noop"
    assert result.dispositions["link"] == "noop"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        conflicts = int(
            connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0]
        )
        assert conflicts == 0
        last_activity = connection.execute(
            "SELECT last_activity_at_us, observation_order FROM sessions"
        ).fetchone()
        # The session still reflects the newest observation, not the replay.
        assert last_activity[0] == last_activity[1]
    finally:
        connection.close()


def test_same_order_different_content_tie_is_skipped_not_conflicted(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    base = _section_event(status="started")
    base["event_id"] = "evt_tie_one"
    base["created_at"] = 1_700_000_000.0
    assert runtime.shadow_v1_event(base).error is None
    tie = _section_event(status="completed")
    tie["event_id"] = "evt_tie_two"
    tie["created_at"] = 1_700_000_000.0
    # Same observation order but different session content (kind changed).
    tie["metadata"]["client_session_kind"] = "child"
    result = runtime.shadow_v1_event(tie)
    assert result.error is None
    assert result.dispositions["session"] == "session_order_tie_deferred"
    assert result.dispositions["fact"] == "inserted"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0])
            == 0
        )
        # Both facts still landed and linked to the one session.
        assert int(connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]) == 2
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM fact_session_links"
                ).fetchone()[0]
            )
            == 2
        )
    finally:
        connection.close()


def test_long_transport_label_is_bounded_to_the_ddl_limit(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    event = _section_event()
    event["event_id"] = "evt_long_transport"
    event["created_at"] = 1_700_000_000.0
    result = runtime.shadow_v1_event(event, transport="x" * 200)
    assert result.error is None
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        transport = connection.execute("SELECT transport FROM facts").fetchone()[0]
        assert len(transport) <= 64 and transport.startswith("live-")
    finally:
        connection.close()


def test_runtime_status_counts_written_noop_skip_and_error(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    recorded = service.record_event(_section_event())
    service.canonical_live.shadow_v1_event(recorded)  # replay -> noop
    service.record_event({"source": "t", "event_type": "note", "metadata": {}})  # skip
    status = service.canonical_live.status()
    assert status["enabled"] is True
    assert status["written"] >= 1
    assert status["noop"] >= 1
    assert status["skipped"] >= 1
    assert status["errors"] == 0 and status["last_error"] is None
    _live_db(store_dir).write_bytes(b"garbage")
    os.chmod(_live_db(store_dir), 0o600)
    service.canonical_live.shadow_v1_event(recorded)
    status = service.canonical_live.status()
    assert status["errors"] == 1 and status["last_error"] is not None


def test_flag_off_keeps_canonical_package_unimported(tmp_path):
    """Invariant A at its root: with the flag off, constructing the service
    and recording an event must not load the canonical package at all."""

    import subprocess
    import sys

    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from agent_chronicle.service import SentinelService\n"
        f"service = SentinelService(Path({str(tmp_path / 'store')!r}))\n"
        "service.record_event({'source': 't', 'event_type': 'section_started',"
        " 'metadata': {'client_session_id': 's1'}})\n"
        "loaded = [name for name in sys.modules if name.startswith('agent_chronicle.canonical.')]\n"
        "assert not loaded, f'canonical modules loaded with flag off: {loaded}'\n"
    )
    environment = dict(os.environ)
    environment.pop("AGENT_CHRONICLE_CANONICAL_LIVE_WRITE", None)
    environment.pop("AGENT_SENTINEL_CANONICAL_LIVE_WRITE", None)
    environment["AGENT_CHRONICLE_STORE_DIR"] = str(tmp_path / "isolated-default-store")
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


def test_stale_staging_file_never_blocks_live_creation(tmp_path):
    """A crash mid-create litters a staging sibling; the reserved name stays
    absent, so the next writer creates cleanly (the old in-place bootstrap
    would have bricked the lane instead)."""

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    stale = store_dir / ".chronicle.sqlite3.new-9999-deadbeef"
    stale.write_bytes(b"torn partial bootstrap")
    store = CanonicalStore.open_or_create_live(store_dir)
    store.close()
    assert _live_db(store_dir).exists()


def test_create_live_race_loser_opens_the_winners_store(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    winner = CanonicalStore.create_live(store_dir)
    winner_uuid = winner.connection.execute(
        "SELECT store_uuid FROM store_metadata"
    ).fetchone()[0]
    winner.close()
    with pytest.raises(FileExistsError):
        CanonicalStore.create_live(store_dir)
    loser = CanonicalStore.open_or_create_live(store_dir)
    loser_uuid = loser.connection.execute(
        "SELECT store_uuid FROM store_metadata"
    ).fetchone()[0]
    loser.close()
    assert loser_uuid == winner_uuid


def test_open_live_of_older_candidate_role_file_refuses_without_migrating(tmp_path):
    """A fail-closed open must not durably rewrite the store it refuses."""

    from test_canonical_schema import _downgrade_to_version_two

    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    candidate_path = scratch / "candidate.sqlite3"
    CanonicalStore.create(candidate_path).close()
    _downgrade_to_version_two(candidate_path)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    target = _live_db(store_dir)
    target.write_bytes(candidate_path.read_bytes())
    os.chmod(target, 0o600)
    with pytest.raises(ValueError, match="metadata is missing or incompatible"):
        CanonicalStore.open_live(store_dir)
    connection = sqlite3.connect(target)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        connection.close()


def test_legacy_importer_refuses_a_live_store_destination(tmp_path):
    live_root = tmp_path / "store"
    live_root.mkdir(mode=0o700)
    # Import scratch rules also apply, so stage the live store inside an
    # owner-only scratch tree to prove the ROLE check is what refuses it.
    live = CanonicalStore.create_live(live_root)
    snapshot = _verified_events_snapshot(tmp_path, b"")
    from agent_chronicle.canonical.legacy_import import LegacyImportError

    with pytest.raises(LegacyImportError, match="must be a candidate store"):
        LegacyImporter(snapshot=snapshot, store=live, scratch_root=live_root)
    live.close()


# --- phase 3.2: absorb parity, lineage, edges, anchors ------------------------


def _observed_event(
    *,
    event_id: str,
    session_id: str,
    created_at: float,
    namespace: str = "fp-32parity",
    kind: str | None = None,
    parent: str | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "client": "claude-code",
        "client_session_id": session_id,
        "section_id": f"sec-{event_id}",
        "section_status": "checkpoint",
        "sentinel_semantic_kind": "section",
        "source_namespace_fingerprint": namespace,
        "updated_at": created_at,
    }
    if kind is not None:
        metadata["client_session_kind"] = kind
    if parent is not None:
        metadata["parent_client_session_id"] = parent
    if started_at is not None:
        metadata["started_at"] = started_at
    return {
        "event_id": event_id,
        "event_type": "section_checkpoint",
        "source": "test-suite",
        "created_at": created_at,
        "metadata": metadata,
    }


def _session_table_state(db: Path) -> list[tuple]:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        sessions = [
            (
                row["client_session_id"],
                row["session_kind"],
                row["started_at_us"],
                row["last_activity_at_us"],
                bytes(row["observation_hash"]).hex(),
            )
            for row in connection.execute(
                "SELECT client_session_id, session_kind, started_at_us, "
                "last_activity_at_us, observation_hash FROM sessions "
                "ORDER BY client_session_id"
            )
        ]
        lineage = [
            (row["parent_client_session_id"], bytes(row["parent_namespace_digest"]).hex())
            for row in connection.execute(
                "SELECT l.parent_client_session_id, l.parent_namespace_digest "
                "FROM session_observed_lineage l JOIN sessions s USING (session_id) "
                "ORDER BY s.client_session_id"
            )
        ]
        edges = [
            (row["relation"], row["validation_state"], bytes(row["content_hash"]).hex())
            for row in connection.execute(
                "SELECT relation, validation_state, content_hash FROM session_edges "
                "ORDER BY session_edge_id"
            )
        ]
        anchors = int(
            connection.execute("SELECT COUNT(*) FROM task_anchors").fetchone()[0]
        )
        return [tuple(sessions), tuple(lineage), tuple(edges), anchors]
    finally:
        connection.close()


def test_live_absorb_matches_importer_for_multi_event_sessions(tmp_path):
    """The keystone for 3.2: a parent session plus a child whose events vary
    started_at/kind/parent must land in the SAME final session, lineage,
    edge, and anchor state through the live writer as through the importer."""

    events = [
        _observed_event(
            event_id="evt_p1", session_id="parent-1", created_at=1_000.0, kind="root"
        ),
        _observed_event(
            event_id="evt_c1",
            session_id="child-1",
            created_at=1_010.0,
            kind="child",
            parent="parent-1",
            started_at=900.0,
        ),
        _observed_event(
            event_id="evt_c2",
            session_id="child-1",
            created_at=1_020.0,
            kind="child",
            parent="parent-1",
            started_at=950.0,  # later event with a LATER start: min() keeps 900
        ),
        _observed_event(
            event_id="evt_p2", session_id="parent-1", created_at=1_030.0, kind="root"
        ),
    ]

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in events:
        result = runtime.shadow_v1_event(event)
        assert result.error is None, result.error
    live_state = _session_table_state(_live_db(store_dir))

    ledger = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    snapshot = _verified_events_snapshot(tmp_path, ledger.encode("utf-8"))
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    candidate = CanonicalStore.create(scratch / "candidate.sqlite3")
    LegacyImporter(snapshot=snapshot, store=candidate, scratch_root=scratch).import_events()
    candidate.close()
    imported_state = _session_table_state(scratch / "candidate.sqlite3")

    assert live_state == imported_state
    # And the child kept the earliest observed start plus its parent lineage.
    sessions, lineage, edges, anchors = live_state
    child = next(row for row in sessions if row[0].endswith("child-1"))
    assert child[2] == 900_000_000
    assert len(lineage) == 1 and lineage[0][0].endswith("parent-1")
    assert len(edges) == 1 and edges[0][1] == "valid"
    assert anchors == 1  # only the parent anchors a Task


def test_stale_arrival_with_earlier_start_absorbs_to_importer_parity(tmp_path):
    """I1: a stale (lower-order) arrival carrying an EARLIER started_at must
    fold that value in exactly as the importer's whole-file min() does, not
    defer — min/max are order-free. The kind/parent stay the incumbent's (it is
    the newest observation), matching the importer's last-wins pass. No
    fabricated order, no spurious conflict."""

    events = [
        _observed_event(event_id="evt_new", session_id="s-1", created_at=2_000.0, kind="root"),
        _observed_event(
            event_id="evt_old", session_id="s-1", created_at=1_500.0, kind="root", started_at=1_400.0
        ),
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert runtime.shadow_v1_event(events[0]).error is None
    result = runtime.shadow_v1_event(events[1])
    assert result.error is None
    assert result.dispositions["session"] == "absorbed"

    connection = sqlite3.connect(_live_db(store_dir))
    try:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0])
            == 0
        )
        started = connection.execute("SELECT started_at_us FROM sessions").fetchone()[0]
        assert started == 1_400_000_000  # the order-free min was folded in
    finally:
        connection.close()

    # And the live row byte-matches the importer's whole-file collapse.
    assert _session_table_state(_live_db(store_dir)) == _session_table_state(
        _run_importer(tmp_path, events)
    )


def test_out_of_order_arrivals_absorb_to_importer_parity(tmp_path):
    """Feeding a session's observations to the live writer in a DIFFERENT order
    than the ledger must still converge on the importer's whole-file row: min
    started_at / max last_activity are associative, so incremental folding and
    one-shot collapse reach the same fixed point. No fork (kind agrees), so the
    highest-order observation's fields win in both worlds."""

    # Ledger/file order (what the importer reads):
    ledger_order = [
        _observed_event(event_id="e_low", session_id="s-1", created_at=1_000.0, kind="root", started_at=500.0),
        _observed_event(event_id="e_mid", session_id="s-1", created_at=2_000.0, kind="root", started_at=2_500.0),
        _observed_event(event_id="e_high", session_id="s-1", created_at=3_000.0, kind="root", started_at=2_000.0),
    ]
    # Live arrival order (deliberately reversed / interleaved):
    arrival_order = [ledger_order[2], ledger_order[1], ledger_order[0]]

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in arrival_order:
        assert runtime.shadow_v1_event(event).error is None

    live_state = _session_table_state(_live_db(store_dir))
    assert live_state == _session_table_state(_run_importer(tmp_path, ledger_order))
    # concretely: earliest start folded in, highest-order activity kept.
    started, last = live_state[0][0][2], live_state[0][0][3]
    assert started == 500_000_000
    assert last == 3_000_000_000


def test_tie_fork_still_folds_in_the_order_free_fields(tmp_path):
    """A genuine tie/order fork stays visibly deferred on the ambiguous
    kind/parent winner, but the order-free min(started)/max(last) are still
    folded in — a strictly smaller divergence than deferring the whole row."""

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    base = _observed_event(event_id="evt_a", session_id="s-1", created_at=2_000.0, kind="root")
    assert runtime.shadow_v1_event(base).error is None
    # Same order (tie) + a disagreeing kind (fork) + an earlier started_at.
    fork = _observed_event(
        event_id="evt_b", session_id="s-1", created_at=2_000.0, kind="child", started_at=1_400.0
    )
    result = runtime.shadow_v1_event(fork)
    assert result.error is None
    assert result.dispositions["session"] == "session_order_tie_deferred"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        # kind stayed the incumbent's (the fork winner is deferred)...
        kind = connection.execute("SELECT session_kind FROM sessions").fetchone()[0]
        assert kind == "root"
        # ...but the order-free earlier start WAS folded in.
        started = connection.execute("SELECT started_at_us FROM sessions").fetchone()[0]
        assert started == 1_400_000_000
        assert (
            int(connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0])
            == 0
        )
    finally:
        connection.close()


def test_edge_waits_for_unobserved_parent_then_lands_on_retry(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    child = _observed_event(
        event_id="evt_c", session_id="c-1", created_at=1_000.0, kind="child", parent="p-1"
    )
    first = runtime.shadow_v1_event(child)
    assert first.dispositions.get("edge") == "parent_session_unobserved"
    parent = _observed_event(
        event_id="evt_p", session_id="p-1", created_at=1_010.0, kind="root"
    )
    assert runtime.shadow_v1_event(parent).error is None
    retry = _observed_event(
        event_id="evt_c2", session_id="c-1", created_at=1_020.0, kind="child", parent="p-1"
    )
    result = runtime.shadow_v1_event(retry)
    assert result.dispositions.get("edge") == "inserted"


def test_valid_parent_retires_a_previously_minted_anchor(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert (
        runtime.shadow_v1_event(
            _observed_event(event_id="evt_r", session_id="r-1", created_at=1_000.0, kind="root")
        ).dispositions.get("task_anchor")
        == "present"
    )
    assert (
        runtime.shadow_v1_event(
            _observed_event(event_id="evt_p", session_id="p-1", created_at=1_005.0, kind="root")
        ).error
        is None
    )
    # r-1 turns out to be a child of p-1: the valid edge must retire its anchor.
    result = runtime.shadow_v1_event(
        _observed_event(
            event_id="evt_r2", session_id="r-1", created_at=1_010.0, kind="child", parent="p-1"
        )
    )
    assert result.dispositions.get("edge") == "inserted"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        anchored = connection.execute(
            "SELECT s.client_session_id FROM task_anchors t "
            "JOIN sessions s ON s.session_id = t.primary_session_id"
        ).fetchall()
        assert [row[0].split(":")[-1] for row in anchored] == ["p-1"]
    finally:
        connection.close()


def test_trusted_session_observation_lane_shadows_canonically(tmp_path):
    from agent_chronicle.usage_truth import mark_trusted_local_session_observation_event

    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    observation = {
        "event_id": "evt_obs1",
        "created_at": 999.0,
        "source": "codex-local-session-observation-import",
        "event_type": "session_observed",
        "run_id": "client_codex_zero_usage_session",
        "metadata": {
            "client": "codex",
            "client_session_id": "obs-sess-1",
            "client_session_kind": "root",
            "source_namespace_fingerprint": "sha256:" + "a" * 64,
            "source_parse_complete": True,
            "started_at": 100.0,
            "updated_at": 200.0,
        },
    }
    recorded = service.record_event(
        mark_trusted_local_session_observation_event(observation),
        trusted_session_observation_import=True,
    )
    assert recorded["event_type"] == "session_observed"
    db = _live_db(store_dir)
    assert db.exists()
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT client_session_id, session_kind, started_at_us FROM sessions"
        ).fetchone()
        assert row is not None
        assert row["client_session_id"].endswith("obs-sess-1")
        assert row["session_kind"] == "root"
        assert row["started_at_us"] == 100_000_000
        # session_observed is not a work claim: no fact row.
        assert int(connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]) == 0
    finally:
        connection.close()


def test_note_event_with_session_id_reconciles_session_without_fact(tmp_path):
    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    service.record_event(
        {
            "source": "t",
            "event_type": "note",
            "metadata": {"client": "claude-code", "client_session_id": "n-1"},
        }
    )
    db = _live_db(store_dir)
    assert db.exists()
    counts = _counts(db)
    assert counts["sessions"] == 1
    assert counts["facts"] == 0


# --- 3.2 review-round regressions --------------------------------------------


def _run_importer(tmp_path: Path, events: list[dict[str, Any]], *, name: str = "review") -> Path:
    ledger = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    snapshot = _verified_events_snapshot(tmp_path, ledger.encode("utf-8"), name=name)
    scratch = tmp_path / f"scratch-{name}"
    scratch.mkdir(mode=0o700)
    candidate = CanonicalStore.create(scratch / "candidate.sqlite3")
    LegacyImporter(snapshot=snapshot, store=candidate, scratch_root=scratch).import_events()
    candidate.close()
    return scratch / "candidate.sqlite3"


def test_parent_switch_supersedes_the_stale_edge_to_importer_parity(tmp_path):
    """The newest absorbed parent must be THE valid edge, not a rejected
    'ambiguous_multiple_parent' row behind a stale valid one."""

    events = [
        _observed_event(event_id="evt_p1", session_id="p-1", created_at=900.0, kind="root"),
        _observed_event(event_id="evt_p2", session_id="p-2", created_at=910.0, kind="root"),
        _observed_event(
            event_id="evt_c1", session_id="c-1", created_at=1_000.0, kind="child", parent="p-1"
        ),
        _observed_event(
            event_id="evt_c2", session_id="c-1", created_at=1_010.0, kind="child", parent="p-2"
        ),
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in events:
        assert runtime.shadow_v1_event(event).error is None
    live_state = _session_table_state(_live_db(store_dir))
    imported_state = _session_table_state(_run_importer(tmp_path, events))
    assert live_state == imported_state
    edges = live_state[2]
    assert len(edges) == 1 and edges[0][1] == "valid"


def test_kind_flip_across_relation_classes_converges_to_importer_parity(tmp_path):
    events = [
        _observed_event(event_id="evt_p", session_id="p-1", created_at=900.0, kind="root"),
        _observed_event(
            event_id="evt_c1", session_id="c-1", created_at=1_000.0, kind="child", parent="p-1"
        ),
        _observed_event(
            event_id="evt_c2",
            session_id="c-1",
            created_at=1_010.0,
            kind="continuation",
            parent="p-1",
        ),
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in events:
        assert runtime.shadow_v1_event(event).error is None
    live_state = _session_table_state(_live_db(store_dir))
    imported_state = _session_table_state(_run_importer(tmp_path, events))
    assert live_state == imported_state
    edges = live_state[2]
    assert len(edges) == 1 and edges[0][0] == "continuation" and edges[0][1] == "valid"


def test_same_relation_kind_mutation_follows_the_newest_edge_hash(tmp_path):
    events = [
        _observed_event(event_id="evt_p", session_id="p-1", created_at=900.0, kind="root"),
        _observed_event(
            event_id="evt_c1", session_id="c-1", created_at=1_000.0, kind="child", parent="p-1"
        ),
        _observed_event(
            event_id="evt_c2", session_id="c-1", created_at=1_010.0, kind="internal", parent="p-1"
        ),
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in events:
        assert runtime.shadow_v1_event(event).error is None
    assert _session_table_state(_live_db(store_dir)) == _session_table_state(
        _run_importer(tmp_path, events)
    )


def test_cross_client_fingerprint_decoy_is_not_linked_as_parent(tmp_path):
    """Two clients sharing one explicit fingerprint hash to the same digest;
    a decoy session under the OTHER client with the parent's session id must
    not satisfy the child's parent claim."""

    namespace = "fp-shared"
    decoy = _observed_event(
        event_id="evt_decoy", session_id="p-1", created_at=900.0, kind="root", namespace=namespace
    )
    decoy["metadata"]["client"] = "codex"
    child = _observed_event(
        event_id="evt_c", session_id="c-1", created_at=1_000.0, kind="child",
        parent="p-1", namespace=namespace,
    )
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert runtime.shadow_v1_event(decoy).error is None
    result = runtime.shadow_v1_event(child)
    assert result.error is None
    assert result.dispositions.get("edge") == "parent_session_unobserved"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM session_edges").fetchone()[0]) == 0
    finally:
        connection.close()


def test_orderless_observation_with_kind_fork_defers_visibly(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    first = _observed_event(event_id="evt_a", session_id="s-1", created_at=1_000.0, kind="root")
    assert runtime.shadow_v1_event(first).error is None
    fork = _observed_event(event_id="evt_b", session_id="s-1", created_at=1_100.0, kind="child")
    # An unparseable updated_at makes the observation order-less; the
    # importer would order it by line, the live writer must defer VISIBLY.
    fork["metadata"]["updated_at"] = "not-a-timestamp"
    del fork["created_at"]
    result = runtime.shadow_v1_event(fork)
    assert result.error is None
    assert result.dispositions["session"] == "session_orderless_deferred"


def test_kind_regression_retires_a_stale_anchor(tmp_path):
    """root -> child (still parentless): the importer would never anchor the
    absorbed session, so the live anchor minted from the earlier honest
    state must retire."""

    events = [
        _observed_event(event_id="evt_1", session_id="s-1", created_at=1_000.0, kind="root"),
        _observed_event(event_id="evt_2", session_id="s-1", created_at=1_010.0, kind="child"),
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert runtime.shadow_v1_event(events[0]).dispositions.get("task_anchor") == "present"
    result = runtime.shadow_v1_event(events[1])
    assert result.dispositions.get("task_anchor") == "retired"
    assert _session_table_state(_live_db(store_dir)) == _session_table_state(
        _run_importer(tmp_path, events)
    )


def test_importer_survives_a_self_parent_row_with_an_issue(tmp_path):
    event = _observed_event(
        event_id="evt_self", session_id="s-1", created_at=1_000.0, kind="child", parent="s-1"
    )
    candidate = _run_importer(tmp_path, [event])
    connection = sqlite3.connect(candidate)
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM session_edges").fetchone()[0]) == 0
        issues = [
            row[0]
            for row in connection.execute("SELECT reason FROM migration_issues").fetchall()
        ]
        assert "self_parent_session" in issues
    finally:
        connection.close()


def test_lineage_row_records_the_parent_client(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    parent_fp_child = _observed_event(
        event_id="evt_c", session_id="c-1", created_at=1_000.0, kind="child", parent="p-1"
    )
    parent_fp_child["metadata"]["parent_source_namespace_fingerprint"] = "fp-other"
    parent_fp_child["metadata"]["parent_client"] = "codex"
    assert runtime.shadow_v1_event(parent_fp_child).error is None
    connection = sqlite3.connect(_live_db(store_dir))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM session_observed_lineage").fetchone()
        assert row["parent_client"] == "codex"
        assert row["parent_client_session_id"].endswith("p-1")
    finally:
        connection.close()


def test_status_counts_deferrals_separately_from_noops(tmp_path):
    # A genuine last-wins fork (same order, disagreeing kind) still defers, so
    # the deferred counter must move. A pure order-free stale arrival no longer
    # defers (it absorbs) — that path is covered separately.
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    base = _observed_event(event_id="evt_a", session_id="s-1", created_at=2_000.0, kind="root")
    assert runtime.shadow_v1_event(base).error is None
    fork = _observed_event(
        event_id="evt_b", session_id="s-1", created_at=2_000.0, kind="child"
    )
    runtime.shadow_v1_event(fork)
    status = runtime.status()
    assert status["deferred"] == 1


def test_status_counts_an_order_free_absorb_as_a_write(tmp_path):
    # I1: a stale arrival carrying an earlier started_at folds that value in
    # (min is order-free), so it is a WRITE, not a deferral or a noop.
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    base = _observed_event(event_id="evt_a", session_id="s-1", created_at=2_000.0)
    assert runtime.shadow_v1_event(base).error is None
    stale = _observed_event(
        event_id="evt_b", session_id="s-1", created_at=1_500.0, started_at=1_400.0
    )
    result = runtime.shadow_v1_event(stale)
    assert result.dispositions["session"] == "absorbed"
    status = runtime.status()
    assert status["deferred"] == 0
    assert status["written"] == 2  # the insert plus the absorb


# --- phase 3.3: usage lane ----------------------------------------------------


def _usage_event(
    *,
    event_id: str,
    session_id: str = "u-sess-1",
    created_at: float = 1_700_000_000.0,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cost: float | None = 0.01,
    namespace: str = "fp-usage",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id,
        "event_type": "model_usage",
        "source": "codex-usage-import",
        "created_at": created_at,
        "provider": "openai",
        "model": "gpt-test",
        "usage_confidence": "client_reported_total",
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "metadata": {
            "client": "codex",
            "client_session_id": session_id,
            "client_session_kind": "root",
            "source_namespace_fingerprint": namespace,
            "usage_update_semantics": "cumulative_snapshot",
            "updated_at": created_at,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    if cost is not None:
        event["estimated_cost_usd"] = cost
    return event


def _usage_rows(db: Path) -> list[tuple]:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        return [
            (
                row["lane"],
                row["input_tokens"],
                row["output_tokens"],
                row["total_tokens"],
                row["totals_eligible"],
                bytes(row["content_hash"]).hex(),
            )
            for row in connection.execute(
                "SELECT * FROM usage_measurements ORDER BY usage_measurement_id"
            )
        ]
    finally:
        connection.close()


def test_usage_event_shadows_a_measurement_and_replay_is_noop(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    event = _usage_event(event_id="evt_u1")
    first = runtime.shadow_v1_event(event)
    assert first.error is None
    assert first.dispositions.get("usage") == "inserted"
    sequence = _canonical_sequence(_live_db(store_dir))
    replay = runtime.shadow_v1_event(event)
    assert replay.dispositions.get("usage") == "noop"
    assert _canonical_sequence(_live_db(store_dir)) == sequence


def test_usage_matches_importer_for_the_same_events(tmp_path):
    events = [
        _usage_event(event_id="evt_u1", created_at=1_000.0, input_tokens=100),
        _usage_event(event_id="evt_u2", created_at=1_010.0, input_tokens=180),
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in events:
        assert runtime.shadow_v1_event(event).error is None
    assert _usage_rows(_live_db(store_dir)) == _usage_rows(_run_importer(tmp_path, events, name="usage"))


def test_pricing_only_change_is_a_physical_noop(tmp_path):
    """The 10+GB churn class: a rescan whose only difference is the derived
    price estimate must not move the canonical store at all."""

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert runtime.shadow_v1_event(_usage_event(event_id="evt_u1", cost=0.01)).error is None
    sequence = _canonical_sequence(_live_db(store_dir))
    repriced = _usage_event(event_id="evt_u1", cost=99.99)
    result = runtime.shadow_v1_event(repriced)
    assert result.dispositions.get("usage") == "noop"
    assert _canonical_sequence(_live_db(store_dir)) == sequence


def test_changed_totals_update_the_measurement(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert runtime.shadow_v1_event(
        _usage_event(event_id="evt_u1", created_at=1_000.0, input_tokens=100)
    ).error is None
    result = runtime.shadow_v1_event(
        _usage_event(event_id="evt_u2", created_at=1_010.0, input_tokens=200)
    )
    assert result.dispositions.get("usage") == "updated"


def test_stale_usage_defers_instead_of_conflicting(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    assert runtime.shadow_v1_event(
        _usage_event(event_id="evt_u2", created_at=2_000.0, input_tokens=200)
    ).error is None
    stale = runtime.shadow_v1_event(
        _usage_event(event_id="evt_u1", created_at=1_000.0, input_tokens=100)
    )
    assert stale.error is None
    assert stale.dispositions.get("usage") == "usage_stale_deferred"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0])
            == 0
        )
    finally:
        connection.close()


def test_stale_usage_deferral_still_matches_importer_parity(tmp_path):
    """A usage measurement is a pure last-wins cumulative snapshot with no
    order-free min/max component (unlike a session), so the newest snapshot has
    already won: 'deferring' a later-arriving STALE snapshot leaves the live row
    byte-identical to the importer's whole-file collapse. This documents why the
    usage lane needs no absorb primitive — its stale deferral is already
    convergent, and only a genuine order tie stays ambiguous."""

    events = [
        _usage_event(event_id="evt_u2", created_at=2_000.0, input_tokens=200),
        _usage_event(event_id="evt_u1", created_at=1_000.0, input_tokens=100),  # stale
    ]
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    for event in events:
        assert runtime.shadow_v1_event(event).error is None
    assert _usage_rows(_live_db(store_dir)) == _usage_rows(
        _run_importer(tmp_path, events, name="usage")
    )


def test_atomic_usage_is_refused_visibly(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    event = _usage_event(event_id="evt_a1")
    event["metadata"]["usage_update_semantics"] = "atomic_increment"
    result = runtime.shadow_v1_event(event)
    assert result.error is None
    assert result.dispositions.get("usage") == "atomic_usage_requires_immutable_event_lane"
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM usage_measurements").fetchone()[0]) == 0
    finally:
        connection.close()


def test_usage_without_session_identity_is_visible(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    event = _usage_event(event_id="evt_u1")
    del event["metadata"]["client_session_id"]
    result = runtime.shadow_v1_event(event)
    assert result.error is None
    assert result.dispositions.get("usage") == "usage_missing_session_identity"


def test_replace_events_lane_shadows_usage_canonically(tmp_path):
    from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event

    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    event = _usage_event(event_id="evt_ignored")
    del event["event_id"]  # replace_events mints server ids
    recorded = service.replace_events(
        lambda existing: False,
        [event],
        trusted_usage_import=True,
    )
    assert len(recorded) == 1
    rows = _usage_rows(_live_db(store_dir))
    assert len(rows) == 1


def test_import_folds_the_wal_after_commit_and_reports_it(tmp_path):
    ledger = json.dumps(_usage_event(event_id="evt_u1"), sort_keys=True) + "\n"
    snapshot = _verified_events_snapshot(tmp_path, ledger.encode("utf-8"), name="wal")
    scratch = tmp_path / "scratch-wal"
    scratch.mkdir(mode=0o700)
    candidate = CanonicalStore.create(scratch / "candidate.sqlite3")
    report = LegacyImporter(
        snapshot=snapshot, store=candidate, scratch_root=scratch
    ).import_events()
    # Asserted BEFORE close() (which would fold the WAL anyway): the
    # checkpoint ran, succeeded, and the report attests it.
    assert report.wal_folded is True
    wal = Path(f"{scratch / 'candidate.sqlite3'}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    candidate.close()


def test_blocked_wal_fold_is_reported_not_raised(tmp_path):
    ledger = json.dumps(_usage_event(event_id="evt_u1"), sort_keys=True) + "\n"
    snapshot = _verified_events_snapshot(tmp_path, ledger.encode("utf-8"), name="walblock")
    scratch = tmp_path / "scratch-walblock"
    scratch.mkdir(mode=0o700)
    candidate = CanonicalStore.create(scratch / "candidate.sqlite3")
    # A concurrent read snapshot blocks TRUNCATE from folding every frame.
    reader = sqlite3.connect(scratch / "candidate.sqlite3")
    reader.execute("PRAGMA busy_timeout = 100")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM sessions").fetchone()
    candidate.connection.execute("PRAGMA busy_timeout = 100")
    try:
        report = LegacyImporter(
            snapshot=snapshot, store=candidate, scratch_root=scratch
        ).import_events()
        assert report.wal_folded is False
        # The committed import itself is intact and visible.
        assert report.parity.matches is True
    finally:
        reader.close()
        candidate.close()


def test_representation_transition_supersedes_instead_of_conflicting(tmp_path):
    """The codex fallback->rollout transition: v1 replaces its row (identity
    excludes representation); the canonical store must follow the newest
    totals truth instead of re-minting a conflict on every watcher scan."""

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    old_rep = _usage_event(event_id="evt_u1", created_at=1_000.0, input_tokens=100)
    old_rep["metadata"]["usage_representation"] = "codex-sqlite-tokens-used-fallback-v1"
    assert runtime.shadow_v1_event(old_rep).dispositions.get("usage") == "inserted"
    new_rep = _usage_event(event_id="evt_u2", created_at=2_000.0, input_tokens=250)
    new_rep["metadata"]["usage_representation"] = "codex-rollout-token-count-v1"
    result = runtime.shadow_v1_event(new_rep)
    assert result.error is None
    assert result.dispositions.get("usage") == "inserted"
    connection = sqlite3.connect(_live_db(store_dir))
    connection.row_factory = sqlite3.Row
    try:
        rows = {
            row["representation"]: (row["totals_eligible"], row["held_reason"], row["input_tokens"])
            for row in connection.execute("SELECT * FROM usage_measurements")
        }
        assert rows["codex-rollout-token-count-v1"] == (1, None, 250)
        assert rows["codex-sqlite-tokens-used-fallback-v1"][0] == 0
        assert rows["codex-sqlite-tokens-used-fallback-v1"][1] == "superseded_totals_representation"
        assert (
            int(connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0])
            == 0
        )
    finally:
        connection.close()
    # Subsequent scans of the new representation stay clean noops.
    replay = runtime.shadow_v1_event(new_rep)
    assert replay.dispositions.get("usage") == "noop"
    assert runtime.status()["conflicts"] == 0


def test_stale_cross_representation_arrival_still_conflicts(tmp_path):
    """Supersession is ordered: an OLDER totals-eligible arrival under a
    different representation is a genuine fork and must conflict, once."""

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    runtime = CanonicalLiveRuntime(store_dir, enabled=True)
    newer = _usage_event(event_id="evt_u2", created_at=2_000.0, input_tokens=250)
    newer["metadata"]["usage_representation"] = "rep-b"
    assert runtime.shadow_v1_event(newer).error is None
    stale = _usage_event(event_id="evt_u1", created_at=1_000.0, input_tokens=100)
    stale["metadata"]["usage_representation"] = "rep-a"
    result = runtime.shadow_v1_event(stale)
    assert result.dispositions.get("usage") == "conflict"
    assert runtime.status()["conflicts"] == 1
    connection = sqlite3.connect(_live_db(store_dir))
    try:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM source_conflicts").fetchone()[0])
            == 1
        )
    finally:
        connection.close()


def test_merge_and_append_lanes_shadow_canonically(tmp_path):
    from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event

    source_dir = tmp_path / "source-store"
    source_service = SentinelService(source_dir)
    foreign = source_service.record_event(
        mark_trusted_local_usage_import_event(_usage_event(event_id="evt_ignored")),
        trusted_usage_import=True,
    )

    target_dir = tmp_path / "target-store"
    target = SentinelService(target_dir, canonical_live_enabled=True)
    appended = target.append_events_preserving_identity([foreign])
    assert len(appended) == 1
    rows = _usage_rows(_live_db(target_dir))
    assert len(rows) == 1
    # The foreign event id is preserved in v1 AND keys the canonical fact
    # lane naturally on re-append (noop, no duplicates).
    again = target.append_events_preserving_identity([foreign])
    assert again == []
    assert len(_usage_rows(_live_db(target_dir))) == 1


# --- phase 3.4: integrated gates + health surfacing ---------------------------


def test_integrated_exact_replay_is_zero_physical_writes(tmp_path):
    """Spike gate 5 wired against the INTEGRATED writer: replaying every
    recorded v1 event through the shadow is zero canonical movement."""

    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    service.record_event(
        _observed_event(event_id="evt_x", session_id="p-1", created_at=1_000.0, kind="root")
    )
    service.record_event(_section_event(session_id="p-1", section_id="s-1"))
    service.record_event(
        _usage_event(event_id="evt_y", session_id="p-1", created_at=1_010.0)
    )
    db = _live_db(store_dir)
    sequence = _canonical_sequence(db)
    counts = _counts(db)
    usage = _usage_rows(db)

    def _all_rows(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }

    # A held reader connection sees PRAGMA data_version move whenever ANY
    # other connection commits a change — including in-place UPDATEs that
    # counts, sequence, and file size are all blind to.
    reader = sqlite3.connect(db)
    try:
        data_version = int(reader.execute("PRAGMA data_version").fetchone()[0])
        rows_before = _all_rows(reader)
        for recorded in service.list_all_events():
            result = service.canonical_live.shadow_v1_event(recorded)
            assert result.error is None
            assert result.written is False
        assert int(reader.execute("PRAGMA data_version").fetchone()[0]) == data_version
        assert _all_rows(reader) == rows_before
    finally:
        reader.close()
    assert _canonical_sequence(db) == sequence
    assert _counts(db) == counts
    assert _usage_rows(db) == usage


def test_integrated_priced_rescan_churn_collapses(tmp_path):
    """The churn gate against the real replace_events funnel: repeated
    identical-content usage batches produce zero canonical writes after the
    first, even across many rounds."""

    from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event

    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)

    def batch(cost: float) -> list[dict]:
        event = _usage_event(event_id="evt_ignored", cost=cost)
        del event["event_id"]
        return [event]

    service.replace_events(lambda existing: False, batch(0.01), trusted_usage_import=True)
    db = _live_db(store_dir)
    sequence = _canonical_sequence(db)
    for round_index in range(5):
        service.replace_events(
            lambda existing: str(existing.get("event_type")) == "model_usage",
            batch(0.01 + round_index),  # pricing-only drift
            trusted_usage_import=True,
        )
    assert _canonical_sequence(db) == sequence
    assert len(_usage_rows(db)) == 1


def test_finding_disposition_lane_flows_through_the_shadow_funnel(tmp_path):
    from agent_chronicle.work_ledger import build_evidence_events

    store_dir = tmp_path / "store"
    service = SentinelService(store_dir, canonical_live_enabled=True)
    service.record_event(
        {
            "source": "codex",
            "event_type": "machine_check",
            "metadata": {
                "sentinel_semantic_kind": "evidence",
                "client": "codex",
                "evidence_type": "security",
                "name": "Boundary probe",
                "result": "failed",
                "summary": "Boundary probe failed.",
            },
        }
    )
    target = next(
        event
        for event in build_evidence_events(service.list_all_events())
        if event.get("result") == "failed"
    )
    status_before = service.canonical_live.status()
    recorded = service.record_finding_disposition(
        target_event=target,
        action="mark_reviewed",
        expected_revision=0,
        note=None,
        idempotency_key="review-once",
    )
    status = service.canonical_live.status()
    # The contract is a VISIBLE SKIP, not merely "the funnel was called":
    # an erroring lane or one that started writing canonical rows for
    # dispositions must fail here.
    assert status["attempts"] == status_before["attempts"] + 1
    assert status["skipped"] == status_before["skipped"] + 1
    assert status["errors"] == status_before["errors"]
    assert status["written"] == status_before["written"]
    assert status["last_error"] is None
    assert recorded["event_type"] == "finding_disposition"


def test_health_endpoint_reports_canonical_live_status(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from agent_chronicle.api import create_local_api_app

    monkeypatch.setenv("AGENT_CHRONICLE_CANONICAL_LIVE_WRITE", "shadow")
    app = create_local_api_app(store_dir=tmp_path / "store")
    client = TestClient(app)
    payload = client.get("/health").json()
    assert payload["canonical_live"]["enabled"] is True
    assert set(payload["canonical_live"]) >= {
        "attempts",
        "written",
        "noop",
        "skipped",
        "deferred",
        "conflicts",
        "errors",
    }
