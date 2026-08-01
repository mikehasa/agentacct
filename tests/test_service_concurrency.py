"""Cross-process safety of the events.jsonl writers (Phase 1 review finding 2).

replace_events used to snapshot-read -> write a SHARED fixed tmp name ->
rename, so (a) an event appended between read and rename was silently deleted
and (b) two concurrent rewrites interleaved bytes into the promoted file.
Both writers now serialize on an fcntl.flock advisory lock (events.jsonl.lock,
POSIX-only like the product) and every rewrite uses a per-writer mkstemp temp
file. flock is per open-file-description, so two SentinelService instances in
one process contend exactly like two processes do — threads are a faithful
regression vehicle here.
"""

from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

from agentacct.service import SentinelService


def _usage_event(session: str, *, source: str = "codex-local-session-import") -> dict:
    return {
        "source": source,
        "event_type": "model_usage",
        "provider": "codex",
        "model": "gpt-5.5",
        "estimated_input_tokens": 10,
        "estimated_output_tokens": 5,
        "metadata": {"client": "codex", "client_session_id": session},
    }


def _section_event(section_id: str) -> dict:
    return {
        "source": "codex",
        "event_type": "section_completed",
        "metadata": {
            "sentinel_semantic_kind": "section",
            "section_id": section_id,
            "section_status": "completed",
            "client": "codex",
            "client_session_id": "concurrent-session",
        },
    }


def test_concurrent_append_during_replace_is_not_lost(tmp_path: Path) -> None:
    """A section recorded while another writer is mid-replace must survive.

    The replacing writer is held inside its critical section (should_replace
    blocks after signalling); the appender starts while the replace is
    provably in flight. Pre-fix, the appended section landed on the old inode
    and the rename deleted it."""
    store = tmp_path / "state"
    replacer = SentinelService(store)
    appender = SentinelService(store)
    replacer.record_event(_usage_event("seed-session"))

    in_critical = threading.Event()
    replace_error: list[BaseException] = []

    def should_replace(event: dict) -> bool:
        in_critical.set()
        time.sleep(0.8)
        return event.get("event_type") == "model_usage"

    def run_replace() -> None:
        try:
            replacer.replace_events(should_replace, [_usage_event("replacement-session")])
        except BaseException as exc:  # noqa: BLE001 - surface in the main thread.
            replace_error.append(exc)

    thread = threading.Thread(target=run_replace)
    thread.start()
    try:
        assert in_critical.wait(timeout=10), "replace_events never entered its critical section"
        recorded = appender.record_event(_section_event("mid-replace-work"))
    finally:
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert not replace_error, replace_error

    final_events = replacer.list_all_events()
    event_ids = {event.get("event_id") for event in final_events}
    assert recorded["event_id"] in event_ids, "section appended during replace_events was lost"
    sessions = {
        event.get("metadata", {}).get("client_session_id")
        for event in final_events
        if event.get("event_type") == "model_usage"
    }
    assert sessions == {"replacement-session"}


def test_concurrent_replace_events_do_not_corrupt_or_leave_tmp_debris(tmp_path: Path, monkeypatch) -> None:
    """Two writers looping replace_events (plus one appender) on one store:
    every iteration completes, the final file is valid JSONL, appended
    sections all survive, and no per-writer temp files are left behind.

    Asserts on the flat file's atomic-rewrite temp-file debris — flat-file
    mechanics only present in legacy mirror mode."""
    monkeypatch.setenv("AGENTACCT_EVENT_LOG_AUTHORITATIVE", "0")
    store = tmp_path / "state"
    writer_a = SentinelService(store)
    writer_b = SentinelService(store)
    appender = SentinelService(store)
    errors: list[BaseException] = []

    def replace_worker(service: SentinelService, session: str) -> None:
        try:
            for _ in range(15):
                service.replace_events(
                    lambda event, session=session: event.get("metadata", {}).get("client_session_id") == session,
                    [_usage_event(session)],
                )
        except BaseException as exc:  # noqa: BLE001 - assert in the main thread.
            errors.append(exc)

    def append_worker() -> None:
        try:
            for index in range(20):
                appender.record_event(_section_event(f"work-{index}"))
        except BaseException as exc:  # noqa: BLE001 - assert in the main thread.
            errors.append(exc)

    threads = [
        threading.Thread(target=replace_worker, args=(writer_a, "writer-a")),
        threading.Thread(target=replace_worker, args=(writer_b, "writer-b")),
        threading.Thread(target=append_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors, errors

    events_path = store / "events.jsonl"
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]  # raises on torn/interleaved bytes
    section_ids = {
        event["metadata"]["section_id"]
        for event in parsed
        if event.get("event_type") == "section_completed"
    }
    assert section_ids == {f"work-{index}" for index in range(20)}
    usage_sessions = sorted(
        event["metadata"]["client_session_id"] for event in parsed if event.get("event_type") == "model_usage"
    )
    assert usage_sessions == ["writer-a", "writer-b"]
    debris = [path for path in store.iterdir() if ".tmp" in path.name]
    assert debris == []


def test_events_lock_is_created_owner_only_and_store_root_private(tmp_path: Path) -> None:
    """The events lock must never inherit the umask (0644 leaked group/other
    read AND hard-failed the rebuild suite's exact-0600 writer-lock gates)."""

    store = tmp_path / "state"
    service = SentinelService(store)
    service.record_event(_usage_event("perm-check"))

    lock = store / "events.jsonl.lock"
    assert lock.exists()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.stat().st_mode) == 0o700


def test_preexisting_loose_events_lock_self_heals_to_0600(tmp_path: Path) -> None:
    store = tmp_path / "state"
    service = SentinelService(store)
    lock = store / "events.jsonl.lock"
    lock.touch()
    lock.chmod(0o644)

    service.record_event(_usage_event("heal-check"))

    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
