from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.evidence import EvidenceEnvelope
from agentacct.evidence_runtime import EvidenceRuntime


FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


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


def test_otlp_http_receiver_is_metadata_only_and_idempotent(tmp_path) -> None:
    store = tmp_path / "state"
    client = TestClient(create_local_api_app(store_dir=store))
    payload = json.loads((FIXTURES / "openlit_otlp.json").read_text(encoding="utf-8"))

    first = client.post("/v1/traces", json=payload)
    second = client.post("/v1/traces", json=payload)

    assert first.status_code == 200, first.text
    assert first.json()["chronicle"]["inserted_count"] == 5
    assert second.json()["chronicle"]["duplicate_count"] == 5
    evidence = client.get("/evidence/events?source_type=otlp_http_json&limit=100").text
    for secret in (
        "OPENLIT_PROMPT_SECRET",
        "OPENLIT_RESPONSE_SECRET",
        "OPENLIT_THOUGHT_SECRET",
        "OPENLIT_TOOL_ARGUMENT_SECRET",
        "OPENLIT_TOOL_RESULT_SECRET",
    ):
        assert secret not in evidence
        assert secret.encode("utf-8") not in (store / "evidence-v2" / "spool.jsonl").read_bytes()
        assert secret.encode("utf-8") not in (store / "evidence-v2" / "projection.sqlite3").read_bytes()
        for page in ("/work-graph", "/evidence-matrix", "/discrepancies", "/cost-outcome-basis"):
            assert secret not in client.get(page).text
    assert client.get("/evidence/status").json()["stats"]["evidence_versions"] == 5


def test_paperclip_http_import_preserves_missing_cost_as_missing(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    payload = json.loads((FIXTURES / "paperclip_snapshot.json").read_text(encoding="utf-8"))

    response = client.post("/connectors/paperclip/import", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["inserted_count"] == 7
    basis = client.get("/evidence/cost-outcome-basis").json()["cost_outcome_basis"]
    cost_records = [record for record in basis["records"] if record["dimension"] == "cost"]
    assert len(cost_records) == 2
    assert any(record["values"] == {} for record in cost_records)
    assert any(record["values"] == {"amount_usd": 0} for record in cost_records)
    assert "total_cost_usd" not in basis


def test_connector_json_body_is_bounded_and_must_be_object(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))

    assert client.post("/connectors/openlit/import", content=b"not-json").status_code == 422
    assert client.post("/connectors/openlit/import", json=[]).status_code == 422


def test_openlit_extreme_timestamp_is_a_structured_422(tmp_path) -> None:
    client = TestClient(create_local_api_app(store_dir=tmp_path / "state"))
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "3" * 32,
                                "spanId": "e" * 16,
                                "name": "coding_agent.session",
                                "startTimeUnixNano": "1" + ("0" * 400),
                                "endTimeUnixNano": "1" + ("0" * 400),
                            }
                        ]
                    }
                ]
            }
        ]
    }

    for path in ("/v1/traces", "/connectors/openlit/import"):
        response = client.post(path, json=payload)

        assert response.status_code == 422
        assert response.json() == {
            "detail": "OTLP startTimeUnixNano is outside the supported uint64 range"
        }


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
