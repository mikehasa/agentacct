from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import agent_chronicle.ingestion_health as health_module
from agent_chronicle.ingestion_health import (
    EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE,
    IngestionHealthStore,
    apply_evidence_refreshable_usage_health,
    evidence_refreshable_usage_failed,
    health_scan_results,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize(
    "outcome",
    [
        {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": False,
            "errors": ["private implementation detail"],
            "conflicts": 0,
        },
        {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": True,
            "errors": [],
            "conflicts": 2,
        },
        {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": True,
            "errors": [],
            "conflicts": 0,
            "existing_conflicts": 2,
        },
        {
            "enabled": True,
            "complete_requested": True,
            "complete_applied": False,
            "errors": [],
            "conflicts": 0,
        },
    ],
)
def test_post_persist_evidence_failure_degrades_configured_sources_without_leaking_action(
    tmp_path: Path,
    outcome: dict[str, object],
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    scan_id = store.begin_scan(
        sources=("codex", "claude-code"),
        scan_limit=20,
        importer_version="test",
        started_at=1_000.0,
    )
    results = apply_evidence_refreshable_usage_health(
        {"codex": {"error_count": 0}},
        sources=("codex", "claude-code"),
        outcome=outcome,
    )

    assert set(results) == {"codex", "claude-code"}
    assert all(
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE in result["error_codes"]
        for result in results.values()
    )
    store.complete_scan(scan_id, results=results, completed_at=1_001.0)
    snapshot = store.snapshot(now=1_002.0)

    assert snapshot["state"] == "degraded"
    assert {issue["code"] for issue in snapshot["issues"]} == {
        EVIDENCE_REFRESHABLE_USAGE_ERROR_CODE
    }
    assert all(
        "private implementation detail" not in issue["action"]
        for issue in snapshot["issues"]
    )


def test_disabled_or_clean_evidence_reconcile_does_not_degrade_health_results() -> None:
    clean = {"codex": {"error_count": 0, "error_codes": []}}
    disabled = {
        "enabled": False,
        "complete_requested": True,
        "complete_applied": False,
        "errors": [],
        "conflicts": 0,
    }

    assert evidence_refreshable_usage_failed(disabled) is False
    assert apply_evidence_refreshable_usage_health(
        clean,
        sources=("codex",),
        outcome=disabled,
    ) == clean


def test_scan_receipts_are_atomic_private_and_project_healthy_state(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    lease = store.acquire_watcher(
        lease_id="watcher-a",
        pid=4242,
        importer_version="0.1.0",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex", "claude-code"),
        now=999.0,
    )
    assert lease.acquired is True
    scan_id = store.begin_scan(
        sources=("codex", "claude-code"),
        scan_limit=20,
        importer_version="0.1.0",
        pid=4242,
        started_at=1_000.0,
    )

    running = store.snapshot(now=1_001.0)
    assert running["state"] == "unknown"
    assert running["active_scan_count"] == 1

    store.complete_scan(
        scan_id,
        results={
            "codex": {
                "discovered": 9,
                "parsed": 8,
                "skipped": 1,
                "error_count": 0,
                "watermark": "codex:1000",
            },
            "claude-code": {
                "discovered": 6,
                "parsed": 6,
                "skipped": 0,
                "error_count": 0,
                "watermark": "claude:1000",
            },
        },
        completed_at=1_002.0,
    )
    snapshot = store.snapshot(now=1_010.0)
    assert snapshot["state"] == "healthy"
    assert snapshot["last_success_at"] == 1_002.0
    assert snapshot["watcher"]["state"] == "running"
    assert [row["source"] for row in snapshot["sources"]] == ["claude-code", "codex"]
    assert snapshot["sources"][1] == {
        "source": "codex",
        "state": "healthy",
        "scope": "watched",
        "last_attempt_at": 1_000.0,
        "last_success_at": 1_002.0,
        "last_failure_at": None,
        "discovered": 9,
        "parsed": 8,
        "skipped": 1,
        "error_count": 0,
        "error_code": None,
        "error_codes": [],
        "scan_limit": 20,
        "watermark": "codex:1000",
        "limit_unit": "rows",
        "selected_root_groups": None,
        "returned_root_groups": None,
        "returned_rows": 0,
        "excluded_by_limit": 0,
        "ignored_non_transcript_files": 0,
        "unresolved_identity_files": 0,
        "excluded_by_source_namespace": 0,
        "source_namespace_conflicts": 0,
        "source_namespace_adoptions": 0,
        "concurrent_refresh_conflicts": 0,
        "incomplete_alias_migrations": 0,
        "unparsed_selected_rows": 0,
        "observed_sessions": 0,
        "usage_sessions": 0,
        "sessions_without_usage": 0,
        "session_observation_conflicts": 0,
        "session_observation_conflict_reasons": {},
        "consecutive_failures": 0,
    }
    assert _mode(store.health_root) == 0o700
    assert _mode(store.state_path) == 0o600
    assert not list(store.health_root.glob("*.tmp-*"))
    assert json.loads(store.state_path.read_text(encoding="utf-8"))["schema_version"]


def test_namespace_conflict_is_a_successful_but_degraded_receipt_and_clean_scan_recovers(
    tmp_path: Path,
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    store.acquire_watcher(
        lease_id="watcher-namespace",
        pid=4242,
        importer_version="0.1.0",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
        now=999.0,
    )
    conflicted = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="0.1.0",
        pid=4242,
        started_at=1_000.0,
    )
    store.complete_scan(
        conflicted,
        results={
            "codex": {
                "discovered": 2,
                "parsed": 2,
                "error_count": 0,
                "source_namespace_conflicts": 1,
            }
        },
        completed_at=1_001.0,
    )

    degraded = store.snapshot(now=1_002.0)
    source = degraded["sources"][0]
    assert degraded["state"] == "degraded"
    assert degraded["last_success_at"] == 1_001.0
    assert degraded["source_namespace_conflicts"] == 1
    assert source["state"] == "degraded"
    assert source["error_count"] == 0
    assert source["source_namespace_conflicts"] == 1
    assert degraded["issues"] == [
        {
            "code": "source_namespace_conflict",
            "source": "codex",
            "action": (
                "Open Advanced and confirm the affected agent's configured data home. "
                "Remove the unintended duplicate home or restore the intended path, then "
                "restart sync and retry only this source."
            ),
        }
    ]

    clean = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="0.1.0",
        pid=4242,
        started_at=1_003.0,
    )
    store.complete_scan(
        clean,
        results={
            "codex": {
                "discovered": 1,
                "parsed": 1,
                "error_count": 0,
                "source_namespace_conflicts": 0,
            }
        },
        completed_at=1_004.0,
    )
    recovered = store.snapshot(now=1_005.0)
    assert recovered["state"] == "healthy"
    assert recovered["source_namespace_conflicts"] == 0
    assert recovered["sources"][0]["source_namespace_conflicts"] == 0
    assert recovered["issues"] == []


@pytest.mark.parametrize(
    ("reason", "action_fragment"),
    [
        ("source_namespace_conflict", "configured data home"),
        ("same_watermark_conflict", "same source revision"),
        ("source_watermark_unorderable", "trustworthy source revision time"),
        ("invalid_observation", "invalid session-presence record"),
    ],
)
def test_session_observation_conflict_reason_is_durable_and_actionable(
    tmp_path: Path,
    reason: str,
    action_fragment: str,
) -> None:
    store = IngestionHealthStore(tmp_path / reason)
    scan_id = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="test",
        started_at=1_000.0,
    )
    results = health_scan_results(
        {
            "codex": {
                "discovered": 1,
                "parsed": 1,
                "error_count": 1,
                "error_codes": ["session_observation_conflict"],
                "session_observation_conflicts": 1,
                "session_observation_conflict_reasons": {reason: 1},
            }
        }
    )

    assert reason in results["codex"]["error_codes"]
    assert results["codex"]["error_code"] == reason
    store.complete_scan(scan_id, results=results, completed_at=1_001.0)
    snapshot = store.snapshot(now=1_002.0)

    source = snapshot["sources"][0]
    assert source["session_observation_conflicts"] == 1
    assert source["session_observation_conflict_reasons"] == {reason: 1}
    assert reason in source["error_codes"]
    assert snapshot["session_observation_conflicts"] == 1
    assert snapshot["session_observation_conflict_reasons"] == {reason: 1}
    assert snapshot["issues"][0]["code"] == reason
    assert action_fragment in snapshot["issues"][0]["action"]


def test_incomplete_alias_migration_persists_actionable_health_issue(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    scan_id = store.begin_scan(
        sources=("claude-code",),
        scan_limit=20,
        importer_version="0.1.0",
        started_at=1_000.0,
    )
    results = health_scan_results(
        {
            "claude-code": {
                "discovered": 2,
                "parsed": 1,
                "skipped": 1,
                "error_count": 2,
                "error_codes": [
                    "claude_transcript_malformed_lines",
                    "alias_migration_incomplete",
                ],
                "incomplete_alias_migrations": 1,
            }
        }
    )

    assert results["claude-code"]["error_code"] == "alias_migration_incomplete"
    store.complete_scan(scan_id, results=results, completed_at=1_001.0)
    snapshot = store.snapshot(now=1_002.0)

    assert snapshot["state"] == "degraded"
    assert snapshot["incomplete_alias_migrations"] == 1
    assert snapshot["sources"][0]["incomplete_alias_migrations"] == 1
    assert snapshot["sources"][0]["error_code"] == "alias_migration_incomplete"
    assert snapshot["issues"] == [
        {
            "code": "alias_migration_incomplete",
            "source": "claude-code",
            "action": (
                "agentacct preserved the legacy rows because the current client log did not "
                "reproduce every stored model lane. Repair the log or source path, then refresh again."
            ),
        }
    ]


def test_structural_claude_error_keeps_priority_across_multiple_codes(
    tmp_path: Path,
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    scan_id = store.begin_scan(
        sources=("claude-code",),
        scan_limit=20,
        importer_version="0.1.0",
        started_at=1_000.0,
    )
    results = health_scan_results(
        {
            "claude-code": {
                "discovered": 3,
                "parsed": 1,
                "error_count": 2,
                "error_codes": [
                    "claude_transcript_stat_failed",
                    "claude_workflow_journal_schema_drift",
                ],
            }
        }
    )

    assert results["claude-code"]["error_code"] == (
        "claude_workflow_journal_schema_drift"
    )
    store.complete_scan(scan_id, results=results, completed_at=1_001.0)
    snapshot = store.snapshot(now=1_002.0)

    source = snapshot["sources"][0]
    assert source["error_code"] == "claude_workflow_journal_schema_drift"
    assert source["error_codes"] == [
        "claude_transcript_stat_failed",
        "claude_workflow_journal_schema_drift",
    ]
    assert snapshot["issues"][0]["code"] == "source_adapter_incompatible"
    assert "Refresh alone cannot repair it." in snapshot["issues"][0]["action"]


def test_live_watcher_requires_current_receipts_and_ignores_out_of_scope_failures(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    historical = store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="old", pid=1, started_at=80.0
    )
    store.fail_scan(historical, error_code="old_failure", failed_at=81.0)
    store.acquire_watcher(
        lease_id="watch-claude",
        pid=2,
        importer_version="current",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("claude-code",),
        now=100.0,
    )

    pending = store.snapshot(now=101.0)
    assert pending["state"] == "unknown"
    assert {row["source"]: (row["state"], row["scope"]) for row in pending["sources"]} == {
        "claude-code": ("pending", "watched"),
        "codex": ("degraded", "historical"),
    }
    assert all(issue.get("source") != "codex" for issue in pending["issues"])

    current = store.begin_scan(
        sources=("claude-code",), scan_limit=20, importer_version="current", pid=2, started_at=102.0
    )
    store.complete_scan(
        current,
        results={"claude-code": {"discovered": 2, "parsed": 2, "error_count": 0}},
        completed_at=103.0,
    )
    healthy = store.snapshot(now=104.0)
    assert healthy["state"] == "healthy"
    assert healthy["last_success_at"] == 103.0
    assert healthy["issues"] == []


def test_failed_scan_preserves_last_success_and_degrades_with_action(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    first = store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="0.1.0", pid=1, started_at=100.0
    )
    store.complete_scan(
        first,
        results={"codex": {"discovered": 3, "parsed": 3, "skipped": 0, "error_count": 0}},
        completed_at=101.0,
    )
    failed = store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="0.1.1", pid=2, started_at=200.0
    )
    store.fail_scan(failed, error_code="sqlite_unavailable", failed_at=201.0)

    snapshot = store.snapshot(now=202.0)
    assert snapshot["state"] == "degraded"
    source = snapshot["sources"][0]
    assert source["last_success_at"] == 101.0
    assert source["last_failure_at"] == 201.0
    assert source["consecutive_failures"] == 1
    assert source["error_code"] == "sqlite_unavailable"
    assert snapshot["issues"] == [
        {
            "code": "source_scan_failed",
            "source": "codex",
            "action": "Refresh now or inspect the source setup.",
        }
    ]


def test_older_overlapping_scan_cannot_overwrite_newer_source_receipt(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    older = store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="old", pid=1, started_at=100.0
    )
    newer = store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="new", pid=2, started_at=200.0
    )
    store.complete_scan(
        newer,
        results={"codex": {"discovered": 4, "parsed": 4, "error_count": 0}},
        completed_at=201.0,
    )

    store.fail_scan(older, error_code="late_old_failure", failed_at=202.0)

    snapshot = store.snapshot(now=203.0)
    source = snapshot["sources"][0]
    assert source["state"] == "healthy"
    assert source["last_success_at"] == 201.0
    assert source["last_failure_at"] is None
    assert source["error_count"] == 0
    assert source["discovered"] == 4


def test_watcher_lease_rejects_live_duplicate_and_supersedes_stale_owner(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    first = store.acquire_watcher(
        lease_id="watch-a",
        pid=10,
        importer_version="old",
        interval_seconds=10.0,
        scan_limit=20,
        sources=("codex",),
        now=100.0,
    )
    assert first.acquired is True

    duplicate = store.acquire_watcher(
        lease_id="watch-b",
        pid=11,
        importer_version="new",
        interval_seconds=10.0,
        scan_limit=200,
        sources=("codex",),
        now=120.0,
    )
    assert duplicate.acquired is False
    assert duplicate.reason == "active_watcher_exists"
    assert duplicate.active_lease_id == "watch-a"
    assert store.heartbeat_watcher("watch-b", now=121.0) is False
    assert store.heartbeat_watcher("watch-a", now=121.0) is True
    store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="old", pid=10, started_at=122.0
    )

    replacement = store.acquire_watcher(
        lease_id="watch-c",
        pid=12,
        importer_version="new",
        interval_seconds=10.0,
        scan_limit=200,
        sources=("codex",),
        now=200.0,
    )
    assert replacement.acquired is True
    assert replacement.reason == "stale_watcher_replaced"
    assert replacement.superseded_lease_id == "watch-a"
    recovered = store.snapshot(now=201.0)
    assert recovered["watcher"]["lease_id"] == "watch-c"
    assert recovered["active_scan_count"] == 0
    assert all(issue["code"] != "scan_stuck" for issue in recovered["issues"])


def test_snapshot_preserves_dead_active_scan_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    scan_id = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="test",
        pid=991_991,
        started_at=100.0,
    )
    state_before = store.state_path.read_bytes()

    def unexpected_write(_state: object) -> None:
        raise AssertionError("snapshot must not repair active scans")

    monkeypatch.setattr(store, "_write_unlocked", unexpected_write)
    snapshot = store.snapshot(now=101.0)

    assert snapshot["active_scan_count"] == 1
    assert store.state_path.read_bytes() == state_before
    assert scan_id in json.loads(state_before)["active_scans"]


def test_repair_dead_active_scans_removes_only_definitively_dead_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    live_scan = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="test",
        pid=101,
        started_at=100.0,
    )
    dead_scan = store.begin_scan(
        sources=("claude-code",),
        scan_limit=20,
        importer_version="test",
        pid=102,
        started_at=100.0,
    )
    unreadable_scan = store.begin_scan(
        sources=("cursor",),
        scan_limit=20,
        importer_version="test",
        pid=103,
        started_at=100.0,
    )
    invalid_scan = store.begin_scan(
        sources=("gemini",),
        scan_limit=20,
        importer_version="test",
        pid=0,
        started_at=100.0,
    )
    probes: list[tuple[int, int]] = []

    def inspect(pid: int, sig: int) -> None:
        probes.append((pid, sig))
        if pid == 102:
            raise ProcessLookupError("process is gone")
        if pid == 103:
            raise PermissionError("inspection unavailable")

    monkeypatch.setattr(health_module.os, "kill", inspect)

    removed = store.repair_dead_active_scans(now=200.0)

    assert removed == (dead_scan,)
    assert {pid for pid, _signal in probes} == {101, 102, 103}
    assert all(signal == 0 for _pid, signal in probes)
    persisted = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert set(persisted["active_scans"]) == {
        live_scan,
        unreadable_scan,
        invalid_scan,
    }
    assert persisted["updated_at"] == 200.0


def test_repair_dead_active_scans_preserves_record_completed_during_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    scan_id = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="test",
        pid=991_991,
        started_at=100.0,
    )

    def complete_during_probe(_pid: object) -> str:
        store.complete_scan(
            scan_id,
            results={"codex": {"discovered": 1, "parsed": 1, "error_count": 0}},
            completed_at=101.0,
        )
        return health_module._WATCHER_PID_DEAD

    monkeypatch.setattr(health_module, "_watcher_pid_liveness", complete_during_probe)

    assert store.repair_dead_active_scans(now=200.0) == ()
    snapshot = store.snapshot(now=201.0)
    assert snapshot["active_scan_count"] == 0
    assert snapshot["sources"][0]["last_success_at"] == 101.0


def test_cli_repair_dead_scans_is_explicit_and_reports_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agent_chronicle.cli import app

    store_dir = tmp_path / "state"
    store = IngestionHealthStore(store_dir)
    scan_id = store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="test",
        pid=991_991,
        started_at=100.0,
    )

    def definitively_dead(_pid: int, signal_number: int) -> None:
        assert signal_number == 0
        raise ProcessLookupError("process is gone")

    monkeypatch.setattr(health_module.os, "kill", definitively_dead)
    result = CliRunner().invoke(
        app,
        ["usage", "repair-dead-scans", "--store-dir", str(store_dir), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["removed_count"] == 1
    assert payload["removed_scan_ids"] == [scan_id]
    assert payload["health"]["active_scan_count"] == 0


def test_runtime_watcher_snapshot_retires_only_a_definitively_dead_pid(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        store.acquire_watcher(
            lease_id="watch-subprocess",
            pid=process.pid,
            importer_version="test",
            interval_seconds=60.0,
            scan_limit=20,
            sources=("codex",),
        )
        live_snapshot, external = store.runtime_watcher_snapshot()
        assert external is True
        assert live_snapshot["watcher"]["state"] == "running"

        process.terminate()
        process.wait(timeout=5)

        stopped_snapshot, external = store.runtime_watcher_snapshot()
        assert external is False
        assert stopped_snapshot["watcher"]["state"] == "stopped"
        assert stopped_snapshot["watcher"]["lease_id"] == "watch-subprocess"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_runtime_watcher_snapshot_preserves_unknown_pid_and_never_signals_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    store.acquire_watcher(
        lease_id="watch-unreadable",
        pid=os.getpid(),
        importer_version="test",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
    )
    probes: list[tuple[int, int]] = []

    def permission_denied(pid: int, sig: int) -> None:
        probes.append((pid, sig))
        raise PermissionError("inspection unavailable")

    monkeypatch.setattr(health_module.os, "kill", permission_denied)

    snapshot, external = store.runtime_watcher_snapshot()

    assert probes == [(os.getpid(), 0)]
    assert external is True
    assert snapshot["watcher"]["state"] == "running"


def test_runtime_watcher_snapshot_cannot_release_a_replacement_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    store.acquire_watcher(
        lease_id="watch-old",
        pid=991_991,
        importer_version="old",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
    )

    def replace_during_probe(_pid: object) -> str:
        assert store.release_watcher("watch-old") is True
        replacement = store.acquire_watcher(
            lease_id="watch-new",
            pid=os.getpid(),
            importer_version="new",
            interval_seconds=60.0,
            scan_limit=20,
            sources=("codex",),
        )
        assert replacement.acquired is True
        return health_module._WATCHER_PID_DEAD

    monkeypatch.setattr(health_module, "_watcher_pid_liveness", replace_during_probe)

    snapshot, external = store.runtime_watcher_snapshot()

    assert external is True
    assert snapshot["watcher"]["state"] == "running"
    assert snapshot["watcher"]["lease_id"] == "watch-new"


def test_stale_watcher_and_stuck_scan_fail_visibly(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    store.acquire_watcher(
        lease_id="watch-a",
        pid=10,
        importer_version="0.1.0",
        interval_seconds=10.0,
        scan_limit=20,
        sources=("codex",),
        now=100.0,
    )
    store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="0.1.0", pid=10, started_at=101.0
    )

    snapshot = store.snapshot(now=200.0)
    assert snapshot["state"] == "degraded"
    assert snapshot["watcher"]["state"] == "stale"
    assert {issue["code"] for issue in snapshot["issues"]} == {"watcher_stale", "scan_stuck"}
    assert any(issue["action"] == "Restart usage watch." for issue in snapshot["issues"])


def test_running_watcher_with_old_build_is_not_reported_healthy(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    store.acquire_watcher(
        lease_id="watch-old",
        pid=10,
        importer_version="0.1.0+stale-build",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
        now=100.0,
    )

    snapshot = store.snapshot(now=101.0)

    assert snapshot["state"] == "degraded"
    assert snapshot["watcher"]["state"] == "running"
    assert any(issue["code"] == "watcher_version_mismatch" for issue in snapshot["issues"])
    assert any(
        issue["action"] == "Restart usage watch to load the current importer."
        for issue in snapshot["issues"]
    )


def test_corrupt_health_state_is_visible_but_next_scan_recovers(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    store.health_root.mkdir(parents=True)
    store.state_path.write_text("{torn", encoding="utf-8")

    corrupt = store.snapshot(now=100.0)
    assert corrupt["state"] == "degraded"
    assert corrupt["issues"] == [
        {
            "code": "health_state_corrupt",
            "source": None,
            "action": "Run a fresh usage scan to rebuild sync health.",
        }
    ]

    scan_id = store.begin_scan(
        sources=("claude-code",), scan_limit=50, importer_version="0.1.0", pid=4, started_at=101.0
    )
    store.complete_scan(
        scan_id,
        results={"claude-code": {"discovered": 1, "parsed": 1, "skipped": 0, "error_count": 0}},
        completed_at=102.0,
    )
    recovered = store.snapshot(now=103.0)
    assert recovered["state"] == "unknown"  # clean manual scan, but no live watcher claim
    assert recovered["issues"] == []
    assert recovered["sources"][0]["state"] == "healthy"


def test_schema_shaped_health_state_with_invalid_timestamps_is_degraded_not_exception(tmp_path: Path) -> None:
    watcher_store = IngestionHealthStore(tmp_path / "watcher-state")
    watcher_store.acquire_watcher(
        lease_id="watch-a",
        pid=1,
        importer_version="0.1.0",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
        now=100.0,
    )
    watcher_state = json.loads(watcher_store.state_path.read_text(encoding="utf-8"))
    watcher_state["watcher"]["heartbeat_at"] = [101.0]
    watcher_store.state_path.write_text(json.dumps(watcher_state), encoding="utf-8")
    assert watcher_store.snapshot(now=102.0)["issues"][0]["code"] == "health_state_corrupt"

    receipt_store = IngestionHealthStore(tmp_path / "receipt-state")
    scan_id = receipt_store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="0.1.0", pid=1, started_at=100.0
    )
    receipt_store.complete_scan(
        scan_id,
        results={"codex": {"discovered": 1, "parsed": 1, "error_count": 0}},
        completed_at=101.0,
    )
    receipt_state = json.loads(receipt_store.state_path.read_text(encoding="utf-8"))
    receipt_state["sources"]["codex"]["last_success_at"] = {"not": "a timestamp"}
    receipt_store.state_path.write_text(json.dumps(receipt_state), encoding="utf-8")
    assert receipt_store.snapshot(now=102.0)["issues"][0]["code"] == "health_state_corrupt"


def test_empty_store_is_unknown_not_healthy(tmp_path: Path) -> None:
    snapshot = IngestionHealthStore(tmp_path / "state").snapshot(now=100.0)
    assert snapshot["state"] == "unknown"
    assert snapshot["sources"] == []
    assert snapshot["watcher"] == {"state": "not_configured"}
    assert snapshot["issues"] == []


def test_acquire_watcher_reaps_scans_owned_by_dead_processes(tmp_path: Path) -> None:
    """A clean stop/start used to leave a killed watcher's in-flight scan
    registered forever: acquire took the non-stale path, the reaper never ran,
    and the health surface reported scan_stuck permanently while its
    "Restart usage watch" action could not clear it."""

    store = IngestionHealthStore(tmp_path / "state")
    # Deterministically dead: spawn a child and reap it before probing.
    child = subprocess.Popen(["true"])
    child.wait()
    dead_pid = child.pid
    store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="old", pid=dead_pid, started_at=100.0
    )

    result = store.acquire_watcher(
        lease_id="watch-fresh",
        pid=os.getpid(),
        importer_version="current",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
        now=10_000.0,
    )

    assert result.acquired is True
    assert result.reason == "acquired"
    state = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert state["active_scans"] == {}
    snapshot = store.snapshot(now=10_001.0)
    assert all(issue["code"] != "scan_stuck" for issue in snapshot["issues"])


def test_acquire_watcher_keeps_scans_owned_by_live_processes(tmp_path: Path) -> None:
    store = IngestionHealthStore(tmp_path / "state")
    live_scan = store.begin_scan(
        sources=("codex",), scan_limit=20, importer_version="current", pid=os.getpid(), started_at=100.0
    )

    store.acquire_watcher(
        lease_id="watch-live",
        pid=os.getpid(),
        importer_version="current",
        interval_seconds=60.0,
        scan_limit=20,
        sources=("codex",),
        now=101.0,
    )

    state = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert live_scan in state["active_scans"]
