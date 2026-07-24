"""Phase 4.3 — /health canonical_store probe, per-surface read counters, and
the projection rebuild policy.

The policy contract under test: rebuilds NEVER run in a read request path —
they ride the watcher loop (or the explicit CLI command), gated by the
canonical live-write flag, triggered only by staleness, and rate-limited by
a cooldown that failures consume too. The /health probe is flag-independent
diagnostics: an operator needs store/projection state while production reads
still serve v1.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_chronicle.canonical_live import CANONICAL_LIVE_ENV, CanonicalRebuildPolicy
from agent_chronicle.canonical_read import (
    CANONICAL_READ_ENV,
    CanonicalReadRuntime,
    CanonicalReadUnavailable,
)

from test_canonical_live_writer import _run_importer, _usage_event
from test_canonical_reader import (
    _api_client,
    _imported_live_store,
    _promote_candidate_to_live,
    _trusted,
    _write_ledger,
)

LIVE_STORE_FILENAME = "chronicle.sqlite3"

runner = CliRunner()


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _advance_canonical_sequence(store_root: Path) -> None:
    from agent_chronicle.canonical.sqlite import CanonicalStore

    store = CanonicalStore.open_live(store_root)
    try:
        repository = store.repository()
        with store.transaction(write=True):
            repository._advance_sequence(store.connection)
    finally:
        store.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)


def _projection_states(store_root: Path) -> dict[str, tuple[str, int]]:
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        return {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT projection_name, state, built_through_sequence "
                "FROM projection_generations"
            )
        }
    finally:
        connection.close()


# --- health_probe -----------------------------------------------------------


def test_health_probe_absent_store_is_honest_and_does_not_count(tmp_path):
    runtime = CanonicalReadRuntime(tmp_path, enabled=False)
    probe = runtime.health_probe()
    assert probe["available"] is False
    assert probe["reason"] == "store_absent"
    status = runtime.status()
    assert status["attempts"] == 0, "a health poll must not look like read traffic"
    assert status["surfaces"] == {}


def test_health_probe_is_flag_independent(tmp_path):
    """enabled=False (production today) still probes — the flag gates PRODUCT
    reads, not operator diagnostics."""

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="probe"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=False)
    probe = runtime.health_probe()
    assert probe["available"] is True
    assert probe["store"]["store_role"] == "live"
    assert probe["projections"]["rm_usage_day"]["stale"] is False
    assert probe["projections"]["rm_task_current"]["pending_writes"] == 0
    # A SUCCESSFUL probe must not look like read traffic either — the
    # absent-store case alone would let a success-path increment hide.
    status = runtime.status()
    assert status["attempts"] == 0 and status["served"] == 0
    assert status["surfaces"] == {}
    with pytest.raises(CanonicalReadUnavailable):
        runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")


def test_health_probe_reports_never_built_projections(tmp_path):
    from agent_chronicle.canonical.sqlite import CanonicalStore

    store_root = tmp_path / "store-shadow"
    store_root.mkdir()
    CanonicalStore.create_live(store_root).close()
    probe = CanonicalReadRuntime(store_root, enabled=False).health_probe()
    assert probe["available"] is True
    assert probe["projections"]["rm_usage_day"]["state"] == "never_built"
    assert probe["projections"]["rm_task_current"]["state"] == "never_built"


def test_health_probe_reports_staleness(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="stale"
    )
    _advance_canonical_sequence(store_root)
    probe = CanonicalReadRuntime(store_root, enabled=False).health_probe()
    assert probe["available"] is True
    assert probe["projections"]["rm_usage_day"]["stale"] is True
    assert probe["projections"]["rm_usage_day"]["pending_writes"] >= 1


def test_health_probe_malformed_summary_is_error_shape_not_raise(tmp_path, monkeypatch):
    """The never-raises contract covers the result PROCESSING too: a
    malformed value out of a byte-corrupted store must become the honest
    error shape, exactly like a failing open."""

    from agent_chronicle.canonical.sqlite import CanonicalRepository

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="malformed"
    )
    monkeypatch.setattr(
        CanonicalRepository,
        "health_summary",
        lambda self: {"store": {"canonical_sequence": "bogus"}, "projections": (), "sources": ()},
    )
    probe = CanonicalReadRuntime(store_root, enabled=False).health_probe()
    assert probe["available"] is False
    assert probe["reason"] == "error"
    assert "ValueError" in probe["detail"]


# --- per-surface counters ---------------------------------------------------


def test_per_surface_counter_lanes(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="lanes"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    runtime.session_list_read()
    miss = runtime.task_detail_read("task_" + "0" * 32)
    assert miss.task is None
    status = runtime.status()
    surfaces = status["surfaces"]
    assert surfaces["usage_days"]["served"] == 1
    assert surfaces["session_list"]["served"] == 1
    assert surfaces["task_detail"]["served"] == 1, "an honest miss is a served read"
    assert status["served"] == 3
    assert status["attempts"] == 3
    for lane in surfaces.values():
        # Contract 4's aggregate/lane consistency: a lane's attempts must
        # reconcile with its outcomes, same as the aggregate.
        assert lane["attempts"] == lane["served"] + lane["unavailable"]


def test_per_surface_unavailable_and_error_lanes(tmp_path):
    runtime = CanonicalReadRuntime(tmp_path, enabled=True)
    with pytest.raises(CanonicalReadUnavailable):
        runtime.task_list_read()
    with pytest.raises(CanonicalReadUnavailable):
        runtime.session_list_read()
    failure = runtime.surface_failure("cube exploded", surface="usage_days")
    assert failure.reason == "error"
    status = runtime.status()
    assert status["surfaces"]["task_list"]["unavailable"] == 1
    assert status["surfaces"]["task_list"]["last_unavailable_reason"] == "store_absent"
    assert status["surfaces"]["session_list"]["unavailable"] == 1
    assert status["surfaces"]["usage_days"]["errors"] == 1
    assert "cube exploded" in status["surfaces"]["usage_days"]["last_error"]
    assert status["errors"] == 1
    assert status["unavailable"] == 2


def test_per_surface_error_lane_via_guarded_read(tmp_path, monkeypatch):
    """A mid-read crash (the _guarded_read generic-exception branch, not
    surface_failure) must count on the surface lane exactly as on the
    aggregate — a lane that under-reports errors is silent wrong data."""

    from agent_chronicle.canonical.sqlite import CanonicalRepository

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="err-lane"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)

    def _boom(self, query):
        raise RuntimeError("mid-read boom")

    monkeypatch.setattr(CanonicalRepository, "usage_days", _boom)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    assert excinfo.value.reason == "error"
    status = runtime.status()
    lane = status["surfaces"]["usage_days"]
    assert lane["attempts"] == 1
    assert lane["unavailable"] == 1
    assert lane["errors"] == 1
    assert lane["last_unavailable_reason"] == "error"
    assert "mid-read boom" in lane["last_error"]
    assert status["errors"] == 1 and status["attempts"] == 1


def test_reads_hold_one_read_snapshot(tmp_path, monkeypatch):
    """Labels and rows must come from ONE committed generation: every data
    statement of a read runs inside the same read transaction as the health
    gate (4.3 introduces the first live-store rm_* writer concurrent with
    request reads), and the snapshot is released when the read returns."""

    from agent_chronicle.canonical.sqlite import CanonicalRepository

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="snapshot"
    )
    runtime = CanonicalReadRuntime(store_root, enabled=True)
    in_transaction: list[bool] = []

    def _spying(method_name: str):
        original = getattr(CanonicalRepository, method_name)

        def spy(self, *args, **kwargs):
            in_transaction.append(self.store.connection.in_transaction)
            return original(self, *args, **kwargs)

        return spy

    for method_name in ("usage_days", "list_tasks", "get_task", "list_sessions"):
        monkeypatch.setattr(CanonicalRepository, method_name, _spying(method_name))

    runtime.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    runtime.task_list_read()
    runtime.task_detail_read("task_" + "0" * 32)
    runtime.session_list_read()
    assert in_transaction and all(in_transaction)
    assert runtime._thread_local.cache.store.connection.in_transaction is False, (
        "the read snapshot must be released when the read returns"
    )


# --- GET /health canonical_store block --------------------------------------


def test_health_endpoint_reports_canonical_store_probe(tmp_path, monkeypatch):
    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="health-block")
    _promote_candidate_to_live(candidate, store_dir)
    client = _api_client(store_dir)
    payload = client.get("/health").json()
    block = payload["canonical_store"]
    assert block["available"] is True
    assert block["store"]["store_role"] == "live"
    assert block["projections"]["rm_usage_day"]["stale"] is False
    # The read-path block keeps its 4.1 shape plus the surfaces lanes.
    assert payload["canonical_read"]["enabled"] is False
    assert payload["canonical_read"]["surfaces"] == {}


def test_health_endpoint_canonical_store_absent(tmp_path):
    client = _api_client(tmp_path / "store")
    payload = client.get("/health").json()
    assert payload["canonical_store"]["available"] is False
    assert payload["canonical_store"]["reason"] == "store_absent"


# --- CanonicalRebuildPolicy -------------------------------------------------


def test_policy_disabled_does_nothing(tmp_path):
    policy = CanonicalRebuildPolicy(tmp_path, enabled=False)
    assert policy.tick() == {"action": "disabled"}
    assert not (tmp_path / LIVE_STORE_FILENAME).exists()


def test_policy_no_store_never_creates(tmp_path):
    policy = CanonicalRebuildPolicy(tmp_path, enabled=True)
    assert policy.tick() == {"action": "no_store"}
    assert not (tmp_path / LIVE_STORE_FILENAME).exists()


def test_policy_rebuilds_the_live_shadow_shape_end_to_end(tmp_path):
    """The soak story: a shadow-written store has canonical rows but
    never-built read models (readers refuse). One policy tick rebuilds, and
    the SAME reader that refused now serves."""

    from agent_chronicle.canonical_live import CanonicalLiveRuntime

    store_root = tmp_path / "store"
    store_root.mkdir()
    live = CanonicalLiveRuntime(store_root, enabled=True)
    result = live.shadow_v1_event(
        _usage_event(event_id="evt_u1", created_at=1_700_000_000.0)
    )
    assert result.error is None
    reader = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable) as excinfo:
        reader.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    assert excinfo.value.reason == "projection_never_built"

    policy = CanonicalRebuildPolicy(store_root, enabled=True, clock=_FakeClock())
    outcome = policy.tick()
    assert outcome["action"] == "rebuilt"
    assert outcome["usage_day_count"] >= 1

    read = reader.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")
    assert read.rows, "after the rebuild the refusing reader serves"
    assert read.projection["stale"] is False


def test_policy_current_store_is_left_alone_and_checked_every_tick(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="cur"
    )
    clock = _FakeClock()
    policy = CanonicalRebuildPolicy(store_root, enabled=True, clock=clock)
    first = policy.tick()
    assert first["action"] == "current"
    # current does NOT consume the cooldown: the next tick checks again.
    clock.advance(1.0)
    assert policy.tick()["action"] == "current"


def test_policy_stale_store_rebuilds_and_cooldown_bounds_attempts(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="cool"
    )
    _advance_canonical_sequence(store_root)
    clock = _FakeClock()
    policy = CanonicalRebuildPolicy(
        store_root, enabled=True, cooldown_seconds=300.0, clock=clock
    )
    assert policy.tick()["action"] == "rebuilt"
    states = _projection_states(store_root)
    assert all(state == "current" for state, _ in states.values())

    # New writes arrive; within the cooldown the policy waits...
    _advance_canonical_sequence(store_root)
    clock.advance(60.0)
    assert policy.tick()["action"] == "cooldown"
    # ...and past it, rebuilds again.
    clock.advance(300.0)
    assert policy.tick()["action"] == "rebuilt"


def test_policy_failure_is_fail_open_and_consumes_cooldown(tmp_path):
    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="fail"
    )
    _advance_canonical_sequence(store_root)
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o644)
    clock = _FakeClock()
    policy = CanonicalRebuildPolicy(store_root, enabled=True, clock=clock)
    outcome = policy.tick()
    assert outcome["action"] == "failed"
    assert outcome["error"]
    clock.advance(60.0)
    assert policy.tick()["action"] == "cooldown", "a broken store must not hot-loop"
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    clock.advance(300.0)
    assert policy.tick()["action"] == "rebuilt"


def test_policy_presence_probe_oserror_is_fail_open_and_consumes_cooldown(tmp_path):
    """The fail-open contract covers the presence lstat too: a store path
    broken in any way other than plain absence (a component that is a file,
    lost permissions, a failing mount) is a cooldown-bounded FAILURE, not a
    raise that would kill the watcher daemon."""

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    clock = _FakeClock()
    policy = CanonicalRebuildPolicy(blocker / "store", enabled=True, clock=clock)
    outcome = policy.tick()
    assert outcome["action"] == "failed"
    assert "NotADirectoryError" in outcome["error"]
    clock.advance(60.0)
    assert policy.tick()["action"] == "cooldown", "a broken path must not hot-loop"


def test_policy_non_current_projection_state_triggers_rebuild(tmp_path):
    """state != 'current' with built_through == canonical_sequence is exactly
    the shape the reader refuses as projection_not_current; the policy must
    repair it, not leave a refuse-forever/rebuild-never split open."""

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="state"
    )
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute(
            "UPDATE projection_generations SET state = 'failed' "
            "WHERE projection_name = 'rm_task_current'"
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    policy = CanonicalRebuildPolicy(store_root, enabled=True, clock=_FakeClock())
    assert policy.tick()["action"] == "rebuilt"
    states = _projection_states(store_root)
    assert all(state == "current" for state, _ in states.values())


def test_policy_single_missing_read_model_row_triggers_rebuild(tmp_path):
    """Per-model divergence: ONE read model missing its generation row while
    the other is current must still trigger the rebuild."""

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="one-missing"
    )
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute(
            "DELETE FROM projection_generations WHERE projection_name = 'rm_task_current'"
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    policy = CanonicalRebuildPolicy(store_root, enabled=True, clock=_FakeClock())
    assert policy.tick()["action"] == "rebuilt"
    states = _projection_states(store_root)
    assert set(states) == {"rm_task_current", "rm_usage_day"}
    assert all(state == "current" for state, _ in states.values())


def test_policy_env_flag_gates_default_enablement(tmp_path, monkeypatch):
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "shadow")
    assert CanonicalRebuildPolicy(tmp_path).enabled is True
    monkeypatch.delenv(CANONICAL_LIVE_ENV)
    assert CanonicalRebuildPolicy(tmp_path).enabled is False


# --- CLI --------------------------------------------------------------------


def test_cli_canonical_health_json(tmp_path):
    from agent_chronicle.cli import app

    absent = runner.invoke(
        app, ["canonical", "health", "--store-dir", str(tmp_path / "empty"), "--json"]
    )
    assert absent.exit_code == 0, absent.output
    payload = json.loads(absent.output)
    assert payload["available"] is False
    assert payload["reason"] == "store_absent"

    events = [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)]
    store_root = _imported_live_store(tmp_path, events, name="cli-health")
    ready = runner.invoke(
        app, ["canonical", "health", "--store-dir", str(store_root), "--json"]
    )
    assert ready.exit_code == 0, ready.output
    payload = json.loads(ready.output)
    assert payload["available"] is True
    assert payload["store"]["store_role"] == "live"
    assert payload["projections"]["rm_usage_day"]["state"] == "current"


def test_cli_canonical_rebuild_read_models(tmp_path):
    from agent_chronicle.cli import app

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="cli-rb"
    )
    connection = sqlite3.connect(store_root / LIVE_STORE_FILENAME)
    try:
        connection.execute("DELETE FROM projection_generations")
        connection.execute("DELETE FROM rm_usage_day")
        connection.commit()
    finally:
        connection.close()
    os.chmod(store_root / LIVE_STORE_FILENAME, 0o600)
    reader = CanonicalReadRuntime(store_root, enabled=True)
    with pytest.raises(CanonicalReadUnavailable):
        reader.usage_day_read(start_day="1970-01-02", end_day="9999-12-31")

    result = runner.invoke(
        app,
        ["canonical", "rebuild-read-models", "--store-dir", str(store_root), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["usage_day_count"] >= 1
    assert reader.usage_day_read(start_day="1970-01-02", end_day="9999-12-31").rows


def test_cli_canonical_health_unresolved_store_dir_is_honest(tmp_path, monkeypatch):
    """An unresolvable store DIRECTORY is still an honest exit-0 answer — a
    monitoring script keyed on 'exit 0, parse the probe' must not break
    because of its working directory."""

    from agent_chronicle.cli import app
    from agent_chronicle.store_resolution import ENV_STORE_DIR, LEGACY_ENV_STORE_DIR

    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.delenv(LEGACY_ENV_STORE_DIR, raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["canonical", "health", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["available"] is False
    assert payload["reason"] == "store_unresolved"
    assert "No agentacct store found" in payload["detail"]

    human = runner.invoke(app, ["canonical", "health"])
    assert human.exit_code == 0, human.output
    assert "canonical store unavailable: store_unresolved" in human.output


def test_cli_canonical_success_paths_human_output(tmp_path):
    """The non-JSON success formatting is operator surface too — it must not
    be reachable only by the first operator who runs it."""

    from agent_chronicle.cli import app

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="cli-human"
    )
    health = runner.invoke(app, ["canonical", "health", "--store-dir", str(store_root)])
    assert health.exit_code == 0, health.output
    assert "store role=live" in health.output
    assert "projection rm_usage_day: state=current" in health.output
    rebuild = runner.invoke(
        app, ["canonical", "rebuild-read-models", "--store-dir", str(store_root)]
    )
    assert rebuild.exit_code == 0, rebuild.output
    assert "rebuilt read models: tasks=" in rebuild.output


def test_cli_canonical_rebuild_midway_failure_is_one_line_exit_2(tmp_path, monkeypatch):
    """A failure AFTER a successful open (lock timeout, full disk) gets the
    same operator contract as the refusal paths: one line on stderr and
    exit 2, never a raw traceback."""

    from agent_chronicle.canonical.sqlite import CanonicalRepository
    from agent_chronicle.cli import app

    store_root = _imported_live_store(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="cli-midfail"
    )

    def _boom(self):
        raise RuntimeError("disk full mid-transaction")

    monkeypatch.setattr(CanonicalRepository, "rebuild_minimal_read_models", _boom)
    result = runner.invoke(
        app, ["canonical", "rebuild-read-models", "--store-dir", str(store_root)]
    )
    assert result.exit_code == 2
    assert "canonical rebuild failed: RuntimeError: disk full mid-transaction" in result.output
    assert "Traceback" not in result.output


def test_cli_canonical_rebuild_absent_store_exits_2(tmp_path):
    from agent_chronicle.cli import app

    result = runner.invoke(
        app, ["canonical", "rebuild-read-models", "--store-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "no canonical store" in result.output


def test_cli_canonical_rebuild_refuses_candidate_role(tmp_path):
    """A candidate copied to the reserved name must be refused by the
    fail-closed open, not rebuilt as if promoted."""

    import shutil

    from agent_chronicle.cli import app

    candidate = _run_importer(
        tmp_path, [_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)], name="cli-role"
    )
    store_root = tmp_path / "store-role"
    store_root.mkdir()
    target = store_root / LIVE_STORE_FILENAME
    shutil.copy2(candidate, target)
    os.chmod(target, 0o600)
    result = runner.invoke(
        app, ["canonical", "rebuild-read-models", "--store-dir", str(store_root)]
    )
    assert result.exit_code == 2
    assert "refused" in result.output


# --- watcher integration ----------------------------------------------------


def test_usage_watch_once_rebuilds_stale_canonical_store(tmp_path, monkeypatch):
    """One watcher tick (usage watch --once) with the shadow flag on performs
    the maintenance rebuild after a successful scan."""

    from agent_chronicle.cli import app

    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="watch-once")
    _promote_candidate_to_live(candidate, store_dir)
    _advance_canonical_sequence(store_dir)
    assert _projection_states(store_dir)["rm_usage_day"][1] >= 1

    homes = {}
    for name in ("codex", "claude", "opencode", "hermes", "openclaw", "cursor"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        homes[name] = home
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "shadow")
    result = runner.invoke(
        app,
        [
            "usage",
            "watch",
            "--once",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(homes["codex"]),
            "--claude-home",
            str(homes["claude"]),
            "--opencode-home",
            str(homes["opencode"]),
            "--hermes-home",
            str(homes["hermes"]),
            "--openclaw-home",
            str(homes["openclaw"]),
            "--cursor-home",
            str(homes["cursor"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "canonical_read_models rebuilt" in result.output
    probe = CanonicalReadRuntime(store_dir, enabled=False).health_probe()
    assert probe["projections"]["rm_usage_day"]["stale"] is False


def test_usage_watch_once_json_rebuild_outcome_stays_json(tmp_path, monkeypatch):
    """--json's stdout contract is one JSON document per line; the rebuild
    notice must honor it too, not corrupt the stream with a plain-text line."""

    from agent_chronicle.cli import app

    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="watch-json")
    _promote_candidate_to_live(candidate, store_dir)
    _advance_canonical_sequence(store_dir)

    homes = {}
    for name in ("codex", "claude", "opencode", "hermes", "openclaw", "cursor"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        homes[name] = home
    monkeypatch.setenv(CANONICAL_LIVE_ENV, "shadow")
    result = runner.invoke(
        app,
        [
            "usage",
            "watch",
            "--once",
            "--json",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(homes["codex"]),
            "--claude-home",
            str(homes["claude"]),
            "--opencode-home",
            str(homes["opencode"]),
            "--hermes-home",
            str(homes["hermes"]),
            "--openclaw-home",
            str(homes["openclaw"]),
            "--cursor-home",
            str(homes["cursor"]),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]
    rebuild_payloads = [p for p in payloads if "canonical_read_models" in p]
    assert len(rebuild_payloads) == 1
    assert rebuild_payloads[0]["canonical_read_models"]["action"] == "rebuilt"


def test_usage_watch_once_failed_scan_skips_maintenance(tmp_path, monkeypatch):
    """Contract 1's 'after a SUCCESSFUL scan': a daemon whose scan fails must
    not perform writable canonical maintenance on that tick."""

    from agent_chronicle import cli as cli_module
    from agent_chronicle.cli import app

    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="watch-failscan")
    _promote_candidate_to_live(candidate, store_dir)
    _advance_canonical_sequence(store_dir)
    before = _projection_states(store_dir)

    def _boom(**_kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setenv(CANONICAL_LIVE_ENV, "shadow")
    monkeypatch.setattr(cli_module, "_local_usage_import_payload", _boom)
    result = runner.invoke(app, ["usage", "watch", "--once", "--store-dir", str(store_dir)])
    assert result.exit_code != 0
    assert "canonical_read_models" not in result.output
    assert _projection_states(store_dir) == before, (
        "a failed scan must not be followed by canonical maintenance"
    )


def test_usage_watch_once_flag_off_touches_nothing(tmp_path, monkeypatch):
    from agent_chronicle.cli import app

    events = _trusted([_usage_event(event_id="evt_u1", created_at=1_700_000_000.0)])
    store_dir = tmp_path / "store"
    _write_ledger(store_dir, events)
    candidate = _run_importer(tmp_path, events, name="watch-off")
    _promote_candidate_to_live(candidate, store_dir)
    _advance_canonical_sequence(store_dir)
    before = _projection_states(store_dir)

    homes = {}
    for name in ("codex", "claude", "opencode", "hermes", "openclaw", "cursor"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        homes[name] = home
    result = runner.invoke(
        app,
        [
            "usage",
            "watch",
            "--once",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(homes["codex"]),
            "--claude-home",
            str(homes["claude"]),
            "--opencode-home",
            str(homes["opencode"]),
            "--hermes-home",
            str(homes["hermes"]),
            "--openclaw-home",
            str(homes["openclaw"]),
            "--cursor-home",
            str(homes["cursor"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "canonical_read_models" not in result.output
    assert _projection_states(store_dir) == before, (
        "flag off: the stale projections stay untouched"
    )
