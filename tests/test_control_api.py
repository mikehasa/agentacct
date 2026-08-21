from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.evidence import EvidenceEnvelope
from agentacct.evidence_runtime import EvidenceRuntime


def _append_billed_control_evidence(store: Path, *, run_id: str) -> EvidenceEnvelope:
    envelope = EvidenceEnvelope.create(
        assertion="observed",
        event_type="provider.cost.observed",
        source_type="provider_invoice",
        source_system="fixture-provider",
        source_instance="fixture-account",
        source_schema="fixture.v1",
        adapter="fixture.v1",
        source_event_id=f"api-cost-{run_id}",
        event_timestamp="2026-07-13T00:00:00Z",
        dimensions=("cost",),
        measurement_basis={"cost": "provider_billed"},
        subjects={"run_id": run_id},
        payload={"amount_usd": 1.0},
    )
    EvidenceRuntime(store).append(envelope)
    return envelope


def test_control_api_evaluates_but_never_dispatches(tmp_path) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))

    advisory = client.post(
        "/control/evaluate",
        json={
            "action": "warn",
            "target_id": "run-1",
            "recommendation": "Review this run.",
        },
    )
    assert advisory.status_code == 200
    assert advisory.json()["effective_mode"] == "advisory"
    assert not (store / "evidence-v2").exists()

    response = client.post(
        "/control/evaluate",
        json={
            "action": "pause",
            "target_id": "run-1",
            "recommendation": "Pause after orchestrator-reported budget threshold.",
            "requested_mode": "hard",
            "evidence_basis": "orchestrator_claim",
            "controller_owns_execution": True,
            "idempotency_key": "pause-run-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_mode"] == "advisory"
    assert payload["hard_enforcement_allowed"] is False
    assert payload["supporting_evidence_validation"]["state"] == "invalid"
    assert payload["external_action_dispatched"] is False
    assert not (store / "evidence-v2").exists()


def test_control_api_validates_format_existence_and_local_billed_basis(tmp_path) -> None:
    store = tmp_path / "state"
    evidence = _append_billed_control_evidence(store, run_id="run-billed")
    client = TestClient(create_local_api_app(store_dir=store))
    base = {
        "action": "pause",
        "target_id": "run-billed",
        "recommendation": "Pause after billed threshold.",
        "requested_mode": "hard",
        "evidence_basis": "provider_billed",
        "cost_confidence": "provider_billed",
        "controller_owns_execution": True,
        "expires_at": "2030-01-01T00:00:00Z",
        "idempotency_key": "pause-run-billed-api",
    }

    malformed = client.post(
        "/control/evaluate",
        json={**base, "supporting_evidence_ids": ["not-evidence"]},
    )
    assert malformed.status_code == 422

    missing = client.post(
        "/control/evaluate",
        json={**base, "supporting_evidence_ids": ["evd_" + ("0" * 64)]},
    )
    assert missing.status_code == 200
    assert missing.json()["hard_enforcement_allowed"] is False
    assert missing.json()["supporting_evidence_validation"]["missing_ids"] == [
        "evd_" + ("0" * 64)
    ]

    valid = client.post(
        "/control/evaluate",
        json={**base, "supporting_evidence_ids": [evidence.evidence_id]},
    )
    assert valid.status_code == 200
    assert valid.json()["hard_enforcement_allowed"] is True
    assert valid.json()["supporting_evidence_validation"]["state"] == "valid"
    assert valid.json()["external_action_dispatched"] is False
