"""Service-level parity: the SQLite mirror tracks events.jsonl across writes.

The owner's step-3 gate for the JSON->SQLite cutover — proven at the service
level, across the real write paths (append, verbatim identity-append, and a
whole-file rewrite), not just the RawEventLog unit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentacct.service import EventLogUnavailable, SentinelService


def _events_file(store_root: Path) -> Path:
    return store_root / "events.jsonl"


def _note(text: str) -> dict:
    return {"event_type": "note", "metadata": {"client": "cc", "client_session_id": "s1"}, "note": text}


def _parity(service: SentinelService) -> dict:
    return service.verify_event_log_parity()


def test_mirror_tracks_appends_and_serves_identical_events(tmp_path: Path) -> None:
    service = SentinelService(tmp_path / "store")
    assert service.event_log is not None

    for i in range(4):
        service.record_event(_note(f"n{i}"))

    result = _parity(service)
    assert result["matches"], result
    assert result["file_lines"] == 4 == result["log_lines"]

    # The mirror serves exactly the events the flat file read serves.
    assert service.event_log.read_events() == service.list_all_events()


def test_mirror_tracks_verbatim_identity_appends(tmp_path: Path) -> None:
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


def test_mirror_heals_after_a_whole_file_rewrite(tmp_path: Path) -> None:
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
    # A store adopted via the env flag must persist a marker, so a later open
    # WITHOUT the env var stays authoritative and never mirror-reconciles the
    # log back down to a stale/absent flat file (the re-review's worst defect).
    store_root = tmp_path / "store"
    store_root.mkdir()
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "1")
    adopter = SentinelService(store_root)
    for i in range(5):
        adopter.record_event(_note(f"n{i}"))
    assert len(adopter.list_all_events()) == 5
    assert (store_root / "events.authoritative").exists()  # env adoption persisted a marker

    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)
    reopened = SentinelService(store_root)
    assert reopened._authoritative()
    assert len(reopened.list_all_events()) == 5  # nothing wiped


def test_adoption_fully_syncs_a_stale_mirror_log_before_freezing(tmp_path: Path, monkeypatch) -> None:
    # Adopting authoritative must sync the log FULLY from the still-authoritative
    # flat file first — a mirror log that is behind the file must not be frozen
    # (abandoning the file's un-absorbed tail) just because it is non-empty.
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)  # mirror-mode setup
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
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)  # mirror-mode setup
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
    monkeypatch.delenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", raising=False)  # mirror-mode setup
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


def test_a_fresh_service_backfills_the_mirror_from_an_existing_ledger(tmp_path: Path) -> None:
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
