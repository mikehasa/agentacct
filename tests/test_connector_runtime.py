from __future__ import annotations

import copy
from pathlib import Path

from agentacct.connector_runtime import import_connector_records
from agentacct.connectors import OpenLITOTLPConnector, PaperclipSnapshotConnector
from agentacct.evidence import EvidenceEnvelope
from agentacct.evidence_runtime import EvidenceRuntime


FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


def test_connector_runtime_import_is_local_and_idempotent(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    records = PaperclipSnapshotConnector().read(FIXTURES / "paperclip_snapshot.json")

    first = import_connector_records(runtime, records, connector="paperclip")
    second = import_connector_records(runtime, records, connector="paperclip")

    assert first.record_count == 7
    assert first.inserted_count == 7
    assert first.error_count == 0
    assert second.duplicate_count == 7
    assert runtime.store.stats().evidence_versions == 7
    assert all(envelope.assertion == "claimed" for envelope in runtime.envelopes())


def test_connector_runtime_dry_run_creates_no_evidence_store(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    records = OpenLITOTLPConnector().read(FIXTURES / "openlit_otlp.json")

    result = import_connector_records(runtime, records, connector="openlit", dry_run=True)

    assert result.record_count == 5
    assert result.inserted_count == 0
    assert len(result.evidence_ids) == 5
    assert not (tmp_path / "evidence-v2").exists()


def test_openlit_same_span_identity_preserves_changed_versions_as_conflicts(tmp_path) -> None:
    span = {
        "traceId": "2" * 32,
        "spanId": "d" * 16,
        "name": "coding_agent.llm.turn",
        "startTimeUnixNano": "1783904400000000000",
        "endTimeUnixNano": "1783904401000000000",
        "attributes": [
            {
                "key": "coding_agent.session.id",
                "value": {"stringValue": "session-conflict"},
            },
            {
                "key": "gen_ai.usage.input_tokens",
                "value": {"intValue": "10"},
            },
        ],
    }
    changed = copy.deepcopy(span)
    changed["attributes"][1]["value"]["intValue"] = "20"
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [span, changed]}]}]}

    connector = OpenLITOTLPConnector()
    records = connector.read(payload)
    reversed_payload = copy.deepcopy(payload)
    reversed_payload["resourceSpans"][0]["scopeSpans"][0]["spans"].reverse()

    assert len(records) == 4
    assert len({record.record_id for record in records}) == 2
    assert [record.to_dict() for record in records] == [
        record.to_dict() for record in connector.read(reversed_payload)
    ]

    runtime = EvidenceRuntime(tmp_path, enabled=True)
    result = import_connector_records(runtime, records, connector="openlit")

    assert result.inserted_count == 2
    assert result.conflict_count == 2
    assert result.duplicate_count == 0
    assert runtime.store.stats().conflict_groups == 2
    assert runtime.store.stats().evidence_versions == 4

    replay = import_connector_records(runtime, records, connector="openlit")

    assert replay.duplicate_count == 4
    assert replay.conflict_count == 0
    assert runtime.store.stats().evidence_versions == 4


def test_connector_runtime_disabled_is_explicit_and_creates_no_store(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=False)
    records = OpenLITOTLPConnector().read(FIXTURES / "openlit_otlp.json")

    result = import_connector_records(runtime, records, connector="openlit")

    assert result.record_count == 5
    assert result.error_count == 1
    assert result.errors == ("evidence_v2_disabled",)
    assert not (tmp_path / "evidence-v2").exists()


def test_paperclip_and_provider_raw_run_ids_require_an_explicit_link(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path, enabled=True)
    records = PaperclipSnapshotConnector().read(FIXTURES / "paperclip_snapshot.json")
    import_connector_records(runtime, records, connector="paperclip")
    provider = EvidenceEnvelope.create(
        assertion="observed",
        event_type="provider.invoice.observed",
        source_type="provider_invoice",
        source_system="openai",
        source_instance="account-redacted",
        source_schema="fixture.v1",
        adapter="fixture.v1",
        source_event_id="invoice-run-1",
        event_timestamp="2026-07-13T02:00:00Z",
        dimensions=("cost",),
        measurement_basis="provider_billed",
        subjects={"run_id": "run-1"},
        payload={"provider_billed_cost_usd": 1.25},
    )
    runtime.append(provider)

    paperclip_cost = next(
        envelope
        for envelope in runtime.envelopes()
        if envelope.source_system == "paperclip"
        and envelope.event_type == "orchestrator.cost.claim"
        and envelope.payload["attributes"].get("amount_usd") == 0
    )
    assert paperclip_cost.subjects.run_id == "run-1"
    product = runtime.product()
    assert "cost_value_conflict" not in product["discrepancies"]["by_kind"]
    run_nodes = [node for node in product["work_graph"]["nodes"] if node["kind"] == "run_id" and node["value"] == "run-1"]
    assert len(run_nodes) == 2
    assert len({node["namespace"] for node in run_nodes}) == 2
