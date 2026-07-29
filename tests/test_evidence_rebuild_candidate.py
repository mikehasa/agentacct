from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import agentacct.evidence_rebuild_candidate as rebuild
from agentacct.cli import _validate_usage_import_args
from agentacct.client_usage import ClientUsageEvent
from agentacct.evidence import ClaimedLink, EvidenceEnvelope, adapt_v1_event
from agentacct.evidence_rebuild_snapshot import create_evidence_snapshot
from agentacct.evidence_store import EvidenceStore
from agentacct.usage_truth import mark_trusted_local_usage_import_event


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path.resolve()


def _snapshot(
    tmp_path: Path,
    *,
    events: bytes,
    generic_spool: bytes | None = None,
    refresh_spool: bytes | None = None,
    name: str = "sealed",
) -> tuple[Path, Path]:
    source = _private_directory(tmp_path / f"{name}-source")
    (source / "events.jsonl").write_bytes(events)
    (source / "events.jsonl.lock").write_bytes(b"")
    evidence = source / "evidence-v2"
    evidence.mkdir(mode=0o700)
    (evidence / ".spool.lock").write_bytes(b"")
    (evidence / "spool.jsonl").write_bytes(generic_spool or b"")
    (evidence / "refreshable-usage.jsonl").write_bytes(refresh_spool or b"")
    # Rebuild the source projection from the exact supplied spools so the P0
    # snapshot tool can independently verify cursor boundaries.
    EvidenceStore(source)
    canonical = sqlite3.connect(source / "chronicle.sqlite3")
    try:
        canonical.executescript(
            """
            CREATE TABLE store_metadata(
                singleton INTEGER PRIMARY KEY,
                store_uuid TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            );
            INSERT INTO store_metadata VALUES(
                1,
                '4ced52f1-76c6-4ba5-b67f-b423506b04d6',
                4
            );
            """
        )
        canonical.commit()
    finally:
        canonical.close()
    for path in source.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    created = create_evidence_snapshot(
        source,
        tmp_path / name,
        confirm_writers_stopped=True,
        deployed_commit="a" * 40,
    )
    return created.snapshot_root, created.manifest_path


def _usage_candidate(
    raw_root: Path,
    *,
    session: str = "session-1",
    input_tokens: int = 10,
    updated_at: int = 1_700_000_000,
    evidenced_event_ids: tuple[str, ...] = (),
    evidenced_event_id_total: int = 0,
    evidenced_outputs_skipped: int = 0,
) -> ClientUsageEvent:
    source_directory = raw_root / "sessions"
    source_directory.mkdir(exist_ok=True)
    source = source_directory / f"rollout-{session}.jsonl"
    if not source.exists():
        source.write_text("{}\n", encoding="utf-8")
    return ClientUsageEvent(
        client="codex",
        client_session_id=session,
        source_path=source,
        title=None,
        cwd=None,
        model="gpt-5",
        input_tokens=input_tokens,
        output_tokens=2,
        updated_at=updated_at,
        cache_creation_tokens_reported=False,
        cache_read_tokens_reported=False,
        evidenced_event_ids=evidenced_event_ids,
        evidenced_event_id_total=evidenced_event_id_total,
        evidenced_outputs_skipped=evidenced_outputs_skipped,
        source_namespace_fingerprint="sha256:" + "a" * 64,
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=input_tokens + 2,
        total_tokens_reported=True,
        usage_representation="session_total",
        usage_precedence_role="authoritative",
    )


def _trusted_usage_event(candidate: ClientUsageEvent, *, event_id: str) -> dict[str, object]:
    event = mark_trusted_local_usage_import_event(candidate.to_sentinel_event())
    event["event_id"] = event_id
    event["created_at"] = "2026-07-23T00:00:00Z"
    return event


def _complete_discovery(events: list[ClientUsageEvent]) -> SimpleNamespace:
    count = len(events)
    return SimpleNamespace(
        events=events,
        diagnostics={
            "codex": {
                "discovered": count,
                "parsed": count,
                "skipped": 0,
                "error_count": 0,
                "error_codes": [],
                "selected_root_groups": count,
                "returned_root_groups": count,
                "returned_rows": count,
                "excluded_by_limit": 0,
                "unparsed_selected_rows": 0,
                "unresolved_identity_files": 0,
                "excluded_by_source_namespace": 0,
                "observed_sessions": count,
                "usage_sessions": count,
                "sessions_without_usage": 0,
                "source_present": True,
            }
        },
    )


def _install_discovery(
    monkeypatch: pytest.MonkeyPatch,
    events: list[ClientUsageEvent],
    *,
    callback: object | None = None,
) -> list[int]:
    limits: list[int] = []

    def fake_discovery(**kwargs: object) -> SimpleNamespace:
        limit = kwargs["limit_sessions"]
        assert isinstance(limit, int)
        limits.append(limit)
        if callable(callback):
            callback()
        return _complete_discovery(events)

    monkeypatch.setattr(rebuild, "discover_client_usage_with_diagnostics", fake_discovery)
    return limits


def _receipt(result: dict[str, object]) -> dict[str, object]:
    return json.loads(Path(str(result["receipt"])).read_text(encoding="utf-8"))


def _candidate_head_input_tokens(candidate_root: Path) -> int:
    projection = candidate_root / "evidence-v2" / "projection.sqlite3"
    connection = sqlite3.connect(f"file:{projection}?mode=ro&immutable=1", uri=True)
    try:
        row = connection.execute(
            """
            SELECT e.envelope_json
            FROM refreshable_usage_heads AS h
            JOIN evidence_versions AS e ON e.evidence_id = h.evidence_id
            WHERE h.tombstoned = 0
            """
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    envelope = json.loads(str(row[0]))
    return int(
        envelope["payload"]["refreshable_usage"]["truth"]["tokens"]["input_tokens"]["value"]
    )


def _build_candidate(*args: object, **kwargs: object) -> dict[str, object]:
    return rebuild.build_evidence_rebuild_candidate(
        *args,
        confirm_owner_approved=True,
        **kwargs,
    )


def _envelope(
    *,
    source_event_id: str,
    assertion: str,
    payload_value: int,
    source_type: str = "mcp_agent_reported",
) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        assertion=assertion,
        event_type="machine_check" if assertion == "observed" else "section_checkpoint",
        source_type=source_type,
        source_system="codex",
        source_instance="explicit-test",
        source_schema="test.v1",
        adapter="test-adapter",
        source_event_id=source_event_id,
        event_timestamp="2026-07-23T00:00:00Z",
        dimensions=("activity",),
        measurement_basis="source_observed" if assertion == "observed" else "agent_claimed",
        payload={"value": payload_value},
        claimant="codex" if assertion == "claimed" else None,
    )


def test_builder_uses_full_history_limit_over_200_without_changing_normal_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    # One physical source file is enough for the inventory; the fake importer
    # proves the builder does not apply the normal public 200-session cap.
    candidates = [
        _usage_candidate(raw, session=f"session-{index}", input_tokens=index + 1)
        for index in range(205)
    ]
    limits = _install_discovery(monkeypatch, candidates)
    snapshot_root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")
    source_hashes_before = {
        path.relative_to(snapshot_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in snapshot_root.rglob("*")
        if path.is_file()
    }

    result = _build_candidate(
        snapshot_root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )

    receipt = _receipt(result)
    assert limits == [rebuild.REBUILD_FULL_HISTORY_LIMIT]
    assert limits[0] > 200
    assert receipt["raw_import"]["events"] == 205
    assert receipt["candidate"]["refreshable_usage_stats"]["current_heads"] == 205
    assert result["activation_ready"] is True
    assert result["verification"]["second_reconcile"]["physical_growth"] == 0
    source_hashes_after = {
        path.relative_to(snapshot_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in snapshot_root.rglob("*")
        if path.is_file()
    }
    assert source_hashes_after == source_hashes_before
    with pytest.raises(typer.BadParameter, match="between 1 and 200"):
        _validate_usage_import_args("codex", 201)


def test_builder_requires_explicit_owner_approval_before_any_candidate_write(
    tmp_path: Path,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(rebuild.EvidenceRebuildSafetyError, match="owner_approved"):
        rebuild.build_evidence_rebuild_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert list(parent.iterdir()) == []


def test_filters_v1_internal_inflation_and_conserves_all_other_generic_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    trusted_usage = _trusted_usage_event(candidate, event_id="evt_usage")
    old_inflated = adapt_v1_event(
        trusted_usage,
        source_instance="v1-internal",
        adapter="chronicle-v1-internal-adapter",
    )
    untrusted_mcp_usage = adapt_v1_event(
        {
            "event_id": "evt_mcp_usage",
            "event_type": "model_usage",
            "source": "mcp",
            "estimated_input_tokens": 3,
            "created_at": "2026-07-23T00:00:00Z",
        },
        source_instance="v1-mcp",
        adapter="chronicle-v1-mcp-adapter",
    )
    ledger_nonusage = {
        "event_id": "evt_section",
        "event_type": "section_checkpoint",
        "source": "mcp",
        "created_at": "2026-07-23T00:00:00Z",
        "metadata": {"section_id": "section-1"},
    }
    claimed_v1 = _envelope(source_event_id="claimed", assertion="claimed", payload_value=1)
    claimed_v2 = _envelope(source_event_id="claimed", assertion="claimed", payload_value=2)
    observed = _envelope(
        source_event_id="observed",
        assertion="observed",
        payload_value=3,
        source_type="process_wrapper",
    )
    valid_link = ClaimedLink.create(
        claimed_evidence_id=claimed_v1.evidence_id,
        observed_evidence_id=observed.evidence_id,
        relationship="corroborates",
        dimensions=("activity",),
        created_at="2026-07-23T00:00:00Z",
        created_by="test",
    )
    dangling_link = ClaimedLink.create(
        claimed_evidence_id=claimed_v1.evidence_id,
        observed_evidence_id=old_inflated.evidence_id,
        relationship="corroborates",
        dimensions=("usage",),
        created_at="2026-07-23T00:00:00Z",
        created_by="test",
    )
    old_store = EvidenceStore(tmp_path / "old-store")
    for envelope in (
        old_inflated,
        untrusted_mcp_usage,
        claimed_v1,
        claimed_v2,
        observed,
    ):
        old_store.append(envelope)
    # Exercise the real historical shape: shadow_v1_event injects transport
    # metadata into Evidence only; events.jsonl does not contain those fields.
    rebuild.EvidenceRuntime(tmp_path / "old-store", enabled=True).shadow_v1_event(
        ledger_nonusage,
        transport="mcp",
    )
    old_nonusage = next(
        record.envelope
        for record in old_store.query(limit=100)
        if record.envelope.source_event_id == "evt_section"
    )
    # Same evidence id is an old duplicate receipt and must compact to one.
    old_store.append(observed)
    old_store.append_claimed_link(valid_link)
    old_store.append_claimed_link(dangling_link)
    generic_bytes = old_store.spool_path.read_bytes()
    events = (
        json.dumps(trusted_usage, sort_keys=True).encode()
        + b"\n"
        + json.dumps(ledger_nonusage, sort_keys=True).encode()
        + b"\n"
    )
    snapshot_root, manifest = _snapshot(
        tmp_path,
        events=events,
        generic_spool=generic_bytes,
    )
    _install_discovery(monkeypatch, [candidate])
    parent = _private_directory(tmp_path / "candidates")

    result = _build_candidate(
        snapshot_root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(result)

    generic = receipt["generic_rebuild"]
    assert generic["filtered_refreshable_usage"] == 1
    assert generic["source_duplicate_evidence_ids"] == 1
    assert generic["ledger_source_fact_matches"] == 1
    assert generic["transport_unresolved_recovered"] == 0
    assert receipt["claimed_links"]["retained"] == 1
    assert receipt["claimed_links"]["quarantined"] == 1
    assert receipt["candidate"]["generic_stats"]["conflict_versions"] == 2
    candidate_store = EvidenceStore(Path(str(result["candidate_root"])))
    evidence_ids = {
        record.evidence_id
        for record in candidate_store.query(order_by="arrival", limit=100)
    }
    assert old_inflated.evidence_id not in evidence_ids
    assert untrusted_mcp_usage.evidence_id in evidence_ids
    assert old_nonusage.evidence_id in evidence_ids
    assert claimed_v1.evidence_id in evidence_ids
    assert claimed_v2.evidence_id in evidence_ids
    assert observed.evidence_id in evidence_ids


def test_unmatched_v1_nonusage_is_fail_visible_legacy_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    _install_discovery(monkeypatch, [])
    event = {
        "event_id": "evt_missing_generic",
        "event_type": "machine_check",
        "source": "mcp",
        "created_at": "2026-07-23T00:00:00Z",
        "metadata": {"exit_code": 0},
    }
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(event, sort_keys=True).encode() + b"\n",
    )

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(result)
    assert receipt["generic_rebuild"]["transport_unresolved_recovered"] == 1
    store = EvidenceStore(Path(str(result["candidate_root"])))
    record = store.query(limit=10)[0].envelope
    assert record.source_instance == "legacy-events-jsonl"
    assert record.adapter == "chronicle-v1-event-adapter"


@pytest.mark.parametrize(
    ("events", "message"),
    [
        (b"not-json\n", "events.jsonl"),
        (b'{"event_id":"evt_torn"}', "torn final"),
    ],
)
def test_corrupt_or_torn_sealed_ledger_fails_closed_before_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: bytes,
    message: str,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    _install_discovery(monkeypatch, [])
    root, manifest = _snapshot(tmp_path, events=events)
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(rebuild.EvidenceRebuildVerificationError, match=message):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )

    assert list(parent.iterdir()) == []


def test_candidate_refuses_a_spool_that_drifted_after_snapshot_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    _install_discovery(monkeypatch, [])
    root, manifest = _snapshot(tmp_path, events=b"")
    (root / "store" / "evidence-v2" / "spool.jsonl").write_bytes(b"not-json\n")
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(rebuild.EvidenceRebuildSafetyError):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert list(parent.iterdir()) == []


def test_trusted_ledger_fallback_is_retained_but_incomplete_source_cannot_delete(
    tmp_path: Path,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    trusted = _trusted_usage_event(candidate, event_id="evt_fallback")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={},
        clients=("codex",),
    )
    receipt = _receipt(result)

    assert result["activation_ready"] is False
    assert receipt["raw_import"]["complete"] is False
    assert receipt["trusted_ledger_usage_fallback"] == 1
    assert receipt["refresh_reconcile"]["complete_applied"] is False
    assert receipt["refresh_reconcile"]["deleted"] == 0
    assert receipt["candidate"]["refreshable_usage_stats"]["current_heads"] == 1


@pytest.mark.parametrize("diagnostics", [{}, {"codex": {"source_present": False}}])
def test_missing_or_absent_source_diagnostics_cannot_claim_complete_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostics: dict[str, object],
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)

    def incomplete_discovery(**kwargs: object) -> SimpleNamespace:
        assert kwargs["limit_sessions"] == rebuild.REBUILD_FULL_HISTORY_LIMIT
        return SimpleNamespace(events=[candidate], diagnostics=diagnostics)

    monkeypatch.setattr(
        rebuild,
        "discover_client_usage_with_diagnostics",
        incomplete_discovery,
    )
    root, manifest = _snapshot(tmp_path, events=b"")
    built = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(built)
    assert built["activation_ready"] is False
    assert receipt["raw_import"]["complete"] is False
    assert "rebuild_completeness_unknown" in receipt["raw_import"]["diagnostics"]["codex"]["error_codes"]
    assert receipt["refresh_reconcile"]["complete_applied"] is False
    assert receipt["refresh_reconcile"]["deleted"] == 0


def test_nonempty_raw_root_without_discoverer_authority_cannot_claim_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    discovery = _complete_discovery([candidate])
    discovery.diagnostics["codex"].pop("source_present")

    monkeypatch.setattr(
        rebuild,
        "discover_client_usage_with_diagnostics",
        lambda **_kwargs: discovery,
    )
    root, manifest = _snapshot(tmp_path, events=b"")
    built = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(built)

    assert built["activation_ready"] is False
    assert receipt["raw_import"]["complete"] is False
    assert receipt["raw_import"]["diagnostics"]["codex"]["source_present"] is None
    assert "rebuild_completeness_unknown" in receipt["raw_import"]["diagnostics"]["codex"]["error_codes"]


def test_diagnostic_returned_rows_must_match_actual_valid_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    discovery = _complete_discovery([candidate])
    discovery.diagnostics["codex"]["returned_rows"] = 2

    monkeypatch.setattr(
        rebuild,
        "discover_client_usage_with_diagnostics",
        lambda **_kwargs: discovery,
    )
    root, manifest = _snapshot(tmp_path, events=b"")
    built = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(built)
    diagnostic = receipt["raw_import"]["diagnostics"]["codex"]

    assert built["activation_ready"] is False
    assert receipt["raw_import"]["events"] == 1
    assert receipt["raw_import"]["complete"] is False
    assert diagnostic["returned_rows"] == 2
    assert "rebuild_returned_rows_mismatch" in diagnostic["error_codes"]
    assert "rebuild_completeness_unknown" in diagnostic["error_codes"]
    assert receipt["refresh_reconcile"]["complete_applied"] is False


def test_unresolved_raw_identity_blocks_complete_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    discovery = _complete_discovery([candidate])
    diagnostic = discovery.diagnostics["codex"]
    diagnostic["unresolved_identity_files"] = 1
    diagnostic["error_count"] = 1
    diagnostic["error_codes"] = ["codex_rollout_identity_unresolved"]

    monkeypatch.setattr(
        rebuild,
        "discover_client_usage_with_diagnostics",
        lambda **_kwargs: discovery,
    )
    root, manifest = _snapshot(tmp_path, events=b"")
    built = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(built)

    assert built["activation_ready"] is False
    assert receipt["raw_import"]["complete"] is False
    observed = receipt["raw_import"]["diagnostics"]["codex"]
    assert observed["unresolved_identity_files"] == 1
    assert "rebuild_completeness_unknown" in observed["error_codes"]
    assert receipt["refresh_reconcile"]["complete_applied"] is False


@pytest.mark.parametrize(
    ("namespace_mode", "quarantine_counter"),
    [
        ("unresolved", "quarantined_unresolved_namespace"),
        ("obsolete", "quarantined_obsolete_slot"),
    ],
)
def test_complete_raw_scan_quarantines_legacy_fallback_outside_explicit_slots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    namespace_mode: str,
    quarantine_counter: str,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    legacy = _trusted_usage_event(candidate, event_id=f"evt_{namespace_mode}")
    metadata = legacy["metadata"]
    assert isinstance(metadata, dict)
    if namespace_mode == "unresolved":
        metadata.pop("source_namespace_fingerprint")
    else:
        metadata["source_namespace_fingerprint"] = "sha256:" + "b" * 64
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(legacy, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [candidate])

    built = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(built)
    isolation = receipt["trusted_ledger_usage_isolation"]

    assert built["activation_ready"] is True
    assert receipt["trusted_ledger_usage_fallback"] == 0
    assert isolation["ledger_refreshable_events"] == 1
    assert isolation["included"] == 0
    assert isolation["quarantined"] == 1
    assert isolation[quarantine_counter] == 1
    assert isolation["quarantine_digest"].startswith("sha256:")
    assert len(isolation["quarantine_digest"]) == 71
    store = EvidenceStore(Path(str(built["candidate_root"])))
    heads = store.refreshable_usage_heads(include_tombstoned=False)
    assert len(heads) == 1
    assert heads[0].slot_identity["namespace_kind"] == "explicit"
    assert heads[0].slot_identity["source_namespace"] == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("ledger_updated_at", "remove_ledger_order", "counter"),
    [
        (1_700_000_100, False, "same_slot_newer_divergent"),
        (1_700_000_000, False, "same_slot_equal_order_divergent"),
        (1_700_000_000, True, "same_slot_unordered_divergent"),
    ],
)
def test_complete_raw_blocks_nonolder_divergent_ledger_without_selecting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_updated_at: int,
    remove_ledger_order: bool,
    counter: str,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(raw, input_tokens=10, updated_at=ledger_updated_at)
    raw_candidate = _usage_candidate(raw, input_tokens=99, updated_at=1_700_000_000)
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_conflict")
    if remove_ledger_order:
        metadata = trusted["metadata"]
        assert isinstance(metadata, dict)
        metadata.pop("updated_at")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(result)

    assert result["activation_ready"] is False
    isolation = receipt["trusted_ledger_usage_isolation"]
    assert isolation["included_same_slot_parity"] == 0
    assert isolation["same_slot_audited"] == 1
    assert isolation["same_slot_blocking_divergent"] == 1
    assert isolation[counter] == 1
    assert isolation["quarantined"] == 0
    assert len(isolation["same_slot_audit_digest"]) == 71
    assert str(raw) not in json.dumps(isolation, sort_keys=True)
    assert receipt["refresh_reconcile"]["complete_applied"] is True
    assert receipt["refresh_reconcile"]["deleted"] == 0
    assert receipt["refresh_reconcile"]["errors"] == []
    assert _candidate_head_input_tokens(Path(str(result["candidate_root"]))) == 99


@pytest.mark.parametrize(
    ("ledger_input", "ledger_updated_at", "counter"),
    [
        (99, 1_700_000_100, "same_slot_equal_truth"),
        (10, 1_699_999_900, "same_slot_older_divergent"),
    ],
)
def test_complete_raw_allows_same_truth_or_strictly_older_divergent_ledger_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_input: int,
    ledger_updated_at: int,
    counter: str,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(
        raw,
        input_tokens=ledger_input,
        updated_at=ledger_updated_at,
    )
    raw_candidate = _usage_candidate(raw, input_tokens=99, updated_at=1_700_000_000)
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_audit")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    isolation = _receipt(result)["trusted_ledger_usage_isolation"]

    assert result["activation_ready"] is True
    assert isolation["included"] == 0
    assert isolation["same_slot_audited"] == 1
    assert isolation[counter] == 1
    assert isolation["same_slot_blocking_divergent"] == 0
    assert len(isolation["same_slot_audit_digest"]) == 71
    assert str(raw) not in json.dumps(isolation, sort_keys=True)
    assert _candidate_head_input_tokens(Path(str(result["candidate_root"]))) == 99


def test_equal_order_evidence_link_drift_is_nonblocking_receipt_not_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-model link growth at equal source order must not block activation.

    A lane's usage can freeze while the session transcript keeps growing, so a
    later raw rescan computes MORE client-log evidence links at the same source
    order. Usage material is identical; only the evidence subtree differs.
    """

    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(
        raw,
        input_tokens=99,
        updated_at=1_700_000_000,
        evidenced_event_ids=("evt_a",),
        evidenced_event_id_total=6,
    )
    raw_candidate = _usage_candidate(
        raw,
        input_tokens=99,
        updated_at=1_700_000_000,
        evidenced_event_ids=("evt_a", "evt_b", "evt_c"),
        evidenced_event_id_total=9,
        evidenced_outputs_skipped=1,
    )
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_link_drift")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(result)
    isolation = receipt["trusted_ledger_usage_isolation"]

    assert result["activation_ready"] is True
    assert receipt["status"] == "verified_candidate"
    assert isolation["same_slot_audited"] == 1
    assert isolation["same_slot_equal_order_link_drift"] == 1
    assert isolation["same_slot_equal_order_divergent"] == 0
    assert isolation["same_slot_blocking_divergent"] == 0
    assert isolation["quarantined"] == 0
    assert receipt["refresh_reconcile"]["errors"] == []
    assert _candidate_head_input_tokens(Path(str(result["candidate_root"]))) == 99


def test_equal_order_usage_material_divergence_still_blocks_despite_link_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different tokens at equal order block even when links also drifted."""

    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(
        raw,
        input_tokens=10,
        updated_at=1_700_000_000,
        evidenced_event_ids=("evt_a",),
        evidenced_event_id_total=1,
    )
    raw_candidate = _usage_candidate(
        raw,
        input_tokens=99,
        updated_at=1_700_000_000,
        evidenced_event_ids=("evt_a", "evt_b"),
        evidenced_event_id_total=2,
    )
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_material_drift")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    isolation = _receipt(result)["trusted_ledger_usage_isolation"]

    assert result["activation_ready"] is False
    assert isolation["same_slot_equal_order_divergent"] == 1
    assert isolation["same_slot_equal_order_link_drift"] == 0
    assert isolation["same_slot_blocking_divergent"] == 1
    assert _candidate_head_input_tokens(Path(str(result["candidate_root"]))) == 99


def test_reducer_ambiguous_raw_slot_yields_blocked_candidate_not_receipt_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build-time and verify-time same-slot audits must share one slot basis.

    Two raw truths for one slot at the same source order cannot be reduced to a
    head; the reconcile errors and the verifier's head-derived view omits the
    slot. The build-time audit must agree (quarantined_obsolete_slot), not
    classify the ledger row as divergent and then die in its own final
    verification with a tamper-shaped receipt mismatch.
    """

    raw = _private_directory(tmp_path / "raw")
    raw_a = _usage_candidate(raw, input_tokens=10, updated_at=1_700_000_000)
    raw_b = _usage_candidate(raw, input_tokens=99, updated_at=1_700_000_000)
    ledger_candidate = _usage_candidate(raw, input_tokens=50, updated_at=1_700_000_000)
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_ambiguous")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_a, raw_b])

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(result)
    isolation = receipt["trusted_ledger_usage_isolation"]

    assert result["activation_ready"] is False
    assert receipt["status"] == "blocked_candidate"
    assert isolation["quarantined_obsolete_slot"] == 1
    assert isolation["same_slot_audited"] == 0
    assert any(
        "unordered divergent" in error
        for error in receipt["refresh_reconcile"]["errors"]
    )


def test_nonempty_sqlite_wal_refuses_build_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running client's uncheckpointed WAL must fail the build closed.

    WAL commits change what discovery reads without touching the main db
    file's fingerprint, so the before/after inventory alone cannot see them.
    """

    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    (raw / "state_5.sqlite").write_bytes(b"stub-main-db")
    (raw / "state_5.sqlite-wal").write_bytes(b"pending-frames")
    trusted = _trusted_usage_event(candidate, event_id="evt_wal")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [candidate])

    with pytest.raises(rebuild.EvidenceRebuildSafetyError, match="SQLite WAL is non-empty"):
        _build_candidate(
            root,
            manifest,
            _private_directory(tmp_path / "candidates"),
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )


def test_raw_source_inventory_drift_fails_before_candidate_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    source = candidate.source_path

    def mutate() -> None:
        source.write_text("changed\n", encoding="utf-8")

    _install_discovery(monkeypatch, [candidate], callback=mutate)
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(rebuild.EvidenceRebuildVerificationError, match="inventory changed"):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert list(parent.iterdir()) == []


def test_raw_inventory_ignores_unrelated_home_cache_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    cache = raw / "cache"
    cache.mkdir()
    unrelated = cache / "index.bin"
    unrelated.write_bytes(b"before")

    def mutate_unrelated_cache() -> None:
        unrelated.write_bytes(b"after-with-different-size")

    _install_discovery(
        monkeypatch,
        [candidate],
        callback=mutate_unrelated_cache,
    )
    root, manifest = _snapshot(tmp_path, events=b"")

    result = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )

    assert result["activation_ready"] is True
    receipt = _receipt(result)
    assert receipt["raw_import"]["complete"] is True


@pytest.mark.parametrize("carrier", ["sessions", "archived_sessions"])
def test_raw_inventory_rejects_declared_carrier_directory_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    carrier: str,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    foreign = _private_directory(tmp_path / f"foreign-{carrier}")
    (raw / carrier).symlink_to(foreign, target_is_directory=True)
    _install_discovery(monkeypatch, [])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="carrier may not be a symlink",
    ):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert list(parent.iterdir()) == []


def test_raw_inventory_rejects_nested_directory_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    foreign = _private_directory(tmp_path / "foreign-history")
    (raw / "sessions" / "linked-history").symlink_to(
        foreign,
        target_is_directory=True,
    )
    _install_discovery(monkeypatch, [candidate])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="carrier contains a symlink",
    ):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize(
    ("client", "carrier", "carrier_kind"),
    [
        ("codex", "state_5.sqlite", "file"),
        ("claude-code", "projects", "directory"),
        ("hermes", "state.db", "file"),
    ],
)
def test_raw_inventory_rejects_symlinked_authority_carrier_for_each_client(
    tmp_path: Path,
    client: str,
    carrier: str,
    carrier_kind: str,
) -> None:
    raw = _private_directory(tmp_path / f"raw-{client}")
    if carrier_kind == "directory":
        foreign = _private_directory(tmp_path / f"foreign-{client}")
        (raw / carrier).symlink_to(foreign, target_is_directory=True)
    else:
        foreign = tmp_path / f"foreign-{client}.bin"
        foreign.write_bytes(b"carrier")
        (raw / carrier).symlink_to(foreign)

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="carrier may not be a symlink",
    ):
        rebuild._inventory_paths({client: (raw,)})


def test_raw_inventory_traversal_error_fails_before_candidate_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    _usage_candidate(raw)
    parent = _private_directory(tmp_path / "candidates")
    real_scandir = rebuild.os.scandir

    def fail_descriptor_scan(path):
        if isinstance(path, int):
            raise PermissionError("synthetic traversal denial")
        return real_scandir(path)

    monkeypatch.setattr(rebuild.os, "scandir", fail_descriptor_scan)

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="carrier traversal failed",
    ):
        rebuild._inventory_paths({"codex": (raw,)})
    assert list(parent.iterdir()) == []


def test_raw_inventory_entry_race_fails_before_candidate_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    source = candidate.source_path
    _install_discovery(monkeypatch, [candidate])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")
    real_stat = rebuild.os.stat
    mutated = False

    def race_after_inventory_stat(path, *args, **kwargs):
        nonlocal mutated
        observed = real_stat(path, *args, **kwargs)
        if (
            not mutated
            and path == source.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            mutated = True
            source.write_bytes(source.read_bytes() + b"changed\n")
        return observed

    monkeypatch.setattr(rebuild.os, "stat", race_after_inventory_stat)

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="changed during inventory",
    ):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert mutated is True
    assert list(parent.iterdir()) == []


def test_raw_inventory_binds_archived_sessions_carrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    archived = raw / "archived_sessions"
    archived.mkdir()
    archived_rollout = archived / "rollout-archived.jsonl"
    archived_rollout.write_bytes(b"{}\n")

    def mutate_archived_history() -> None:
        archived_rollout.write_bytes(b'{"changed":true}\n')

    _install_discovery(
        monkeypatch,
        [candidate],
        callback=mutate_archived_history,
    )
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="inventory changed",
    ):
        _build_candidate(
            root,
            manifest,
            parent,
            raw_source_roots={"codex": raw},
            clients=("codex",),
        )
    assert list(parent.iterdir()) == []


def test_old_refresh_spool_is_validated_reported_and_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    trusted = _trusted_usage_event(candidate, event_id="evt_refresh")
    old_store = EvidenceStore(tmp_path / "old-store")
    result = old_store.reconcile_refreshable_usage_snapshot(
        [
            rebuild.EvidenceRuntime._refreshable_usage_envelope(trusted)[0]  # type: ignore[index]
        ],
        complete=True,
    )
    assert result.changed is True
    main = old_store.spool_path.read_bytes() if old_store.spool_path.exists() else b""
    refresh = old_store.refreshable_usage_spool_path.read_bytes()
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
        generic_spool=main,
        refresh_spool=refresh,
    )
    _install_discovery(monkeypatch, [candidate])

    built = _build_candidate(
        root,
        manifest,
        _private_directory(tmp_path / "candidates"),
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    receipt = _receipt(built)
    assert receipt["derived_refresh_spool_ignored"]["records"] == 1
    assert receipt["candidate"]["refreshable_usage_stats"]["batch_receipts"] == 1


def test_logical_digest_is_deterministic_and_receipt_is_metadata_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    _install_discovery(monkeypatch, [candidate])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")

    first = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    second = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )

    assert first["logical_digest"] == second["logical_digest"]
    receipt_text = Path(str(first["receipt"])).read_text(encoding="utf-8")
    assert str(raw) not in receipt_text
    first_receipt = _receipt(first)
    assert first_receipt["privacy"] == {
        "prompts_transcripts_secrets_in_receipt": False,
        "receipt_content": "counts_digests_and_stable_codes_only",
    }
    evidence_tree = first_receipt["candidate"]["evidence_tree"]
    assert len(evidence_tree["tree_sha256"]) == 64
    assert evidence_tree["path"] == first_receipt["candidate"]["evidence_path"]
    assert all(
        set(entry) == {
            "relative_path",
            "kind",
            "mode",
            "size_bytes",
            "device",
            "inode",
            "mtime_ns",
            "ctime_ns",
            "sha256",
        }
        for entry in evidence_tree["entries"]
    )


def test_sealed_candidate_tree_is_stable_across_repeated_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    _install_discovery(monkeypatch, [candidate])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    candidate_root = Path(str(built["candidate_root"]))
    projection = candidate_root / "evidence-v2" / "projection.sqlite3"
    sidecars = [
        projection.with_name(projection.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
    ]
    receipt = _receipt(built)
    before = rebuild.fingerprint_evidence_tree(candidate_root / "evidence-v2").to_dict()
    assert before == receipt["candidate"]["evidence_tree"]
    assert projection.read_bytes()[18:20] == b"\x01\x01"
    assert not any(path.exists() for path in sidecars)

    real_connect = sqlite3.connect

    def reject_sealed_projection(database, *args, **kwargs):
        value = str(database)
        if value == str(projection) or value.startswith(f"file:{projection}"):
            raise AssertionError("verifier opened the sealed candidate projection")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(rebuild.sqlite3, "connect", reject_sealed_projection)
    first = rebuild.verify_evidence_rebuild_candidate(
        candidate_root,
        scratch_parent=parent,
        snapshot_root=root,
        snapshot_manifest=manifest,
        required_clients=("codex",),
    )
    second = rebuild.verify_evidence_rebuild_candidate(
        candidate_root,
        scratch_parent=parent,
        snapshot_root=root,
        snapshot_manifest=manifest,
        required_clients=("codex",),
    )

    assert first == second
    assert first["second_reconcile"] == receipt["candidate"]["second_reconcile"]
    assert rebuild.fingerprint_evidence_tree(candidate_root / "evidence-v2").to_dict() == before
    assert not any(path.exists() for path in sidecars)


def test_verify_rejects_receipted_candidate_with_uncheckpointed_wal_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    _install_discovery(monkeypatch, [candidate])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    candidate_root = Path(str(built["candidate_root"]))
    projection = candidate_root / "evidence-v2" / "projection.sqlite3"
    connection = sqlite3.connect(projection)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE wal_only_marker(value INTEGER)")
        connection.commit()
        assert projection.with_name(projection.name + "-wal").is_file()

        receipt = _receipt(built)
        receipt["candidate"]["evidence_tree"] = rebuild.fingerprint_evidence_tree(
            candidate_root / "evidence-v2"
        ).to_dict()
        forged = rebuild._receipt_with_digest(rebuild._receipt_body(receipt))
        Path(str(built["receipt"])).write_bytes(rebuild.canonical_json_bytes(forged) + b"\n")

        with pytest.raises(rebuild.EvidenceRebuildVerificationError, match="sidecar"):
            rebuild.verify_evidence_rebuild_candidate(candidate_root, scratch_parent=parent)
    finally:
        connection.close()


def test_verify_rederives_blocked_readiness_after_receipt_self_hash_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(raw, input_tokens=10, updated_at=1_700_000_000)
    raw_candidate = _usage_candidate(raw, input_tokens=99, updated_at=1_700_000_000)
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_blocked")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    assert built["activation_ready"] is False

    receipt = _receipt(built)
    receipt["status"] = "verified_candidate"
    receipt["activation_ready"] = True
    forged = rebuild._receipt_with_digest(rebuild._receipt_body(receipt))
    Path(str(built["receipt"])).write_bytes(rebuild.canonical_json_bytes(forged) + b"\n")

    with pytest.raises(rebuild.EvidenceRebuildVerificationError, match="readiness declaration"):
        rebuild.verify_evidence_rebuild_candidate(
            Path(str(built["candidate_root"])),
            scratch_parent=parent,
        )


def test_verify_rejects_correlated_isolation_tamper_with_rehashed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(raw, input_tokens=10, updated_at=1_700_000_000)
    raw_candidate = _usage_candidate(raw, input_tokens=99, updated_at=1_700_000_000)
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_blocked_correlated")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    assert built["activation_ready"] is False

    receipt = _receipt(built)
    isolation = receipt["trusted_ledger_usage_isolation"]
    for key in (
        "same_slot_audited",
        "same_slot_equal_truth",
        "same_slot_older_divergent",
        "same_slot_blocking_divergent",
        "same_slot_newer_divergent",
        "same_slot_equal_order_divergent",
        "same_slot_unordered_divergent",
        "ledger_refreshable_events",
    ):
        isolation[key] = 0
    receipt["status"] = "verified_candidate"
    receipt["activation_ready"] = True
    forged = rebuild._receipt_with_digest(rebuild._receipt_body(receipt))
    Path(str(built["receipt"])).write_bytes(rebuild.canonical_json_bytes(forged) + b"\n")

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="trusted ledger isolation does not match verified evidence",
    ):
        rebuild.verify_evidence_rebuild_candidate(
            Path(str(built["candidate_root"])),
            scratch_parent=parent,
            snapshot_root=root,
            snapshot_manifest=manifest,
            required_clients=("codex",),
        )


def test_verify_rejects_correlated_authority_client_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    ledger_candidate = _usage_candidate(raw, input_tokens=10, updated_at=1_700_000_000)
    raw_candidate = _usage_candidate(raw, input_tokens=99, updated_at=1_700_000_000)
    trusted = _trusted_usage_event(ledger_candidate, event_id="evt_blocked_authority")
    root, manifest = _snapshot(
        tmp_path,
        events=json.dumps(trusted, sort_keys=True).encode() + b"\n",
    )
    _install_discovery(monkeypatch, [raw_candidate])
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    assert built["activation_ready"] is False

    receipt = _receipt(built)
    codex_diagnostic = receipt["raw_import"]["diagnostics"]["codex"]
    receipt["raw_import"]["clients"] = ["claude-code"]
    receipt["raw_import"]["diagnostics"] = {"claude-code": codex_diagnostic}
    _fallback, isolation = rebuild._refreshable_fallback_plan(
        [trusted],
        current_raw={},
        raw_slots={"claude-code": set()},
        authoritative_clients=("claude-code",),
    )
    receipt["trusted_ledger_usage_isolation"] = isolation
    receipt["trusted_ledger_usage_fallback"] = 1
    receipt["refresh_reconcile"]["input_count"] = 2
    receipt["status"] = "verified_candidate"
    receipt["activation_ready"] = True
    forged = rebuild._receipt_with_digest(rebuild._receipt_body(receipt))
    Path(str(built["receipt"])).write_bytes(rebuild.canonical_json_bytes(forged) + b"\n")

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="externally required client set",
    ):
        rebuild.verify_evidence_rebuild_candidate(
            Path(str(built["candidate_root"])),
            scratch_parent=parent,
            snapshot_root=root,
            snapshot_manifest=manifest,
            required_clients=("codex",),
        )


def test_verify_binds_the_cli_default_required_client_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercises the real verifier with the exact tuple the CLI supplies
    # (DEFAULT_REBUILD_CLIENTS), not a single-client stand-in, so the
    # authority binding is proven end-to-end against the production value.
    raw = _private_directory(tmp_path / "raw")
    raw_candidate = _usage_candidate(raw, input_tokens=42, updated_at=1_700_000_000)
    root, manifest = _snapshot(tmp_path, events=b"")
    _install_discovery(monkeypatch, [raw_candidate])
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )

    with pytest.raises(
        rebuild.EvidenceRebuildVerificationError,
        match="externally required client set",
    ):
        rebuild.verify_evidence_rebuild_candidate(
            Path(str(built["candidate_root"])),
            scratch_parent=parent,
            snapshot_root=root,
            snapshot_manifest=manifest,
            required_clients=rebuild.DEFAULT_REBUILD_CLIENTS,
        )


def test_verify_rejects_tampered_candidate_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _private_directory(tmp_path / "raw")
    candidate = _usage_candidate(raw)
    _install_discovery(monkeypatch, [candidate])
    root, manifest = _snapshot(tmp_path, events=b"")
    parent = _private_directory(tmp_path / "candidates")
    built = _build_candidate(
        root,
        manifest,
        parent,
        raw_source_roots={"codex": raw},
        clients=("codex",),
    )
    candidate_root = Path(str(built["candidate_root"]))
    refresh_spool = candidate_root / "evidence-v2" / "refreshable-usage.jsonl"
    refresh_spool.write_bytes(refresh_spool.read_bytes() + b"tamper\n")

    with pytest.raises(rebuild.EvidenceRebuildVerificationError, match="file digest"):
        rebuild.verify_evidence_rebuild_candidate(candidate_root, scratch_parent=parent)
