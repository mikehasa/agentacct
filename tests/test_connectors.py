from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentacct.confidence import COST_PROVIDER_BILLED
from agentacct.connectors import (
    ConnectorError,
    ConnectorRecord,
    ConnectorRegistry,
    ControlSignal,
    EntireGitConnector,
    EvidenceCoreUnavailable,
    HardEnforcementRefused,
    OpenLITOTLPConnector,
    PaperclipSnapshotConnector,
    build_default_registry,
    evaluate_control_signal,
    require_hard_enforcement,
)
from agentacct.connectors.entire import ENTIRE_LICENSE, ENTIRE_UPSTREAM_SHA
from agentacct.connectors.control import validate_supporting_evidence
from agentacct.connectors.openlit import OPENLIT_LICENSE, OPENLIT_UPSTREAM_SHA
from agentacct.connectors.paperclip import PAPERCLIP_LICENSE, PAPERCLIP_UPSTREAM_SHA
from agentacct.evidence import EvidenceEnvelope
from agentacct.evidence_runtime import EvidenceRuntime


FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


def _serialized(records: tuple[ConnectorRecord, ...]) -> str:
    return json.dumps([record.to_dict() for record in records], sort_keys=True)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _append_control_evidence(
    runtime: EvidenceRuntime,
    *,
    run_id: str,
    source_event_id: str,
    basis: str = COST_PROVIDER_BILLED,
    amount: float = 1.0,
    assertion: str = "observed",
) -> EvidenceEnvelope:
    envelope = EvidenceEnvelope.create(
        assertion=assertion,
        event_type="provider.cost.observed",
        source_type="provider_invoice",
        source_system="fixture-provider",
        source_instance="fixture-account",
        source_schema="fixture.v1",
        adapter="fixture.v1",
        source_event_id=source_event_id,
        event_timestamp="2026-07-13T00:00:00Z",
        dimensions=("cost",),
        measurement_basis={"cost": basis},
        subjects={"run_id": run_id},
        payload={"amount_usd": amount},
        claimant="fixture-provider" if assertion == "claimed" else None,
    )
    runtime.append(envelope)
    return envelope


def _hard_control_signal(
    *,
    run_id: str,
    evidence_ids: tuple[str, ...] = (),
    approval: bool = False,
    expires_at: str | None = "2030-01-01T00:00:00Z",
) -> ControlSignal:
    return ControlSignal(
        action="pause",
        target_type="execution",
        target_id=run_id,
        recommendation="pause after validated threshold",
        requested_mode="hard",
        evidence_basis=COST_PROVIDER_BILLED,
        cost_confidence=COST_PROVIDER_BILLED,
        supporting_evidence_ids=evidence_ids,
        explicit_conservative_approval=approval,
        controller_owns_execution=True,
        idempotency_key=f"pause-{run_id}-v1",
        expires_at=expires_at,
    )


def test_connector_record_is_deterministic_and_exposes_evidence_seam() -> None:
    record = ConnectorRecord(
        connector="fixture",
        source_type="snapshot",
        source_event_id="event-1",
        event_kind="fixture.observed",
        evidence_type="observation",
        measurement_basis="fixture_observed",
        completeness="partial",
        occurred_at="2026-07-13T01:00:00Z",
        observed_at="2026-07-13T01:00:01Z",
        subjects={"client_session": "session-1"},
        attributes={"count": 1},
        raw_digest="a" * 64,
    )

    same = ConnectorRecord(
        connector="fixture",
        source_type="snapshot",
        source_event_id="event-1",
        event_kind="fixture.observed",
        evidence_type="observation",
        measurement_basis="fixture_observed",
        completeness="partial",
        occurred_at="2026-07-13T01:00:00Z",
        observed_at="2026-07-13T01:00:01Z",
        subjects={"client_session": "session-1"},
        attributes={"count": 2},
        raw_digest="b" * 64,
    )
    assert record.record_id == same.record_id
    assert record.idempotency_key == same.idempotency_key
    payload = record.to_evidence_dict()
    assert payload["source_system"] == "fixture"
    assert payload["assertion"] == "observed"
    assert payload["privacy"]["raw_content_included"] is False
    assert payload["payload"]["attributes"] == {"count": 1}

    if importlib.util.find_spec("agentacct.evidence") is None:
        with pytest.raises(EvidenceCoreUnavailable):
            record.to_evidence_envelope()
    else:
        assert record.to_evidence_envelope() is not None


def test_registry_is_explicit_and_rejects_duplicate_names() -> None:
    paperclip = PaperclipSnapshotConnector()
    registry = ConnectorRegistry((paperclip,))
    assert registry.names() == ("paperclip",)
    assert registry.get("paperclip") is paperclip
    with pytest.raises(ConnectorError, match="already registered"):
        registry.register(PaperclipSnapshotConnector())
    with pytest.raises(ConnectorError, match="unknown connector"):
        registry.get("missing")
    assert build_default_registry().names() == ("openlit", "paperclip")


def test_paperclip_snapshot_maps_claims_and_preserves_missing_cost() -> None:
    connector = PaperclipSnapshotConnector()
    records = connector.read(FIXTURES / "paperclip_snapshot.json")
    replay = connector.read(FIXTURES / "paperclip_snapshot.json")

    assert records == replay
    assert len(records) == 7
    assert all(record.evidence_type == "claim" for record in records)
    assert all(record.measurement_basis == "orchestrator_claim" for record in records)
    kinds = {record.event_kind for record in records}
    assert kinds == {
        "orchestrator.agent.claim",
        "orchestrator.company.claim",
        "orchestrator.cost.claim",
        "orchestrator.execution.claim",
        "orchestrator.work_item.claim",
        "orchestrator.work_product.claim",
    }

    costs = {
        record.source_event_id: record
        for record in records
        if record.event_kind == "orchestrator.cost.claim"
    }
    assert "amount_usd" not in costs["cost-missing"].attributes
    assert costs["cost-missing"].attributes["cost_missing"] is True
    assert costs["cost-missing"].completeness == "partial"
    assert costs["cost-zero"].attributes["amount_usd"] == 0
    assert "cost_missing" not in costs["cost-zero"].attributes
    assert all(record.cost_confidence != COST_PROVIDER_BILLED for record in costs.values())
    assert all(
        record.to_evidence_envelope(observed_at="2026-07-13T02:00:00Z").verify_integrity()
        for record in records
    )

    serialized = _serialized(records)
    for secret in (
        "PAPERCLIP_COMPANY_SECRET",
        "PAPERCLIP_AGENT_PROMPT_SECRET",
        "hidden@example.invalid",
        "PAPERCLIP_ISSUE_TITLE_SECRET",
        "PAPERCLIP_ISSUE_BODY_SECRET",
        "PAPERCLIP_RUN_LOG_SECRET",
        "PAPERCLIP_PATH_SECRET",
        "PAPERCLIP_ARTIFACT_BODY_SECRET",
    ):
        assert secret not in serialized
    assert not hasattr(connector, "pause")
    assert not hasattr(connector, "cancel")
    assert not hasattr(connector, "write")


def test_openlit_otlp_dedupes_and_never_retains_body_attributes() -> None:
    fixture = json.loads((FIXTURES / "openlit_otlp.json").read_text(encoding="utf-8"))
    connector = OpenLITOTLPConnector()
    records = connector.read(fixture)

    assert len(records) == 5
    assert [record.to_dict() for record in records] == [
        record.to_dict() for record in connector.read(FIXTURES / "openlit_otlp.json")
    ]
    kinds = [record.event_kind for record in records]
    assert kinds.count("coding_agent.session.observed") == 1
    assert kinds.count("coding_agent.turn.observed") == 1
    assert kinds.count("coding_agent.tool.observed") == 1
    assert kinds.count("coding_agent.usage.observed") == 2
    assert all(record.evidence_type == "observation" for record in records)
    assert all(record.capture_level == "metadata_only" for record in records)
    assert all(record.cost_confidence != COST_PROVIDER_BILLED for record in records)
    assert all(record.to_evidence_envelope().verify_integrity() for record in records)

    usage = [record for record in records if record.event_kind == "coding_agent.usage.observed"]
    assert any(record.attributes.get("input_tokens") == 10 for record in usage)
    assert any(record.attributes.get("cost_usd") == 0.001 for record in usage)
    session = next(record for record in records if record.event_kind == "coding_agent.session.observed")
    assert session.attributes["dropped_attribute_count"] >= 2
    assert "input_tokens" not in session.attributes
    assert "cost_usd" not in session.attributes

    serialized = _serialized(records)
    for secret in (
        "OPENLIT_RESOURCE_SECRET",
        "OPENLIT_PROMPT_SECRET",
        "OPENLIT_RESPONSE_SECRET",
        "OPENLIT_BODY_SECRET",
        "OPENLIT_THOUGHT_SECRET",
        "OPENLIT_STATUS_MESSAGE_SECRET",
        "OPENLIT_TOOL_ARGUMENT_SECRET",
        "OPENLIT_TOOL_RESULT_SECRET",
        "OPENLIT_COMMAND_SECRET",
    ):
        assert secret not in serialized
    for forbidden_key in ("input.messages", "output.messages", "arguments", "result", "body", "thought"):
        assert forbidden_key not in serialized.lower()

    reversed_fixture = copy.deepcopy(fixture)
    reversed_fixture["resourceSpans"][0]["scopeSpans"][0]["spans"].reverse()
    assert [record.to_dict() for record in records] == [
        record.to_dict() for record in connector.read(reversed_fixture)
    ]


def test_entire_connector_reads_only_public_metadata_and_leaves_git_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "entire-repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Connector Test")
    _git(repository, "config", "user.email", "connector@example.invalid")

    session_dir = repository / "sessions" / "session-entire-1"
    session_dir.mkdir(parents=True)
    (session_dir / "metadata.json").write_text(
        (FIXTURES / "entire_checkpoint_metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (session_dir / "transcript.jsonl").write_text("ENTIRE_TRANSCRIPT_SECRET\n", encoding="utf-8")
    (session_dir / "prompt.txt").write_text("ENTIRE_PROMPT_FILE_SECRET\n", encoding="utf-8")
    _git(repository, "add", "sessions")
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        "checkpoint metadata",
        "-m",
        "Entire-Checkpoint: checkpoint-1\nEntire-Session: session-entire-1\nEntire-Strategy: manual-commit",
    )
    _git(repository, "branch", "entire/checkpoints/v1")

    before_status = _git(repository, "status", "--porcelain=v1")
    before_refs = _git(repository, "show-ref")
    connector = EntireGitConnector(repository)
    calls: list[tuple[str, ...]] = []
    real_git = connector._git

    def recording_git(*arguments: str) -> str:
        calls.append(arguments)
        return real_git(*arguments)

    monkeypatch.setattr(connector, "_git", recording_git)
    records = connector.read()

    assert len(records) == 1
    record = records[0]
    assert record.event_kind == "git.checkpoint.observed"
    assert record.attribution == "heuristic"
    assert record.completeness == "partial"
    assert record.truncation_reason == "no_change_does_not_prove_completion"
    assert record.attributes["no_change"] is True
    assert record.attributes["no_change_incomplete"] is True
    assert record.attributes["files_touched_count"] == 0
    assert record.attributes["input_tokens"] == 21
    assert record.attributes["strategy"] == "manual-commit"
    assert record.subjects["commit"]
    assert record.subjects["artifact"] == "checkpoint-1"
    assert record.subjects["client_session"] == "session-entire-1"
    envelope = record.to_evidence_envelope()
    assert envelope.verify_integrity()
    assert envelope.source_instance.startswith("instance_")

    serialized = _serialized(records)
    for secret in (
        "ENTIRE_METADATA_SUMMARY_SECRET",
        "ENTIRE_METADATA_PROMPT_SECRET",
        "ENTIRE_TRANSCRIPT_SECRET",
        "ENTIRE_PROMPT_FILE_SECRET",
        "checkpoint metadata",
    ):
        assert secret not in serialized
    opened_specs = "\n".join(" ".join(call) for call in calls if call and call[0] == "cat-file")
    assert "metadata.json" in opened_specs
    assert "transcript" not in opened_specs.lower()
    assert "prompt" not in opened_specs.lower()
    assert _git(repository, "status", "--porcelain=v1") == before_status
    assert _git(repository, "show-ref") == before_refs
    assert not hasattr(connector, "write")
    assert not hasattr(connector, "checkout")


def test_control_policy_is_store_bound_fail_closed_and_never_dispatches(tmp_path) -> None:
    runtime = EvidenceRuntime(tmp_path / "state", enabled=True)
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)

    empty = _hard_control_signal(run_id="run-empty")
    empty_validation = validate_supporting_evidence(empty, runtime)
    empty_decision = evaluate_control_signal(empty, now=now, supporting_evidence=empty_validation)
    assert empty_decision.hard_enforcement_allowed is False
    assert "no supporting evidence" in empty_decision.reason
    with pytest.raises(HardEnforcementRefused):
        require_hard_enforcement(empty, now=now, supporting_evidence=empty_validation)

    with pytest.raises(ValueError, match="64 lowercase hex"):
        _hard_control_signal(run_id="run-malformed", evidence_ids=("not-evidence",))

    # Initialize a real local projection, then prove an unknown id cannot be
    # satisfied by merely looking syntactically valid.
    _append_control_evidence(runtime, run_id="run-seed", source_event_id="seed")
    missing = _hard_control_signal(run_id="run-missing", evidence_ids=("evd_" + ("0" * 64),))
    missing_validation = validate_supporting_evidence(missing, runtime)
    assert missing_validation.missing_ids == missing.supporting_evidence_ids
    assert evaluate_control_signal(
        missing,
        now=now,
        supporting_evidence=missing_validation,
    ).hard_enforcement_allowed is False

    wrong_target_evidence = _append_control_evidence(
        runtime,
        run_id="run-other",
        source_event_id="wrong-target",
    )
    wrong_target = _hard_control_signal(
        run_id="run-target",
        evidence_ids=(wrong_target_evidence.evidence_id,),
    )
    wrong_target_validation = validate_supporting_evidence(wrong_target, runtime)
    assert wrong_target_validation.target_mismatch_ids == (wrong_target_evidence.evidence_id,)
    assert evaluate_control_signal(
        wrong_target,
        now=now,
        supporting_evidence=wrong_target_validation,
    ).hard_enforcement_allowed is False

    billed_evidence = _append_control_evidence(
        runtime,
        run_id="run-billed",
        source_event_id="billed",
    )
    billed = _hard_control_signal(
        run_id="run-billed",
        evidence_ids=(billed_evidence.evidence_id,),
    )
    billed_validation = validate_supporting_evidence(billed, runtime)
    allowed = require_hard_enforcement(
        billed,
        now=now,
        supporting_evidence=billed_validation,
    )
    assert billed_validation.valid is True
    assert billed_validation.provider_billed_ids == (billed_evidence.evidence_id,)
    assert allowed.effective_mode == "hard"
    assert evaluate_control_signal(
        wrong_target,
        now=now,
        supporting_evidence=billed_validation,
    ).hard_enforcement_allowed is False

    estimated_evidence = _append_control_evidence(
        runtime,
        run_id="run-estimated",
        source_event_id="estimated",
        basis="estimated_from_tokens",
    )
    estimated = _hard_control_signal(
        run_id="run-estimated",
        evidence_ids=(estimated_evidence.evidence_id,),
    )
    estimated_validation = validate_supporting_evidence(estimated, runtime)
    assert evaluate_control_signal(
        estimated,
        now=now,
        supporting_evidence=estimated_validation,
    ).hard_enforcement_allowed is False
    conservative = _hard_control_signal(
        run_id="run-estimated",
        evidence_ids=(estimated_evidence.evidence_id,),
        approval=True,
    )
    assert evaluate_control_signal(
        conservative,
        now=now,
        supporting_evidence=validate_supporting_evidence(conservative, runtime),
    ).hard_enforcement_allowed is True

    claimed_billed_evidence = _append_control_evidence(
        runtime,
        run_id="run-claimed-billed",
        source_event_id="claimed-billed",
        assertion="claimed",
    )
    claimed_billed = _hard_control_signal(
        run_id="run-claimed-billed",
        evidence_ids=(claimed_billed_evidence.evidence_id,),
    )
    claimed_billed_validation = validate_supporting_evidence(claimed_billed, runtime)
    assert claimed_billed_validation.valid is True
    assert claimed_billed_validation.provider_billed_ids == ()
    assert evaluate_control_signal(
        claimed_billed,
        now=now,
        supporting_evidence=claimed_billed_validation,
    ).hard_enforcement_allowed is False

    no_expiry = _hard_control_signal(
        run_id="run-billed",
        evidence_ids=(billed_evidence.evidence_id,),
        expires_at=None,
    )
    no_expiry_decision = evaluate_control_signal(
        no_expiry,
        now=now,
        supporting_evidence=validate_supporting_evidence(no_expiry, runtime),
    )
    assert no_expiry_decision.hard_enforcement_allowed is False
    assert "freshness is unproven" in no_expiry_decision.reason

    original = _append_control_evidence(
        runtime,
        run_id="run-conflict",
        source_event_id="conflicting-cost",
        amount=1.0,
    )
    _append_control_evidence(
        runtime,
        run_id="run-conflict",
        source_event_id="conflicting-cost",
        amount=2.0,
    )
    conflict_signal = _hard_control_signal(
        run_id="run-conflict",
        evidence_ids=(original.evidence_id,),
    )
    conflict_validation = validate_supporting_evidence(conflict_signal, runtime)
    assert conflict_validation.conflicting_ids == (original.evidence_id,)
    assert evaluate_control_signal(
        conflict_signal,
        now=now,
        supporting_evidence=conflict_validation,
    ).hard_enforcement_allowed is False

    disabled = EvidenceRuntime(tmp_path / "disabled", enabled=False)
    disabled_validation = validate_supporting_evidence(billed, disabled)
    assert disabled_validation.store_available is False
    assert evaluate_control_signal(
        billed,
        now=now,
        supporting_evidence=disabled_validation,
    ).hard_enforcement_allowed is False

    assert not hasattr(billed, "dispatch")
    assert not hasattr(billed, "execute")


def test_upstream_pins_and_licenses_are_explicit() -> None:
    assert (PAPERCLIP_UPSTREAM_SHA, PAPERCLIP_LICENSE) == (
        "c36f1a4afd91e4ddf0e5c7224b288ce722c7404f",
        "MIT",
    )
    assert (OPENLIT_UPSTREAM_SHA, OPENLIT_LICENSE) == (
        "8adf21c8f952c0768fd5ff85d853798bb3c028f3",
        "Apache-2.0",
    )
    assert (ENTIRE_UPSTREAM_SHA, ENTIRE_LICENSE) == (
        "7cd6662805fbd525f2f418ecf465a247b924af70",
        "MIT",
    )
