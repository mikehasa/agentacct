from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import agent_chronicle.cli as cli
import agent_chronicle.evidence_rebuild_activation as activation
import agent_chronicle.evidence_rebuild_candidate as candidate
import agent_chronicle.evidence_rebuild_snapshot as snapshot
from test_evidence_rebuild_snapshot import COMMIT, STORE_UUID, _build_source


runner = CliRunner()


def _owner_dir(base: Path, name: str) -> Path:
    """Create an owner-only 0700 directory (umask-proof) under ``base``."""

    directory = base / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _owner_receipt(directory: Path, name: str, raw: bytes) -> Path:
    """Write a mode-0600 receipt file with exactly the given bytes."""

    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_rebuild_help_exposes_the_complete_owner_gated_workflow() -> None:
    result = runner.invoke(cli.app, ["evidence", "rebuild", "--help"])

    assert result.exit_code == 0
    for command in (
        "snapshot",
        "verify-snapshot",
        "build-candidate",
        "verify-candidate",
        "activate",
        "inspect-activation",
        "recover-activation",
        "verify-activation",
        "rollback",
        "inspect-rollback",
        "recover-rollback",
    ):
        assert command in result.stdout


def test_mutating_commands_refuse_before_reading_receipts_without_owner_approval(
    monkeypatch,
) -> None:
    def receipt_read_must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("receipt reader ran before the owner gate")

    monkeypatch.setattr(cli, "_evidence_rebuild_receipt_json", receipt_read_must_not_run)
    cases = (
        [
            "build-candidate",
            "--snapshot-receipt",
            "/private/snapshot/snapshot-receipt.json",
            "--candidate-parent",
            "/private/candidates",
            "--codex-home",
            "/private/codex",
            "--claude-home",
            "/private/claude",
            "--hermes-home",
            "/private/hermes",
        ],
        [
            "activate",
            "--candidate-receipt",
            "/private/candidate/evidence-rebuild-receipt.json",
            "--snapshot-receipt",
            "/private/snapshot/snapshot-receipt.json",
            "--archive-parent",
            "/private/archive",
            "--receipt",
            "/private/receipts/activation.json",
        ],
        [
            "rollback",
            "--activation-receipt",
            "/private/receipts/activation.json",
            "--failed-archive-parent",
            "/private/archive",
            "--receipt",
            "/private/receipts/rollback.json",
        ],
    )

    for command in cases:
        result = runner.invoke(cli.app, ["evidence", "rebuild", *command, "--json"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["status"] == "refused"
        assert "owner" in payload["error"]

    monkeypatch.setattr(
        activation,
        "recover_activation_from_intent",
        receipt_read_must_not_run,
    )
    monkeypatch.setattr(
        activation,
        "recover_rollback_from_intent",
        receipt_read_must_not_run,
    )
    recovery_cases = (
        [
            "recover-activation",
            "--activation-intent",
            "/private/receipts/activation.json.intent.json",
            "--receipt",
            "/private/receipts/activation-recovered.json",
        ],
        [
            "recover-rollback",
            "--rollback-intent",
            "/private/receipts/rollback.json.intent.json",
            "--receipt",
            "/private/receipts/rollback-recovered.json",
        ],
    )
    for command in recovery_cases:
        result = runner.invoke(cli.app, ["evidence", "rebuild", *command, "--json"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["status"] == "refused"
        assert "owner" in payload["error"]


def test_build_candidate_cli_forwards_only_the_complete_approved_rebuild(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}
    snapshot_receipt = Path("/private/snapshot/snapshot-receipt.json")

    monkeypatch.setattr(
        cli,
        "_evidence_rebuild_receipt_json",
        lambda path, expected_name=None: (snapshot_receipt, {}),
    )

    def fake_build(snapshot_root, snapshot_manifest, candidate_parent, **kwargs):
        observed.update(
            snapshot_root=snapshot_root,
            snapshot_manifest=snapshot_manifest,
            candidate_parent=candidate_parent,
            **kwargs,
        )
        return {
            "status": "verified_candidate",
            "candidate_root": "/private/candidates/candidate-1",
            "receipt": "/private/candidates/candidate-1/evidence-rebuild-receipt.json",
            "activation_ready": True,
        }

    monkeypatch.setattr(candidate, "build_evidence_rebuild_candidate", fake_build)
    result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "build-candidate",
            "--snapshot-receipt",
            str(snapshot_receipt),
            "--candidate-parent",
            "/private/candidates",
            "--codex-home",
            "/private/codex",
            "--claude-home",
            "/private/claude",
            "--hermes-home",
            "/private/hermes",
            "--all-retained-sessions",
            "--require-source-complete",
            "codex",
            "--require-source-complete",
            "claude-code",
            "--require-source-complete",
            "hermes",
            "--preserve-old-generic",
            "--confirm-writers-stopped",
            "--confirm-owner-approved-rebuild",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    progress = observed.pop("progress", None)
    assert callable(progress)
    assert observed == {
        "snapshot_root": snapshot_receipt.parent,
        "snapshot_manifest": snapshot_receipt.parent / snapshot.MANIFEST_FILENAME,
        "candidate_parent": Path("/private/candidates"),
        "confirm_owner_approved": True,
        "raw_source_roots": {
            "codex": Path("/private/codex"),
            "claude-code": Path("/private/claude"),
            "hermes": Path("/private/hermes"),
        },
        "clients": ("codex", "claude-code", "hermes"),
    }
    assert json.loads(result.stdout)["activation_ready"] is True


def test_verify_candidate_cli_forwards_the_fixed_required_client_set(
    monkeypatch,
) -> None:
    candidate_receipt = Path("/private/candidate/evidence-rebuild-receipt.json")
    snapshot_receipt = Path("/private/snapshot/snapshot-receipt.json")
    observed: dict[str, object] = {}

    def fake_receipt(path, expected_name=None):
        if Path(path).name == candidate.REBUILD_RECEIPT_FILENAME:
            return candidate_receipt, {}
        return snapshot_receipt, {}

    monkeypatch.setattr(cli, "_evidence_rebuild_receipt_json", fake_receipt)

    def fake_verify(candidate_root, **kwargs):
        observed.update(candidate_root=candidate_root, **kwargs)
        return {
            "status": "verified",
            "candidate_root": str(candidate_root),
            "logical_digest": "e" * 64,
            "activation_ready": True,
        }

    monkeypatch.setattr(candidate, "verify_evidence_rebuild_candidate", fake_verify)
    result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "verify-candidate",
            "--candidate-receipt",
            str(candidate_receipt),
            "--snapshot-receipt",
            str(snapshot_receipt),
            "--scratch-parent",
            "/private/scratch",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert observed == {
        "candidate_root": candidate_receipt.parent,
        "scratch_parent": Path("/private/scratch"),
        "snapshot_root": snapshot_receipt.parent,
        "snapshot_manifest": snapshot_receipt.parent / snapshot.MANIFEST_FILENAME,
        "required_clients": ("codex", "claude-code", "hermes"),
    }
    assert observed["required_clients"] == candidate.DEFAULT_REBUILD_CLIENTS
    assert json.loads(result.stdout)["activation_ready"] is True


def test_activate_cli_binds_verified_snapshot_candidate_and_live_tree(
    monkeypatch,
) -> None:
    candidate_receipt = Path("/private/candidate/evidence-rebuild-receipt.json")
    snapshot_receipt = Path("/private/snapshot/snapshot-receipt.json")
    candidate_tree = {"tree_sha256": "c" * 64, "total_bytes": 200}
    live_tree = {"tree_sha256": "a" * 64, "total_bytes": 100}
    candidate_payload = {
        "activation_ready": True,
        "receipt_sha256": "b" * 64,
        "candidate": {
            "evidence_path": str(candidate_receipt.parent / "evidence-v2"),
            "evidence_tree": candidate_tree,
        },
    }
    snapshot_payload = {"live_evidence_tree": live_tree}
    observed: dict[str, object] = {}

    def fake_receipt(path, expected_name=None):
        if Path(path).name == candidate.REBUILD_RECEIPT_FILENAME:
            return candidate_receipt, candidate_payload
        return snapshot_receipt, snapshot_payload

    monkeypatch.setattr(cli, "_evidence_rebuild_receipt_json", fake_receipt)

    def fake_verify(candidate_root, **kwargs):
        observed.update(verify_candidate_root=candidate_root, verify_kwargs=kwargs)
        return {"activation_ready": True}

    monkeypatch.setattr(candidate, "verify_evidence_rebuild_candidate", fake_verify)
    monkeypatch.setattr(
        snapshot,
        "verify_evidence_snapshot",
        lambda *args, **kwargs: SimpleNamespace(manifest_sha256="d" * 64),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_cli_store_dir",
        lambda value: SimpleNamespace(path=Path("/private/live-store")),
    )

    def fake_prepare(live, candidate_path, **kwargs):
        observed.update(live=live, candidate=candidate_path, **kwargs)
        return SimpleNamespace(to_dict=lambda: {"intent": "sealed"})

    fake_result = SimpleNamespace(
        installed_live=SimpleNamespace(path=Path("/private/live-store/evidence-v2")),
        archived_previous_live=SimpleNamespace(path=Path("/private/archive/old")),
        to_dict=lambda: {"status": "activated"},
    )
    monkeypatch.setattr(activation, "prepare_activation_intent", fake_prepare)

    def fake_activate(intent_path, **kwargs):
        observed.update(activation_intent_path=intent_path, activation_kwargs=kwargs)
        return fake_result

    monkeypatch.setattr(activation, "activate_evidence_rebuild", fake_activate)
    result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "activate",
            "--candidate-receipt",
            str(candidate_receipt),
            "--snapshot-receipt",
            str(snapshot_receipt),
            "--archive-parent",
            "/private/archive",
            "--receipt",
            "/private/receipts/activation.json",
            "--confirm-writers-stopped",
            "--confirm-owner-approved-activation",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert observed["verify_candidate_root"] == candidate_receipt.parent
    assert observed["verify_kwargs"] == {
        "scratch_parent": candidate_receipt.parent.parent,
        "snapshot_root": snapshot_receipt.parent,
        "snapshot_manifest": snapshot_receipt.parent / snapshot.MANIFEST_FILENAME,
        "required_clients": ("codex", "claude-code", "hermes"),
    }
    assert observed["verify_kwargs"]["required_clients"] == candidate.DEFAULT_REBUILD_CLIENTS
    assert observed["live"] == Path("/private/live-store/evidence-v2")
    assert observed["candidate"] == candidate_receipt.parent / "evidence-v2"
    assert observed["expected_live_tree_sha256"] == "a" * 64
    assert observed["expected_live_total_bytes"] == 100
    assert observed["expected_candidate_tree_sha256"] == "c" * 64
    assert observed["expected_candidate_total_bytes"] == 200
    assert observed["candidate_receipt_sha256"] == "b" * 64
    assert observed["snapshot_manifest_sha256"] == "d" * 64
    assert observed["require_receipt_bindings"] is True
    assert observed["confirm_writers_stopped"] is True
    assert observed["activation_intent_path"] == Path(
        "/private/receipts/activation.json.intent.json"
    )
    assert observed["activation_kwargs"] == {
        "final_receipt_path": Path("/private/receipts/activation.json"),
        "confirm_writers_stopped": True,
    }


def test_crash_inspection_cli_is_read_only_and_fail_visible(monkeypatch) -> None:
    monkeypatch.setattr(
        activation,
        "inspect_activation_state",
        lambda path: "swapped_pending_archive",
    )
    activation_result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "inspect-activation",
            "--activation-intent",
            "/private/receipts/activation.intent.json",
            "--json",
        ],
    )
    assert activation_result.exit_code == 0, activation_result.stdout
    assert json.loads(activation_result.stdout) == {
        "mutation_required": True,
        "operation": "activation",
        "recoverable": True,
        "state": "swapped_pending_archive",
        "status": "inspected",
    }

    monkeypatch.setattr(
        activation,
        "inspect_rollback_state",
        lambda path: "ambiguous",
    )
    rollback_result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "inspect-rollback",
            "--rollback-intent",
            "/private/receipts/rollback.intent.json",
            "--json",
        ],
    )
    assert rollback_result.exit_code == 0, rollback_result.stdout
    assert json.loads(rollback_result.stdout) == {
        "mutation_required": False,
        "operation": "rollback",
        "recoverable": False,
        "state": "ambiguous",
        "status": "inspected",
    }


def test_crash_recovery_cli_forwards_only_durable_paths_after_both_gates(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}
    activation_result_value = SimpleNamespace(
        installed_live=SimpleNamespace(path=Path("/private/live/evidence-v2")),
        archived_previous_live=SimpleNamespace(path=Path("/private/archive/old")),
        to_dict=lambda: {"operation": "activation"},
    )

    def fake_recover_activation(intent_path, **kwargs):
        observed["activation"] = (intent_path, kwargs)
        return activation_result_value

    monkeypatch.setattr(
        activation,
        "recover_activation_from_intent",
        fake_recover_activation,
    )
    activation_result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "recover-activation",
            "--activation-intent",
            "/private/receipts/activation.intent.json",
            "--receipt",
            "/private/receipts/activation.recovered.json",
            "--confirm-writers-stopped",
            "--confirm-owner-approved-activation",
            "--json",
        ],
    )
    assert activation_result.exit_code == 0, activation_result.stdout
    assert observed["activation"] == (
        Path("/private/receipts/activation.intent.json"),
        {
            "final_receipt_path": Path("/private/receipts/activation.recovered.json"),
            "confirm_writers_stopped": True,
        },
    )

    rollback_result_value = SimpleNamespace(
        restored_live=SimpleNamespace(path=Path("/private/live/evidence-v2")),
        failed_live_archive=SimpleNamespace(path=Path("/private/archive/failed")),
        to_dict=lambda: {"operation": "rollback"},
    )

    def fake_recover_rollback(intent_path, **kwargs):
        observed["rollback"] = (intent_path, kwargs)
        return rollback_result_value

    monkeypatch.setattr(
        activation,
        "recover_rollback_from_intent",
        fake_recover_rollback,
    )
    rollback_result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "recover-rollback",
            "--rollback-intent",
            "/private/receipts/rollback.intent.json",
            "--receipt",
            "/private/receipts/rollback.recovered.json",
            "--confirm-writers-stopped",
            "--confirm-owner-approved-rollback",
            "--json",
        ],
    )
    assert rollback_result.exit_code == 0, rollback_result.stdout
    assert observed["rollback"] == (
        Path("/private/receipts/rollback.intent.json"),
        {
            "final_receipt_path": Path("/private/receipts/rollback.recovered.json"),
            "confirm_writers_stopped": True,
        },
    )


# ---------------------------------------------------------------------------
# GROUP 1 — real execution coverage for the receipt security gate. The gate is
# never monkeypatched here: every case exercises _evidence_rebuild_receipt_json
# against real files under tmp_path.  tmp_path is resolved first because on
# macOS pytest's basetemp lives behind the /var -> /private/var symlink and the
# gate rejects any symlinked path component.
# ---------------------------------------------------------------------------


def test_receipt_gate_accepts_an_owner_only_receipt_and_returns_the_parsed_object(
    tmp_path: Path,
) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    payload = {"schema_version": "test.v1", "nested": {"ok": True}, "count": 1}
    path = _owner_receipt(parent, "snapshot-receipt.json", json.dumps(payload).encode())

    resolved, parsed = cli._evidence_rebuild_receipt_json(
        path,
        expected_name="snapshot-receipt.json",
    )

    assert resolved == path
    assert parsed == payload


def test_receipt_gate_refuses_a_symlinked_receipt_file(tmp_path: Path) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    real = _owner_receipt(parent, "real.json", b"{}")
    link = parent / "link.json"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="symlink components"):
        cli._evidence_rebuild_receipt_json(link)


def test_receipt_gate_refuses_a_symlinked_parent_directory(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    parent = _owner_dir(base, "receipts")
    _owner_receipt(parent, "receipt.json", b"{}")
    linked_parent = base / "linked-receipts"
    linked_parent.symlink_to(parent)

    with pytest.raises(ValueError, match="symlink components"):
        cli._evidence_rebuild_receipt_json(linked_parent / "receipt.json")


def test_receipt_gate_refuses_a_receipt_whose_mode_is_not_exactly_0600(
    tmp_path: Path,
) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = _owner_receipt(parent, "receipt.json", b"{}")
    path.chmod(0o644)

    with pytest.raises(ValueError, match="owner-only unique regular file"):
        cli._evidence_rebuild_receipt_json(path)


def test_receipt_gate_refuses_a_group_or_other_accessible_parent(
    tmp_path: Path,
) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = _owner_receipt(parent, "receipt.json", b"{}")
    parent.chmod(0o755)
    try:
        with pytest.raises(ValueError, match="parent must be owner-only"):
            cli._evidence_rebuild_receipt_json(path)
    finally:
        parent.chmod(0o700)


def test_receipt_gate_refuses_a_hard_linked_receipt(tmp_path: Path) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = _owner_receipt(parent, "receipt.json", b"{}")
    os.link(path, parent / "alias.json")

    with pytest.raises(ValueError, match="owner-only unique regular file"):
        cli._evidence_rebuild_receipt_json(path)


def test_receipt_gate_refuses_duplicate_json_keys(tmp_path: Path) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = _owner_receipt(parent, "receipt.json", b'{"key": 1, "key": 2}')

    with pytest.raises(ValueError, match="duplicate JSON keys"):
        cli._evidence_rebuild_receipt_json(path)


def test_receipt_gate_refuses_non_object_json(tmp_path: Path) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = _owner_receipt(parent, "receipt.json", b"[]")

    with pytest.raises(ValueError, match="must be a JSON object"):
        cli._evidence_rebuild_receipt_json(path)


def test_receipt_gate_refuses_relative_paths(tmp_path: Path) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    _owner_receipt(parent, "receipt.json", b"{}")

    with pytest.raises(ValueError, match="must be absolute"):
        cli._evidence_rebuild_receipt_json(Path("receipts/receipt.json"))


def test_receipt_gate_refuses_the_wrong_filename_when_expected_name_is_enforced(
    tmp_path: Path,
) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = _owner_receipt(parent, "other.json", b"{}")

    with pytest.raises(ValueError, match="must be named snapshot-receipt.json"):
        cli._evidence_rebuild_receipt_json(path, expected_name="snapshot-receipt.json")


def test_receipt_gate_refuses_a_receipt_larger_than_the_size_cap(
    tmp_path: Path,
) -> None:
    parent = _owner_dir(tmp_path.resolve(), "receipts")
    path = parent / "receipt.json"
    path.touch()
    # Sparse one-shot: the size check runs on st_size before any byte is read,
    # so a truncate-extended file exercises the cap without 16 MiB of I/O.
    os.truncate(path, cli._EVIDENCE_REBUILD_RECEIPT_MAX_BYTES + 1)
    path.chmod(0o600)

    with pytest.raises(ValueError, match="unexpectedly large"):
        cli._evidence_rebuild_receipt_json(path)


# ---------------------------------------------------------------------------
# GROUP 2 — end-to-end snapshot / verify-snapshot CLI runs against a real
# store fixture with NO monkeypatching anywhere in the pipeline.
# ---------------------------------------------------------------------------


def test_snapshot_and_verify_snapshot_cli_end_to_end_without_monkeypatching(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve()
    source = _build_source(base / "source")
    output_parent = _owner_dir(base, "snapshots")
    restore_parent = _owner_dir(base, "restores")

    snapshot_result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "snapshot",
            "--store-dir",
            str(source),
            "--output-parent",
            str(output_parent),
            "--deployed-commit",
            COMMIT,
            "--confirm-writers-stopped",
            "--json",
        ],
    )

    assert snapshot_result.exit_code == 0, snapshot_result.stdout
    payload = json.loads(snapshot_result.stdout)
    assert payload["status"] == "verified_snapshot"
    snapshot_root = Path(payload["snapshot_root"])
    assert snapshot_root.parent == output_parent
    receipt_path = Path(payload["receipt_path"])
    assert receipt_path.name == snapshot.RECEIPT_FILENAME
    assert receipt_path.parent == snapshot_root
    assert receipt_path.is_file()
    verification = payload["verification"]
    assert verification["canonical_store_uuid"] == STORE_UUID
    assert verification["main_spool_records"] == 1
    assert verification["refreshable_spool_records"] == 1
    assert verification["file_count"] > 0
    assert verification["total_bytes"] > 0

    verify_result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "verify-snapshot",
            "--snapshot-receipt",
            str(receipt_path),
            "--restore-parent",
            str(restore_parent),
            "--json",
        ],
    )

    assert verify_result.exit_code == 0, verify_result.stdout
    verified = json.loads(verify_result.stdout)
    assert verified["status"] == "restore_rehearsal_verified"
    snapshot_verification = verified["snapshot_verification"]
    assert snapshot_verification["manifest_sha256"] == verification["manifest_sha256"]
    assert snapshot_verification["tree_digest"] == verification["tree_digest"]
    assert snapshot_verification["canonical_store_uuid"] == STORE_UUID
    rehearsal = verified["restore_rehearsal"]
    assert rehearsal["source_manifest_sha256"] == verification["manifest_sha256"]
    restore_root = Path(rehearsal["restore_root"])
    assert restore_root.parent == restore_parent
    assert Path(rehearsal["receipt_path"]).is_file()


def test_verify_snapshot_cli_refuses_a_tampered_snapshot_receipt(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve()
    source = _build_source(base / "source")
    snapshots = _owner_dir(base, "snapshots")
    restore_parent = _owner_dir(base, "restores")
    created = snapshot.create_evidence_snapshot(
        source,
        snapshots / "snap",
        confirm_writers_stopped=True,
        deployed_commit=COMMIT,
    )

    receipt_path = created.receipt_path
    raw = receipt_path.read_bytes()
    digest = json.loads(raw)["tree_digest"].encode()
    flipped = (b"1" if digest[:1] == b"0" else b"0") + digest[1:]
    tampered = raw.replace(digest, flipped)
    assert tampered != raw
    receipt_path.write_bytes(tampered)
    receipt_path.chmod(0o600)

    result = runner.invoke(
        cli.app,
        [
            "evidence",
            "rebuild",
            "verify-snapshot",
            "--snapshot-receipt",
            str(receipt_path),
            "--restore-parent",
            str(restore_parent),
            "--json",
        ],
    )

    assert result.exit_code == 2
    refusal = json.loads(result.stdout)
    assert refusal["status"] == "refused"
    assert refusal["action"] == "snapshot verification"
    assert refusal["error_type"]
    assert not any(restore_parent.iterdir())
