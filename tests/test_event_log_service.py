"""Service-level parity: the SQLite mirror tracks events.jsonl across writes.

The owner's step-3 gate for the JSON->SQLite cutover — proven at the service
level, across the real write paths (append, verbatim identity-append, and a
whole-file rewrite), not just the RawEventLog unit.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentacct.service import EventLogUnavailable, SentinelService


def _events_file(store_root: Path) -> Path:
    return store_root / "events.jsonl"


def _note(text: str) -> dict:
    return {"event_type": "note", "metadata": {"client": "cc", "client_session_id": "s1"}, "note": text}


def _parity(service: SentinelService) -> dict:
    return service.verify_event_log_parity()


def test_mirror_tracks_appends_and_serves_identical_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror mode: flat file authoritative
    service = SentinelService(tmp_path / "store")
    assert service.event_log is not None

    for i in range(4):
        service.record_event(_note(f"n{i}"))

    result = _parity(service)
    assert result["matches"], result
    assert result["file_lines"] == 4 == result["log_lines"]

    # The mirror serves exactly the events the flat file read serves.
    assert service.event_log.read_events() == service.list_all_events()


def test_mirror_tracks_verbatim_identity_appends(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror mode: flat file authoritative
    service = SentinelService(tmp_path / "store")
    service.record_event(_note("first"))

    verbatim = {
        "event_id": "evt_imported_1",
        "event_type": "section_completed",
        "created_at": 1_700_000_000.0,
        "metadata": {"client": "codex", "client_session_id": "s2", "sentinel_semantic_kind": "section"},
    }
    service.append_events_preserving_identity([verbatim])

    result = _parity(service)
    assert result["matches"], result
    assert service.event_log.read_events() == service.list_all_events()


def test_mirror_heals_after_a_whole_file_rewrite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror mode: flat file authoritative
    service = SentinelService(tmp_path / "store")
    for i in range(5):
        service.record_event(_note(f"n{i}"))
    service.list_all_events()  # sync the mirror
    assert _parity(service)["matches"]

    # Simulate a redaction: rewrite the ledger dropping and editing lines so it
    # is no longer a prefix of the mirrored content.
    events = service.list_all_events()
    rewritten = [events[0], {**events[2], "note": "[REDACTED]"}, events[4]]
    _events_file(service.store.root).write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in rewritten),
        encoding="utf-8",
    )

    # The next read heals the mirror wholesale.
    served = service.list_all_events()
    result = _parity(service)
    assert result["matches"], result
    assert result["file_lines"] == 3 == result["log_lines"]
    assert service.event_log.read_events() == served


def test_authoritative_mode_writes_to_the_log_and_survives_file_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    store_root = tmp_path / "store"
    store_root.mkdir()
    # A pre-existing flat ledger to cut over from (backfilled once on open).
    _events_file(store_root).write_text(
        json.dumps({"event_id": "evt_a", "event_type": "note", "created_at": 1.0}) + "\n",
        encoding="utf-8",
    )

    service = SentinelService(store_root)
    assert service._authoritative()
    assert service.event_log.count() == 1  # backfilled from the flat file

    # A new event goes to the LOG; the flat file is not touched.
    file_before = _events_file(store_root).read_text(encoding="utf-8")
    service.record_event(_note("in-auth"))
    assert _events_file(store_root).read_text(encoding="utf-8") == file_before
    assert service.event_log.count() == 2

    # The flat file can now be deleted and the store keeps working.
    _events_file(store_root).unlink()
    assert len(service.list_all_events()) == 2
    service.record_event(_note("post-delete"))
    assert not _events_file(store_root).exists()
    assert len(service.list_all_events()) == 3
    assert service.verify_event_log_parity()["authoritative"] is True


def test_event_snapshot_reuses_parsed_rows_and_invalidates_cross_process(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    store_root = tmp_path / "store"
    service = SentinelService(store_root)
    service.record_event(_note("first"))

    reads = 0
    original = service._read_events_file_order

    def counted_read(*, run_id=None):
        nonlocal reads
        reads += 1
        return original(run_id=run_id)

    monkeypatch.setattr(service, "_read_events_file_order", counted_read)
    first = service.all_events_snapshot()
    second = service.all_events_snapshot()

    assert first is second
    assert reads == 1
    assert len(first.events) == 1

    # A separate service simulates another MCP/CLI process writing the same
    # authoritative store. The persistent SQLite revision must invalidate the
    # first process without a restart or a full-ledger probe.
    writer = SentinelService(store_root)
    writer.record_event(_note("second"))

    refreshed = service.all_events_snapshot()
    assert refreshed is not first
    assert reads == 2
    assert len(refreshed.events) == 2


def test_event_snapshot_single_flights_concurrent_cold_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    service = SentinelService(tmp_path / "store")
    for index in range(25):
        service.record_event(_note(f"event-{index}"))

    reads = 0
    reads_lock = threading.Lock()
    original = service._read_events_file_order

    def slow_read(*, run_id=None):
        nonlocal reads
        with reads_lock:
            reads += 1
        time.sleep(0.03)
        return original(run_id=run_id)

    monkeypatch.setattr(service, "_read_events_file_order", slow_read)
    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = list(executor.map(lambda _: service.all_events_snapshot(), range(8)))

    assert reads == 1
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert len(snapshots[0].events) == 25


def test_authoritative_mode_rewrite_operates_on_the_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    store_root = tmp_path / "store"
    store_root.mkdir()
    service = SentinelService(store_root)
    for i in range(4):
        service.record_event(_note(f"n{i}"))
    _events_file(store_root).unlink(missing_ok=True)  # no flat file at all

    # Drive a whole-ledger rewrite (a redaction that drops two rows) through the
    # same primitives the redaction/replace paths use.
    with service._events_write_lock():
        parsed, preserved = service._partition_existing_for_rewrite()
        assert len(parsed) == 4
        service._write_event_partition_unlocked([parsed[0], parsed[2]], preserved)

    assert len(service.list_all_events()) == 2
    assert not _events_file(store_root).exists()


def test_env_adoption_persists_a_marker_so_a_no_env_reopen_does_not_wipe(tmp_path: Path, monkeypatch) -> None:
    # A store adopted via the env flag must persist a marker, so a later open in
    # explicit mirror mode (env=0) stays authoritative via the marker alone and
    # never mirror-reconciles the log back down to a stale/absent flat file (the
    # re-review's worst defect). Pinning the reopen to mirror mode proves the
    # marker — not the now-default authoritative mode — is what protects the log.
    store_root = tmp_path / "store"
    store_root.mkdir()
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    adopter = SentinelService(store_root)
    for i in range(5):
        adopter.record_event(_note(f"n{i}"))
    assert len(adopter.list_all_events()) == 5
    assert (store_root / "events.authoritative").exists()  # env adoption persisted a marker

    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    reopened = SentinelService(store_root)
    assert reopened._authoritative()  # the marker overrides the mirror-mode env
    assert len(reopened.list_all_events()) == 5  # nothing wiped


def test_adoption_fully_syncs_a_stale_mirror_log_before_freezing(tmp_path: Path, monkeypatch) -> None:
    # Adopting authoritative must sync the log FULLY from the still-authoritative
    # flat file first — a mirror log that is behind the file must not be frozen
    # (abandoning the file's un-absorbed tail) just because it is non-empty.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror-mode setup: flat file authoritative
    store_root = tmp_path / "store"
    store_root.mkdir()
    seeder = SentinelService(store_root)
    for i in range(5):
        seeder.record_event(_note(f"n{i}"))
    seeder.list_all_events()  # log synced to 5

    # Append 3 more lines straight to the flat file so the log is stale (5 < 8).
    with _events_file(store_root).open("a", encoding="utf-8") as handle:
        for i in range(5, 8):
            handle.write(json.dumps({"event_id": f"tail{i}", "event_type": "note", "created_at": float(i)}) + "\n")

    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    adopted = SentinelService(store_root)
    assert adopted._authoritative()
    assert len(adopted.list_all_events()) == 8  # the stale tail was absorbed, not lost
    assert adopted.event_log.count() == 8


def test_a_cutover_store_with_a_lost_marker_self_heals(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror-mode setup: flat file authoritative
    store_root = tmp_path / "store"
    store_root.mkdir()
    service = SentinelService(store_root)
    for i in range(6):
        service.record_event(_note(f"n{i}"))
    service.list_all_events()
    service.mark_authoritative()
    _events_file(store_root).unlink()          # cut over
    (store_root / "events.authoritative").unlink()  # marker lost

    # No marker, no flat file, but a populated log → inferred authoritative and
    # the marker is re-written; the log is preserved, reads are correct.
    healed = SentinelService(store_root)
    assert healed._authoritative()
    assert (store_root / "events.authoritative").exists()
    assert len(healed.list_all_events()) == 6


def test_a_cutover_store_missing_its_log_db_fails_loud(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror-mode setup: flat file authoritative
    store_root = tmp_path / "store"
    store_root.mkdir()
    service = SentinelService(store_root)
    for i in range(4):
        service.record_event(_note(f"n{i}"))
    service.list_all_events()
    service.mark_authoritative()
    _events_file(store_root).unlink()
    (store_root / "events.sqlite3").unlink()  # the sole ledger DB is gone

    # Must raise, never silently create a fresh empty log and serve an empty store.
    with pytest.raises(EventLogUnavailable):
        SentinelService(store_root)


def test_a_fresh_service_backfills_the_mirror_from_an_existing_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror mode: the flat ledger is authoritative
    store_root = tmp_path / "store"
    # Prime a service and write events, then drop the mirror db to simulate a
    # store that has a ledger but no SQLite mirror yet (the real XDG store).
    primer = SentinelService(store_root)
    for i in range(6):
        primer.record_event(_note(f"n{i}"))
    primer.list_all_events()
    (store_root / "events.sqlite3").unlink()

    # A fresh service backfills the mirror from the flat ledger on open.
    reopened = SentinelService(store_root)
    result = _parity(reopened)
    assert result["matches"], result
    assert result["log_lines"] == 6


def test_a_lost_marker_does_not_truncate_a_log_that_leads_its_frozen_backup(
    tmp_path: Path, monkeypatch
) -> None:
    # An adopted store keeps events.jsonl as a FROZEN backup while the SQLite log
    # keeps growing. If the authoritative marker is later lost (a swallowed
    # mark_authoritative error, or the marker deleted out of band) the store is
    # still opened authoritative by default and re-enters adoption's reconcile
    # with the log AHEAD of the frozen file. That reconcile must PRESERVE the
    # leading log, never rebuild it down to the shorter backup (silent data loss).
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror-mode seed
    store_root = tmp_path / "store"
    store_root.mkdir()
    seeder = SentinelService(store_root)
    for i in range(3):
        seeder.record_event(_note(f"old{i}"))
    seeder.list_all_events()  # events.jsonl now has 3 lines

    # Adopt under the default, then grow the log to 8 while events.jsonl stays 3.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    adopted = SentinelService(store_root)
    assert adopted._authoritative()
    for i in range(5):
        adopted.record_event(_note(f"new{i}"))
    assert len(adopted.list_all_events()) == 8
    # events.jsonl stays the frozen 3-line backup; the 5 new records went to the log.
    assert len(_events_file(store_root).read_text(encoding="utf-8").splitlines()) == 3

    # Marker lost; reopen at the product default re-enters adoption with the log
    # LEADING the frozen file. absorb must neither truncate the log down to the
    # 3-line backup nor resurrect anything (all 3 ids are already known).
    (store_root / "events.authoritative").unlink()
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    reopened = SentinelService(store_root)
    assert reopened._authoritative()
    assert len(reopened.list_all_events()) == 8  # the 5 post-adoption events survive
    assert (store_root / "events.authoritative").exists()  # marker self-healed


def test_an_empty_but_present_events_file_does_not_wipe_a_populated_log(
    tmp_path: Path, monkeypatch
) -> None:
    # The no-wipe guard must cover an events.jsonl that EXISTS yet is empty, not
    # only a missing file: an out-of-band truncation of the frozen backup must
    # not empty the authoritative log on the next adoption reconcile.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    store_root = tmp_path / "store"
    store_root.mkdir()
    service = SentinelService(store_root)
    for i in range(4):
        service.record_event(_note(f"n{i}"))
    assert len(service.list_all_events()) == 4

    # Truncate the backup to empty, drop the marker, reopen at the default.
    _events_file(store_root).write_text("", encoding="utf-8")
    (store_root / "events.authoritative").unlink()
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    reopened = SentinelService(store_root)
    assert len(reopened.list_all_events()) == 4  # not wiped by the empty backup


def test_authoritative_open_absorbs_straggler_events_jsonl_writes_from_an_old_process(
    tmp_path: Path, monkeypatch
) -> None:
    # Rolling upgrade (0.5.x -> SQLite default): after a store cuts over to the
    # log, a not-yet-restarted OLD mirror-mode process keeps appending to
    # events.jsonl. Those straggler events must be drained into the authoritative
    # log (a union by event_id), so writes never split between the two ledgers and
    # no post-cutover event is stranded.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # existing mirror-mode store
    store_root = tmp_path / "store"
    store_root.mkdir()
    seeder = SentinelService(store_root)
    for i in range(3):
        seeder.record_event(_note(f"old{i}"))
    seeder.list_all_events()  # events.jsonl has 3

    # New code adopts and records 2 events straight to the log.
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    new_process = SentinelService(store_root)
    assert new_process._authoritative()
    for i in range(2):
        new_process.record_event(_note(f"new{i}"))
    assert len(new_process.list_all_events()) == 5
    # events.jsonl is left INTACT (a backup, not mutated by adoption); the new
    # records went to the log, not the file.
    assert len(_events_file(store_root).read_text(encoding="utf-8").splitlines()) == 3

    # An OLD process, unaware of the marker/log, writes stragglers to events.jsonl.
    with _events_file(store_root).open("a", encoding="utf-8") as handle:
        for i in range(2):
            handle.write(
                json.dumps({"event_id": f"strag{i}", "event_type": "note", "created_at": 100.0 + i}) + "\n"
            )

    # Any new-code open/read drains the stragglers into the log — nothing split —
    # while leaving events.jsonl itself untouched.
    reader = SentinelService(store_root)
    events = reader.list_all_events()
    ids = {event.get("event_id") for event in events}
    assert len(events) == 7
    assert {"strag0", "strag1"} <= ids
    assert len(_events_file(store_root).read_text(encoding="utf-8").splitlines()) == 5
    # Idempotent: a second read does not re-absorb / duplicate.
    assert len(SentinelService(store_root).list_all_events()) == 7


def test_a_reconcile_adopted_event_removed_by_a_rewrite_is_not_resurrected(
    tmp_path: Path, monkeypatch
) -> None:
    # An event adopted into the log via mirror-mode reconcile (how EVERY existing
    # store upgrades) has no absorbed_flat key of its own. After the flip to
    # authoritative, the first absorb must record every retained-backup event's
    # key, so that when a later rewrite (redaction / dedup / supersession) removes
    # one, a subsequent absorb does NOT resurrect it from the frozen events.jsonl.
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # mirror: reconcile adopts, absorbed_flat empty
    store_root = tmp_path / "store"
    store_root.mkdir()
    seeder = SentinelService(store_root)
    for i in range(3):
        seeder.record_event(_note(f"keep{i}"))
    events = seeder.list_all_events()  # log mirrors 3 via reconcile; absorbed_flat empty
    victim_id = events[1]["event_id"]

    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    service = SentinelService(store_root)  # adoption absorb records all 3 keys
    assert service._authoritative()

    # An authoritative rewrite drops the middle event from the LOG; the retained
    # events.jsonl backup still holds it.
    with service._events_write_lock():
        parsed, preserved = service._partition_existing_for_rewrite()
        kept = [event for event in parsed if event.get("event_id") != victim_id]
        service._write_event_partition_unlocked(kept, preserved)
    assert victim_id not in {event.get("event_id") for event in service.list_all_events()}

    # A fresh process re-absorbs the retained backup — the removed event must NOT
    # come back (its key is in absorbed_flat from adoption).
    reopened = SentinelService(store_root)
    ids = {event.get("event_id") for event in reopened.list_all_events()}
    assert victim_id not in ids
    assert len(reopened.list_all_events()) == 2


def test_a_straggler_written_after_open_is_drained_on_the_next_read(
    tmp_path: Path, monkeypatch
) -> None:
    # A long-lived authoritative process must pick up a straggler an old process
    # writes to events.jsonl AFTER it opened — the init-time change stamp is
    # captured under the lock, so a later write changes the signature and the
    # next read drains it (rather than being masked as already-synced).
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")  # existing mirror-mode store
    store_root = tmp_path / "store"
    store_root.mkdir()
    seeder = SentinelService(store_root)
    for i in range(2):
        seeder.record_event(_note(f"old{i}"))
    seeder.list_all_events()

    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    service = SentinelService(store_root)  # adopt; init stamp captured under the lock
    assert len(service.list_all_events()) == 2

    # An old process appends a straggler after this service opened.
    with _events_file(store_root).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event_id": "strag", "event_type": "note", "created_at": 9.0}) + "\n")

    # The next read on the SAME service drains it (signature changed).
    events = service.list_all_events()
    assert len(events) == 3
    assert "strag" in {event.get("event_id") for event in events}


def test_a_create_false_probe_of_a_nonexistent_store_is_empty_not_a_failure(
    tmp_path: Path,
) -> None:
    # Probing a not-yet-created store (create=False) under the authoritative
    # default must read as EMPTY and never raise EventLogUnavailable — otherwise
    # `agentacct setup instructions` / `hooks install`, which existence-probe
    # their target store before recording, crash when it does not exist yet.
    missing = tmp_path / "never-created"
    service = SentinelService(missing, create=False)
    assert service.list_all_events() == []
    assert service._authoritative() is False
    assert not missing.exists()  # the probe created nothing
