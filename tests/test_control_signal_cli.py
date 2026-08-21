from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentacct.cli import app
from agentacct.evidence import EvidenceEnvelope
from agentacct.evidence_runtime import EvidenceRuntime


runner = CliRunner()


def _append_cli_billed_evidence(store: Path, *, run_id: str) -> EvidenceEnvelope:
    envelope = EvidenceEnvelope.create(
        assertion="observed",
        event_type="provider.cost.observed",
        source_type="provider_invoice",
        source_system="fixture-provider",
        source_instance="fixture-account",
        source_schema="fixture.v1",
        adapter="fixture.v1",
        source_event_id=f"cli-cost-{run_id}",
        event_timestamp="2026-07-13T00:00:00Z",
        dimensions=("cost",),
        measurement_basis={"cost": "provider_billed"},
        subjects={"run_id": run_id},
        payload={"amount_usd": 1.0},
    )
    EvidenceRuntime(store).append(envelope)
    return envelope


def test_control_cli_downgrades_untrusted_hard_signal_without_dispatch(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "control",
            "evaluate",
            "--action",
            "pause",
            "--target-id",
            "run-1",
            "--recommendation",
            "Pause after estimated threshold.",
            "--requested-mode",
            "hard",
            "--evidence-basis",
            "orchestrator_claim",
            "--controller-owns-execution",
            "--idempotency-key",
            "pause-run-1",
            "--store-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective_mode"] == "advisory"
    assert payload["hard_enforcement_allowed"] is False
    assert payload["external_action_dispatched"] is False


def test_control_cli_can_mark_store_bound_provider_billed_signal_eligible_but_not_dispatch(tmp_path) -> None:
    store = tmp_path / "state"
    evidence = _append_cli_billed_evidence(store, run_id="run-1")
    result = runner.invoke(
        app,
        [
            "control",
            "evaluate",
            "--action",
            "pause",
            "--target-id",
            "run-1",
            "--recommendation",
            "Pause after billed threshold.",
            "--requested-mode",
            "hard",
            "--evidence-basis",
            "provider_billed",
            "--cost-confidence",
            "provider_billed",
            "--controller-owns-execution",
            "--evidence-id",
            evidence.evidence_id,
            "--idempotency-key",
            "pause-run-1-billed",
            "--expires-at",
            "2030-01-01T00:00:00Z",
            "--store-dir",
            str(store),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective_mode"] == "hard"
    assert payload["hard_enforcement_allowed"] is True
    assert payload["supporting_evidence_validation"]["state"] == "valid"
    assert payload["external_action_dispatched"] is False


def test_control_cli_rejects_malformed_evidence_id() -> None:
    result = runner.invoke(
        app,
        [
            "control",
            "evaluate",
            "--action",
            "pause",
            "--target-id",
            "run-1",
            "--recommendation",
            "Pause.",
            "--requested-mode",
            "hard",
            "--evidence-id",
            "not-evidence",
        ],
    )

    assert result.exit_code == 2
    assert "supporting evidence ids" in result.output
    assert "lowercase hex" in result.output


def test_control_cli_disabled_evidence_store_fails_closed(tmp_path, monkeypatch) -> None:
    store = tmp_path / "state"
    evidence = _append_cli_billed_evidence(store, run_id="run-disabled")
    monkeypatch.setenv("AGENT_CHRONICLE_EVIDENCE_V2", "0")

    result = runner.invoke(
        app,
        [
            "control",
            "evaluate",
            "--action",
            "pause",
            "--target-id",
            "run-disabled",
            "--recommendation",
            "Pause.",
            "--requested-mode",
            "hard",
            "--evidence-basis",
            "provider_billed",
            "--cost-confidence",
            "provider_billed",
            "--controller-owns-execution",
            "--evidence-id",
            evidence.evidence_id,
            "--idempotency-key",
            "pause-run-disabled",
            "--expires-at",
            "2030-01-01T00:00:00Z",
            "--store-dir",
            str(store),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective_mode"] == "advisory"
    assert payload["hard_enforcement_allowed"] is False
    assert payload["supporting_evidence_validation"]["state"] == "unavailable"
    assert payload["external_action_dispatched"] is False
