"""Tests for version-stamped client-integration re-sync (#2b).

The re-sync keeps a user's installed MCP config / hooks / instructions current
after an agentacct upgrade. These tests pin the version-gate + scope logic; the
actual writers (which touch ~/.claude, ~/.codex) are already covered by the
onboard tests and are stubbed here so the gate is exercised hermetically.

Global installs stamp ``project_dir == home`` (that is what ``_onboard_global``
records), so the "global" cases below use ``Path.home()`` — the re-sync only runs
for global installs, since it re-runs the USER-scope onboard writers.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import agentacct.cli as cli
from agentacct.activation import ActivationStateStore
from agentacct.cli import app
from agentacct.service import SentinelService

_HOME = str(Path.home())


def test_mark_configured_stamps_and_reads_version(tmp_path):
    store = ActivationStateStore(tmp_path / "state")
    record = store.mark_configured(project_dir=tmp_path, clients=["codex"], agentacct_version="1.2.3")
    assert record["agentacct_version"] == "1.2.3"
    assert store.snapshot()["agentacct_version"] == "1.2.3"
    # An unstamped record reads back as None (pre-stamping installs).
    other = ActivationStateStore(tmp_path / "state2")
    other.mark_configured(project_dir=tmp_path, clients=["codex"])
    assert other.snapshot()["agentacct_version"] is None


def _stub_resync(monkeypatch, calls, *, errored=()):
    errored = list(errored)

    def _fake(store_dir, clients, command, **kwargs):
        calls.append(list(clients))
        ok = [c for c in clients if c not in errored]
        return ok, list(errored)

    monkeypatch.setattr(cli, "_resync_integration", _fake)
    monkeypatch.setattr(cli, "_resolve_absolute_mcp_command", lambda: "agentacct")


def test_resync_gate_fires_on_version_change_and_restamps(tmp_path, monkeypatch):
    calls: list = []
    _stub_resync(monkeypatch, calls)
    monkeypatch.setattr(cli, "_package_version", lambda: "9.9.9")
    store_dir = tmp_path / "state"
    store = ActivationStateStore(store_dir)

    # No activation record → nothing to re-sync.
    assert cli._resync_integration_if_stale(store_dir)["status"] == "no-activation"

    # An OLD stamped version on a GLOBAL install → re-sync fires and re-stamps.
    store.mark_configured(project_dir=_HOME, clients=["codex", "claude-code"], agentacct_version="0.0.1")
    result = cli._resync_integration_if_stale(store_dir)
    assert result["status"] == "resynced" and result["from"] == "0.0.1" and result["to"] == "9.9.9"
    assert calls and set(calls[-1]) == {"codex", "claude-code"}
    assert store.snapshot()["agentacct_version"] == "9.9.9"  # re-stamped
    assert set(store.snapshot()["clients"]) == {"codex", "claude-code"}


def test_resync_gate_is_noop_when_current(tmp_path, monkeypatch):
    calls: list = []
    _stub_resync(monkeypatch, calls)
    monkeypatch.setattr(cli, "_package_version", lambda: "9.9.9")
    store_dir = tmp_path / "state"
    ActivationStateStore(store_dir).mark_configured(
        project_dir=_HOME, clients=["codex"], agentacct_version="9.9.9"
    )
    assert cli._resync_integration_if_stale(store_dir)["status"] == "current"
    assert calls == []  # no writers run when the version is unchanged
    # force overrides the gate.
    assert cli._resync_integration_if_stale(store_dir, force=True)["status"] == "resynced"
    assert calls  # ran under force


def test_resync_gate_fires_on_unstamped_record(tmp_path, monkeypatch):
    calls: list = []
    _stub_resync(monkeypatch, calls)
    monkeypatch.setattr(cli, "_package_version", lambda: "9.9.9")
    store_dir = tmp_path / "state"
    ActivationStateStore(store_dir).mark_configured(project_dir=_HOME, clients=["codex"])  # no version
    result = cli._resync_integration_if_stale(store_dir)
    assert result["status"] == "resynced" and result["from"] is None
    assert calls


def test_resync_skips_project_scope(tmp_path, monkeypatch):
    # A project-scoped install (project_dir != home) must NOT run the USER-scope
    # global writers — that would write config for the wrong scope.
    calls: list = []
    _stub_resync(monkeypatch, calls)
    monkeypatch.setattr(cli, "_package_version", lambda: "9.9.9")
    store_dir = tmp_path / "state"
    ActivationStateStore(store_dir).mark_configured(
        project_dir=tmp_path, clients=["codex"], agentacct_version="0.0.1"  # stale, but project scope
    )
    result = cli._resync_integration_if_stale(store_dir, force=True)
    assert result["status"] == "project-scope-skipped"
    assert calls == []  # writers never run for a project-scoped record


def test_resync_partial_failure_keeps_stamp_stale_and_retries(tmp_path, monkeypatch):
    # A client whose writer RAISES must NOT advance the version stamp — so the next
    # start retries it (fixing a transient failure) instead of silently skipping it
    # until the next upgrade.
    calls: list = []
    _stub_resync(monkeypatch, calls, errored=["claude-code"])
    monkeypatch.setattr(cli, "_package_version", lambda: "9.9.9")
    store_dir = tmp_path / "state"
    store = ActivationStateStore(store_dir)
    store.mark_configured(project_dir=_HOME, clients=["codex", "claude-code"], agentacct_version="0.0.1")

    r1 = cli._resync_integration_if_stale(store_dir)
    assert r1["status"] == "partial" and r1["errored"] == ["claude-code"] and r1["clients"] == ["codex"]
    assert store.snapshot()["agentacct_version"] == "0.0.1"  # stamp NOT advanced

    calls.clear()
    r2 = cli._resync_integration_if_stale(store_dir)
    assert r2["status"] == "partial"  # retried, not silently "current"
    assert calls  # the writers ran again


def test_sync_command_forces_resync(tmp_path, monkeypatch):
    calls: list = []
    _stub_resync(monkeypatch, calls)
    monkeypatch.setattr(cli, "_package_version", lambda: "9.9.9")
    SentinelService(tmp_path)  # make tmp_path a store the CLI resolver accepts
    ActivationStateStore(tmp_path).mark_configured(
        project_dir=_HOME, clients=["codex"], agentacct_version="9.9.9"  # already current + global
    )
    result = CliRunner().invoke(app, ["sync", "--store-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "resynced"  # sync forces even when version matches
    assert calls  # the writers ran
