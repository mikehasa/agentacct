"""Service-level parity: the SQLite mirror tracks events.jsonl across writes.

The owner's step-3 gate for the JSON->SQLite cutover — proven at the service
level, across the real write paths (append, verbatim identity-append, and a
whole-file rewrite), not just the RawEventLog unit.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.service import SentinelService


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
