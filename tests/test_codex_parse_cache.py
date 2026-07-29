"""Codex rollout parse memoization (refresh performance).

The cache memoizes the expensive per-rollout parse keyed by the file's identity
(path, mtime_ns, size, inode) and a hash of the parsing code, so a hit is only
ever an identical re-parse of identical bytes with identical code. These tests
pin that the cache is output-equivalent (a hit yields the same event as a fresh
parse), invalidates on any file change, and fails open on a corrupt/disabled
cache — because this sits on the correctness-critical usage-truth path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import agentacct.client_usage as cu
from tests.test_client_usage import _codex_token_count, _make_codex_home


def _rollout(home: Path) -> Path:
    return next((home / "sessions").rglob("rollout-*.jsonl"))


def _event_total(event):
    return event.input_tokens + event.output_tokens + event.cached_input_tokens


def _event_signatures(events):
    return [event.to_sentinel_event()["metadata"] for event in events]


def test_cache_hit_yields_identical_events_and_reuses_the_parse(tmp_path):
    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"

    baseline = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
    with cu.codex_parse_cache_scope(store):
        cold = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
    assert (store / "cache" / "codex_rollout_parse.pkl").exists()

    with cu.codex_parse_cache_scope(store) as cache:
        warm = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
        assert cache is not None and cache.hits >= 1  # the rollout was served from cache

    # A hit is provably an identical re-parse: same events, cache or not.
    assert _event_signatures(baseline) == _event_signatures(cold) == _event_signatures(warm)


def test_cache_invalidates_and_reparses_when_the_file_changes(tmp_path):
    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"

    with cu.codex_parse_cache_scope(store):
        before = cu.discover_codex_usage(codex_home=home, limit_sessions=10)[0]
    before_total = _event_total(before)

    # Append a larger cumulative token event: the file's size (and content)
    # changes, so the cached parse must NOT be reused.
    roll = _rollout(home)
    roll.write_text(
        roll.read_text(encoding="utf-8")
        + json.dumps(
            _codex_token_count(
                "2026-06-27T00:03:00.000Z",
                {
                    "input_tokens": 9000,
                    "cached_input_tokens": 1000,
                    "output_tokens": 400,
                    "reasoning_output_tokens": 40,
                    "total_tokens": 9400,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with cu.codex_parse_cache_scope(store) as cache:
        after = cu.discover_codex_usage(codex_home=home, limit_sessions=10)[0]
        assert cache is not None and cache.misses >= 1  # changed file re-parsed
    # The new totals reflect the appended event, not a stale cached parse.
    assert _event_total(after) > before_total

    # And it matches a from-scratch parse with no cache.
    fresh = cu.discover_codex_usage(codex_home=home, limit_sessions=10)[0]
    assert _event_total(after) == _event_total(fresh)


def test_same_size_content_change_still_reparses_because_mtime_is_keyed(tmp_path):
    """A content edit that keeps the byte size identical (and keeps the inode,
    via in-place truncate+rewrite) must STILL invalidate: mtime is part of the
    key. This guards against a future narrowing of the key to (path, size,
    inode), which would serve a stale parse for same-size edits."""
    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"

    with cu.codex_parse_cache_scope(store):
        before = _event_signatures(cu.discover_codex_usage(codex_home=home, limit_sessions=10))

    roll = _rollout(home)
    original = roll.read_text(encoding="utf-8")
    # Swap one cumulative total for a different same-length value: identical file
    # size, identical inode (write_text truncates in place), different bytes.
    changed = original.replace('"input_tokens": 2500', '"input_tokens": 2400', 1)
    assert changed != original and len(changed) == len(original)
    roll.write_text(changed, encoding="utf-8")
    # Force mtime distinctly forward so the difference is unambiguous even on a
    # filesystem with coarse mtime resolution.
    st = roll.stat()
    os.utime(roll, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    with cu.codex_parse_cache_scope(store) as cache:
        after = _event_signatures(cu.discover_codex_usage(codex_home=home, limit_sessions=10))
        assert cache is not None and cache.misses >= 1  # keyed on mtime → a miss

    fresh = _event_signatures(cu.discover_codex_usage(codex_home=home, limit_sessions=10))
    assert after == fresh  # served the edited file, not a stale cached parse
    assert after != before  # and the edit is genuinely observable


def test_corrupt_cache_file_falls_back_to_a_full_parse(tmp_path):
    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"
    (store / "cache").mkdir(parents=True)
    (store / "cache" / "codex_rollout_parse.pkl").write_bytes(b"this is not a pickle")

    with cu.codex_parse_cache_scope(store):
        events = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
    assert events  # a corrupt cache never breaks the scan

    baseline = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
    assert _event_signatures(events) == _event_signatures(baseline)


def test_poisoned_cache_cannot_execute_code_on_load(tmp_path):
    """The parse cache stores only plain data, so unpickling refuses to build
    any class. A cache poisoned with a ``__reduce__`` code-execution payload
    must be rejected (falling back to a full parse), never executed."""
    import pickle

    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"
    cache_dir = store / "cache"
    cache_dir.mkdir(parents=True)
    marker = tmp_path / "pwned"

    class _Evil:
        def __reduce__(self):
            return (os.system, (f"touch {marker}",))

    with open(cache_dir / "codex_rollout_parse.pkl", "wb") as handle:
        pickle.dump(
            {"version": cu._codex_parser_code_version(), "entries": {"k": _Evil()}},
            handle,
        )

    with cu.codex_parse_cache_scope(store):
        events = cu.discover_codex_usage(codex_home=home, limit_sessions=10)

    assert not marker.exists()  # the __reduce__ payload never ran
    assert events  # and the scan still completed via a clean re-parse


def test_stale_code_version_is_never_trusted(tmp_path):
    import pickle

    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"
    cache_dir = store / "cache"
    cache_dir.mkdir(parents=True)
    # A cache file written by a different parser version must be ignored.
    poison_key = (str(_rollout(home)), 0, 0, 0)
    with open(cache_dir / "codex_rollout_parse.pkl", "wb") as handle:
        pickle.dump({"version": "stale-version", "entries": {poison_key: (None, {}, {})}}, handle)

    with cu.codex_parse_cache_scope(store):
        events = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
    # The stale entry was ignored, so the real rollout still produced its event.
    assert events and _event_total(events[0]) > 0


def test_cache_can_be_disabled_by_env(monkeypatch, tmp_path):
    home = _make_codex_home(tmp_path)
    store = tmp_path / "state"
    monkeypatch.setenv("AGENT_CHRONICLE_CODEX_PARSE_CACHE", "0")
    with cu.codex_parse_cache_scope(store) as cache:
        assert cache is None
        events = cu.discover_codex_usage(codex_home=home, limit_sessions=10)
    assert events
    assert not (store / "cache" / "codex_rollout_parse.pkl").exists()
