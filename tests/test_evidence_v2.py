from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent_chronicle.evidence import (
    CLAIMED_LINK_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    ClaimedLink,
    Completeness,
    EvidenceEnvelope,
    PrivacyMetadata,
    SubjectRefs,
    Truncation,
    adapt_v1_event,
    raw_digest_for,
)
from agent_chronicle.source_policy import Authority, EvidenceDimension, default_source_authority_policy


EVENT_TIME = "2026-07-13T08:30:00+08:00"


def _observed(**overrides: object) -> EvidenceEnvelope:
    values: dict[str, object] = {
        "assertion": "observed",
        "event_type": "tool_completed",
        "source_type": "client_hook",
        "source_system": "claude-code",
        "source_instance": "workstation-a",
        "source_schema": "claude-hook.v1",
        "adapter": "claude-code-hook-adapter.v1",
        "source_event_id": "hook-event-17",
        "event_timestamp": EVENT_TIME,
        "observed_at": "2026-07-13T00:30:01Z",
        "dimensions": ("tool_activity", "session_identity"),
        "measurement_basis": {
            "tool_activity": "client_hook_observed",
            "session_identity": "client_hook_observed",
        },
        "completeness": Completeness(status="complete", covered_dimensions=("tool_activity", "session_identity")),
        "subjects": SubjectRefs(client_session_id="session-1", turn_id="turn-2", tool_call_id="call-3"),
        "payload": {"tool_name": "Read", "duration_ms": 4},
        "raw_digest": raw_digest_for(b"raw hook row"),
        "raw_ref": "claude://session-1/hook-event-17",
    }
    values.update(overrides)
    if "dimensions" in overrides and "completeness" not in overrides:
        values["completeness"] = Completeness(status="complete", covered_dimensions=tuple(values["dimensions"]))  # type: ignore[arg-type]
    return EvidenceEnvelope.create(**values)  # type: ignore[arg-type]


def _claimed(**overrides: object) -> EvidenceEnvelope:
    values: dict[str, object] = {
        "assertion": "claimed",
        "claimant": "agent:codex",
        "event_type": "task_checkpoint",
        "source_type": "mcp_agent_reported",
        "source_system": "codex",
        "source_instance": "workstation-a",
        "source_schema": "sentinel-mcp.v1",
        "adapter": "mcp-v1-adapter",
        "source_event_id": "evt_abc123",
        "event_timestamp": EVENT_TIME,
        "dimensions": ("task_semantics",),
        "measurement_basis": "agent_claimed",
        "subjects": SubjectRefs(client_session_id="session-1", section_id="evidence-kernel"),
        "payload": {"status": "checkpoint", "files": ["evidence.py"]},
    }
    values.update(overrides)
    return EvidenceEnvelope.create(**values)  # type: ignore[arg-type]


def test_envelope_is_deeply_immutable_and_deterministic() -> None:
    first = _observed(dimensions=("session_identity", "tool_activity"))
    second = _observed(dimensions=("tool_activity", "session_identity"))

    assert first.schema_version == EVIDENCE_SCHEMA_VERSION
    assert first.event_timestamp == "2026-07-13T00:30:00.000000Z"
    assert first.idempotency_key == second.idempotency_key
    assert first.integrity_hash == second.integrity_hash
    assert first.evidence_id == second.evidence_id
    assert first.verify_integrity() is True
    assert {first, second} == {first}

    with pytest.raises(FrozenInstanceError):
        first.event_type = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.payload["tool_name"] = "Write"  # type: ignore[index]
    with pytest.raises(TypeError):
        first.measurement_basis["tool_activity"] = "forged"  # type: ignore[index]


def test_observed_and_claimed_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="observed evidence cannot carry claimant"):
        _observed(claimant="agent:codex")
    with pytest.raises(ValueError, match="claimed evidence requires claimant"):
        _claimed(claimant=None)
    with pytest.raises(ValueError, match="assertion"):
        _observed(assertion="mixed")


def test_measurement_completeness_and_truncation_invariants() -> None:
    with pytest.raises(ValueError, match="exactly every evidence dimension"):
        _observed(measurement_basis={"tool_activity": "client_hook_observed"})
    with pytest.raises(ValueError, match="both covered and missing"):
        Completeness(status="partial", covered_dimensions=("usage",), missing_dimensions=("usage",))
    with pytest.raises(ValueError, match="reason_code"):
        Truncation(truncated=True)
    with pytest.raises(ValueError, match="cannot claim complete"):
        _observed(
            truncation=Truncation(truncated=True, reason_code="adapter_cap"),
            completeness=Completeness(status="complete", covered_dimensions=("tool_activity", "session_identity")),
        )

    truncated = _observed(
        truncation=Truncation(truncated=True, reason_code="adapter_cap", original_count=2000, retained_count=200),
        completeness=Completeness(status="partial", missing_dimensions=("tool_result_count",)),
    )
    assert truncated.truncation.truncated is True
    assert truncated.truncation.original_count == 2000
    assert truncated.completeness.status == "partial"


def test_prompt_and_tool_bodies_are_omitted_by_default() -> None:
    secret = "body-must-never-reach-the-envelope"
    envelope = _observed(
        payload={
            "tool_name": "Bash",
            "tool_input": {"command": secret},
            "command": secret,
            "arguments": {"path": secret},
            "api_key": secret,
            "messages": [{"role": "user", "content": secret}],
            "input_tokens": 17,
        }
    )

    serialized = str(envelope.to_dict())
    assert secret not in serialized
    assert envelope.payload == {"tool_name": "Bash", "messages": ({"role": "user"},), "input_tokens": 17}
    assert envelope.privacy.redacted is True
    assert envelope.privacy.redacted_fields == ("api_key", "arguments", "command", "messages.0.content", "tool_input")
    assert envelope.privacy.redaction_methods == ("field_omission",)


def test_common_snake_and_camel_case_secrets_are_always_omitted() -> None:
    canaries = {
        "aws_secret_access_key": "AWS_SECRET_CANARY",
        "clientSecret": "CLIENT_SECRET_CANARY",
        "githubToken": "GITHUB_TOKEN_CANARY",
        "apiToken": "API_TOKEN_CANARY",
        "AWSAccessKeyId": "AWS_ACCESS_CANARY",
        "requestHeaders": {"x-api-key": "HEADER_CANARY"},
    }
    envelope = _observed(payload={"metadata": canaries, "input_tokens": 17})

    serialized = str(envelope.to_dict())
    assert all(
        value not in serialized
        for value in (
            "AWS_SECRET_CANARY",
            "CLIENT_SECRET_CANARY",
            "GITHUB_TOKEN_CANARY",
            "API_TOKEN_CANARY",
            "AWS_ACCESS_CANARY",
            "HEADER_CANARY",
        )
    )
    assert envelope.payload == {"metadata": {}, "input_tokens": 17}
    assert envelope.privacy.redacted is True
    assert envelope.privacy.redacted_fields == (
        "metadata.AWSAccessKeyId",
        "metadata.apiToken",
        "metadata.aws_secret_access_key",
        "metadata.clientSecret",
        "metadata.githubToken",
        "metadata.requestHeaders",
    )


def test_v1_shadow_adapter_does_not_copy_secret_metadata() -> None:
    canaries = {
        "aws_secret_access_key": "AWS_SECRET_CANARY",
        "clientSecret": "CLIENT_SECRET_CANARY",
        "safe": "kept",
    }
    envelope = adapt_v1_event(
        {
            "event_id": "evt_secret_fixture",
            "event_type": "task_checkpoint",
            "created_at": "2026-07-13T00:30:00Z",
            "source": "codex",
            "metadata": canaries,
        }
    )

    serialized = str(envelope.to_dict())
    assert "AWS_SECRET_CANARY" not in serialized
    assert "CLIENT_SECRET_CANARY" not in serialized
    assert envelope.payload["legacy"]["metadata"] == {"safe": "kept"}
    assert "legacy.metadata.aws_secret_access_key" in envelope.privacy.redacted_fields
    assert "legacy.metadata.clientSecret" in envelope.privacy.redacted_fields


def test_raw_content_requires_explicit_restricted_opt_in() -> None:
    with pytest.raises(ValueError, match="requires PrivacyMetadata"):
        _observed(payload={"prompt": "explicit secret"}, include_raw_content=True)

    envelope = _observed(
        payload={"prompt": "explicit secret"},
        include_raw_content=True,
        privacy=PrivacyMetadata(
            classification="restricted",
            content_capture="full",
            raw_content_included=True,
        ),
    )
    assert envelope.payload["prompt"] == "explicit secret"
    assert envelope.privacy.raw_content_included is True

    with_secret_key = _observed(
        payload={"prompt": "allowed raw prompt", "githubToken": "NEVER_STORE_THIS"},
        include_raw_content=True,
        privacy=PrivacyMetadata(
            classification="restricted",
            content_capture="full",
            raw_content_included=True,
        ),
    )
    assert with_secret_key.payload == {"prompt": "allowed raw prompt"}
    assert "NEVER_STORE_THIS" not in str(with_secret_key.to_dict())


def test_from_dict_cannot_use_raw_content_opt_in_to_reintroduce_secrets() -> None:
    envelope = _observed(
        payload={"prompt": "allowed raw prompt"},
        include_raw_content=True,
        privacy=PrivacyMetadata(
            classification="restricted",
            content_capture="full",
            raw_content_included=True,
        ),
    )
    forged = envelope.to_dict()
    forged["payload"]["apiKey"] = "RAW_SECRET_CANARY"
    forged["integrity_hash"] = ""
    forged["evidence_id"] = ""

    with pytest.raises(ValueError, match="secret fields are never permitted"):
        EvidenceEnvelope.from_dict(forged)


def test_absolute_raw_refs_are_pseudonymized() -> None:
    envelope = _observed(raw_ref="/Users/alice/.claude/projects/private/session.jsonl")
    assert envelope.raw_ref is not None
    assert envelope.raw_ref.startswith("local-redacted://session.jsonl#")
    assert "/Users/alice" not in envelope.raw_ref
    assert "raw_ref" in envelope.privacy.redacted_fields


def test_vendor_subject_aliases_map_to_indexable_core_refs_and_preserve_extras() -> None:
    subjects = SubjectRefs.from_dict(
        {
            "client_session": "session-vendor",
            "work_item": "issue-17",
            "execution": "run-4",
            "principal": "agent-2",
            "commit": "abc123",
        }
    )
    assert subjects.client_session_id == "session-vendor"
    assert subjects.work_id == "issue-17"
    assert subjects.run_id == "run-4"
    assert subjects.agent_id == "agent-2"
    assert subjects.extra == {"commit": "abc123"}


def test_dict_round_trip_verifies_integrity_and_rejects_tampering() -> None:
    envelope = _observed()
    restored = EvidenceEnvelope.from_dict(envelope.to_dict())
    assert restored == envelope
    assert restored.verify_integrity() is True

    tampered = envelope.to_dict()
    tampered["payload"]["duration_ms"] = 999
    with pytest.raises(ValueError, match="integrity_hash"):
        EvidenceEnvelope.from_dict(tampered)


def test_same_source_event_with_corrected_content_is_same_logical_key_but_new_version() -> None:
    original = _observed(payload={"tool_name": "Read", "duration_ms": 4})
    corrected = _observed(payload={"tool_name": "Read", "duration_ms": 5})
    assert original.idempotency_key == corrected.idempotency_key
    assert original.integrity_hash != corrected.integrity_hash
    assert original.evidence_id != corrected.evidence_id


def test_claimed_link_is_directional_immutable_and_deterministic() -> None:
    claimed = _claimed()
    observed = _observed(dimensions=("task_semantics",), measurement_basis="client_hook_observed")
    first = ClaimedLink.create(
        claimed_evidence_id=claimed.evidence_id,
        observed_evidence_id=observed.evidence_id,
        relationship="corroborates",
        dimensions=("task_semantics",),
        created_at=EVENT_TIME,
        created_by="joiner.v1",
    )
    second = ClaimedLink.from_dict(first.to_dict())

    assert first.schema_version == CLAIMED_LINK_SCHEMA_VERSION
    assert first == second
    assert first.link_id.startswith("clm_")
    with pytest.raises(FrozenInstanceError):
        first.relationship = "contradicts"  # type: ignore[misc]
    with pytest.raises(ValueError, match="cannot point to itself"):
        ClaimedLink.create(
            claimed_evidence_id=claimed.evidence_id,
            observed_evidence_id=claimed.evidence_id,
            relationship="corroborates",
            dimensions=("task_semantics",),
            created_at=EVENT_TIME,
            created_by="joiner.v1",
        )


def test_authority_is_resolved_per_source_and_dimension() -> None:
    policy = default_source_authority_policy()
    hook = _observed()
    assert policy.evaluate(hook, EvidenceDimension.TOOL_ACTIVITY).authority is Authority.AUTHORITATIVE
    assert policy.evaluate(hook, EvidenceDimension.SESSION_IDENTITY).authority is Authority.AUTHORITATIVE

    local_usage = _observed(
        event_type="usage_observed",
        source_type="local_client_log",
        source_schema="codex-rollout.v1",
        adapter="codex-usage.v1",
        dimensions=("usage", "cost"),
        measurement_basis={"usage": "client_reported", "cost": "estimated_from_tokens"},
        payload={"input_tokens": 10, "estimated_cost_usd": 0.001},
    )
    assert policy.evaluate(local_usage, "usage").authority is Authority.AUTHORITATIVE
    assert policy.evaluate(local_usage, "cost").authority is Authority.CORROBORATING
    assert policy.evaluate(local_usage, "task_semantics").authority is Authority.NONE


def test_claim_never_inherits_observation_authority_from_source_type() -> None:
    forged_shape = _claimed(
        source_type="client_hook",
        source_system="claude-code",
        dimensions=("tool_activity",),
        measurement_basis="agent_claimed",
    )
    decision = default_source_authority_policy().evaluate(forged_shape, "tool_activity")
    assert decision.authority is Authority.CLAIMED
    assert decision.reason == "explicit_claim_not_observation"


def test_openlit_and_git_authority_stays_dimension_specific() -> None:
    policy = default_source_authority_policy()
    otlp = _observed(
        source_type="otlp_http_json",
        source_system="openlit",
        event_type="coding_agent_usage_observed",
        dimensions=("lifecycle", "usage", "cost"),
        measurement_basis="telemetry_reported",
    )
    assert policy.evaluate(otlp, "lifecycle").authority is Authority.AUTHORITATIVE
    assert policy.evaluate(otlp, "usage").authority is Authority.CORROBORATING
    assert policy.evaluate(otlp, "cost").authority is Authority.CORROBORATING

    checkpoint = _observed(
        source_type="git_checkpoint",
        source_system="entire",
        event_type="git_checkpoint_observed",
        dimensions=("artifact", "lifecycle"),
        measurement_basis="entire_checkpoint_observed",
    )
    assert policy.evaluate(checkpoint, "artifact").authority is Authority.AUTHORITATIVE
    assert policy.evaluate(checkpoint, "lifecycle").authority is Authority.CORROBORATING


def test_v1_local_usage_adapter_becomes_observation_and_preserves_only_digest() -> None:
    secret = "legacy prompt body must be omitted"
    legacy = {
        "event_id": "evt_abcdef123456",
        "event_type": "model_usage",
        "created_at": 1783902600.0,
        "source": "codex",
        "provider": "openai",
        "model": "gpt-5",
        "estimated_input_tokens": 20,
        "metadata": {
            "usage_source": "local_client_session_store",
            "usage_provenance": "agent_sentinel_local_usage_import",
            "client": "codex",
            "client_session_id": "session-legacy",
            "project_dir": "/Users/alice/private-project",
            "prompt": secret,
            "input_tokens": 20,
        },
    }

    envelope = adapt_v1_event(legacy)
    assert envelope.assertion == "observed"
    assert envelope.source_type == "local_client_log"
    assert "usage" in envelope.dimensions
    assert envelope.subjects.client_session_id == "session-legacy"
    assert envelope.subjects.project_id is not None and "/Users/alice" not in envelope.subjects.project_id
    assert envelope.raw_digest == raw_digest_for(legacy)
    serialized = str(envelope.to_dict())
    assert secret not in serialized
    assert "/Users/alice" not in serialized
    assert "legacy.metadata.prompt" in envelope.privacy.redacted_fields
    assert "legacy.metadata.project_dir" in envelope.privacy.redacted_fields


def test_v1_session_observation_adapter_is_local_log_without_usage_claim() -> None:
    legacy = {
        "event_id": "evt_observed12345",
        "event_type": "session_observed",
        "created_at": 1783902600.0,
        "source": "codex-local-session-observation-import",
        "run_id": "local-observation-run",
        "metadata": {
            "observation_source": "local_client_session_store_observation",
            "observation_provenance": (
                "agent_chronicle_local_session_observation_import"
            ),
            "client": "codex",
            "client_session_id": "session-without-usage",
            "source_namespace_fingerprint": "sha256:" + "a" * 64,
            "source_parse_complete": True,
        },
    }

    envelope = adapt_v1_event(legacy)

    assert envelope.assertion == "observed"
    assert envelope.source_type == "local_client_log"
    assert envelope.dimensions == ("session_identity",)
    assert "usage" not in envelope.dimensions
    assert envelope.subjects.client_session_id == "session-without-usage"
    assert envelope.subjects.extra["source_namespace_fingerprint"] == (
        "sha256:" + "a" * 64
    )


def test_v1_agent_event_stays_an_explicit_claim() -> None:
    envelope = adapt_v1_event(
        {
            "event_id": "evt_deadbeef1234",
            "event_type": "section_checkpoint",
            "created_at": "2026-07-13T00:30:00Z",
            "source": "codex",
            "metadata": {"section_id": "kernel", "summary": "implemented models"},
        }
    )
    assert envelope.assertion == "claimed"
    assert envelope.claimant == "codex"
    assert envelope.source_type == "mcp_agent_reported"
    assert envelope.measurement_basis["task_semantics"] == "agent_claimed"
