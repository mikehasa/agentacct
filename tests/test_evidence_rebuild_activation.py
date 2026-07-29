from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import agentacct.evidence_rebuild_activation as activation
from agentacct.evidence_rebuild_activation import (
    EvidenceActivationDrift,
    EvidenceActivationError,
    EvidenceActivationLockBusy,
    EvidenceActivationPostSwapError,
    EvidenceActivationReceiptError,
    EvidenceRollbackPostSwapError,
    WritersStoppedConfirmationRequired,
    activate_evidence_rebuild,
    fingerprint_evidence_tree,
    inspect_activation_state,
    inspect_rollback_state,
    load_activation_intent,
    load_activation_receipt,
    load_rollback_intent,
    load_rollback_receipt,
    prepare_activation_intent,
    recover_activation_from_intent,
    recover_rollback_from_intent,
    rollback_evidence_activation,
    verify_evidence_activation,
)


@dataclass(frozen=True)
class Layout:
    root: Path
    live: Path
    candidate: Path
    receipts: Path


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


def _private_file(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def _tree(path: Path, marker: str) -> Path:
    _private_dir(path)
    _private_file(path / ".spool.lock", b"")
    _private_file(path / "spool.jsonl", f'{marker}:spool\n'.encode())
    _private_file(path / "refreshable-usage.jsonl", f'{marker}:refresh\n'.encode())
    _private_file(path / "projection.sqlite3", f'{marker}:projection\n'.encode())
    nested = _private_dir(path / "nested")
    _private_file(nested / "receipt.txt", marker.encode())
    return path


def _layout(root: Path) -> Layout:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    _private_file(root / "events.jsonl", b'{"event_type":"fixture"}\n')
    _private_file(root / "events.jsonl.lock", b"")
    health = _private_dir(root / "ingestion-health")
    _private_file(health / ".state.lock", b"")
    runtime = _private_dir(root / "runtime")
    _private_file(runtime / ".runtime.lock", b"")
    return Layout(
        root=root,
        live=_tree(root / "evidence-v2", "old"),
        candidate=_tree(root / "evidence-v2-candidate", "new"),
        receipts=_private_dir(root / "receipts"),
    )


def _injected_swap(left: Path, right: Path) -> None:
    """Test-only exchange; production code never uses this rename sequence."""

    temporary = left.parent / f".test-swap-{uuid.uuid4().hex}"
    os.rename(left, temporary)
    os.rename(right, left)
    os.rename(temporary, right)


def _injected_move(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    os.rename(source, destination)


def _prepare(layout: Layout):
    return prepare_activation_intent(
        layout.live,
        layout.candidate,
        intent_path=layout.receipts / "activation-intent.json",
        confirm_writers_stopped=True,
    )


def _activate(layout: Layout, intent):
    return activate_evidence_rebuild(
        layout.receipts / "activation-intent.json",
        final_receipt_path=layout.receipts / "activation.json",
        confirm_writers_stopped=True,
        swap_fn=_injected_swap,
        move_fn=_injected_move,
    )


def _receipt_bindings(layout: Layout) -> dict[str, object]:
    live = fingerprint_evidence_tree(layout.live)
    candidate = fingerprint_evidence_tree(layout.candidate)
    return {
        "expected_live_tree_sha256": live.tree_sha256,
        "expected_live_total_bytes": live.total_bytes,
        "expected_candidate_tree_sha256": candidate.tree_sha256,
        "expected_candidate_total_bytes": candidate.total_bytes,
        "candidate_receipt_sha256": "c" * 64,
        "snapshot_manifest_sha256": "d" * 64,
        "require_receipt_bindings": True,
    }


def _rewrite_intent_in_place(path: Path, *, field: str) -> None:
    """Change canonical JSON bytes while retaining the same receipt inode."""

    inode = path.stat().st_ino
    payload = json.loads(path.read_text(encoding="utf-8"))
    current = payload[field]
    assert isinstance(current, str) and current
    replacement = ("0" if current[0] != "0" else "1") + current[1:]
    payload[field] = replacement
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()
    with path.open("r+b") as handle:
        handle.seek(0)
        handle.write(encoded)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    assert path.stat().st_ino == inode


def test_prepare_intent_requires_confirmation_is_private_and_does_not_mutate_sources(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    live_before = fingerprint_evidence_tree(layout.live)
    candidate_before = fingerprint_evidence_tree(layout.candidate)
    intent_path = layout.receipts / "activation-intent.json"

    with pytest.raises(WritersStoppedConfirmationRequired):
        prepare_activation_intent(
            layout.live,
            layout.candidate,
            intent_path=intent_path,
            confirm_writers_stopped=False,
        )
    assert not intent_path.exists()

    intent = prepare_activation_intent(
        layout.live,
        layout.candidate,
        intent_path=intent_path,
        confirm_writers_stopped=True,
    )

    assert stat.S_IMODE(intent_path.stat().st_mode) == 0o600
    assert load_activation_intent(intent_path) == intent
    assert fingerprint_evidence_tree(layout.live) == live_before
    assert fingerprint_evidence_tree(layout.candidate) == candidate_before
    assert inspect_activation_state(intent_path) == "pre_swap"
    json.dumps(intent.to_dict(), sort_keys=True)
    assert list(layout.receipts.glob(".*.staged-*")) == []


def test_fingerprint_rejects_directory_symlinks_omitted_by_os_walk(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    outside = _private_dir(tmp_path / "outside")
    _private_file(outside / "not-in-evidence.txt", b"outside")
    (layout.candidate / "linked-directory").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(EvidenceActivationError, match="symlink"):
        fingerprint_evidence_tree(layout.candidate)


def test_activation_requires_the_persisted_intent_path_and_refuses_if_deleted(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)

    with pytest.raises(EvidenceActivationReceiptError, match="durable intent"):
        activate_evidence_rebuild(
            intent,  # type: ignore[arg-type]
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )

    intent_path.unlink()
    with pytest.raises(EvidenceActivationError, match="does not exist"):
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )
    assert layout.live.is_dir() and layout.candidate.is_dir()
    assert not intent.archive_path.exists()


def test_activation_reloads_durable_intent_inside_writer_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)
    original = activation.load_activation_intent
    reads = 0
    swap_called = False

    def delete_after_first_read(path: Path | str):
        nonlocal reads
        loaded = original(path)
        reads += 1
        if reads == 1:
            intent_path.unlink()
        return loaded

    def unexpected_swap(_left: Path, _right: Path) -> None:
        nonlocal swap_called
        swap_called = True

    monkeypatch.setattr(activation, "load_activation_intent", delete_after_first_read)
    with pytest.raises(EvidenceActivationError, match="does not exist"):
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=unexpected_swap,
            move_fn=_injected_move,
        )

    assert reads == 1
    assert swap_called is False
    assert layout.live.is_dir() and layout.candidate.is_dir()
    assert not intent.archive_path.exists()


def test_prepare_intent_binds_verified_snapshot_and_candidate_receipts(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    bindings = _receipt_bindings(layout)

    intent = prepare_activation_intent(
        layout.live,
        layout.candidate,
        intent_path=layout.receipts / "activation-intent.json",
        confirm_writers_stopped=True,
        **bindings,
    )

    assert intent.expected_live_tree_sha256 == bindings["expected_live_tree_sha256"]
    assert intent.expected_live_total_bytes == bindings["expected_live_total_bytes"]
    assert (
        intent.expected_candidate_tree_sha256
        == bindings["expected_candidate_tree_sha256"]
    )
    assert (
        intent.expected_candidate_total_bytes
        == bindings["expected_candidate_total_bytes"]
    )
    assert intent.candidate_receipt_sha256 == "c" * 64
    assert intent.snapshot_manifest_sha256 == "d" * 64
    assert load_activation_intent(layout.receipts / "activation-intent.json") == intent
    receipt = _activate(layout, intent)
    assert receipt.intent == intent


def test_prepare_intent_can_require_complete_upstream_receipt_bindings(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    with pytest.raises(EvidenceActivationError, match="bindings are missing"):
        prepare_activation_intent(
            layout.live,
            layout.candidate,
            intent_path=layout.receipts / "activation-intent.json",
            confirm_writers_stopped=True,
            require_receipt_bindings=True,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("expected_live_tree_sha256", "0" * 64),
        ("expected_live_total_bytes", 999_999),
        ("expected_candidate_tree_sha256", "1" * 64),
        ("expected_candidate_total_bytes", 999_999),
    ],
)
def test_prepare_intent_refuses_upstream_tree_binding_mismatch_before_sealing(
    tmp_path: Path,
    field: str,
    replacement: str | int,
) -> None:
    layout = _layout(tmp_path)
    bindings = _receipt_bindings(layout)
    bindings[field] = replacement

    with pytest.raises(EvidenceActivationDrift, match="bound"):
        prepare_activation_intent(
            layout.live,
            layout.candidate,
            intent_path=layout.receipts / "activation-intent.json",
            confirm_writers_stopped=True,
            **bindings,
        )

    assert not (layout.receipts / "activation-intent.json").exists()


@pytest.mark.parametrize(
    "relative_lock",
    [
        Path("events.jsonl.lock"),
        Path("ingestion-health/.state.lock"),
        Path("runtime/.runtime.lock"),
    ],
)
def test_prepare_intent_refuses_any_active_store_root_writer_lock(
    tmp_path: Path,
    relative_lock: Path,
) -> None:
    layout = _layout(tmp_path)
    descriptor = os.open(layout.root / relative_lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy, match="active"):
            _prepare(layout)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not (layout.receipts / "activation-intent.json").exists()


def test_prepare_intent_fails_closed_when_store_root_writer_lock_is_missing(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    (layout.root / "runtime/.runtime.lock").unlink()

    with pytest.raises(EvidenceActivationError, match="required store writer lock is missing"):
        _prepare(layout)

    assert not (layout.receipts / "activation-intent.json").exists()


def test_prepare_intent_refuses_an_active_writer_lock(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    descriptor = os.open(layout.live / ".spool.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy, match="active"):
            _prepare(layout)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not (layout.receipts / "activation-intent.json").exists()


@pytest.mark.parametrize("changed", ["live", "candidate"])
def test_activation_refuses_tree_or_receipt_drift_before_swap(
    tmp_path: Path,
    changed: str,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)
    target = layout.live if changed == "live" else layout.candidate
    with (target / "spool.jsonl").open("ab") as handle:
        handle.write(b"drift\n")
    swap_called = False

    def unexpected_swap(_left: Path, _right: Path) -> None:
        nonlocal swap_called
        swap_called = True

    with pytest.raises(EvidenceActivationDrift, match="drifted"):
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=unexpected_swap,
            move_fn=_injected_move,
        )

    assert swap_called is False
    assert layout.live.is_dir()
    assert layout.candidate.is_dir()
    assert not intent.archive_path.exists()
    assert inspect_activation_state(intent) == "ambiguous"


def test_cross_device_activation_fails_closed_before_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    real_device = activation._path_device

    def split_device(path: Path) -> int:
        observed = real_device(path)
        return observed + 1 if path == layout.candidate else observed

    monkeypatch.setattr(activation, "_path_device", split_device)

    with pytest.raises(EvidenceActivationError, match="one device"):
        _prepare(layout)
    assert layout.live.is_dir() and layout.candidate.is_dir()


def test_activation_and_rollback_support_separate_private_archive_parent(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    archive_parent = _private_dir(
        tmp_path.parent / f"{tmp_path.name}-owner-private-archives"
    )
    archive_path = archive_parent / "previous-live"
    intent = prepare_activation_intent(
        layout.live,
        layout.candidate,
        intent_path=layout.receipts / "activation-intent.json",
        confirm_writers_stopped=True,
        archive_path=archive_path,
        **_receipt_bindings(layout),
    )

    activation_receipt = _activate(layout, intent)
    assert activation_receipt.archived_previous_live.path == archive_path
    assert archive_path.parent == archive_parent
    assert load_activation_receipt(layout.receipts / "activation.json") == activation_receipt

    failed_path = archive_parent / "failed-new-generation"
    rollback_receipt = rollback_evidence_activation(
        activation_receipt,
        rollback_intent_path=layout.receipts / "rollback-intent.json",
        final_receipt_path=layout.receipts / "rollback.json",
        confirm_writers_stopped=True,
        failed_archive_path=failed_path,
        swap_fn=_injected_swap,
        move_fn=_injected_move,
    )
    assert rollback_receipt.failed_live_archive.path == failed_path
    assert failed_path.is_dir()
    assert layout.live.is_dir()


def test_archive_parent_must_be_owner_private_mode_0700(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    archive_parent = _private_dir(tmp_path.parent / f"{tmp_path.name}-archives")
    archive_parent.chmod(0o755)

    with pytest.raises(EvidenceActivationError, match="mode 0700"):
        prepare_activation_intent(
            layout.live,
            layout.candidate,
            intent_path=layout.receipts / "activation-intent.json",
            confirm_writers_stopped=True,
            archive_path=archive_parent / "previous-live",
        )


def test_cross_device_archive_parent_fails_closed_before_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    archive_parent = _private_dir(
        tmp_path.parent / f"{tmp_path.name}-cross-device-archives"
    )
    real_device = activation._path_device

    def split_device(path: Path) -> int:
        observed = real_device(path)
        return observed + 1 if path == archive_parent else observed

    monkeypatch.setattr(activation, "_path_device", split_device)

    with pytest.raises(EvidenceActivationError, match="one device"):
        prepare_activation_intent(
            layout.live,
            layout.candidate,
            intent_path=layout.receipts / "activation-intent.json",
            confirm_writers_stopped=True,
            archive_path=archive_parent / "previous-live",
        )
    assert not (layout.receipts / "activation-intent.json").exists()


def test_receipts_and_archives_never_overwrite_existing_names(tmp_path: Path) -> None:
    first = _layout(tmp_path / "first")
    existing_intent = _private_file(first.receipts / "activation-intent.json", b"keep")
    with pytest.raises(EvidenceActivationReceiptError, match="already exists"):
        _prepare(first)
    assert existing_intent.read_bytes() == b"keep"

    second = _layout(tmp_path / "second")
    intent = _prepare(second)
    _private_dir(intent.archive_path)
    _private_file(intent.archive_path / "keep", b"archive occupant")
    with pytest.raises(EvidenceActivationError, match="archive path already exists"):
        _activate(second, intent)
    assert (intent.archive_path / "keep").read_bytes() == b"archive occupant"
    assert second.live.is_dir() and second.candidate.is_dir()

    third = _layout(tmp_path / "third")
    third_intent = _prepare(third)
    final_path = _private_file(third.receipts / "activation.json", b"keep final")
    with pytest.raises(EvidenceActivationError, match="receipt already exists"):
        _activate(third, third_intent)
    assert final_path.read_bytes() == b"keep final"
    assert inspect_activation_state(third_intent) == "pre_swap"


def test_activation_swaps_whole_tree_archives_old_live_and_verifies_exact_receipts(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)

    receipt = _activate(layout, intent)

    final_path = layout.receipts / "activation.json"
    assert stat.S_IMODE(final_path.stat().st_mode) == 0o600
    assert load_activation_receipt(final_path) == receipt
    assert inspect_activation_state(intent) == "installed"
    assert not layout.candidate.exists()
    assert (layout.live / "spool.jsonl").read_bytes() == b"new:spool\n"
    assert (intent.archive_path / "spool.jsonl").read_bytes() == b"old:spool\n"
    verification = verify_evidence_activation(
        final_path,
        confirm_writers_stopped=True,
    )
    assert verification.ok is True
    assert verification.state == "installed"
    assert verification.errors == ()
    json.dumps(receipt.to_dict(), sort_keys=True)

    with (layout.live / "spool.jsonl").open("ab") as handle:
        handle.write(b"post-activation-write\n")
    drifted = verify_evidence_activation(receipt, confirm_writers_stopped=True)
    assert drifted.ok is False
    assert "drifted" in drifted.errors[0]


def test_activation_requires_confirmation_and_rechecks_active_lock(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)
    with pytest.raises(WritersStoppedConfirmationRequired):
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=False,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )

    descriptor = os.open(layout.candidate / ".spool.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy):
            _activate(layout, intent)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert inspect_activation_state(intent) == "pre_swap"


def test_activation_rechecks_store_root_writer_lock_inside_critical_section(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)
    descriptor = os.open(layout.root / "events.jsonl.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy, match="events.jsonl.lock"):
            _activate(layout, intent)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert inspect_activation_state(intent) == "pre_swap"


def test_activation_holds_store_root_writer_lock_through_swap_and_archive_move(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)
    checks: list[str] = []

    def assert_global_lock_held(phase: str) -> None:
        descriptor = os.open(layout.root / "events.jsonl.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        checks.append(phase)

    def checked_swap(left: Path, right: Path) -> None:
        assert_global_lock_held("swap")
        _injected_swap(left, right)

    def checked_move(source: Path, destination: Path) -> None:
        assert_global_lock_held("archive_move")
        _injected_move(source, destination)

    activate_evidence_rebuild(
        layout.receipts / "activation-intent.json",
        final_receipt_path=layout.receipts / "activation.json",
        confirm_writers_stopped=True,
        swap_fn=checked_swap,
        move_fn=checked_move,
    )

    assert checks == ["swap", "archive_move"]


def test_before_swap_fault_leaves_source_and_candidate_byte_exact(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)

    def fail(phase: str) -> None:
        if phase == "before_swap":
            raise RuntimeError("stop before exchange")

    with pytest.raises(EvidenceActivationError, match="before exchange"):
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=fail,
        )

    assert fingerprint_evidence_tree(layout.live) == intent.live
    assert fingerprint_evidence_tree(layout.candidate) == intent.candidate
    assert inspect_activation_state(intent) == "pre_swap"


def test_activation_refuses_if_durable_intent_is_unlinked_at_before_swap(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)
    swap_called = False

    def unlink_intent(phase: str) -> None:
        if phase == "before_swap":
            intent_path.unlink()

    def unexpected_swap(_left: Path, _right: Path) -> None:
        nonlocal swap_called
        swap_called = True

    with pytest.raises(EvidenceActivationError, match="before exchange"):
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=unexpected_swap,
            move_fn=_injected_move,
            fault_injector=unlink_intent,
        )

    assert swap_called is False
    assert fingerprint_evidence_tree(layout.live) == intent.live
    assert fingerprint_evidence_tree(layout.candidate) == intent.candidate
    assert not intent.archive_path.exists()


def test_activation_refuses_same_inode_intent_rewrite_immediately_before_swap(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)
    swap_called = False

    def rewrite_intent(phase: str) -> None:
        if phase == "before_swap":
            _rewrite_intent_in_place(intent_path, field="operation_id")

    def unexpected_swap(_left: Path, _right: Path) -> None:
        nonlocal swap_called
        swap_called = True

    with pytest.raises(EvidenceActivationError, match="intent receipt"):
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=unexpected_swap,
            move_fn=_injected_move,
            fault_injector=rewrite_intent,
        )

    assert swap_called is False
    assert fingerprint_evidence_tree(layout.live) == intent.live
    assert fingerprint_evidence_tree(layout.candidate) == intent.candidate
    assert not intent.archive_path.exists()


@pytest.mark.parametrize("mutation", ["unlink", "replace"])
def test_activation_rechecks_intent_path_after_loader_returns_at_swap_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)
    original_loader = activation.load_activation_intent
    reads = 0
    swap_called = False

    def mutate_after_loader(path: Path | str):
        nonlocal reads
        reads += 1
        loaded = original_loader(path)
        if reads == 4:
            if mutation == "unlink":
                intent_path.unlink()
            else:
                replacement = layout.receipts / "replacement-intent.json"
                replacement.write_bytes(intent_path.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, intent_path)
        return loaded

    def unexpected_swap(_left: Path, _right: Path) -> None:
        nonlocal swap_called
        swap_called = True

    monkeypatch.setattr(activation, "load_activation_intent", mutate_after_loader)
    with pytest.raises(EvidenceActivationError, match="mutation boundary"):
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=unexpected_swap,
            move_fn=_injected_move,
        )

    assert reads == 4
    assert swap_called is False
    assert fingerprint_evidence_tree(layout.live) == intent.live
    assert fingerprint_evidence_tree(layout.candidate) == intent.candidate
    assert not intent.archive_path.exists()


def test_swap_exception_after_exchange_is_classified_from_crash_intent(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)

    def exchange_then_fail(left: Path, right: Path) -> None:
        _injected_swap(left, right)
        raise RuntimeError("exchange returned an error after crossing")

    with pytest.raises(EvidenceActivationPostSwapError) as raised:
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=exchange_then_fail,
            move_fn=_injected_move,
        )

    assert raised.value.state == "swapped_pending_archive"
    assert inspect_activation_state(intent_path) == "swapped_pending_archive"
    assert (layout.live / "spool.jsonl").read_bytes() == b"new:spool\n"
    assert (layout.candidate / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert not intent.archive_path.exists()


def test_swap_exception_before_exchange_is_still_typed_as_may_have_crossed(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)

    def fail_without_exchange(_left: Path, _right: Path) -> None:
        raise RuntimeError("exchange outcome is not trustworthy")

    with pytest.raises(EvidenceActivationPostSwapError) as raised:
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=fail_without_exchange,
            move_fn=_injected_move,
        )

    assert raised.value.state == "pre_swap"
    assert fingerprint_evidence_tree(layout.live) == intent.live
    assert fingerprint_evidence_tree(layout.candidate) == intent.candidate


def test_activation_classification_failure_after_swap_is_typed_unknown_post_swap(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)

    def make_classification_fail(phase: str) -> None:
        if phase == "after_swap":
            layout.root.chmod(0o755)

    try:
        with pytest.raises(EvidenceActivationPostSwapError) as raised:
            activate_evidence_rebuild(
                layout.receipts / "activation-intent.json",
                final_receipt_path=layout.receipts / "activation.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
                fault_injector=make_classification_fail,
            )
    finally:
        layout.root.chmod(0o700)

    assert raised.value.state == "unknown_post_swap"
    assert isinstance(raised.value.classification_error, EvidenceActivationError)
    assert (layout.live / "spool.jsonl").read_bytes() == b"new:spool\n"
    assert (layout.candidate / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert not intent.archive_path.exists()


def test_final_receipt_fault_keeps_both_installed_trees_and_crash_state(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)

    def fail(phase: str) -> None:
        if phase == "before_final_receipt":
            raise RuntimeError("receipt unavailable")

    with pytest.raises(EvidenceActivationPostSwapError) as raised:
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=fail,
        )

    assert raised.value.state == "installed"
    assert inspect_activation_state(intent) == "installed"
    assert layout.live.is_dir()
    assert intent.archive_path.is_dir()
    assert not layout.candidate.exists()
    assert not (layout.receipts / "activation.json").exists()


def test_intent_and_post_swap_fsync_failures_are_fail_visible_and_non_destructive(
    tmp_path: Path,
) -> None:
    first = _layout(tmp_path / "intent")

    def always_fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    with pytest.raises(OSError, match="fsync failed"):
        prepare_activation_intent(
            first.live,
            first.candidate,
            intent_path=first.receipts / "activation-intent.json",
            confirm_writers_stopped=True,
            fsync_fn=always_fail_fsync,
        )
    assert first.live.is_dir() and first.candidate.is_dir()
    assert not (first.receipts / "activation-intent.json").exists()
    assert list(first.receipts.glob(".*.staged-*")) == []

    second = _layout(tmp_path / "final")
    intent = _prepare(second)
    calls = 0

    def fail_final_receipt_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        # 1-2 durable intent file/parent, 3 before swap, 4 after swap,
        # 5 after archive move, 6 final receipt stage.
        if calls == 6:
            raise OSError("final receipt fsync failed")
        os.fsync(descriptor)

    with pytest.raises(EvidenceActivationPostSwapError) as raised:
        activate_evidence_rebuild(
            second.receipts / "activation-intent.json",
            final_receipt_path=second.receipts / "activation.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fsync_fn=fail_final_receipt_fsync,
        )
    assert raised.value.state == "installed"
    assert inspect_activation_state(intent) == "installed"
    assert not (second.receipts / "activation.json").exists()
    assert list(second.receipts.glob(".*.staged-*")) == []


def test_non_macos_without_injected_swap_fails_closed_without_two_rename_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)
    monkeypatch.setattr(activation.sys, "platform", "linux")

    with pytest.raises(EvidenceActivationError, match="no production fallback"):
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "activation.json",
            confirm_writers_stopped=True,
        )

    assert inspect_activation_state(intent) == "pre_swap"
    assert layout.live.is_dir() and layout.candidate.is_dir()


def test_rollback_atomically_restores_old_live_and_preserves_observation_writes(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    activation_receipt = _activate(layout, _prepare(layout))
    with (layout.live / "spool.jsonl").open("ab") as handle:
        handle.write(b"observation-window-write\n")
    _private_file(layout.live / "post-activation.bin", b"must survive rollback")

    receipt = rollback_evidence_activation(
        activation_receipt,
        rollback_intent_path=layout.receipts / "rollback-intent.json",
        final_receipt_path=layout.receipts / "rollback.json",
        confirm_writers_stopped=True,
        swap_fn=_injected_swap,
        move_fn=_injected_move,
    )

    assert stat.S_IMODE((layout.receipts / "rollback-intent.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((layout.receipts / "rollback.json").stat().st_mode) == 0o600
    assert load_rollback_intent(layout.receipts / "rollback-intent.json") == receipt.intent
    assert load_rollback_receipt(layout.receipts / "rollback.json") == receipt
    assert inspect_rollback_state(receipt.intent) == "rolled_back"
    assert (layout.live / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert not activation_receipt.archived_previous_live.path.exists()
    failed = receipt.failed_live_archive.path
    assert failed.is_dir()
    assert b"observation-window-write" in (failed / "spool.jsonl").read_bytes()
    assert (failed / "post-activation.bin").read_bytes() == b"must survive rollback"
    json.dumps(receipt.to_dict(), sort_keys=True)


def test_rollback_requires_confirmation_and_exclusive_locks(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    receipt = _activate(layout, _prepare(layout))
    with pytest.raises(WritersStoppedConfirmationRequired):
        rollback_evidence_activation(
            receipt,
            rollback_intent_path=layout.receipts / "rollback-intent.json",
            final_receipt_path=layout.receipts / "rollback.json",
            confirm_writers_stopped=False,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )

    descriptor = os.open(layout.live / ".spool.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy):
            rollback_evidence_activation(
                receipt,
                rollback_intent_path=layout.receipts / "rollback-intent.json",
                final_receipt_path=layout.receipts / "rollback.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert layout.live.is_dir()
    assert receipt.archived_previous_live.path.is_dir()

    global_descriptor = os.open(
        layout.root / "ingestion-health/.state.lock",
        os.O_RDWR,
    )
    try:
        fcntl.flock(global_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy, match=".state.lock"):
            rollback_evidence_activation(
                receipt,
                rollback_intent_path=layout.receipts / "rollback-intent.json",
                final_receipt_path=layout.receipts / "rollback.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
            )
    finally:
        fcntl.flock(global_descriptor, fcntl.LOCK_UN)
        os.close(global_descriptor)
    assert not (layout.receipts / "rollback-intent.json").exists()


def test_rollback_swap_and_receipt_faults_retain_both_generations(
    tmp_path: Path,
) -> None:
    first = _layout(tmp_path / "swap")
    first_receipt = _activate(first, _prepare(first))

    def exchange_then_fail(left: Path, right: Path) -> None:
        _injected_swap(left, right)
        raise RuntimeError("rollback exchange crossed")

    with pytest.raises(EvidenceRollbackPostSwapError) as raised:
        rollback_evidence_activation(
            first_receipt,
            rollback_intent_path=first.receipts / "rollback-intent.json",
            final_receipt_path=first.receipts / "rollback.json",
            confirm_writers_stopped=True,
            swap_fn=exchange_then_fail,
            move_fn=_injected_move,
        )
    assert raised.value.state == "swapped_pending_failed_archive"
    assert inspect_rollback_state(first.receipts / "rollback-intent.json") == (
        "swapped_pending_failed_archive"
    )
    assert first.live.is_dir()
    assert first_receipt.archived_previous_live.path.is_dir()

    second = _layout(tmp_path / "receipt")
    second_receipt = _activate(second, _prepare(second))

    def fail(phase: str) -> None:
        if phase == "before_rollback_receipt":
            raise RuntimeError("rollback receipt unavailable")

    with pytest.raises(EvidenceRollbackPostSwapError) as second_raised:
        rollback_evidence_activation(
            second_receipt,
            rollback_intent_path=second.receipts / "rollback-intent.json",
            final_receipt_path=second.receipts / "rollback.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=fail,
        )
    assert second_raised.value.state == "rolled_back"
    rollback_intent = load_rollback_intent(second.receipts / "rollback-intent.json")
    assert inspect_rollback_state(rollback_intent) == "rolled_back"
    assert second.live.is_dir()
    assert rollback_intent.failed_archive_path.is_dir()
    assert not (second.receipts / "rollback.json").exists()


def test_rollback_swap_exception_before_exchange_is_typed_as_may_have_crossed(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    activation_receipt = _activate(layout, _prepare(layout))

    def fail_without_exchange(_left: Path, _right: Path) -> None:
        raise RuntimeError("rollback exchange outcome is not trustworthy")

    with pytest.raises(EvidenceRollbackPostSwapError) as raised:
        rollback_evidence_activation(
            activation_receipt,
            rollback_intent_path=layout.receipts / "rollback-intent.json",
            final_receipt_path=layout.receipts / "rollback.json",
            confirm_writers_stopped=True,
            swap_fn=fail_without_exchange,
            move_fn=_injected_move,
        )

    assert raised.value.state == "pre_swap"
    assert fingerprint_evidence_tree(layout.live) == activation_receipt.installed_live
    assert (
        fingerprint_evidence_tree(activation_receipt.archived_previous_live.path)
        == activation_receipt.archived_previous_live
    )


def test_rollback_classification_failure_after_swap_is_typed_unknown_post_swap(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    activation_receipt = _activate(layout, _prepare(layout))

    def make_classification_fail(phase: str) -> None:
        if phase == "after_rollback_swap":
            layout.root.chmod(0o755)

    try:
        with pytest.raises(EvidenceRollbackPostSwapError) as raised:
            rollback_evidence_activation(
                activation_receipt,
                rollback_intent_path=layout.receipts / "rollback-intent.json",
                final_receipt_path=layout.receipts / "rollback.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
                fault_injector=make_classification_fail,
            )
    finally:
        layout.root.chmod(0o700)

    assert raised.value.state == "unknown_post_swap"
    assert isinstance(raised.value.classification_error, EvidenceActivationError)
    assert (layout.live / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert (
        activation_receipt.archived_previous_live.path / "spool.jsonl"
    ).read_bytes() == b"new:spool\n"
    assert not raised.value.intent.failed_archive_path.exists()


@pytest.mark.parametrize(
    ("crash_state", "expected_swaps", "expected_moves"),
    (
        ("pre_swap", 1, 1),
        ("swapped_pending_archive", 0, 1),
        ("installed", 0, 0),
    ),
)
def test_activation_crash_recovery_resumes_each_unambiguous_state(
    tmp_path: Path,
    crash_state: str,
    expected_swaps: int,
    expected_moves: int,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)
    if crash_state == "swapped_pending_archive":
        def exchange_then_fail(left: Path, right: Path) -> None:
            _injected_swap(left, right)
            raise RuntimeError("simulated process loss after swap")

        with pytest.raises(EvidenceActivationPostSwapError):
            activate_evidence_rebuild(
                intent_path,
                final_receipt_path=layout.receipts / "unwritten.json",
                confirm_writers_stopped=True,
                swap_fn=exchange_then_fail,
                move_fn=_injected_move,
            )
    elif crash_state == "installed":
        def fail_before_receipt(phase: str) -> None:
            if phase == "before_final_receipt":
                raise RuntimeError("simulated process loss before receipt")

        with pytest.raises(EvidenceActivationPostSwapError):
            activate_evidence_rebuild(
                intent_path,
                final_receipt_path=layout.receipts / "unwritten.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
                fault_injector=fail_before_receipt,
            )
    assert inspect_activation_state(intent_path) == crash_state

    swaps = 0
    moves = 0

    def counted_swap(left: Path, right: Path) -> None:
        nonlocal swaps
        swaps += 1
        _injected_swap(left, right)

    def counted_move(source: Path, destination: Path) -> None:
        nonlocal moves
        moves += 1
        _injected_move(source, destination)

    final_path = layout.receipts / "recovered-activation.json"
    receipt = recover_activation_from_intent(
        intent_path,
        final_receipt_path=final_path,
        confirm_writers_stopped=True,
        swap_fn=counted_swap,
        move_fn=counted_move,
    )

    assert swaps == expected_swaps
    assert moves == expected_moves
    assert inspect_activation_state(intent_path) == "installed"
    assert load_activation_receipt(final_path) == receipt
    assert intent_path.exists()
    assert (layout.live / "spool.jsonl").read_bytes() == b"new:spool\n"
    assert (intent.archive_path / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert not layout.candidate.exists()


def test_activation_recovery_classification_failure_after_swap_is_typed_unknown(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)

    def make_classification_fail(phase: str) -> None:
        if phase == "after_recovery_swap":
            layout.root.chmod(0o755)

    try:
        with pytest.raises(EvidenceActivationPostSwapError) as raised:
            recover_activation_from_intent(
                intent_path,
                final_receipt_path=layout.receipts / "recovered.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
                fault_injector=make_classification_fail,
            )
    finally:
        layout.root.chmod(0o700)

    assert raised.value.state == "unknown_post_swap"
    assert isinstance(raised.value.classification_error, EvidenceActivationError)
    assert (layout.live / "spool.jsonl").read_bytes() == b"new:spool\n"
    assert (layout.candidate / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert not intent.archive_path.exists()


def test_activation_crash_recovery_refuses_ambiguous_state_and_existing_receipt(
    tmp_path: Path,
) -> None:
    ambiguous = _layout(tmp_path / "ambiguous")
    ambiguous_intent = _prepare(ambiguous)
    _private_file(ambiguous.candidate / "unbound.bin", b"drift")
    assert inspect_activation_state(ambiguous_intent) == "ambiguous"
    with pytest.raises(EvidenceActivationDrift, match="ambiguous"):
        recover_activation_from_intent(
            ambiguous.receipts / "activation-intent.json",
            final_receipt_path=ambiguous.receipts / "recovered.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )
    assert ambiguous.live.is_dir() and ambiguous.candidate.is_dir()
    assert not ambiguous_intent.archive_path.exists()

    installed = _layout(tmp_path / "existing")
    installed_intent = _prepare(installed)

    def fail_before_receipt(phase: str) -> None:
        if phase == "before_final_receipt":
            raise RuntimeError("leave installed without final receipt")

    with pytest.raises(EvidenceActivationPostSwapError):
        activate_evidence_rebuild(
            installed.receipts / "activation-intent.json",
            final_receipt_path=installed.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=fail_before_receipt,
        )
    existing = _private_file(installed.receipts / "recovered.json", b"do not overwrite")
    with pytest.raises(EvidenceActivationError, match="already exists"):
        recover_activation_from_intent(
            installed.receipts / "activation-intent.json",
            final_receipt_path=existing,
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )
    assert existing.read_bytes() == b"do not overwrite"
    assert inspect_activation_state(installed_intent) == "installed"


def test_crash_recovery_requires_stopped_writer_confirmation_and_exclusive_locks(
    tmp_path: Path,
) -> None:
    activation_layout = _layout(tmp_path / "activation")
    _prepare(activation_layout)
    with pytest.raises(WritersStoppedConfirmationRequired):
        recover_activation_from_intent(
            activation_layout.receipts / "activation-intent.json",
            final_receipt_path=activation_layout.receipts / "recovered.json",
            confirm_writers_stopped=False,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )
    activation_lock = os.open(
        activation_layout.root / "events.jsonl.lock",
        os.O_RDWR,
    )
    try:
        fcntl.flock(activation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy):
            recover_activation_from_intent(
                activation_layout.receipts / "activation-intent.json",
                final_receipt_path=activation_layout.receipts / "recovered.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
            )
    finally:
        fcntl.flock(activation_lock, fcntl.LOCK_UN)
        os.close(activation_lock)

    rollback_layout = _layout(tmp_path / "rollback")
    activated = _activate(rollback_layout, _prepare(rollback_layout))
    rollback_intent_path = rollback_layout.receipts / "rollback-intent.json"

    def stop_before_swap(phase: str) -> None:
        if phase == "before_rollback_swap":
            raise RuntimeError("leave pre-swap rollback intent")

    with pytest.raises(EvidenceActivationError):
        rollback_evidence_activation(
            activated,
            rollback_intent_path=rollback_intent_path,
            final_receipt_path=rollback_layout.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=stop_before_swap,
        )
    with pytest.raises(WritersStoppedConfirmationRequired):
        recover_rollback_from_intent(
            rollback_intent_path,
            final_receipt_path=rollback_layout.receipts / "recovered.json",
            confirm_writers_stopped=False,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )
    rollback_lock = os.open(
        rollback_layout.root / "runtime/.runtime.lock",
        os.O_RDWR,
    )
    try:
        fcntl.flock(rollback_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(EvidenceActivationLockBusy):
            recover_rollback_from_intent(
                rollback_intent_path,
                final_receipt_path=rollback_layout.receipts / "recovered.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
            )
    finally:
        fcntl.flock(rollback_lock, fcntl.LOCK_UN)
        os.close(rollback_lock)


def test_activation_crash_recovery_rechecks_intent_before_pending_archive_move(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent_path = layout.receipts / "activation-intent.json"
    intent = _prepare(layout)

    def exchange_then_fail(left: Path, right: Path) -> None:
        _injected_swap(left, right)
        raise RuntimeError("leave pending archive")

    with pytest.raises(EvidenceActivationPostSwapError):
        activate_evidence_rebuild(
            intent_path,
            final_receipt_path=layout.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=exchange_then_fail,
            move_fn=_injected_move,
        )
    move_called = False

    def rewrite_before_move(phase: str) -> None:
        if phase == "before_recovery_archive_move":
            _rewrite_intent_in_place(intent_path, field="operation_id")

    def unexpected_move(_source: Path, _destination: Path) -> None:
        nonlocal move_called
        move_called = True

    with pytest.raises(EvidenceActivationPostSwapError) as raised:
        recover_activation_from_intent(
            intent_path,
            final_receipt_path=layout.receipts / "recovered.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=unexpected_move,
            fault_injector=rewrite_before_move,
        )
    assert raised.value.state == "swapped_pending_archive"
    assert move_called is False
    assert layout.live.is_dir() and layout.candidate.is_dir()
    assert not intent.archive_path.exists()


def test_activation_crash_recovery_receipt_fsync_failure_retains_installed_generations(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    intent = _prepare(layout)

    def fail_before_receipt(phase: str) -> None:
        if phase == "before_final_receipt":
            raise RuntimeError("leave installed")

    with pytest.raises(EvidenceActivationPostSwapError):
        activate_evidence_rebuild(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=fail_before_receipt,
        )
    calls = 0

    def fail_receipt_stage_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("recovery receipt fsync failed")
        os.fsync(descriptor)

    with pytest.raises(EvidenceActivationPostSwapError) as raised:
        recover_activation_from_intent(
            layout.receipts / "activation-intent.json",
            final_receipt_path=layout.receipts / "recovered.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fsync_fn=fail_receipt_stage_fsync,
        )
    assert raised.value.state == "installed"
    assert inspect_activation_state(intent) == "installed"
    assert not (layout.receipts / "recovered.json").exists()
    assert list(layout.receipts.glob(".*.staged-*")) == []


@pytest.mark.parametrize(
    ("crash_state", "expected_swaps", "expected_moves"),
    (
        ("pre_swap", 1, 1),
        ("swapped_pending_failed_archive", 0, 1),
        ("rolled_back", 0, 0),
    ),
)
def test_rollback_crash_recovery_resumes_each_unambiguous_state(
    tmp_path: Path,
    crash_state: str,
    expected_swaps: int,
    expected_moves: int,
) -> None:
    layout = _layout(tmp_path)
    activation_receipt = _activate(layout, _prepare(layout))
    intent_path = layout.receipts / "rollback-intent.json"
    original_final = layout.receipts / "unwritten-rollback.json"

    def fault(phase: str) -> None:
        if crash_state == "pre_swap" and phase == "before_rollback_swap":
            raise RuntimeError("leave rollback pre-swap")
        if crash_state == "rolled_back" and phase == "before_rollback_receipt":
            raise RuntimeError("leave rollback without receipt")

    def maybe_cross_then_fail(left: Path, right: Path) -> None:
        _injected_swap(left, right)
        if crash_state == "swapped_pending_failed_archive":
            raise RuntimeError("leave rollback pending archive")

    expected_error = (
        EvidenceActivationError
        if crash_state == "pre_swap"
        else EvidenceRollbackPostSwapError
    )
    with pytest.raises(expected_error):
        rollback_evidence_activation(
            activation_receipt,
            rollback_intent_path=intent_path,
            final_receipt_path=original_final,
            confirm_writers_stopped=True,
            swap_fn=maybe_cross_then_fail,
            move_fn=_injected_move,
            fault_injector=fault,
        )
    assert inspect_rollback_state(intent_path) == crash_state

    swaps = 0
    moves = 0

    def counted_swap(left: Path, right: Path) -> None:
        nonlocal swaps
        swaps += 1
        _injected_swap(left, right)

    def counted_move(source: Path, destination: Path) -> None:
        nonlocal moves
        moves += 1
        _injected_move(source, destination)

    final_path = layout.receipts / "recovered-rollback.json"
    receipt = recover_rollback_from_intent(
        intent_path,
        final_receipt_path=final_path,
        confirm_writers_stopped=True,
        swap_fn=counted_swap,
        move_fn=counted_move,
    )

    assert swaps == expected_swaps
    assert moves == expected_moves
    assert inspect_rollback_state(intent_path) == "rolled_back"
    assert load_rollback_receipt(final_path) == receipt
    assert intent_path.exists()
    assert (layout.live / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert (receipt.failed_live_archive.path / "spool.jsonl").read_bytes() == b"new:spool\n"
    assert not activation_receipt.archived_previous_live.path.exists()


def test_rollback_recovery_classification_failure_after_swap_is_typed_unknown(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    activation_receipt = _activate(layout, _prepare(layout))
    intent_path = layout.receipts / "rollback-intent.json"

    def stop_before_swap(phase: str) -> None:
        if phase == "before_rollback_swap":
            raise RuntimeError("leave rollback pre-swap")

    with pytest.raises(EvidenceActivationError):
        rollback_evidence_activation(
            activation_receipt,
            rollback_intent_path=intent_path,
            final_receipt_path=layout.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=stop_before_swap,
        )
    rollback_intent = load_rollback_intent(intent_path)

    def make_classification_fail(phase: str) -> None:
        if phase == "after_recovery_rollback_swap":
            layout.root.chmod(0o755)

    try:
        with pytest.raises(EvidenceRollbackPostSwapError) as raised:
            recover_rollback_from_intent(
                intent_path,
                final_receipt_path=layout.receipts / "recovered.json",
                confirm_writers_stopped=True,
                swap_fn=_injected_swap,
                move_fn=_injected_move,
                fault_injector=make_classification_fail,
            )
    finally:
        layout.root.chmod(0o700)

    assert raised.value.state == "unknown_post_swap"
    assert isinstance(raised.value.classification_error, EvidenceActivationError)
    assert (layout.live / "spool.jsonl").read_bytes() == b"old:spool\n"
    assert (rollback_intent.source_archive_path / "spool.jsonl").read_bytes() == (
        b"new:spool\n"
    )
    assert not rollback_intent.failed_archive_path.exists()


def test_rollback_crash_recovery_refuses_ambiguous_and_rechecks_intent_before_move(
    tmp_path: Path,
) -> None:
    ambiguous = _layout(tmp_path / "ambiguous")
    activation_receipt = _activate(ambiguous, _prepare(ambiguous))
    ambiguous_intent_path = ambiguous.receipts / "rollback-intent.json"

    def stop_before_swap(phase: str) -> None:
        if phase == "before_rollback_swap":
            raise RuntimeError("leave rollback intent")

    with pytest.raises(EvidenceActivationError):
        rollback_evidence_activation(
            activation_receipt,
            rollback_intent_path=ambiguous_intent_path,
            final_receipt_path=ambiguous.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=stop_before_swap,
        )
    _private_file(ambiguous.live / "unbound.bin", b"drift")
    assert inspect_rollback_state(ambiguous_intent_path) == "ambiguous"
    with pytest.raises(EvidenceActivationDrift, match="ambiguous"):
        recover_rollback_from_intent(
            ambiguous_intent_path,
            final_receipt_path=ambiguous.receipts / "recovered.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )

    pending = _layout(tmp_path / "pending")
    pending_activation = _activate(pending, _prepare(pending))
    pending_intent_path = pending.receipts / "rollback-intent.json"

    def exchange_then_fail(left: Path, right: Path) -> None:
        _injected_swap(left, right)
        raise RuntimeError("leave rollback pending archive")

    with pytest.raises(EvidenceRollbackPostSwapError):
        rollback_evidence_activation(
            pending_activation,
            rollback_intent_path=pending_intent_path,
            final_receipt_path=pending.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=exchange_then_fail,
            move_fn=_injected_move,
        )
    move_called = False

    def rewrite_before_move(phase: str) -> None:
        if phase == "before_recovery_failed_archive_move":
            _rewrite_intent_in_place(pending_intent_path, field="rollback_id")

    def unexpected_move(_source: Path, _destination: Path) -> None:
        nonlocal move_called
        move_called = True

    with pytest.raises(EvidenceRollbackPostSwapError) as raised:
        recover_rollback_from_intent(
            pending_intent_path,
            final_receipt_path=pending.receipts / "recovered.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=unexpected_move,
            fault_injector=rewrite_before_move,
        )
    assert raised.value.state == "swapped_pending_failed_archive"
    assert move_called is False
    assert pending.live.is_dir()
    assert pending_activation.archived_previous_live.path.is_dir()


def test_rollback_crash_recovery_never_overwrites_and_receipt_fsync_is_fail_visible(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    activation_receipt = _activate(layout, _prepare(layout))
    intent_path = layout.receipts / "rollback-intent.json"

    def fail_before_receipt(phase: str) -> None:
        if phase == "before_rollback_receipt":
            raise RuntimeError("leave rolled back without receipt")

    with pytest.raises(EvidenceRollbackPostSwapError):
        rollback_evidence_activation(
            activation_receipt,
            rollback_intent_path=intent_path,
            final_receipt_path=layout.receipts / "unwritten.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fault_injector=fail_before_receipt,
        )
    rollback_intent = load_rollback_intent(intent_path)
    assert inspect_rollback_state(rollback_intent) == "rolled_back"

    existing = _private_file(layout.receipts / "existing.json", b"do not overwrite")
    with pytest.raises(EvidenceActivationError, match="already exists"):
        recover_rollback_from_intent(
            intent_path,
            final_receipt_path=existing,
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
        )
    assert existing.read_bytes() == b"do not overwrite"

    calls = 0

    def fail_receipt_stage_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("rollback recovery receipt fsync failed")
        os.fsync(descriptor)

    with pytest.raises(EvidenceRollbackPostSwapError) as raised:
        recover_rollback_from_intent(
            intent_path,
            final_receipt_path=layout.receipts / "recovered.json",
            confirm_writers_stopped=True,
            swap_fn=_injected_swap,
            move_fn=_injected_move,
            fsync_fn=fail_receipt_stage_fsync,
        )
    assert raised.value.state == "rolled_back"
    assert inspect_rollback_state(rollback_intent) == "rolled_back"
    assert not (layout.receipts / "recovered.json").exists()
    assert list(layout.receipts.glob(".*.staged-*")) == []
    assert layout.live.is_dir()
    assert rollback_intent.failed_archive_path.is_dir()
