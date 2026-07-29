"""Phase 4.1 — canonical read flag, cached read-only accessor, usage-days surface.

Conventions follow test_canonical_live_writer.py: stores are built in
tmp_path via the runtime/importer, never against real paths; the read flag is
pinned off suite-wide by conftest and exercised here through the constructor
override or explicit monkeypatch.setenv.

The "promoted" fixtures flip an import-produced candidate's store_role to
'live' by direct UPDATE and place it at the reserved name — the sanctioned
promote operation is phase-5 work; these tests intentionally pre-empt only
its mechanical effect so the reader can be validated against real imported
data today (the framing decision recorded in the migration plan doc).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentacct.activation import RUNTIME_ENV_ALLOWLIST
from agentacct.canonical_read import (
    CANONICAL_READ_ENV,
    CanonicalReadRuntime,
    CanonicalReadUnavailable,
    canonical_read_enabled,
)
from agentacct.canonical_live import CANONICAL_LIVE_ENV, canonical_live_write_enabled
from agentacct.usage_truth import mark_trusted_local_usage_import_event

from test_canonical_live_writer import _run_importer, _usage_event

LIVE_STORE_FILENAME = "chronicle.sqlite3"


# --- store builders --------------------------------------------------------


def _promote_candidate_to_live(candidate: Path, store_root: Path) -> Path:
    """Place an import-produced candidate at the reserved live name.

    Mechanically what the phase-5 promote op will do under sanction: flip
    store_metadata.store_role and give the file the identity open_live
    demands (reserved name, 0600, regular file).
    """

    store_root.mkdir(parents=True, exist_ok=True)
    target = store_root / LIVE_STORE_FILENAME
    shutil.copy2(candidate, target)
    connection = sqlite3.connect(target)
    try:
        connection.execute("UPDATE store_metadata SET store_role = 'live' WHERE singleton = 1")
        connection.commit()
    finally:
        connection.close()
    os.chmod(target, 0o600)
    return target


def _imported_live_store(
    tmp_path: Path, events: list[dict[str, Any]], *, name: str = "reader"
) -> Path:
    """Import ``events`` into a candidate and promote it into ``store_root``."""

    candidate = _run_importer(tmp_path, events, name=name)
    store_root = tmp_path / f"store-{name}"
    _promote_candidate_to_live(candidate, store_root)
    return store_root


def _undated_usage_event(*, event_id: str, session_id: str = "u-undated") -> dict[str, Any]:
    """A usage event whose every timestamp is absent → epoch-0 sentinel day."""

    event = _usage_event(event_id=event_id, session_id=session_id)
    event.pop("created_at", None)
    event["metadata"] = {
        key: value
        for key, value in event["metadata"].items()
        if key not in {"updated_at", "started_at"}
    }
    return event


# --- flag semantics --------------------------------------------------------


def test_read_flag_default_off():
    assert canonical_read_enabled() is False
    runtime = CanonicalReadRuntime(Path("/tmp/nonexistent-store-root"))
    assert runtime.enabled is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_read_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv(CANONICAL_READ_ENV, value)
    assert canonical_read_enabled() is True


@pytest.mark.parametrize("value", ["0", "off", "false", "", "   ", "shadow"])
def test_read_flag_non_truthy_values(monkeypatch, value):
    """'shadow' is a WRITE-mode word; the read flag deliberately rejects it."""

    monkeypatch.setenv(CANONICAL_READ_ENV, value)
    assert canonical_read_enabled() is False


def test_read_flag_legacy_alias_accepted_and_new_name_wins(monkeypatch):
    monkeypatch.setenv("AGENT_SENTINEL_CANONICAL_READ", "1")
    assert canonical_read_enabled() is True
    monkeypatch.setenv(CANONICAL_READ_ENV, "0")
    assert canonical_read_enabled() is False


def test_read_flag_pair_is_forwarded_to_managed_runtime_children():
    assert "AGENT_CHRONICLE_CANONICAL_READ" in RUNTIME_ENV_ALLOWLIST
    assert "AGENT_SENTINEL_CANONICAL_READ" in RUNTIME_ENV_ALLOWLIST


def test_read_and_live_write_flags_remain_independent(monkeypatch):
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "shadow")
    assert canonical_live_write_enabled() is True
    assert canonical_read_enabled() is False

    monkeypatch.delenv(CANONICAL_LIVE_ENV)
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    assert canonical_read_enabled() is True
    assert canonical_live_write_enabled() is False


def test_runtime_constructor_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    assert CanonicalReadRuntime(tmp_path, enabled=False).enabled is False
    monkeypatch.delenv(CANONICAL_READ_ENV)
    assert CanonicalReadRuntime(tmp_path, enabled=True).enabled is True


# --- unavailability taxonomy ----------------------------------------------


def _read_days(runtime: CanonicalReadRuntime, **kwargs: Any):
    defaults: dict[str, Any] = {"start_day": "1970-01-02", "end_day": "9999-12-31"}
    defaults.update(kwargs)
    return runtime.usage_day_read(**defaults)


def test_read_unavailable_when_disabled(tmp_path):
    runtime = CanonicalReadRuntime(tmp_path, enabled=False)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "read_flag_disabled"


def test_read_unavailable_when_store_absent(tmp_path):
    runtime = CanonicalReadRuntime(tmp_path, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "store_absent"
    status = runtime.status()
    assert status["enabled"] is True
    assert status["attempts"] == 1
    assert status["served"] == 0
    assert status["unavailable"] == 1
    assert status["last_unavailable_reason"] == "store_absent"


def test_read_unavailable_wrong_permissions(tmp_path):
    store_root = _imported_live_store(tmp_path, [_usage_event(event_id="evt_u1")], name="perm")
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o644)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "store_permissions"


def test_read_unavailable_candidate_role_store(tmp_path):
    """A candidate that was never promoted must be refused, not served."""

    candidate = _run_importer(tmp_path, [_usage_event(event_id="evt_u1")], name="role")
    store_root = tmp_path / "store-role"
    store_root.mkdir()
    target = store_root / LIVE_STORE_FILENAME
    shutil.copy2(candidate, target)
    os.chmod(target, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "store_refused"
    assert runtime.status()["last_unavailable_reason"] == "store_refused"


def test_read_unavailable_older_schema_says_upgrade(tmp_path):
    store_root = _imported_live_store(tmp_path, [_usage_event(event_id="evt_u1")], name="old")
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "store_refused"
    assert "writable" in excinfo.value.detail


def test_read_unavailable_never_built_projection(tmp_path):
    """The live shadow store shape: canonical rows exist (or not), but the
    read model was never rebuilt — an empty rm_usage_day must not
    impersonate "no usage"."""

    from agentacct.canonical.sqlite import CanonicalStore

    store_root = tmp_path / "store-shadow"
    store_root.mkdir()
    CanonicalStore.create_live(store_root).close()
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "projection_never_built"


# --- served reads, labels, caching -----------------------------------------


def test_usage_day_read_serves_rows_with_fresh_projection(tmp_path):
    events = [
        _usage_event(
            event_id="evt_u1", session_id="s-1", created_at=1_700_000_000.0, input_tokens=100
        ),
        _usage_event(
            event_id="evt_u2", session_id="s-2", created_at=1_700_100_000.0, input_tokens=40
        ),
    ]
    store_root = _imported_live_store(tmp_path, events, name="serve")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = _read_days(runtime)
    assert read.rows, "imported usage must surface as rm_usage_day rows"
    assert read.undated_rows == [] or read.undated_rows == ()
    assert read.projection["stale"] is False
    assert read.projection["pending_writes"] == 0
    assert read.store["store_role"] == "live"
    assert read.day_basis == "utc"
    assert read.truncated is False
    status = runtime.status()
    assert status["served"] == 1
    assert status["unavailable"] == 0


def test_usage_day_read_stale_projection_is_labeled_not_refused(tmp_path):
    from agentacct.canonical.sqlite import CanonicalStore

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1")], name="stale"
    )
    store = CanonicalStore.open_live(store_root)
    try:
        repository = store.repository()
        with store.transaction(write=True):
            repository._advance_sequence(store.connection)
    finally:
        store.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = _read_days(runtime)
    assert read.projection["stale"] is True
    assert read.projection["pending_writes"] >= 1


def test_store_cached_per_thread_and_reopened_on_identity_change(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1")], name="cache"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    _read_days(runtime)
    first_store = runtime._thread_local.cache.store
    _read_days(runtime)
    assert runtime._thread_local.cache.store is first_store, "same inode must reuse the handle"
    # Replace the file (new inode) — the promotion shape.
    target = store_root / LIVE_STORE_FILENAME
    replacement = tmp_path / "replacement.sqlite3"
    shutil.copy2(target, replacement)
    target.unlink()
    shutil.copy2(replacement, target)
    os.chmod(target, 0o600)
    read = _read_days(runtime)
    assert runtime._thread_local.cache.store is not first_store, "new inode must reopen"
    assert read.rows


def test_model_probe_distinguishes_unknown_model_from_empty_range(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1")], name="model"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    known = _read_days(runtime, model="gpt-test")
    assert known.model_matches_store is True
    unknown = _read_days(runtime, model="never-seen")
    assert unknown.model_matches_store is False
    assert not unknown.rows


def test_truncation_is_labeled(tmp_path, monkeypatch):
    import agentacct.canonical_read as canonical_read_module

    events = [
        _usage_event(event_id="evt_u1", session_id="s-1", created_at=1_700_000_000.0),
        _usage_event(event_id="evt_u2", session_id="s-2", created_at=1_700_100_000.0),
    ]
    store_root = _imported_live_store(tmp_path, events, name="trunc")
    monkeypatch.setattr(canonical_read_module, "USAGE_DAYS_QUERY_LIMIT", 1)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = _read_days(runtime)
    assert len(read.rows) == 1
    assert read.truncated is True


# --- endpoint: flag off is untouched, flag on serves/falls back labeled ----


def _api_client(store_dir: Path):
    from starlette.testclient import TestClient

    from agentacct.api import create_local_api_app

    return TestClient(create_local_api_app(store_dir=store_dir))


def _write_ledger(store_dir: Path, events: list[dict[str, Any]]) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _trusted(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [mark_trusted_local_usage_import_event(event) for event in events]


def test_usage_summary_flag_off_is_untouched(tmp_path):
    client = _api_client(tmp_path / "store")
    payload = client.get("/usage/summary").json()
    assert payload["schema_version"] == "agent-sentinel.usage-summary.v1"
    assert "canonical_read" not in payload
    health = client.get("/health").json()
    assert health["canonical_read"]["enabled"] is False
    assert health["canonical_read"]["attempts"] == 0


def test_usage_summary_flag_on_store_absent_falls_back_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(tmp_path / "store")
    payload = client.get("/usage/summary").json()
    assert payload["schema_version"] == "agent-sentinel.usage-summary.v1"
    assert payload["canonical_read"] == {
        "active": False,
        "source": "v1_fallback",
        "reason": "store_absent",
        "detail": payload["canonical_read"]["detail"],
    }
    health = client.get("/health").json()
    assert health["canonical_read"]["enabled"] is True
    assert health["canonical_read"]["unavailable"] >= 1
    assert health["canonical_read"]["last_unavailable_reason"] == "store_absent"


def test_usage_summary_flag_on_serves_canonical(tmp_path, monkeypatch):
    events = _trusted(
        [
            _usage_event(
                event_id="evt_u1", session_id="s-1", created_at=1_700_000_000.0, input_tokens=100
            ),
            _usage_event(
                event_id="evt_u2", session_id="s-2", created_at=1_700_100_000.0, input_tokens=40
            ),
        ]
    )
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="endpoint")
    _promote_candidate_to_live(candidate, store_dir)
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    payload = client.get(
        "/usage/summary", params={"days": "all", "granularity": "daily"}
    ).json()
    assert payload["schema_version"] == "agent-sentinel.usage-summary.v2-canonical"
    label = payload["canonical_read"]
    assert label["active"] is True
    assert label["source"] == "canonical"
    assert label["day_basis"] == "utc"
    assert label["projection"]["stale"] is False
    assert label["truncated"] is False
    # 1_700_000_000 → 2023-11-14 UTC, +100_000s → 2023-11-16 UTC.
    periods = {entry["period"] for entry in payload["by_period"] if entry["measurement_days"]}
    assert periods == {"2023-11-14", "2023-11-16"}
    assert payload["totals"]["input_tokens"] == 140
    assert payload["totals"]["output_tokens"] == 100
    assert payload["totals"]["sessions"] is None
    assert payload["undated"]["measurement_days"] == 0
    assert payload["undated"]["bucket"] is None
    health = client.get("/health").json()
    assert health["canonical_read"]["served"] >= 1


def test_usage_summary_canonical_undated_bucket(tmp_path, monkeypatch):
    events = _trusted(
        [
            _usage_event(event_id="evt_u1", session_id="s-1", created_at=1_700_000_000.0),
            _undated_usage_event(event_id="evt_u2", session_id="s-undated"),
        ]
    )
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="undated")
    _promote_candidate_to_live(candidate, store_dir)
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)

    bounded = client.get("/usage/summary", params={"days": "30"}).json()
    assert bounded["schema_version"] == "agent-sentinel.usage-summary.v2-canonical"
    bounded_periods = {entry["period"] for entry in bounded["by_period"]}
    assert "1970-01-01" not in bounded_periods
    assert "unknown" not in bounded_periods
    assert bounded["undated"]["measurement_days"] >= 1
    assert bounded["undated"]["included_in_totals"] is False
    assert bounded["undated"]["bucket"]["input_tokens"] == 100
    assert bounded["totals"]["undated_included"] is False

    everything = client.get("/usage/summary", params={"days": "all"}).json()
    all_periods = {entry["period"] for entry in everything["by_period"]}
    assert "1970-01-01" not in all_periods
    assert "unknown" in all_periods
    assert everything["undated"]["included_in_totals"] is True
    # totals include the undated rows for days=all (v1's unknown-period rule).
    assert everything["totals"]["input_tokens"] == 200


def test_usage_summary_totals_parity_v1_vs_canonical(tmp_path, monkeypatch):
    """Range-totals parity on the SAME ledger: per-day buckets are
    deliberately not comparable (v1 = whole cumulative on one local day;
    canonical = UTC day-sliced deltas), but the range totals must agree
    field-for-field where both models measure the same thing."""

    events = _trusted(
        [
            _usage_event(
                event_id="evt_u1", session_id="s-1", created_at=1_700_000_000.0,
                input_tokens=100, output_tokens=50,
            ),
            _usage_event(
                event_id="evt_u2", session_id="s-2", created_at=1_700_100_000.0,
                input_tokens=40, output_tokens=7,
            ),
        ]
    )
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="parity")
    _promote_candidate_to_live(candidate, store_dir)

    client = _api_client(store_dir)
    v1 = client.get("/usage/summary", params={"days": "all"}).json()
    assert v1["schema_version"] == "agent-sentinel.usage-summary.v1"

    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    canonical_client = _api_client(store_dir)
    canonical = canonical_client.get("/usage/summary", params={"days": "all"}).json()
    assert canonical["schema_version"] == "agent-sentinel.usage-summary.v2-canonical"

    assert canonical["totals"]["input_tokens"] == v1["totals"]["input_tokens"] == 140
    assert canonical["totals"]["output_tokens"] == v1["totals"]["output_tokens"] == 57
    assert canonical["totals"]["fresh_tokens"] == v1["totals"]["fresh_tokens"] == 197
    assert (
        canonical["totals"]["total_tokens_including_cached"]
        == v1["totals"]["total_tokens_including_cached"]
    )
    v1_clients = {entry["client"] for entry in v1["by_client"]}
    canonical_clients = {entry["client"] for entry in canonical["by_client"]}
    assert canonical_clients == v1_clients == {"codex"}


# --- review-round tests (adversarial review of a1f8d3d) --------------------


def _rm_row(**overrides: Any) -> dict[str, Any]:
    """A synthetic rm_usage_day row with every metric unreported."""

    row: dict[str, Any] = {
        "rm_usage_day_id": 1,
        "day": "2023-11-14",
        "client": "codex",
        "provider": "openai",
        "model": "gpt-test",
        "usage_basis": "authoritative_cumulative_delta",
        "measurement_count": 1,
        "held_measurement_count": 0,
        "estimated_cost_microusd": None,
        "cost_completeness": "unavailable",
    }
    for value_key, reported_key, missing_key in (
        ("input_tokens", "input_reported_count", "input_missing_count"),
        ("output_tokens", "output_reported_count", "output_missing_count"),
        ("cached_input_tokens", "cache_reported_count", "cache_missing_count"),
        (
            "cache_creation_input_tokens",
            "cache_creation_reported_count",
            "cache_creation_missing_count",
        ),
        ("cache_read_input_tokens", "cache_read_reported_count", "cache_read_missing_count"),
        ("reasoning_output_tokens", "reasoning_reported_count", "reasoning_missing_count"),
        ("total_tokens", "total_reported_count", "total_missing_count"),
    ):
        row[value_key] = None
        row[reported_key] = 0
        row[missing_key] = 0
    row.update(overrides)
    return row


def _build_cube(rows: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    from datetime import date

    from agentacct.canonical_day_cube import build_canonical_day_cube

    defaults: dict[str, Any] = {
        "granularity": "daily",
        "days": None,
        "today": date(2023, 11, 20),
    }
    defaults.update(kwargs)
    return build_canonical_day_cube(rows, [], **defaults)


def test_held_rows_do_not_poison_additive_sums():
    """One held row (value NULL, reported>0 — the projection's shape for held
    measurements) must not null the additive sums or inflate the reported
    counts; v1 excludes non-additive rows from token sums the same way."""

    additive = _rm_row(
        measurement_count=2,
        input_tokens=1200,
        input_reported_count=2,
        output_tokens=300,
        output_reported_count=2,
        estimated_cost_microusd=50_000,
        cost_completeness="complete",
    )
    held = _rm_row(
        rm_usage_day_id=2,
        usage_basis="enrichment_held_non_additive",
        measurement_count=1,
        held_measurement_count=1,
        input_tokens=None,
        input_reported_count=1,
    )
    totals = _build_cube([additive, held])["totals"]
    assert totals["input_tokens"] == 1200
    assert totals["output_tokens"] == 300
    assert totals["fresh_tokens"] == 1500
    assert totals["input_tokens_reported_count"] == 2, "held reporting must not inflate the count"
    assert totals["measurement_days"] == 3
    assert totals["held_measurement_days"] == 1
    assert totals["usage_availability"] == "partial"
    # v1 rule: excluded rows present -> estimated cost None, additive cost kept.
    assert totals["estimated_cost_usd"] is None
    assert totals["known_additive_cost_usd"] == pytest.approx(0.05)


def test_unreported_fields_stay_none_and_gap_days_claim_nothing():
    from datetime import date

    row = _rm_row(
        day="2023-11-14",
        input_tokens=1000,
        input_reported_count=1,
        output_tokens=500,
        output_reported_count=1,
        cache_creation_missing_count=1,
    )
    cube = _build_cube([row], days=7, today=date(2023, 11, 16))
    totals = cube["totals"]
    assert totals["cache_creation_tokens"] is None, "never-reported must stay None, not 0"
    assert totals["cache_creation_reporting"] == "not_reported"
    assert totals["reasoning_output_tokens"] is None
    gap = next(entry for entry in cube["by_period"] if entry["period"] == "2023-11-15")
    assert gap["measurement_days"] == 0
    assert gap["input_tokens"] is None, "a gap-filled day claims nothing"
    assert gap["usage_availability"] == "unknown"
    assert len(cube["by_period"]) == 7, "every period in the bounded range is enumerated"


def test_total_including_cached_none_when_cached_incomputable():
    """Defensive: a reported-but-incomputable cache value must null the
    cache-inclusive total instead of riding along as 0."""

    row = _rm_row(
        input_tokens=1000,
        input_reported_count=1,
        output_tokens=500,
        output_reported_count=1,
        cached_input_tokens=None,
        cache_reported_count=1,
    )
    totals = _build_cube([row])["totals"]
    assert totals["fresh_tokens"] == 1500
    assert totals["cached_input_tokens"] is None
    assert totals["total_tokens_including_cached"] is None


def test_read_unavailable_projection_not_current(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="notcur"
    )
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute(
            "UPDATE projection_generations SET state = 'failed' "
            "WHERE projection_name = 'rm_usage_day'"
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "projection_not_current"


def test_error_reason_counts_and_drops_cache(tmp_path, monkeypatch):
    """An unexpected failure after the store opened must become the typed
    'error' fallback, count on the errors counter, and evict the possibly
    poisoned per-thread handle."""

    from agentacct.canonical.sqlite import CanonicalRepository

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="err"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    assert _read_days(runtime).rows

    def _boom(self, query):  # noqa: ANN001
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(CanonicalRepository, "usage_days", _boom)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "error"
    status = runtime.status()
    assert status["errors"] == 1
    assert "OperationalError" in status["last_error"]
    assert getattr(runtime._thread_local, "cache", None) is None, "poisoned handle must drop"


def test_read_unavailable_malformed_day_is_store_invalid_data(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="badday"
    )
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute("UPDATE rm_usage_day SET day = '2023-11-1x'")
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "store_invalid_data"
    assert "2023-11-1x" in excinfo.value.detail


def test_endpoint_malformed_day_falls_back_labeled_not_500(tmp_path, monkeypatch):
    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="badday-api")
    _promote_candidate_to_live(candidate, store_dir)
    connection = sqlite3.connect(store_dir / LIVE_STORE_FILENAME)
    try:
        connection.execute("UPDATE rm_usage_day SET day = '2023-11-1x'")
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_dir / LIVE_STORE_FILENAME, 0o600)
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")
    client = _api_client(store_dir)
    response = client.get("/usage/summary", params={"days": "all"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "agent-sentinel.usage-summary.v1"
    assert payload["canonical_read"]["active"] is False
    assert payload["canonical_read"]["reason"] == "store_invalid_data"


def test_endpoint_payload_build_failure_falls_back_labeled(tmp_path, monkeypatch):
    """Defense in depth: any crash while building the canonical response body
    becomes the labeled v1 fallback (reason 'error'), never an HTTP 500, and
    the crash is visible on /health."""

    import agentacct.api as api_module

    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="cube-crash")
    _promote_candidate_to_live(candidate, store_dir)
    monkeypatch.setenv(CANONICAL_READ_ENV, "1")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ValueError("cube exploded")

    monkeypatch.setattr(api_module, "build_canonical_day_cube", _boom)
    client = _api_client(store_dir)
    response = client.get("/usage/summary", params={"days": "all"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "agent-sentinel.usage-summary.v1"
    assert payload["canonical_read"]["reason"] == "error"
    assert "cube exploded" in payload["canonical_read"]["detail"]
    health = client.get("/health").json()
    assert health["canonical_read"]["errors"] >= 1


def test_same_inode_role_demotion_is_refused_on_next_read(tmp_path):
    """In-place store mutations keep the inode; the cached handle must
    re-prove role/schema per acquisition instead of serving forever."""

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="demote"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    assert _read_days(runtime).rows
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute("UPDATE store_metadata SET store_role = 'candidate' WHERE singleton = 1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        _read_days(runtime)
    assert excinfo.value.reason == "store_refused"


def test_multithreaded_reads_use_distinct_handles(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="threads"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    barrier = threading.Barrier(2)
    results: list[tuple[int, int, int]] = []

    def _worker() -> None:
        barrier.wait(timeout=10)
        read = _read_days(runtime)
        results.append(
            (threading.get_ident(), id(runtime._thread_local.cache.store), len(read.rows))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker), pool.submit(_worker)]
        for future in futures:
            future.result(timeout=30)

    assert len(results) == 2
    threads = {entry[0] for entry in results}
    handles = {entry[1] for entry in results}
    assert len(threads) == 2, "barrier must have forced two distinct threads"
    assert len(handles) == 2, "each thread must own its own store handle"
    assert all(entry[2] >= 1 for entry in results)
    assert runtime.status()["served"] == 2


def test_model_probe_is_whole_store_not_range_scoped(tmp_path):
    """v1 semantics: 'known model, empty range' is not 'unknown model'."""

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="probe"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = _read_days(
        runtime, start_day="2026-01-01", end_day="2026-12-31", model="gpt-test"
    )
    assert not read.rows, "the bounded range excludes the data on purpose"
    assert read.model_matches_store is True


def test_sentinel_start_day_never_enters_dated_rows(tmp_path):
    """A caller expressing 'all history' as 1970-01-01 must not receive
    sentinel rows as dated data (they would double-count with undated)."""

    events = [
        _usage_event(event_id="evt_u1", created_at=1_700_000_000.0),
        _undated_usage_event(event_id="evt_u2", session_id="s-undated"),
    ]
    store_root = _imported_live_store(tmp_path, events, name="clamp")
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    read = _read_days(runtime, start_day="1970-01-01", end_day="9999-12-31")
    assert read.undated_rows, "the sentinel rows must surface in the undated bucket"
    assert all(str(row["day"]) != "1970-01-01" for row in read.rows)
