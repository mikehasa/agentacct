"""RawEventLog — the SQLite mirror that will replace events.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.event_log import RawEventLog, serialize_event


def _write_ledger(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(serialize_event(event) + "\n" for event in events), encoding="utf-8"
    )


def _event(event_id: str, event_type: str = "note", **extra) -> dict:
    return {"event_id": event_id, "event_type": event_type, "created_at": 1.0, **extra}


def test_append_and_read_preserve_order_and_content(tmp_path: Path) -> None:
    log = RawEventLog(tmp_path / "events.sqlite3")
    events = [_event("e1"), _event("e2", run_id="r1"), _event("e3")]
    for event in events:
        log.append_event(event)
    assert log.count() == 3
    assert log.read_events() == events
    assert log.read_events(run_id="r1") == [events[1]]
    # Verbatim lines match the canonical serialization exactly.
    assert log.read_lines() == [serialize_event(event) for event in events]


def test_reconcile_from_file_backfills_an_empty_log(tmp_path: Path) -> None:
    ledger = tmp_path / "events.jsonl"
    events = [_event(f"e{i}") for i in range(5)]
    _write_ledger(ledger, events)
    log = RawEventLog(tmp_path / "events.sqlite3")
    result = log.reconcile_from_file(ledger)
    assert result.matches
    assert result.file_lines == 5 == result.log_lines
    assert log.read_events() == events


def test_reconcile_fast_paths_a_pure_append(tmp_path: Path) -> None:
    ledger = tmp_path / "events.jsonl"
    events = [_event(f"e{i}") for i in range(3)]
    _write_ledger(ledger, events)
    log = RawEventLog(tmp_path / "events.sqlite3")
    log.reconcile_from_file(ledger)

    # Append two more lines to the file, then reconcile: only the tail is added.
    more = [_event("e3"), _event("e4")]
    with ledger.open("a", encoding="utf-8") as handle:
        for event in more:
            handle.write(serialize_event(event) + "\n")
    result = log.reconcile_from_file(ledger)
    assert result.matches
    assert log.count() == 5
    assert log.read_events() == events + more


def test_reconcile_rebuilds_after_a_whole_file_rewrite(tmp_path: Path) -> None:
    ledger = tmp_path / "events.jsonl"
    _write_ledger(ledger, [_event(f"e{i}") for i in range(4)])
    log = RawEventLog(tmp_path / "events.sqlite3")
    log.reconcile_from_file(ledger)

    # A redaction-style rewrite removes a line and changes another — no longer
    # a prefix of the old content.
    rewritten = [_event("e0"), _event("e2", note="redacted"), _event("e3")]
    _write_ledger(ledger, rewritten)
    result = log.reconcile_from_file(ledger)
    assert result.matches
    assert log.read_events() == rewritten


def test_replace_all_resets_seq_and_matches(tmp_path: Path) -> None:
    log = RawEventLog(tmp_path / "events.sqlite3")
    for i in range(3):
        log.append_event(_event(f"e{i}"))
    log.replace_all([serialize_event(_event("only"))])
    assert log.count() == 1
    assert log.read_events() == [_event("only")]


def test_verify_against_file_detects_divergence(tmp_path: Path) -> None:
    ledger = tmp_path / "events.jsonl"
    _write_ledger(ledger, [_event("e0"), _event("e1")])
    log = RawEventLog(tmp_path / "events.sqlite3")
    log.append_event(_event("e0"))
    log.append_event(_event("DIFFERENT"))
    result = log.verify_against_file(ledger)
    assert not result.matches
    assert result.first_divergence == 1


def test_corrupt_and_nonobject_lines_are_mirrored_verbatim(tmp_path: Path) -> None:
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        serialize_event(_event("e0")) + "\n"
        + "{not-valid-json\n"
        + "123\n"  # valid JSON but not an object
        + serialize_event(_event("e1")) + "\n",
        encoding="utf-8",
    )
    log = RawEventLog(tmp_path / "events.sqlite3")
    result = log.reconcile_from_file(ledger)
    assert result.matches  # all four non-blank lines mirrored verbatim
    assert log.count() == 4
    # Parsed reads skip the corrupt/non-object lines, exactly like the file reader.
    assert log.read_events() == [_event("e0"), _event("e1")]
