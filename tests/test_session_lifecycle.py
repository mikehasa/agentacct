"""Ambient session-end capture: spool -> drain -> event, and the join helper."""

from __future__ import annotations

import json
from pathlib import Path

from agentacct.session_lifecycle import (
    SESSION_END_EVENT_TYPE,
    build_session_end_by_session,
    drain_session_end_spool,
    ingest_session_end_spool,
    record_session_end_tick,
    session_end_spool_path,
)


def test_spool_drain_roundtrip_latest_end_wins(tmp_path: Path) -> None:
    record_session_end_tick(tmp_path, client="claude-code", session_id="s1", reason="logout", at=100.0)
    record_session_end_tick(tmp_path, client="claude-code", session_id="s1", reason="clear", at=200.0)
    record_session_end_tick(tmp_path, client="claude-code", session_id="s2", at=150.0)
    events = drain_session_end_spool(tmp_path, now=300.0)
    by = {e["metadata"]["client_session_id"]: e for e in events}
    assert by["s1"]["event_type"] == SESSION_END_EVENT_TYPE
    assert by["s1"]["metadata"]["ended_at"] == 200.0  # latest wins
    assert by["s1"]["metadata"]["reason"] == "clear"  # the latest end's reason
    assert by["s2"]["metadata"]["ended_at"] == 150.0
    assert "reason" not in by["s2"]["metadata"]  # honest gap, no fabricated reason
    # spool is consumed; a second drain sees nothing.
    assert drain_session_end_spool(tmp_path) == []


def test_capture_is_content_free(tmp_path: Path) -> None:
    record_session_end_tick(tmp_path, client="claude-code", session_id="s", reason="other", at=1.0)
    spool_text = session_end_spool_path(tmp_path).read_text(encoding="utf-8")
    # only the tiny scalars {c,s,t}[,r] — never any payload
    assert set(json.loads(spool_text.strip())) <= {"c", "s", "t", "r"}
    [event] = drain_session_end_spool(tmp_path, now=2.0)
    blob = json.dumps(event)
    for forbidden in ("prompt", "message", "tool_input", "transcript", "content", "cwd"):
        assert forbidden not in blob


def test_missing_client_or_session_is_dropped(tmp_path: Path) -> None:
    record_session_end_tick(tmp_path, client="", session_id="s", at=1.0)
    record_session_end_tick(tmp_path, client="claude-code", session_id="", at=1.0)
    assert drain_session_end_spool(tmp_path) == []


def test_build_session_end_by_session_latest_wins() -> None:
    events = [
        {"event_type": SESSION_END_EVENT_TYPE, "created_at": 100.0, "source": "claude-code",
         "metadata": {"client": "claude-code", "client_session_id": "s1", "ended_at": 100.0}},
        {"event_type": SESSION_END_EVENT_TYPE, "created_at": 200.0, "source": "claude-code",
         "metadata": {"client": "claude-code", "client_session_id": "s1", "ended_at": 200.0}},
        {"event_type": "model_usage", "metadata": {"client": "claude-code", "client_session_id": "s1"}},
        # falls back to created_at when ended_at is absent
        {"event_type": SESSION_END_EVENT_TYPE, "created_at": 150.0,
         "metadata": {"client": "claude-code", "client_session_id": "s2"}},
    ]
    result = build_session_end_by_session(events)
    assert result[("claude-code", "s1")] == 200.0
    assert result[("claude-code", "s2")] == 150.0
    assert ("claude-code", "nope") not in result


def _end(sid, t):
    # created_at (record/import time) deliberately later than ended_at, as in the
    # real store, to prove the activity comparison uses ended_at, not created_at.
    return {"event_type": SESSION_END_EVENT_TYPE, "created_at": t + 999,
            "metadata": {"client": "claude-code", "client_session_id": sid, "ended_at": t}}


def _act(sid, t, typ="model_usage"):
    return {"event_type": typ, "created_at": t, "metadata": {"client": "claude-code", "client_session_id": sid}}


def test_end_that_is_the_sessions_last_word_is_authoritative() -> None:
    assert build_session_end_by_session([_act("s1", 100), _end("s1", 200)]) == {("claude-code", "s1"): 200.0}


def test_post_end_activity_supersedes_a_stale_end_so_a_resumed_session_is_live() -> None:
    # THE HONESTY GUARD (review finding): claude --resume reuses the session id, so
    # a stored end alone is not death. Any activity AFTER the end (the resumed
    # session's own usage/tool work) supersedes the end -> not returned -> the
    # reducer never infers ended_open for a live, resumed session.
    assert build_session_end_by_session([_act("s1", 100), _end("s1", 200), _act("s1", 300)]) == {}
    # tool activity (not just usage) also supersedes
    assert build_session_end_by_session([_end("s1", 200), _act("s1", 300, typ="tool_activity_observed")]) == {}


def test_a_session_that_ends_again_after_resuming_keeps_the_later_end() -> None:
    got = build_session_end_by_session([_end("s1", 200), _act("s1", 300), _end("s1", 400)])
    assert got == {("claude-code", "s1"): 400.0}


def test_an_end_for_one_session_never_appears_for_another(tmp_path: Path) -> None:
    # Cross-session: session A ended; session B is live (only activity, no end).
    # The map must key strictly on (client, session) so B is never stamped.
    got = build_session_end_by_session([_end("A", 200), _act("B", 300)])
    assert got == {("claude-code", "A"): 200.0}
    assert ("claude-code", "B") not in got


def test_ingest_records_each_event(tmp_path: Path) -> None:
    record_session_end_tick(tmp_path, client="claude-code", session_id="s1", at=1.0)
    record_session_end_tick(tmp_path, client="claude-code", session_id="s2", at=2.0)
    recorded: list[dict] = []
    n = ingest_session_end_spool(tmp_path, record=recorded.append, now=3.0)
    assert n == 2 and len(recorded) == 2
    assert all(e["event_type"] == SESSION_END_EVENT_TYPE for e in recorded)
