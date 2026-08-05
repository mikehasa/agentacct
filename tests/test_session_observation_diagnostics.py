from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agentacct.cli as cli_module
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


def _reject_observations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    _reject_observations(monkeypatch, tmp_path, reason)
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
def test_import_persists_specific_observation_conflict_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    _reject_observations(monkeypatch, tmp_path, reason)
    store_dir = tmp_path / "state"

    result = CliRunner().invoke(
        cli_module.app,
        [
            "usage",
            "import-local",
            "--client",
            "codex",
            "--store-dir",
            str(store_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["session_observation_conflict_rows"] == 1

    # The non-dry-run import must persist the specific conflict reason to the
    # ingestion-health store so later readers see it without re-importing.
    snapshot = IngestionHealthStore(store_dir).snapshot()
    source = snapshot["sources"][0]
    assert source["session_observation_conflict_reasons"] == {reason: 1}
    assert reason in source["error_codes"]
    assert "session_observation_conflict" in source["error_codes"]
    assert snapshot["issues"][0]["code"] == reason
