from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from agent_chronicle.canonical import legacy_import as legacy_import_module
from agent_chronicle.canonical import product_parity as product_parity_module
from agent_chronicle.canonical.legacy_recovery import RECOVERY_DESIGN_ID
from agent_chronicle.canonical.product_parity import (
    RECOVERY_PRODUCT_ORACLE_CODE_VERSION,
    RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION,
    _candidate_product_events,
    _claim_surface_key,
)
from agent_chronicle.canonical.types import RECOVERY_FACT_TRANSPORT
from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event


_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "legacy_recovery_parity.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "legacy_recovery_parity_test_module",
    _RUNNER_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)

_BINDING_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "gate1_evidence_binding.py"
)
_BINDING_SPEC = importlib.util.spec_from_file_location(
    "gate1_evidence_binding_recovery_test_module",
    _BINDING_PATH,
)
assert _BINDING_SPEC is not None and _BINDING_SPEC.loader is not None
binding = importlib.util.module_from_spec(_BINDING_SPEC)
sys.modules[_BINDING_SPEC.name] = binding
_BINDING_SPEC.loader.exec_module(binding)

_PUBLIC_RECOVERY_EVIDENCE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "evidence"
    / "canonical-sqlite-unscoped-legacy-recovery.json"
)

SECRET = "TOP_SECRET_LEGACY_RECOVERY_SENTINEL_6f13"
NAMESPACE = f"synthetic-namespace-{SECRET}"
OTHER_NAMESPACE = f"synthetic-other-namespace-{SECRET}"
SESSION_ID = f"synthetic-session-{SECRET}"
OTHER_SESSION_ID = f"synthetic-other-session-{SECRET}"
RECOVERED_SECTION_ID = "evt_710001"
RECOVERED_TASK_ID = "evt_710002"
SCOPED_COMPLETION_ID = "evt_710003"
NO_PROOF_ID = f"evt-no-proof-{SECRET}"
UNSUPPORTED_ID = f"evt-unsupported-{SECRET}"


def _section(
    event_id: str,
    *,
    work_id: str,
    section_id: str,
    namespace: str | None = None,
    status: str = "started",
    created_at: int = 100,
    client: str | None = None,
    client_session_id: str | None = None,
    summary: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "sentinel_semantic_kind": "section",
        "section_status": status,
        "section_title": f"private title {SECRET}",
        "summary": summary or f"private summary {SECRET}",
        "work_id": work_id,
        "section_id": section_id,
    }
    if namespace is not None:
        metadata["source_namespace_fingerprint"] = namespace
    if client is not None:
        metadata["client"] = client
    if client_session_id is not None:
        metadata["client_session_id"] = client_session_id
        metadata["client_context_keys_authored"] = ["client_session_id"]
    if project_dir is not None:
        metadata["project_dir"] = project_dir
    return {
        "event_id": event_id,
        "event_type": f"section_{status}",
        "source": "synthetic-runner-test",
        "run_id": f"private-run-{SECRET}",
        "created_at": created_at,
        "metadata": metadata,
    }


def _task() -> dict[str, Any]:
    return {
        "event_id": RECOVERED_TASK_ID,
        "event_type": "task_started",
        "source": "synthetic-runner-test",
        "run_id": f"private-run-{SECRET}",
        "created_at": 101,
        "metadata": {
            "sentinel_semantic_kind": "task",
            "task_status": "started",
            "task_title": f"private task {SECRET}",
            "work_id": "recovered-task-work",
            "section_id": "recovered-task-section",
        },
    }


def _usage_donor() -> dict[str, Any]:
    event = {
        "event_id": "evt_7100ff",
        "event_type": "model_usage",
        "source": "codex-local-session-import",
        "provider": "synthetic-provider",
        "model": "synthetic-model",
        "usage_confidence": "client_reported",
        "estimated_input_tokens": 5,
        "estimated_output_tokens": 1,
        "created_at": 200,
        "metadata": {
            "client": "codex",
            "client_session_id": SESSION_ID,
            "client_session_kind": "continuation",
            "parent_client_session_id": OTHER_SESSION_ID,
            "source_namespace_fingerprint": NAMESPACE,
            "started_at": 90,
            "updated_at": 200,
            "usage_update_semantics": "codex_rollout_token_count_events",
            "usage_additive": True,
            "usage_granularity": "session",
            "precedence_role": "authoritative",
            "evidenced_event_ids": [RECOVERED_SECTION_ID, RECOVERED_TASK_ID],
            "evidenced_event_id_total": 2,
            "evidenced_outputs_skipped": 0,
            "input_tokens_reported": True,
            "output_tokens_reported": True,
            "cached_input_tokens": 0,
            "cached_input_tokens_reported": True,
            "cache_creation_input_tokens": 0,
            "cache_creation_input_tokens_reported": True,
            "cache_read_input_tokens": 0,
            "cache_read_input_tokens_reported": True,
            "reasoning_output_tokens": 0,
            "reasoning_output_tokens_reported": True,
            "total_tokens": 6,
            "total_tokens_reported": True,
        },
    }
    return mark_trusted_local_usage_import_event(event)


def _other_usage_donor() -> dict[str, Any]:
    event = _usage_donor()
    event["event_id"] = "evt_7100fe"
    metadata = dict(event["metadata"])
    metadata["client_session_id"] = OTHER_SESSION_ID
    metadata["client_session_kind"] = "root"
    metadata.pop("parent_client_session_id", None)
    metadata["evidenced_event_ids"] = []
    metadata["evidenced_event_id_total"] = 0
    metadata["evidenced_outputs_skipped"] = 0
    event["metadata"] = metadata
    return mark_trusted_local_usage_import_event(event)


def _other_source_usage_donor() -> dict[str, Any]:
    event = _usage_donor()
    event["event_id"] = "evt_7100fd"
    event["source"] = "claude-local-session-import"
    metadata = dict(event["metadata"])
    metadata["client"] = "claude"
    metadata["source_namespace_fingerprint"] = OTHER_NAMESPACE
    metadata["client_session_kind"] = "root"
    metadata.pop("parent_client_session_id", None)
    metadata["evidenced_event_ids"] = []
    metadata["evidenced_event_id_total"] = 0
    metadata["evidenced_outputs_skipped"] = 0
    event["metadata"] = metadata
    return mark_trusted_local_usage_import_event(event)


def _snapshot(
    tmp_path: Path,
    *,
    recovered_semantic_namespace: str | None = None,
    scoped_completion_summary: str | None = None,
    malformed_before_target: bool = False,
) -> tuple[Path, Path, Path]:
    recovered_section = _section(
        RECOVERED_SECTION_ID,
        work_id="recovered-section-work",
        section_id="recovered-section",
    )
    if recovered_semantic_namespace is not None:
        recovered_section["metadata"]["session_namespace_fingerprint"] = (
            recovered_semantic_namespace
        )
    events: list[dict[str, Any]] = [
        recovered_section,
        _section(
            SCOPED_COMPLETION_ID,
            work_id="recovered-section-work",
            section_id="recovered-section",
            namespace=NAMESPACE,
            status="completed",
            created_at=102,
            client="codex",
            client_session_id=SESSION_ID,
            summary=(
                scoped_completion_summary
                if scoped_completion_summary is not None
                else f"private final summary {SECRET}"
            ),
        ),
        _task(),
        _section(
            NO_PROOF_ID,
            work_id="no-proof-work",
            section_id="no-proof-section",
            client="codex",
            client_session_id=SESSION_ID,
        ),
        _section(
            "evt-explicit-scoped",
            work_id="scoped-work",
            section_id="scoped-section",
            namespace=NAMESPACE,
        ),
        {
            "event_id": UNSUPPORTED_ID,
            "event_type": "machine_check",
            "source": "synthetic-runner-test",
            "created_at": 110,
            "metadata": {"summary": f"quarantined {SECRET}"},
        },
        _usage_donor(),
        _other_usage_donor(),
        _other_source_usage_donor(),
    ]
    payload = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for event in events
    )
    if malformed_before_target:
        payload = (
            f'{{"event_id":"malformed-prefix-{SECRET}":\n'.encode("utf-8")
            + payload
        )
    payload += f'{{"event_id":"malformed-{SECRET}":\n'.encode("utf-8")

    root = tmp_path / f"snapshot-{SECRET}"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    source = root / "events.jsonl"
    source.write_bytes(payload)
    os.chmod(source, 0o600)

    manifest = tmp_path / f"manifest-{SECRET}.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "legacy-chronicle",
                "files": [
                    {
                        "path": "events.jsonl",
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(manifest, 0o600)

    scratch = tmp_path / f"scratch-{SECRET}"
    scratch.mkdir(mode=0o700)
    os.chmod(scratch, 0o700)
    return root.resolve(), manifest.resolve(), scratch.resolve()


def _arguments(root: Path, manifest: Path, scratch: Path) -> argparse.Namespace:
    return argparse.Namespace(
        snapshot_root=str(root),
        snapshot_manifest=str(manifest),
        scratch_root=str(scratch),
    )


def _assert_counts_and_booleans(value: object) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_counts_and_booleans(item)
        return
    assert isinstance(value, (bool, int))
    assert not isinstance(value, int) or value >= 0


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _commit_in_head_history(repo_root: Path, source_commit: str) -> bool:
    """True iff ``source_commit`` exists and is an ancestor of ``HEAD``.

    Mirrors the two git gates inside ``verify_source_binding`` as a boolean
    probe so the sealed-evidence test can distinguish "history present, verify
    strictly" from "history absent, nothing to bind against" — instead of the
    binding raising ``git provenance check failed closed``. A squashed public
    export (built via ``git archive`` into a fresh ``git init``) deliberately
    drops the canonical repo's commit history, so the declared commit is gone.
    """
    for arguments in (
        ("cat-file", "-e", f"{source_commit}^{{commit}}"),
        ("merge-base", "--is-ancestor", source_commit, "HEAD"),
    ):
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode != 0:
            return False
    return True


def test_public_recovery_evidence_matches_executable_contract() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    payload = json.loads(_PUBLIC_RECOVERY_EVIDENCE.read_text(encoding="utf-8"))
    classification = payload["classification"]
    oracle = payload["existing_product_core_oracle"]
    source_binding = payload["source_binding"]
    # The sealed evidence is bound to an exact canonical-repo commit. Verify it
    # fully wherever that commit still lives in history; in a detached tree that
    # dropped the history (a squashed public export), there is provably nothing
    # to bind against, so self-skip — never fail closed, never falsely pass.
    _declared_commit = source_binding.get("source_commit")
    if not isinstance(_declared_commit, str) or not _commit_in_head_history(
        repository_root, _declared_commit
    ):
        pytest.skip(
            "sealed legacy-recovery evidence is bound to a canonical-repo commit "
            "absent from this history (e.g. a squashed public export); the "
            "retrospective provenance proof runs only where that commit survives"
        )
    # Retrospective verification: the sealed evidence must describe exactly
    # the bytes of its declared commit, and that commit must remain in this
    # branch's history. The current worktree is deliberately out of scope —
    # dated evidence stays verifiable after the product code moves on.
    recomputed_binding = binding.verify_source_binding(
        repo_root=repository_root,
        binding=source_binding,
    )

    assert payload["schema_version"] == (
        "agent-chronicle.unscoped-legacy-recovery.v3"
    )
    assert payload["design_id"] == RECOVERY_DESIGN_ID
    assert sum(
        classification[key]
        for key in (
            "candidate_recovery_high_log_evidenced",
            "canonical_imported",
            "canonical_no_effect",
            "quarantine_namespace_only",
            "quarantine_identity_conflict",
            "quarantine_ambiguous_session",
            "quarantine_conflicting_project_identity",
            "quarantine_no_proof",
            "quarantine_unsupported_retained",
            "invalid_retained",
        )
    ) == classification["partition_sum"] == 3979
    assert classification["visible_quarantine_total"] == 1161
    assert oracle["contract_version"] == (
        RECOVERY_PRODUCT_ORACLE_CONTRACT_VERSION
    )
    assert oracle["oracle_code_version"] == RECOVERY_PRODUCT_ORACLE_CODE_VERSION
    assert oracle["runner_version"] == runner._RECOVERY_PARITY_RUNNER_VERSION
    assert oracle["target_work_identity_mismatch_count"] == 0
    assert oracle["work_output_mismatch_count"] == 0
    assert oracle["task_output_mismatch_count"] == 0
    assert oracle["task_output_comparison_scope"] == (
        "target_recovered_work_only"
    )
    assert oracle["full_endpoint_parity_claimed"] is False
    assert oracle["full_task_detail_parity_claimed"] is False
    assert oracle["declared_semantic_namespace_conflicts_not_overwritten"] is True
    assert oracle["scoped_sibling_project_and_namespace_vetoes_applied"] is True
    assert source_binding["source_commit"] == recomputed_binding["source_commit"]
    assert source_binding["source_tree_sha256"] == recomputed_binding[
        "source_tree_sha256"
    ]
    assert source_binding["scope_file_count"] == recomputed_binding[
        "scope_file_count"
    ] == 92
    assert source_binding["source_commit_is_ancestor_of_evidence_commit"] is True
    assert source_binding["worktree_matched_source_commit_at_binding_time"] is True
    assert payload["decision"]["production_migration"] == "no-go"
    assert payload["decision"]["live_cutover"] == "no-go"


def test_runner_replays_a_twice_b_once_with_private_redacted_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest, scratch = _snapshot(tmp_path)
    report, report_path = runner.run_recovery_parity(
        _arguments(root, manifest, scratch)
    )

    assert report["completed"] is True
    assert report["passed"] is True
    assert report["classification"]["candidate_recovery"] == 2
    assert report["classification"]["no_proof"] >= 1
    assert report["classification"]["unsupported_retained"] >= 1
    assert report["classification"]["invalid_retained"] >= 1
    assert report["classification"]["classification_conservation"] is True
    assert report["candidate_a"]["rerun_canonical_writes"] == 0
    assert report["candidate_a"]["rerun_sequence_delta"] == 0
    assert report["candidate_a"]["rerun_projection_rebuilt"] is False
    assert report["candidate_a"]["rerun_total_changes_delta"] == 0
    assert report["candidate_a"]["rerun_task_ids_stable"] is True
    assert report["candidate_a"]["rerun_table_counts_stable"] is True
    assert report["candidate_a"]["first_recovery_conservation"] is True
    assert report["candidate_a"]["first_scoped_import_parity_match"] is True
    assert report["candidate_a"]["rerun_recovery_conservation"] is True
    assert report["candidate_a"]["rerun_scoped_import_parity_match"] is True
    assert report["candidate_a"]["task_ids_present_when_recovery_nonzero"] is True
    assert report["candidate_a"]["quarantine_excluded_from_canonical"] is True
    assert report["candidate_b"]["first_recovery_conservation"] is True
    assert report["candidate_b"]["first_scoped_import_parity_match"] is True
    assert report["candidate_b"]["task_ids_present_when_recovery_nonzero"] is True
    assert report["candidate_b"]["quarantine_excluded_from_canonical"] is True
    assert report["archive"]["receipt_conservation"] is True
    assert report["parity"]["semantic_match"] is True
    assert report["parity"]["canonical_sequence_match"] is True
    assert report["parity"]["table_counts_match"] is True
    assert report["parity"]["task_count_match"] is True
    assert report["parity"]["opaque_task_ids_valid"] is True
    oracle = report["product_oracle"]
    assert oracle["contract_version"] == 5
    assert oracle["oracle_code_version"] == 5
    assert oracle["runner_version"] == 6
    assert oracle["eligible_recovery_rows"] == 2
    assert oracle["recovered_section_rows"] == 1
    assert oracle["adapter_membership_only_task_rows"] == 1
    assert oracle["all_recovered_rows_have_existing_product_work_output_coverage"] is False
    assert oracle["source_adapter_rows"] == 2
    assert oracle["candidate_adapter_rows"] == 2
    assert oracle["recovery_transport_fact_rows"] == 2
    assert oracle["source_adapter_conservation"] is True
    assert oracle["candidate_adapter_conservation"] is True
    assert oracle["adapter_contract_match"] is True
    assert oracle["claim_fields_match"] is True
    assert oracle["task_membership_match"] is True
    assert oracle["existing_product_work_covered_recovery_rows"] == 1
    assert oracle["candidate_existing_product_work_covered_recovery_rows"] == 1
    # The recovered start and ordinary scoped completion are one full-context
    # cohort, so the independent candidate must preserve both revisions.
    assert oracle["source_existing_product_work_context_rows"] == 2
    assert oracle["candidate_existing_product_work_context_rows"] == 2
    assert oracle["target_work_identities_match"] is True
    assert oracle["source_distinct_target_work_identity_rows"] == 1
    assert oracle["candidate_distinct_target_work_identity_rows"] == 1
    assert oracle["target_work_identity_presence_conservation"] is True
    assert oracle["source_target_work_output_rows"] == 1
    assert oracle["candidate_target_work_output_rows"] == 1
    assert oracle["source_work_output_conservation"] is True
    assert oracle["candidate_work_output_conservation"] is True
    assert oracle["work_outputs_match"] is True
    assert oracle["source_affected_task_output_rows"] == 1
    assert oracle["candidate_affected_task_output_rows"] == 1
    assert oracle["source_target_work_affected_task_occurrence_rows"] == 1
    assert oracle["candidate_target_work_affected_task_occurrence_rows"] == 1
    assert oracle["source_target_work_task_membership_conservation"] is True
    assert oracle["candidate_target_work_task_membership_conservation"] is True
    assert oracle["task_output_comparison_target_only"] is True
    assert oracle["task_outputs_match"] is True
    assert oracle["quarantine_event_ids_checked"] == 2
    assert oracle["quarantine_zero_contribution"] is True
    assert oracle["candidate_reports_match"] is True
    assert oracle["passed"] is True
    _assert_counts_and_booleans(report)

    retained = report_path.read_text(encoding="utf-8")
    encoded = json.dumps(report, sort_keys=True)
    for public_output in (retained, encoded):
        assert SECRET not in public_output
        assert str(root) not in public_output
        assert str(manifest) not in public_output
        assert str(scratch) not in public_output
        assert re.search(r"\b[0-9a-f]{64}\b", public_output) is None
    assert json.loads(retained) == report

    expected_candidates = report["classification"]["candidate_recovery"]
    expected_quarantine_event_ids = report["candidate_a"][
        "quarantine_event_id_count"
    ]
    inventory_lines = report["inventory"]["physical_line_count"]
    negative_gates = (
        ("archive", "receipt_count", inventory_lines - 1),
        ("archive", "sealed_plan_verified", False),
        ("classification", "classification_conservation", False),
        (
            "classification",
            "canonical_imported",
            report["classification"]["canonical_imported"] + 1,
        ),
        ("candidate_a", "first_recovery_candidate_count", expected_candidates - 1),
        ("candidate_a", "first_fact_inserted_count", expected_candidates - 1),
        ("candidate_a", "first_link_inserted_count", expected_candidates - 1),
        ("candidate_a", "first_projection_rebuilt", False),
        ("candidate_a", "first_scoped_import_parity_match", False),
        ("candidate_a", "rerun_fact_noop_count", expected_candidates - 1),
        ("candidate_a", "rerun_link_noop_count", expected_candidates - 1),
        ("candidate_a", "rerun_scoped_import_parity_match", False),
        ("candidate_a", "task_count", 0),
        ("candidate_b", "first_recovery_candidate_count", expected_candidates - 1),
        ("candidate_b", "first_fact_inserted_count", expected_candidates - 1),
        ("candidate_b", "first_link_inserted_count", expected_candidates - 1),
        ("candidate_b", "first_projection_rebuilt", False),
        ("candidate_b", "first_scoped_import_parity_match", False),
        ("candidate_b", "task_count", 0),
        ("product_oracle", "contract_version", 0),
        ("product_oracle", "oracle_code_version", 0),
        ("product_oracle", "runner_version", 1),
        ("product_oracle", "adapter_contract_match", False),
        ("product_oracle", "claim_fields_match", False),
        ("product_oracle", "task_membership_match", False),
        ("product_oracle", "target_work_identities_match", False),
        (
            "product_oracle",
            "target_work_identity_presence_conservation",
            False,
        ),
        ("product_oracle", "source_work_output_conservation", False),
        ("product_oracle", "candidate_work_output_conservation", False),
        (
            "product_oracle",
            "source_target_work_task_membership_conservation",
            False,
        ),
        (
            "product_oracle",
            "candidate_target_work_task_membership_conservation",
            False,
        ),
        ("product_oracle", "task_output_comparison_target_only", False),
        ("product_oracle", "source_target_work_output_rows", 0),
        ("product_oracle", "candidate_target_work_output_rows", 0),
        ("product_oracle", "source_affected_task_output_rows", 0),
        ("product_oracle", "candidate_affected_task_output_rows", 0),
        (
            "product_oracle",
            "source_target_work_affected_task_occurrence_rows",
            0,
        ),
        (
            "product_oracle",
            "candidate_target_work_affected_task_occurrence_rows",
            0,
        ),
        ("product_oracle", "work_outputs_match", False),
        ("product_oracle", "task_outputs_match", False),
        ("product_oracle", "quarantine_event_ids_checked", 0),
        ("product_oracle", "quarantine_zero_contribution", False),
        ("product_oracle", "candidate_reports_match", False),
        ("product_oracle", "passed", False),
    )
    for section, key, value in negative_gates:
        broken = copy.deepcopy(report)
        broken[section][key] = value
        assert runner._acceptance_passed(
            broken,
            expected_candidates=expected_candidates,
            expected_quarantine_event_id_count=expected_quarantine_event_ids,
            inventory_line_count=inventory_lines,
        ) is False, (section, key)

    # Equality alone must not accept a same-wrong partial result.  Model two
    # target identities while retaining only one Work output / Task occurrence
    # on both sides and leave the reported booleans deliberately green.
    partial = copy.deepcopy(report)
    for prefix in ("source", "candidate"):
        partial["product_oracle"][
            f"{prefix}_distinct_target_work_identity_rows"
        ] = 2
    assert runner._acceptance_passed(
        partial,
        expected_candidates=expected_candidates,
        expected_quarantine_event_id_count=expected_quarantine_event_ids,
        inventory_line_count=inventory_lines,
    ) is False

    all_outputs_missing = copy.deepcopy(report)
    for field_name in (
        "source_target_work_output_rows",
        "candidate_target_work_output_rows",
        "source_affected_task_output_rows",
        "candidate_affected_task_output_rows",
    ):
        all_outputs_missing["product_oracle"][field_name] = 0
    assert runner._acceptance_passed(
        all_outputs_missing,
        expected_candidates=expected_candidates,
        expected_quarantine_event_id_count=expected_quarantine_event_ids,
        inventory_line_count=inventory_lines,
    ) is False

    run_directory = report_path.parent
    archive_directory = run_directory / "archive"
    candidate_a = run_directory / "candidate-a.sqlite3"
    candidate_b = run_directory / "candidate-b.sqlite3"
    assert _mode(scratch) == 0o700
    assert _mode(run_directory) == 0o700
    assert _mode(archive_directory) == 0o700
    assert _mode(candidate_a) == 0o600
    assert _mode(candidate_b) == 0o600
    assert _mode(report_path) == 0o600
    assert all(
        child.is_file()
        and not child.is_symlink()
        and _mode(child) == 0o600
        for child in archive_directory.iterdir()
    )

    connection = sqlite3.connect(f"file:{candidate_a}?mode=ro", uri=True)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM facts WHERE transport = ?",
            (RECOVERY_FACT_TRANSPORT,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM facts WHERE source_event_id IN (?, ?)",
            (NO_PROOF_ID, UNSUPPORTED_ID),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM fact_session_links link "
            "JOIN facts fact USING(fact_id) "
            "WHERE fact.transport = ? AND link.confidence = 'exact'",
            (RECOVERY_FACT_TRANSPORT,),
        ).fetchone()[0] == 0
    finally:
        connection.close()

    monkeypatch.setattr(
        runner,
        "run_recovery_parity",
        lambda _arguments: (report, Path(f"/private/{SECRET}/result.json")),
    )
    exit_code = runner.main(
        [
            "--snapshot-root",
            f"/private/{SECRET}/snapshot",
            "--snapshot-manifest",
            f"/private/{SECRET}/manifest.json",
            "--scratch-root",
            f"/private/{SECRET}/scratch",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert SECRET not in captured.out
    assert re.search(r"/[^\"\s]+", captured.out) is None
    assert re.search(r"\b[0-9a-f]{64}\b", captured.out) is None
    assert json.loads(captured.out) == report


def test_runner_quarantines_declared_semantic_namespace_conflict(
    tmp_path: Path,
) -> None:
    root, manifest, scratch = _snapshot(
        tmp_path,
        recovered_semantic_namespace=OTHER_NAMESPACE,
    )

    report, _report_path = runner.run_recovery_parity(
        _arguments(root, manifest, scratch)
    )

    assert report["completed"] is True
    assert report["passed"] is True
    assert report["classification"]["candidate_recovery"] == 1
    assert report["classification"]["identity_conflict"] >= 1
    oracle = report["product_oracle"]
    assert oracle["eligible_recovery_rows"] == 1
    assert oracle["recovered_section_rows"] == 0
    assert oracle["adapter_membership_only_task_rows"] == 1
    assert oracle["passed"] is True


def test_runner_oracle_sees_valid_line_larger_than_canonical_import_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_import_module, "MAX_JSONL_LINE_BYTES", 2_000)
    monkeypatch.setattr(
        product_parity_module,
        "MAX_JSONL_LINE_BYTES",
        2_000,
        raising=False,
    )
    root, manifest, scratch = _snapshot(
        tmp_path,
        scoped_completion_summary="oversized-valid-completion-" + ("x" * 2048),
    )

    report, _report_path = runner.run_recovery_parity(
        _arguments(root, manifest, scratch)
    )

    assert report["completed"] is True
    assert report["parity"]["semantic_match"] is True
    oracle = report["product_oracle"]
    assert oracle["source_existing_product_work_context_rows"] == 2
    assert oracle["candidate_existing_product_work_context_rows"] == 1
    assert oracle["existing_product_work_context_rows_match"] is False
    assert oracle["work_outputs_match"] is False
    assert oracle["passed"] is False
    assert report["passed"] is False


def test_runner_maps_recovery_overlay_after_malformed_physical_line(
    tmp_path: Path,
) -> None:
    root, manifest, scratch = _snapshot(
        tmp_path,
        malformed_before_target=True,
    )

    report, _report_path = runner.run_recovery_parity(
        _arguments(root, manifest, scratch)
    )

    assert report["completed"] is True
    assert report["passed"] is True
    assert report["classification"]["candidate_recovery"] == 2
    assert report["product_oracle"]["source_malformed_or_non_object_rows"] == 2
    assert report["product_oracle"]["passed"] is True


def test_runner_rejects_identical_candidates_when_scoped_source_parity_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest, scratch = _snapshot(tmp_path)
    replay = runner.replay_verified_recovery

    def replay_with_scoped_mismatch(**kwargs: Any) -> Any:
        report = replay(**kwargs)
        assert report.scoped_imports
        scoped_imports = tuple(
            replace(
                scoped,
                parity=replace(
                    scoped.parity,
                    matches=False,
                    differences={
                        **scoped.parity.differences,
                        "synthetic_source_mismatch": {
                            "source": 1,
                            "candidate": 0,
                        },
                    },
                ),
            )
            for scoped in report.scoped_imports
        )
        return replace(report, scoped_imports=scoped_imports)

    monkeypatch.setattr(
        runner,
        "replay_verified_recovery",
        replay_with_scoped_mismatch,
    )

    report, _report_path = runner.run_recovery_parity(
        _arguments(root, manifest, scratch)
    )

    # Both candidates still contain the same deterministic wrong result.  The
    # source-to-candidate mismatch must independently veto acceptance.
    assert report["parity"]["semantic_match"] is True
    assert report["parity"]["table_counts_match"] is True
    assert report["candidate_a"]["first_scoped_import_parity_match"] is False
    assert report["candidate_a"]["rerun_scoped_import_parity_match"] is False
    assert report["candidate_b"]["first_scoped_import_parity_match"] is False
    assert report["passed"] is False


@pytest.mark.parametrize(
    ("corruption", "red_oracle_fields"),
    [
        (
            "claim",
            {
                "adapter_contract_match",
                "claim_fields_match",
            },
        ),
        (
            "membership",
            {
                "adapter_contract_match",
                "task_membership_match",
                "target_work_identities_match",
                "task_outputs_match",
            },
        ),
        (
            "wrong_source_session",
            {
                "adapter_contract_match",
                "task_membership_match",
                "target_work_identities_match",
                "task_outputs_match",
            },
        ),
        (
            "context_link_rejected",
            {
                "existing_product_work_context_rows_match",
                "work_outputs_match",
                "task_outputs_match",
            },
        ),
        (
            "ordering",
            {
                "adapter_contract_match",
                "work_outputs_match",
                "task_outputs_match",
            },
        ),
        (
            "quarantine",
            {
                "adapter_contract_match",
                "claim_fields_match",
                "quarantine_zero_contribution",
            },
        ),
    ],
)
def test_independent_product_oracle_rejects_same_wrong_candidate_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    red_oracle_fields: set[str],
) -> None:
    root, manifest, scratch = _snapshot(tmp_path)
    source_oracle = runner.build_legacy_recovery_product_oracle_report

    def corrupt_then_compare(**kwargs: Any) -> dict[str, int | bool]:
        repository = kwargs["repository"]
        if corruption == "claim":
            cursor = repository.connection.execute(
                "UPDATE work_claims SET title = 'deliberately-corrupted-title' "
                "WHERE fact_id = (SELECT fact_id FROM facts "
                "WHERE transport = ? AND source_event_id = ?)",
                (RECOVERY_FACT_TRANSPORT, RECOVERED_SECTION_ID),
            )
        elif corruption == "membership":
            cursor = repository.connection.execute(
                "UPDATE fact_session_links SET session_id = ("
                "SELECT session_id FROM sessions WHERE client_session_id = ?"
                ") WHERE fact_id = (SELECT fact_id FROM facts "
                "WHERE transport = ? AND source_event_id = ?)",
                (
                    OTHER_SESSION_ID,
                    RECOVERY_FACT_TRANSPORT,
                    RECOVERED_SECTION_ID,
                ),
            )
        elif corruption == "wrong_source_session":
            other_digest = hashlib.sha256(
                b"legacy-explicit-namespace-v1\x00"
                + OTHER_NAMESPACE.encode("utf-8")
            ).digest()
            cursor = repository.connection.execute(
                "UPDATE fact_session_links SET session_id = ("
                "SELECT session.session_id FROM sessions session "
                "JOIN source_instances source USING(source_instance_id) "
                "WHERE session.client_session_id = ? "
                "AND source.namespace_digest = ?"
                ") WHERE fact_id = (SELECT fact_id FROM facts "
                "WHERE transport = ? AND source_event_id = ?)",
                (
                    SESSION_ID,
                    other_digest,
                    RECOVERY_FACT_TRANSPORT,
                    RECOVERED_SECTION_ID,
                ),
            )
        elif corruption == "ordering":
            cursor = repository.connection.execute(
                "UPDATE facts SET occurred_at_us = ("
                "SELECT occurred_at_us + 1000000 FROM facts "
                "WHERE source_event_id = ?"
                ") WHERE transport = ? AND source_event_id = ?",
                (
                    SCOPED_COMPLETION_ID,
                    RECOVERY_FACT_TRANSPORT,
                    RECOVERED_SECTION_ID,
                ),
            )
        elif corruption == "context_link_rejected":
            cursor = repository.connection.execute(
                "UPDATE fact_session_links SET validation_state = 'rejected', "
                "veto_reason = 'synthetic_rejected_context' "
                "WHERE fact_id = (SELECT fact_id FROM facts "
                "WHERE source_event_id = ?)",
                (SCOPED_COMPLETION_ID,),
            )
        else:
            cursor = repository.connection.execute(
                "UPDATE facts SET source_event_id = ? "
                "WHERE transport = ? AND source_event_id = ?",
                (
                    UNSUPPORTED_ID,
                    RECOVERY_FACT_TRANSPORT,
                    RECOVERED_SECTION_ID,
                ),
            )
        assert cursor.rowcount == 1
        return source_oracle(**kwargs)

    monkeypatch.setattr(
        runner,
        "build_legacy_recovery_product_oracle_report",
        corrupt_then_compare,
    )
    report, _report_path = runner.run_recovery_parity(
        _arguments(root, manifest, scratch)
    )

    # Both candidates receive the same deliberate damage, so A/B agreement and
    # the importer's pre-corruption parity stay green.  Only the independently
    # rebuilt existing-product oracle vetoes acceptance.
    assert report["parity"]["semantic_match"] is True
    assert report["parity"]["table_counts_match"] is True
    assert report["candidate_a"]["first_recovery_conservation"] is True
    assert report["candidate_b"]["first_recovery_conservation"] is True
    assert report["candidate_a"]["first_scoped_import_parity_match"] is True
    oracle = report["product_oracle"]
    assert oracle["candidate_reports_match"] is True
    for field_name in red_oracle_fields:
        assert oracle[field_name] is False
    assert oracle["passed"] is False
    assert report["passed"] is False
    assert runner._acceptance_passed(
        report,
        expected_candidates=2,
        expected_quarantine_event_id_count=report["candidate_a"][
            "quarantine_event_id_count"
        ],
        inventory_line_count=report["inventory"]["physical_line_count"],
    ) is False


def test_candidate_product_surface_uses_source_identity_for_duplicate_multifile_event_id() -> None:
    duplicate_event_id = "evt-duplicate-across-manifest-files"
    source_a = {
        "client": "codex",
        "namespace_scheme": "legacy-explicit-fingerprint-sha256-v1",
        "namespace_digest": "aa" * 32,
    }
    source_b = {
        "client": "codex",
        "namespace_scheme": "legacy-explicit-fingerprint-sha256-v1",
        "namespace_digest": "bb" * 32,
    }

    def claim(source: Mapping[str, object]) -> dict[str, object]:
        return {
            "source": source,
            "source_event_id": duplicate_event_id,
            "source_order": 1,
            "occurred_at_us": 1_000_000,
            "claim": {
                "section_id": "same-raw-section-id",
                "status": "started",
                "title": "synthetic",
                "summary": None,
                "blocker": None,
                "next_step": None,
            },
            "session": {},
            "link": {"validation_state": None},
        }

    events = _candidate_product_events(
        candidate_claim_rows=[claim(source_a), claim(source_b)],
        # Models a manifest where only file/source A supplied the Work-surface
        # row.  The same raw event id from file/source B must not cross-talk.
        source_product_claim_keys={
            _claim_surface_key(source_a, duplicate_event_id)
        },
    )

    assert len(events) == 1
    assert events[0]["source"] == "codex"


@pytest.mark.parametrize("overlap", ["snapshot", "manifest", "scratch"])
def test_configured_live_overlap_fails_before_manifest_read_verify_or_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
) -> None:
    live = tmp_path / "configured-live"
    live.mkdir(mode=0o700)
    os.chmod(live, 0o700)
    safe_scratch = tmp_path / "safe-scratch"
    safe_scratch.mkdir(mode=0o700)
    os.chmod(safe_scratch, 0o700)
    live_scratch = live / "scratch"
    live_scratch.mkdir(mode=0o700)
    os.chmod(live_scratch, 0o700)
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(live))

    snapshot = live / "snapshot" if overlap == "snapshot" else tmp_path / "snapshot"
    manifest = live / "manifest.json" if overlap == "manifest" else tmp_path / "manifest.json"
    scratch = live_scratch if overlap == "scratch" else safe_scratch
    calls = {"load": 0, "verify": 0, "mkdir": 0}

    def fail_load(*_args: object, **_kwargs: object) -> object:
        calls["load"] += 1
        raise AssertionError("manifest load must not run")

    def fail_verify(*_args: object, **_kwargs: object) -> object:
        calls["verify"] += 1
        raise AssertionError("snapshot verification must not run")

    def fail_mkdir(*_args: object, **_kwargs: object) -> object:
        calls["mkdir"] += 1
        raise AssertionError("run directory creation must not run")

    monkeypatch.setattr(runner.SnapshotManifest, "load", fail_load)
    monkeypatch.setattr(runner.VerifiedSnapshot, "verify", fail_verify)
    monkeypatch.setattr(runner, "create_anchored_run_directory", fail_mkdir)

    with pytest.raises(runner.RecoveryParityRefusal, match="live state"):
        runner.run_recovery_parity(_arguments(snapshot, manifest, scratch))

    assert calls == {"load": 0, "verify": 0, "mkdir": 0}
    assert not any(
        path.name.startswith("legacy-recovery-parity-")
        for parent in (live, safe_scratch, live_scratch)
        for path in parent.iterdir()
    )


@pytest.mark.parametrize("mode", [0o755, 0o500])
def test_scratch_requires_exact_0700_before_manifest_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    scratch = tmp_path / "unsafe-scratch"
    scratch.mkdir(mode=0o700)
    os.chmod(scratch, mode)
    calls = 0

    def fail_load(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("manifest load must not run")

    monkeypatch.setattr(runner.SnapshotManifest, "load", fail_load)
    with pytest.raises(runner.RecoveryParityRefusal, match="0700"):
        runner.run_recovery_parity(
            _arguments(
                tmp_path / "snapshot",
                tmp_path / "manifest.json",
                scratch.resolve(),
            )
        )
    assert calls == 0
    assert tuple(scratch.iterdir()) == ()


def test_scratch_symlink_is_rejected_before_manifest_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private-scratch"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    scratch_link = tmp_path / "scratch-link"
    scratch_link.symlink_to(target, target_is_directory=True)
    calls = 0

    def fail_load(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("manifest load must not run")

    monkeypatch.setattr(runner.SnapshotManifest, "load", fail_load)
    with pytest.raises(runner.RecoveryParityRefusal, match="symlink"):
        runner.run_recovery_parity(
            _arguments(
                tmp_path / "snapshot",
                tmp_path / "manifest.json",
                scratch_link,
            )
        )
    assert calls == 0
    assert tuple(target.iterdir()) == ()


@pytest.mark.parametrize(
    "component",
    [
        ".agent-sentinel",
        ".agent-sentinel-global",
        ".agent-chronicle",
        ".agent-chronicle-global",
        ".codex",
        ".AGENT-SENTINEL",
    ],
)
def test_scratch_forbidden_component_is_rejected_before_mkdir_or_manifest_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    live_shaped = tmp_path / component
    live_shaped.mkdir(mode=0o700)
    os.chmod(live_shaped, 0o700)
    scratch = live_shaped / "scratch"
    scratch.mkdir(mode=0o700)
    os.chmod(scratch, 0o700)
    calls = {"load": 0, "verify": 0, "mkdir": 0}

    def fail_load(*_args: object, **_kwargs: object) -> object:
        calls["load"] += 1
        raise AssertionError("manifest load must not run")

    def fail_verify(*_args: object, **_kwargs: object) -> object:
        calls["verify"] += 1
        raise AssertionError("snapshot verification must not run")

    def fail_mkdir(*_args: object, **_kwargs: object) -> object:
        calls["mkdir"] += 1
        raise AssertionError("run directory creation must not run")

    monkeypatch.setattr(runner.SnapshotManifest, "load", fail_load)
    monkeypatch.setattr(runner.VerifiedSnapshot, "verify", fail_verify)
    monkeypatch.setattr(runner, "create_anchored_run_directory", fail_mkdir)

    with pytest.raises(
        runner.RecoveryParityRefusal,
        match="live agentacct or Codex state|disjoint from live state",
    ):
        runner.run_recovery_parity(
            _arguments(
                tmp_path / "snapshot",
                tmp_path / "manifest.json",
                scratch.resolve(),
            )
        )

    assert calls == {"load": 0, "verify": 0, "mkdir": 0}
    assert tuple(scratch.iterdir()) == ()


def test_descriptor_private_root_check_closes_post_preflight_chmod_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest, scratch = _snapshot(tmp_path)
    original_create = runner.create_anchored_run_directory
    manifest_loads = 0
    original_load = runner.SnapshotManifest.load

    def tracked_load(*args: object, **kwargs: object) -> object:
        nonlocal manifest_loads
        manifest_loads += 1
        return original_load(*args, **kwargs)

    def chmod_after_path_preflight(
        scratch_root: Path,
        *,
        prefix: str,
        require_private_root: bool = False,
    ) -> object:
        assert require_private_root is True
        os.chmod(scratch_root, 0o755)
        try:
            return original_create(
                scratch_root,
                prefix=prefix,
                require_private_root=require_private_root,
            )
        finally:
            os.chmod(scratch_root, 0o700)

    monkeypatch.setattr(runner.SnapshotManifest, "load", tracked_load)
    monkeypatch.setattr(
        runner,
        "create_anchored_run_directory",
        chmod_after_path_preflight,
    )

    with pytest.raises(runner.RecoveryParityRefusal, match="0700"):
        runner.run_recovery_parity(_arguments(root, manifest, scratch))

    assert manifest_loads == 0
    assert not any(
        child.name.startswith("legacy-recovery-parity-")
        for child in scratch.iterdir()
    )


def test_semantic_fingerprint_keeps_source_times_and_only_ignores_runtime_fields() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE facts(
            fact_id INTEGER PRIMARY KEY,
            occurred_at_us INTEGER NOT NULL,
            created_at_us INTEGER NOT NULL
        );
        CREATE TABLE usage_measurements(
            usage_measurement_id INTEGER PRIMARY KEY,
            observed_at_us INTEGER NOT NULL,
            updated_at_us INTEGER NOT NULL
        );
        CREATE TABLE cost_calculations(
            cost_calculation_id INTEGER PRIMARY KEY,
            price_effective_at_us INTEGER NOT NULL,
            calculated_at_us INTEGER NOT NULL
        );
        CREATE TABLE sessions(
            session_id INTEGER PRIMARY KEY,
            started_at_us INTEGER NOT NULL,
            last_activity_at_us INTEGER NOT NULL,
            sort_activity_at_us INTEGER NOT NULL,
            created_at_us INTEGER NOT NULL,
            updated_at_us INTEGER NOT NULL
        );
        CREATE TABLE store_metadata(
            singleton INTEGER PRIMARY KEY,
            store_uuid TEXT NOT NULL,
            created_at_us INTEGER NOT NULL
        );
        CREATE TABLE task_anchors(
            task_anchor_id INTEGER PRIMARY KEY,
            public_task_id TEXT NOT NULL,
            created_at_us INTEGER NOT NULL
        );
        CREATE TABLE rm_task_current(
            public_task_id TEXT PRIMARY KEY,
            task_anchor_id INTEGER NOT NULL
        );
        CREATE TABLE projection_generations(
            projection_name TEXT PRIMARY KEY,
            built_at_us INTEGER NOT NULL
        );
        INSERT INTO facts VALUES (1, 100, 900);
        INSERT INTO usage_measurements VALUES (1, 200, 201);
        INSERT INTO cost_calculations VALUES (1, 300, 301);
        INSERT INTO sessions VALUES (1, 400, 401, 402, 901, 902);
        INSERT INTO store_metadata VALUES (1, 'store-random-a', 903);
        INSERT INTO task_anchors VALUES (1, 'task_random_a', 904);
        INSERT INTO rm_task_current VALUES ('task_random_a', 1);
        INSERT INTO projection_generations VALUES ('rm_task_current', 905);
        """
    )
    store = SimpleNamespace(connection=connection)
    baseline, _rows = runner._semantic_fingerprint(store)

    semantic_times = (
        ("facts", "occurred_at_us"),
        ("usage_measurements", "observed_at_us"),
        ("usage_measurements", "updated_at_us"),
        ("cost_calculations", "price_effective_at_us"),
        ("cost_calculations", "calculated_at_us"),
        ("sessions", "started_at_us"),
        ("sessions", "last_activity_at_us"),
        ("sessions", "sort_activity_at_us"),
    )
    for table, column in semantic_times:
        connection.execute(f"UPDATE {table} SET {column} = {column} + 1")
        mutated, _rows = runner._semantic_fingerprint(store)
        assert mutated != baseline, (table, column)
        connection.execute(f"UPDATE {table} SET {column} = {column} - 1")
        restored, _rows = runner._semantic_fingerprint(store)
        assert restored == baseline, (table, column)

    runtime_updates = (
        "UPDATE facts SET created_at_us = created_at_us + 1",
        "UPDATE sessions SET created_at_us = created_at_us + 1",
        "UPDATE sessions SET updated_at_us = updated_at_us + 1",
        "UPDATE store_metadata SET created_at_us = created_at_us + 1",
        "UPDATE store_metadata SET store_uuid = 'store-random-b'",
        "UPDATE task_anchors SET created_at_us = created_at_us + 1",
        "UPDATE task_anchors SET public_task_id = 'task_random_b'",
        "UPDATE rm_task_current SET public_task_id = 'task_random_b'",
        "UPDATE projection_generations SET built_at_us = built_at_us + 1",
    )
    for statement in runtime_updates:
        connection.execute(statement)
    runtime_mutated, _rows = runner._semantic_fingerprint(store)
    assert runtime_mutated == baseline
    connection.close()
