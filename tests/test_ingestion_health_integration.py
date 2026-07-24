from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agent_chronicle.cli as cli_module
import agent_chronicle.api as api_module
import agent_chronicle.client_usage as client_usage_module
from agent_chronicle.api import UsageDiscoveryConfig, create_local_api_app
from agent_chronicle.cli import app
from agent_chronicle.ingestion_health import (
    INGESTION_HEALTH_DIRNAME,
    INGESTION_HEALTH_FILENAME,
    IngestionHealthStore,
)
from agent_chronicle.service import SentinelService
from agent_chronicle.usage_truth import is_local_session_observation_event
from refresh_flash import refresh_flash_qs, run_dashboard_refresh


def _make_codex_home(root: Path) -> Path:
    codex_home = root / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-07-15T00-00-00-health-session.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "health-session"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 120,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 150,
                                }
                            },
                            "model": "gpt-5.6-sol",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    database = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        database.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null,
                title text not null,
                tokens_used integer not null,
                model text,
                cli_version text
            )
            """
        )
        database.execute(
            "insert into threads values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "health-session",
                str(rollout),
                100,
                200,
                "/work/project",
                "Verify trusted ingestion",
                150,
                "gpt-5.6-sol",
                "0.test",
            ),
        )
        database.commit()
    finally:
        database.close()
    return codex_home


def _codex_import_args(store_dir: Path, codex_home: Path, *extra: str) -> list[str]:
    return [
        "usage",
        "import-local",
        "--client",
        "codex",
        "--store-dir",
        str(store_dir),
        "--codex-home",
        str(codex_home),
        *extra,
        "--json",
    ]


def _source(payload: dict[str, object], name: str) -> dict[str, object]:
    sources = payload.get("sources")
    assert isinstance(sources, list)
    return next(row for row in sources if isinstance(row, dict) and row.get("source") == name)


def test_import_local_writes_durable_per_source_receipt_and_returns_diagnostics(tmp_path: Path) -> None:
    codex_home = _make_codex_home(tmp_path / "home")
    store_dir = tmp_path / "state"

    result = CliRunner().invoke(app, _codex_import_args(store_dir, codex_home))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    diagnostic = payload["source_diagnostics"]["codex"]
    assert diagnostic["client"] == "codex"
    assert diagnostic["discovered"] == 1
    assert diagnostic["parsed"] == 1
    assert diagnostic["error_count"] == 0
    assert diagnostic["limit_unit"] == "root_groups"
    assert diagnostic["selected_root_groups"] == 1

    state_path = store_dir / INGESTION_HEALTH_DIRNAME / INGESTION_HEALTH_FILENAME
    assert state_path.is_file()
    persisted = IngestionHealthStore(store_dir).snapshot()
    assert persisted["state"] == "unknown"  # A manual scan is not a live-watcher claim.
    codex = _source(persisted, "codex")
    assert codex["state"] == "healthy"
    assert codex["discovered"] == 1
    assert codex["parsed"] == 1
    assert codex["error_count"] == 0
    assert payload["ingestion_health"] == persisted


def test_import_local_dry_run_returns_diagnostics_without_writing_health_state(tmp_path: Path) -> None:
    codex_home = _make_codex_home(tmp_path / "home")
    store_dir = tmp_path / "state"

    result = CliRunner().invoke(
        app,
        _codex_import_args(store_dir, codex_home, "--dry-run"),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["source_diagnostics"]["codex"]["parsed"] == 1
    assert not (store_dir / "events.jsonl").exists()
    assert not (store_dir / INGESTION_HEALTH_DIRNAME).exists()


def test_ingestion_health_endpoints_distinguish_service_liveness_from_sync_health(tmp_path: Path) -> None:
    store_dir = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store_dir))

    initial = client.get("/ingestion/health")
    assert initial.status_code == 200
    assert initial.json()["state"] == "unknown"
    assert initial.json()["sources"] == []

    service_health = client.get("/health")
    assert service_health.status_code == 200
    assert service_health.json()["ok"] is True
    assert service_health.json()["ingestion_status"] == "unknown"
    assert service_health.json()["ingestion"]["state"] == "unknown"

    health_store = IngestionHealthStore(store_dir)
    scan_id = health_store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="integration-test",
        pid=4242,
        started_at=time.time() - 2,
    )
    health_store.fail_scan(scan_id, error_code="sqlite_read_failed", failed_at=time.time() - 1)

    degraded = client.get("/ingestion/health")
    assert degraded.status_code == 200
    assert degraded.json()["state"] == "degraded"
    assert _source(degraded.json(), "codex")["error_code"] == "sqlite_read_failed"

    service_health = client.get("/health")
    assert service_health.status_code == 200
    assert service_health.json()["ok"] is True
    assert service_health.json()["ingestion_status"] == "degraded"
    assert service_health.json()["ingestion"]["state"] == "degraded"


def test_dashboard_degraded_sync_state_gives_a_concrete_action(tmp_path: Path) -> None:
    store_dir = tmp_path / "state"
    health_store = IngestionHealthStore(store_dir)
    scan_id = health_store.begin_scan(
        sources=("codex",),
        scan_limit=20,
        importer_version="integration-test",
        pid=4242,
        started_at=time.time() - 2,
    )
    health_store.fail_scan(scan_id, error_code="sqlite_read_failed", failed_at=time.time() - 1)

    html = TestClient(create_local_api_app(store_dir=store_dir)).get("/").text

    assert "Refresh now or inspect the source setup." in html
    assert 'action="/usage/import-local"' in html
    assert '<button class="button" type="submit">Refresh</button>' in html
    # A warning without a recovery path was the old product failure mode.
    assert "Needs attention" not in html


def test_claude_structural_recovery_is_source_specific_and_retries_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    claude_project = home / ".claude" / "projects" / "-tmp-project"
    claude_project.mkdir(parents=True)
    transcript = claude_project / "claude-session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "claude-session",
                "timestamp": "2026-07-17T00:00:00Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store_dir = tmp_path / "state"
    health_store = IngestionHealthStore(store_dir)
    failed = health_store.begin_scan(
        sources=("claude-code",),
        scan_limit=20,
        importer_version="integration-test",
        pid=4242,
        started_at=time.time() - 2,
    )
    health_store.complete_scan(
        failed,
        results={
            "claude-code": {
                "discovered": 2,
                "parsed": 1,
                "error_count": 2,
                "error_code": "claude_transcript_stat_failed",
                "error_codes": [
                    "claude_transcript_stat_failed",
                    "claude_workflow_journal_schema_drift",
                ],
            }
        },
        completed_at=time.time() - 1,
    )
    client = TestClient(
        create_local_api_app(
            store_dir=store_dir,
            usage_discovery=UsageDiscoveryConfig.from_home(home),
        )
    )

    home_html = client.get("/").text
    assert "Refresh alone cannot repair it." in home_html
    assert 'href="/advanced#activity-sync-recovery"' in home_html
    advanced_html = client.get("/advanced").text
    assert 'id="activity-sync-recovery"' in advanced_html
    assert "Parsed selected / all candidates" in advanced_html
    assert "Usage-bearing selected / all candidates" not in advanced_html
    assert (
        "agentacct usage import-local --client claude-code --dry-run --json"
        in advanced_html
    )
    assert 'action="/usage/import-local/claude-code"' in advanced_html

    def unexpected_pricing_refresh(**_kwargs):
        raise AssertionError("Claude recovery must not refresh the global pricing catalog")

    monkeypatch.setattr(
        api_module,
        "ensure_fresh_pricing_snapshot",
        unexpected_pricing_refresh,
    )
    recovered = client.post(
        "/usage/import-local/claude-code",
        follow_redirects=False,
    )
    assert recovered.status_code == 303
    recovered_health = client.get("/ingestion/health").json()
    assert recovered_health["issues"] == []
    assert {row["source"] for row in recovered_health["sources"]} == {
        "claude-code"
    }
    last_success = recovered_health["sources"][0]["last_success_at"]
    ledger_before_failure = SentinelService(
        store_dir,
        create=False,
    ).list_all_events()

    unresolved = claude_project / "late-child.jsonl"
    unresolved.write_bytes(
        b"x" * (client_usage_module._CLAUDE_IDENTITY_SCAN_MAX_BYTES + 1_024)
        + b"\n"
    )
    ledger_after_first_failed_scan = None
    for expected_failures in (1, 2):
        retry = client.post(
            "/usage/import-local/claude-code",
            follow_redirects=False,
        )
        assert retry.status_code == 303
        retry_health = client.get("/ingestion/health").json()
        claude = next(
            row
            for row in retry_health["sources"]
            if row["source"] == "claude-code"
        )
        assert retry_health["state"] == "degraded"
        assert retry_health["issues"][0]["code"] == "source_identity_unresolved"
        assert claude["consecutive_failures"] == expected_failures
        assert claude["last_success_at"] == last_success
        current_ledger = SentinelService(store_dir, create=False).list_all_events()
        added = [
            event
            for event in current_ledger
            if event not in ledger_before_failure
        ]
        assert len(added) == 1
        assert is_local_session_observation_event(added[0])
        assert added[0]["metadata"]["client_session_id"] == "claude-session"
        assert "estimated_input_tokens" not in added[0]
        assert "estimated_output_tokens" not in added[0]
        assert all(
            event.get("metadata", {}).get("client_session_id") != "late-child"
            for event in current_ledger
        )
        if ledger_after_first_failed_scan is None:
            ledger_after_first_failed_scan = current_ledger
        else:
            assert current_ledger == ledger_after_first_failed_scan

    unresolved.unlink()
    clean_retry = client.post(
        "/usage/import-local/claude-code",
        follow_redirects=False,
    )
    assert clean_retry.status_code == 303
    assert client.get("/ingestion/health").json()["issues"] == []
    assert (
        SentinelService(store_dir, create=False).list_all_events()
        == ledger_after_first_failed_scan
    )


def test_hermes_multiple_home_failure_requires_selection_not_refresh(tmp_path: Path) -> None:
    store_dir = tmp_path / "state"
    health_store = IngestionHealthStore(store_dir)
    scan_id = health_store.begin_scan(
        sources=("hermes",),
        scan_limit=20,
        importer_version="integration-test",
        pid=4242,
        started_at=time.time() - 2,
    )
    health_store.complete_scan(
        scan_id,
        results={
            "hermes": {
                "discovered": 0,
                "parsed": 0,
                "error_count": 1,
                "error_code": "hermes_multiple_source_homes_require_explicit_selection",
                "error_codes": [
                    "hermes_multiple_source_homes_require_explicit_selection"
                ],
            }
        },
        completed_at=time.time() - 1,
    )
    client = TestClient(create_local_api_app(store_dir=store_dir))

    health = client.get("/ingestion/health").json()
    home_html = client.get("/").text
    advanced_html = client.get("/advanced").text

    assert health["state"] == "degraded"
    assert health["issues"] == [
        {
            "code": "source_home_selection_required",
            "source": "hermes",
            "action": (
                "Multiple Hermes homes are configured, so agentacct stopped before reading or merging "
                "them. Set HERMES_HOME to one absolute Hermes home in the dashboard process and restart "
                "agentacct. Refreshing the unchanged configuration cannot repair this."
            ),
        }
    ]
    assert "Refreshing the unchanged configuration cannot repair this." in home_html
    assert 'href="/advanced#activity-sync-recovery"' in home_html
    assert '<button class="button" type="submit">Refresh now</button>' not in home_html
    assert "Choose and verify one home:" in advanced_html
    assert (
        "agentacct usage import-local --client hermes --hermes-home "
        "/absolute/path/to/chosen-home --dry-run --json"
        in advanced_html
    )


def test_dashboard_refresh_persists_receipts_exposed_by_health_api(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_codex_home(home)
    store_dir = tmp_path / "state"
    client = TestClient(
        create_local_api_app(
            store_dir=store_dir,
            usage_discovery=UsageDiscoveryConfig.from_home(home),
        )
    )

    response = run_dashboard_refresh(client)

    assert response.status_code == 303
    assert "imported=1" in refresh_flash_qs(response)
    health = client.get("/ingestion/health").json()
    assert health["state"] == "unknown"  # Manual refresh succeeded; no watcher is running.
    assert {row["source"] for row in health["sources"]} == {
        "claude-code",
        "codex",
        "cursor",
        "hermes",
        "openclaw",
        "opencode",
    }
    codex = _source(health, "codex")
    assert codex["state"] == "healthy"
    assert codex["parsed"] == 1


def test_usage_watch_refuses_a_second_live_watcher_before_scanning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store_dir = tmp_path / "state"
    health_store = IngestionHealthStore(store_dir)
    acquired = health_store.acquire_watcher(
        lease_id="existing-watch",
        pid=4242,
        importer_version="integration-test",
        interval_seconds=60,
        scan_limit=20,
        sources=("codex",),
        now=time.time(),
    )
    assert acquired.acquired is True

    def _unexpected_scan(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("duplicate watcher must be rejected before scanning")

    monkeypatch.setattr(cli_module, "_local_usage_import_payload", _unexpected_scan)
    result = CliRunner().invoke(
        app,
        [
            "usage",
            "watch",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    output = result.output.lower()
    assert "watcher" in output
    assert "active" in output or "already" in output


def test_usage_watch_sigterm_releases_lease_without_waiting_for_interval(tmp_path: Path) -> None:
    store_dir = tmp_path / "state"
    codex_home = tmp_path / "empty-codex-home"
    codex_home.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(repo_root / "src")
        if not existing_pythonpath
        else f"{repo_root / 'src'}{os.pathsep}{existing_pythonpath}"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_chronicle.cli",
            "usage",
            "watch",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--codex-home",
            str(codex_home),
            "--interval-seconds",
            "60",
            "--json",
        ],
        cwd=repo_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            snapshot = IngestionHealthStore(store_dir).snapshot()
            watcher = snapshot.get("watcher")
            if isinstance(watcher, dict) and watcher.get("state") == "running":
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"watcher exited early ({process.returncode}): {stdout}\n{stderr}")
            time.sleep(0.05)
        else:
            raise AssertionError("usage watcher did not acquire its lease")

        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=4) == 0
        assert time.monotonic() - started < 4.0

        stopped = IngestionHealthStore(store_dir).snapshot()
        assert stopped["watcher"]["state"] == "stopped"
        assert stopped["watcher"]["pid"] == process.pid
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
