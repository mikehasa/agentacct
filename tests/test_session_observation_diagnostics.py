from __future__ import annotations

import json
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import agentacct.api as api_module
import agentacct.cli as cli_module
from agentacct.api import UsageDiscoveryConfig, create_local_api_app
from agentacct.client_usage import (
    ClientSessionObservation,
    ClientUsageDiscoveryResult,
)
from agentacct.ingestion_health import IngestionHealthStore
from agentacct.service import SentinelService, SessionObservationConflict


def _discovery(tmp_path: Path) -> ClientUsageDiscoveryResult:
    return ClientUsageDiscoveryResult(
        events=[],
        diagnostics={
            "codex": {
                "discovered": 1,
                "parsed": 1,
                "skipped": 0,
                "error_count": 0,
                "error_codes": [],
            }
        },
        session_observations=[
            ClientSessionObservation(
                client="codex",
                client_session_id="observation-conflict-session",
                source_path=tmp_path / "rollout.jsonl",
                title="Observed without usage",
                updated_at=1_700_000_000,
                source_revision_at=1_700_000_000_000_000_000,
                source_namespace_fingerprint="codex-home-a",
            )
        ],
    )


@pytest.mark.parametrize(
    "reason",
    [
        "source_namespace_conflict",
        "same_watermark_conflict",
        "source_watermark_unorderable",
        "invalid_observation",
    ],
)
def test_cli_import_reports_specific_observation_conflict_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    discovery = _discovery(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "discover_client_usage_with_diagnostics",
        lambda **_kwargs: discovery,
    )

    def reject(
        _service: SentinelService,
        _event: dict,
        *,
        transport: str | None = "internal",
    ) -> dict:
        del transport
        raise SessionObservationConflict(reason, "test conflict")

    monkeypatch.setattr(
        SentinelService,
        "record_trusted_session_observation",
        reject,
    )
    result = CliRunner().invoke(
        cli_module.app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # The monkeypatched rejection exercises one incoming conflict row but does
    # not persist a complete raw-identity cohort. The row counter carries this
    # adapter diagnostic; the identity counter is reserved for durable
    # quarantines observed in the store.
    assert payload["session_observation_conflict_rows"] == 1
    assert payload["session_observation_conflict_reasons"] == {reason: 1}
    assert payload["session_observation_conflict_reasons_by_client"] == {
        "codex": {reason: 1}
    }
    source = payload["source_diagnostics"]["codex"]
    assert source["session_observation_conflict_reasons"] == {reason: 1}
    assert reason in source["error_codes"]
    assert "session_observation_conflict" in source["error_codes"]


@pytest.mark.parametrize(
    "reason",
    [
        "source_namespace_conflict",
        "same_watermark_conflict",
        "source_watermark_unorderable",
        "invalid_observation",
    ],
)
def test_dashboard_import_persists_specific_observation_conflict_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    discovery = _discovery(tmp_path)
    monkeypatch.setattr(
        api_module,
        "_discover_local_usage",
        lambda *_args, **_kwargs: discovery,
    )

    def reject(
        _service: SentinelService,
        _event: dict,
        *,
        transport: str | None = "internal",
    ) -> dict:
        del transport
        raise SessionObservationConflict(reason, "test conflict")

    monkeypatch.setattr(
        SentinelService,
        "record_trusted_session_observation",
        reject,
    )
    store_dir = tmp_path / "state"
    service = SentinelService(store_dir)
    health = IngestionHealthStore(store_dir)

    result = api_module._import_local_usage_events(
        service,
        UsageDiscoveryConfig(enabled=True),
        health,
        client="codex",
    )

    assert result["session_observation_conflict_rows"] == 1
    snapshot = health.snapshot()
    source = snapshot["sources"][0]
    assert source["session_observation_conflict_reasons"] == {reason: 1}
    assert reason in source["error_codes"]
    assert "session_observation_conflict" in source["error_codes"]
    assert snapshot["issues"][0]["code"] == reason

    client = TestClient(create_local_api_app(store_dir=store_dir))
    home_html = client.get("/").text
    assert "Open recovery steps" in home_html
    assert 'href="/advanced#activity-sync-recovery"' in home_html
    assert str(snapshot["issues"][0]["action"]) in unescape(home_html)

    advanced_html = client.get("/advanced").text
    assert 'id="activity-sync-recovery"' in advanced_html
    assert "Codex source recovery" in advanced_html
    assert str(snapshot["issues"][0]["action"]) in unescape(advanced_html)
    assert (
        "agentacct usage import-local --client codex --dry-run --json"
        in advanced_html
    )
    assert 'action="/usage/import-local/codex"' in advanced_html
    assert "Retry Codex after repair" in advanced_html


def test_observation_recovery_post_retries_only_the_selected_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_clients: list[str] = []

    def import_one_source(
        _service: SentinelService,
        _usage_config: UsageDiscoveryConfig,
        _health: IngestionHealthStore,
        *,
        limit_sessions: int = 500,
        client: str = "all",
    ) -> dict[str, int]:
        del limit_sessions
        selected_clients.append(client)
        return {"scanned": 0}

    monkeypatch.setattr(api_module, "_import_local_usage_events", import_one_source)
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    response = client.post("/usage/import-local/codex", follow_redirects=False)

    assert response.status_code == 303
    assert selected_clients == ["codex"]
    assert client.post(
        "/usage/import-local/not-a-client",
        follow_redirects=False,
    ).status_code == 404
