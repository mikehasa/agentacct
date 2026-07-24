from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import agent_chronicle.canonical.snapshot as snapshot_module
from agent_chronicle.canonical.snapshot import (
    CandidateTargetError,
    ManifestValidationError,
    SnapshotManifest,
    SnapshotVerificationError,
    VerifiedSnapshot,
    validate_candidate_target,
    verify_snapshot,
)


def _entry(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _write_manifest(
    path: Path,
    files: list[dict[str, object]],
    *,
    kind: str = "legacy-codex",
) -> tuple[Path, bytes]:
    raw = json.dumps(
        {"version": 1, "kind": kind, "files": files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(raw)
    return path, raw


def _verified_fixture(tmp_path: Path) -> tuple[VerifiedSnapshot, Path, bytes]:
    root = tmp_path / "snapshot"
    payload = b'{"type":"session_meta","id":"opaque"}\n'
    source = root / "sessions" / "rollout.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    manifest_path, _ = _write_manifest(
        tmp_path / "snapshot-manifest.json",
        [_entry("sessions/rollout.jsonl", payload)],
    )
    return VerifiedSnapshot.verify(root=root, manifest=manifest_path), source, payload


def test_verified_snapshot_proves_manifest_and_opens_only_declared_files_read_only(
    tmp_path: Path,
) -> None:
    snapshot, source, payload = _verified_fixture(tmp_path)
    raw_manifest = snapshot.manifest.path.read_bytes()

    assert snapshot.root == source.parents[1].resolve()
    assert snapshot.kind == "legacy-codex"
    assert snapshot.manifest_kind == "legacy-codex"
    assert snapshot.manifest_digest == hashlib.sha256(raw_manifest).hexdigest()
    assert snapshot.manifest_digest_sha256 == snapshot.manifest.digest_sha256
    assert snapshot.path_for("sessions/rollout.jsonl") == source
    assert snapshot.files[0].relative_path == "sessions/rollout.jsonl"
    with snapshot.open_binary("sessions/rollout.jsonl") as handle:
        assert handle.read() == payload
        assert handle.writable() is False
    with pytest.raises(SnapshotVerificationError, match="not declared"):
        snapshot.path_for("unlisted.jsonl")

    loaded = SnapshotManifest.load(snapshot.manifest.path)
    assert loaded.files == loaded.entries
    assert verify_snapshot(snapshot.root, loaded).manifest_digest == snapshot.manifest_digest


def test_manifest_records_whether_kind_was_explicitly_declared(tmp_path: Path) -> None:
    payload = b"offline"
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "events.jsonl").write_bytes(payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "files": [_entry("events.jsonl", payload)]}),
        encoding="utf-8",
    )

    loaded = SnapshotManifest.load(manifest)
    snapshot = VerifiedSnapshot.verify(root, loaded)

    assert loaded.kind == "legacy"
    assert loaded.kind_declared is False
    assert snapshot.manifest_kind_declared is False


def test_paths_must_be_explicit_absolute_and_manifest_must_be_regular(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _source, _payload = _verified_fixture(tmp_path)

    with pytest.raises(ManifestValidationError, match="absolute"):
        SnapshotManifest.load(Path("snapshot-manifest.json"))
    with pytest.raises(SnapshotVerificationError, match="absolute"):
        VerifiedSnapshot.verify(Path("snapshot"), snapshot.manifest.path)

    manifest_link = tmp_path / "manifest-link.json"
    manifest_link.symlink_to(snapshot.manifest.path)
    with pytest.raises(ManifestValidationError, match="symlink"):
        SnapshotManifest.load(manifest_link)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CandidateTargetError, match="absolute"):
        snapshot.validate_candidate_target(Path("candidate.sqlite3"), scratch_root=scratch)


@pytest.mark.parametrize(
    "raw, message",
    [
        (
            b'{"version":1,"version":1,"files":[]}',
            "duplicate JSON key",
        ),
        (
            b'{"version":1,"files":[],"unexpected":true}',
            "unknown fields",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "files": [
                        {"path": "../escape", "size_bytes": 0, "sha256": "0" * 64}
                    ],
                }
            ).encode(),
            "dot traversal",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "files": [
                        {"path": "data", "size_bytes": 0, "sha256": "0" * 64},
                        {"path": "data", "size_bytes": 0, "sha256": "0" * 64},
                    ],
                }
            ).encode(),
            "duplicate path",
        ),
    ],
)
def test_manifest_json_and_relative_paths_are_strict(
    tmp_path: Path,
    raw: bytes,
    message: str,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(raw)

    with pytest.raises(ManifestValidationError, match=message):
        SnapshotManifest.load(manifest)


def test_snapshot_rejects_symlinks_and_non_regular_objects_even_when_unlisted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"outside")
    (root / "source.jsonl").symlink_to(outside)
    manifest, _ = _write_manifest(
        tmp_path / "manifest.json",
        [_entry("source.jsonl", b"outside")],
    )

    with pytest.raises(SnapshotVerificationError, match="symlink"):
        VerifiedSnapshot.verify(root, manifest)

    (root / "source.jsonl").unlink()
    (root / "source.jsonl").write_bytes(b"outside")
    fifo = root / "unlisted.fifo"
    os.mkfifo(fifo)
    with pytest.raises(SnapshotVerificationError, match="regular files"):
        VerifiedSnapshot.verify(root, manifest)

    fifo.unlink()
    os.link(outside, root / "unlisted-hardlink.jsonl")
    with pytest.raises(SnapshotVerificationError, match="hard-linked"):
        VerifiedSnapshot.verify(root, manifest)


def test_snapshot_rejects_size_digest_and_post_verification_changes(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    source = root / "events.jsonl"
    source.write_bytes(b"abc")
    wrong_size_manifest, _ = _write_manifest(
        tmp_path / "wrong-size.json",
        [{"path": "events.jsonl", "size_bytes": 4, "sha256": hashlib.sha256(b"abc").hexdigest()}],
    )
    with pytest.raises(SnapshotVerificationError, match="size mismatch"):
        VerifiedSnapshot.verify(root, wrong_size_manifest)

    wrong_digest_manifest, _ = _write_manifest(
        tmp_path / "wrong-digest.json",
        [_entry("events.jsonl", b"abd")],
    )
    with pytest.raises(SnapshotVerificationError, match="SHA-256 mismatch"):
        VerifiedSnapshot.verify(root, wrong_digest_manifest)

    good_manifest, _ = _write_manifest(
        tmp_path / "good.json",
        [_entry("events.jsonl", b"abc")],
    )
    snapshot = VerifiedSnapshot.verify(root, good_manifest)
    source.write_bytes(b"abd")
    with pytest.raises(SnapshotVerificationError, match="SHA-256 mismatch"):
        snapshot.verify_unchanged()
    with pytest.raises(SnapshotVerificationError, match="changed"):
        with snapshot.open_binary("events.jsonl"):
            pass


def test_snapshot_detects_mutate_then_restore_and_reverifies_on_consumer_error(
    tmp_path: Path,
) -> None:
    snapshot, source, payload = _verified_fixture(tmp_path)
    source.write_bytes(b"changed")
    source.write_bytes(payload)
    with pytest.raises(SnapshotVerificationError, match="identity changed"):
        snapshot.verify_unchanged()

    fresh_snapshot, fresh_source, _ = _verified_fixture(tmp_path / "fresh")
    with pytest.raises(SnapshotVerificationError, match="changed while open"):
        with fresh_snapshot.open_binary("sessions/rollout.jsonl"):
            fresh_source.write_bytes(b"mutated")
            raise RuntimeError("consumer failed")


def test_snapshot_requires_exact_inventory_and_detects_mutation_while_open(
    tmp_path: Path,
) -> None:
    snapshot, source, _payload = _verified_fixture(tmp_path)
    unlisted = snapshot.root / "unlisted.sqlite3"
    unlisted.write_bytes(b"not-declared")
    with pytest.raises(SnapshotVerificationError, match="unlisted=1"):
        snapshot.verify_unchanged()
    unlisted.unlink()

    with pytest.raises(SnapshotVerificationError, match="changed while open"):
        with snapshot.open_binary("sessions/rollout.jsonl") as handle:
            assert handle.read(1)
            source.write_bytes(b'{"type":"changed-with-same-ish-shape"}\n')


def test_snapshot_rejects_chronicle_state_and_live_runtime_markers(tmp_path: Path) -> None:
    state_root = tmp_path / ".agent-sentinel" / "state" / "copied-snapshot"
    state_root.mkdir(parents=True)
    source = state_root / "events.jsonl"
    source.write_bytes(b"safe-copy")
    manifest, _ = _write_manifest(
        tmp_path / "manifest.json",
        [_entry("events.jsonl", b"safe-copy")],
    )
    with pytest.raises(
        SnapshotVerificationError,
        match=r"[.]agent-" r"sentinel/state",
    ):
        VerifiedSnapshot.verify(state_root, manifest)

    global_state_root = tmp_path / ".agent-sentinel-global" / "state" / "copied-snapshot"
    global_state_root.mkdir(parents=True)
    (global_state_root / "events.jsonl").write_bytes(b"safe-copy")
    with pytest.raises(
        SnapshotVerificationError,
        match=r"[.]agent-" r"sentinel-global/state",
    ):
        VerifiedSnapshot.verify(global_state_root, manifest)

    codex_root = tmp_path / ".codex" / "copied-snapshot"
    codex_root.mkdir(parents=True)
    (codex_root / "events.jsonl").write_bytes(b"safe-copy")
    with pytest.raises(SnapshotVerificationError, match="live Codex"):
        VerifiedSnapshot.verify(codex_root, manifest)

    root = tmp_path / "offline-snapshot"
    root.mkdir()
    (root / "events.jsonl").write_bytes(b"safe-copy")
    (root / "events.jsonl.lock").write_bytes(b"")
    with pytest.raises(SnapshotVerificationError, match="runtime marker"):
        VerifiedSnapshot.verify(root, manifest)

    (root / "events.jsonl.lock").unlink()
    runtime = root / "runtime"
    runtime.mkdir()
    (runtime / "watcher.log").write_bytes(b"")
    with pytest.raises(SnapshotVerificationError, match="runtime marker"):
        VerifiedSnapshot.verify(root, manifest)


@pytest.mark.parametrize(
    "component, product",
    [
        (".agent-sentinel", "agentacct"),
        (".agent-sentinel-global", "agentacct"),
        (".agent-chronicle", "agentacct"),
        (".agent-chronicle-global", "agentacct"),
        (".codex", "Codex"),
    ],
)
def test_snapshot_rejects_exact_live_root_components_without_state_suffix(
    tmp_path: Path,
    component: str,
    product: str,
) -> None:
    root = tmp_path / component / "copied-snapshot"
    root.mkdir(parents=True)
    payload = b"safe-copy"
    (root / "events.jsonl").write_bytes(payload)
    manifest, _ = _write_manifest(
        tmp_path / f"{component[1:]}-manifest.json",
        [_entry("events.jsonl", payload)],
    )

    with pytest.raises(SnapshotVerificationError, match=f"live {product} state root"):
        VerifiedSnapshot.verify(root, manifest)


def test_snapshot_rejects_configured_live_codex_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_root = tmp_path / "custom-codex-home"
    live_root.mkdir()
    (live_root / "events.jsonl").write_bytes(b"live")
    manifest, _ = _write_manifest(
        tmp_path / "manifest.json",
        [_entry("events.jsonl", b"live")],
    )
    monkeypatch.setenv("CODEX_HOME", str(live_root))

    with pytest.raises(SnapshotVerificationError, match="configured live Codex"):
        VerifiedSnapshot.verify(live_root, manifest)


@pytest.mark.parametrize(
    "variable",
    ["AGENT_CHRONICLE_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"],
)
def test_all_configured_live_roots_are_rejected_before_snapshot_or_manifest_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    snapshot, _source, _payload = _verified_fixture(tmp_path / "verified")
    live_root = tmp_path / "configured-live-root"
    live_root.mkdir()
    live_payload = b"synthetic-live"
    (live_root / "events.jsonl").write_bytes(live_payload)
    outside_manifest, _ = _write_manifest(
        tmp_path / "outside-manifest.json",
        [_entry("events.jsonl", live_payload)],
    )
    live_manifest, _ = _write_manifest(
        live_root / "manifest.json",
        [_entry("events.jsonl", live_payload)],
    )
    monkeypatch.setenv(variable, str(live_root))

    def fail_scan(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("configured live root was scanned")

    monkeypatch.setattr(snapshot_module, "_scan_snapshot_tree", fail_scan)
    with pytest.raises(SnapshotVerificationError, match="configured live state root"):
        VerifiedSnapshot.verify(live_root, outside_manifest)

    def fail_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("configured live manifest was read")

    monkeypatch.setattr(snapshot_module, "_read_standalone_regular_file", fail_read)
    with pytest.raises(ManifestValidationError, match="configured live state root"):
        SnapshotManifest.load(live_manifest)
    with pytest.raises(CandidateTargetError, match="configured live state root"):
        snapshot.validate_candidate_target(
            live_root / "candidate.sqlite3",
            scratch_root=live_root,
        )


def test_snapshot_scan_keeps_nested_directory_descriptor_anchored_during_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "snapshot"
    nested = root / "nested"
    nested.mkdir(parents=True)
    payload = b"offline"
    (nested / "events.jsonl").write_bytes(payload)
    manifest, _ = _write_manifest(
        tmp_path / "manifest.json",
        [_entry("nested/events.jsonl", payload)],
    )
    synthetic_live = tmp_path / "synthetic-live"
    synthetic_live.mkdir()
    (synthetic_live / "secret.jsonl").write_bytes(b"must-not-be-scanned")
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(synthetic_live))

    nested_identity = (nested.stat().st_dev, nested.stat().st_ino)
    live_identity = (synthetic_live.stat().st_dev, synthetic_live.stat().st_ino)
    parked = tmp_path / "parked-nested"
    real_scandir = os.scandir
    swapped = False

    def racing_scandir(target: int | str | bytes | os.PathLike[str]):  # type: ignore[type-arg]
        nonlocal swapped
        if isinstance(target, int):
            target_identity = (os.fstat(target).st_dev, os.fstat(target).st_ino)
            is_nested = target_identity == nested_identity
        else:
            target_path = Path(target)
            is_nested = target_path == nested
            target_identity = None
        if is_nested and not swapped:
            nested.rename(parked)
            nested.symlink_to(synthetic_live, target_is_directory=True)
            swapped = True
        if isinstance(target, int):
            target_identity = (os.fstat(target).st_dev, os.fstat(target).st_ino)
        else:
            target_stat = Path(target).stat()
            target_identity = (target_stat.st_dev, target_stat.st_ino)
        if target_identity == live_identity:
            raise AssertionError("scanner followed the swapped path into synthetic live state")
        return real_scandir(target)

    monkeypatch.setattr(snapshot_module.os, "scandir", racing_scandir)

    with pytest.raises(SnapshotVerificationError, match="identity changed"):
        VerifiedSnapshot.verify(root, manifest)
    assert swapped is True


def test_manifest_parent_swap_is_refused_before_replacement_content_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_parent = tmp_path / "manifest-parent-anchor"
    safe_parent.mkdir()
    manifest_name = "snapshot-manifest.json"
    safe_manifest, _ = _write_manifest(
        safe_parent / manifest_name,
        [_entry("safe.jsonl", b"safe")],
    )
    synthetic_live = tmp_path / "synthetic-live-manifests"
    synthetic_live.mkdir()
    live_manifest, _ = _write_manifest(
        synthetic_live / manifest_name,
        [_entry("live.jsonl", b"must-not-be-read")],
    )
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(synthetic_live))

    live_identity = (live_manifest.stat().st_dev, live_manifest.stat().st_ino)
    parked = tmp_path / "parked-manifest-parent"
    real_open = os.open
    real_read = os.read
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        full_leaf_open = dir_fd is None and Path(path) == safe_manifest
        component_open = dir_fd is not None and os.fspath(path) == safe_parent.name
        if not swapped and (full_leaf_open or component_open):
            safe_parent.rename(parked)
            safe_parent.symlink_to(synthetic_live, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def guarded_read(descriptor: int, length: int) -> bytes:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == live_identity:
            raise AssertionError("replacement manifest content was read")
        return real_read(descriptor, length)

    monkeypatch.setattr(snapshot_module.os, "open", racing_open)
    monkeypatch.setattr(snapshot_module.os, "read", guarded_read)

    with pytest.raises(ManifestValidationError):
        SnapshotManifest.load(safe_manifest)
    assert swapped is True


def test_snapshot_root_descriptor_stays_pinned_across_manifest_load_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_parent = tmp_path / "safe-root-parent"
    root = safe_parent / "snapshot"
    root.mkdir(parents=True)
    payload = b"same-content-does-not-make-a-replacement-safe"
    (root / "events.jsonl").write_bytes(payload)
    replacement_parent = tmp_path / "replacement-root-parent"
    replacement_root = replacement_parent / "snapshot"
    replacement_root.mkdir(parents=True)
    replacement_file = replacement_root / "events.jsonl"
    replacement_file.write_bytes(payload)
    manifest, _ = _write_manifest(
        tmp_path / "manifest.json",
        [_entry("events.jsonl", payload)],
    )

    replacement_identity = (replacement_file.stat().st_dev, replacement_file.stat().st_ino)
    parked = tmp_path / "parked-safe-root-parent"
    real_manifest_read = snapshot_module._read_standalone_regular_file
    real_read = os.read
    swapped = False

    def swapping_manifest_read(path: Path) -> bytes:
        nonlocal swapped
        raw = real_manifest_read(path)
        safe_parent.rename(parked)
        safe_parent.symlink_to(replacement_parent, target_is_directory=True)
        swapped = True
        return raw

    def guarded_read(descriptor: int, length: int) -> bytes:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == replacement_identity:
            raise AssertionError("hash followed a replaced snapshot root")
        return real_read(descriptor, length)

    monkeypatch.setattr(
        snapshot_module,
        "_read_standalone_regular_file",
        swapping_manifest_read,
    )
    monkeypatch.setattr(snapshot_module.os, "read", guarded_read)

    with pytest.raises(SnapshotVerificationError, match="symlink|identity changed"):
        VerifiedSnapshot.verify(root, manifest)
    assert swapped is True


def test_candidate_target_must_be_outside_snapshot_and_inside_real_scratch_root(
    tmp_path: Path,
) -> None:
    snapshot, _source, _payload = _verified_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = scratch / "candidate.sqlite3"

    assert snapshot.validate_candidate_target(target, scratch_root=scratch) == target.resolve()
    assert validate_candidate_target(snapshot, target, scratch) == target.resolve()
    assert not target.exists()

    with pytest.raises(CandidateTargetError, match="under"):
        snapshot.validate_candidate_target(tmp_path / "outside.sqlite3", scratch_root=scratch)

    with pytest.raises(CandidateTargetError, match="outside"):
        snapshot.validate_candidate_target(
            snapshot.root / "candidate.sqlite3",
            scratch_root=snapshot.root,
        )

    live_parent = scratch / ".agent-sentinel" / "state"
    live_parent.mkdir(parents=True)
    with pytest.raises(
        CandidateTargetError,
        match=r"[.]agent-" r"sentinel/state",
    ):
        snapshot.validate_candidate_target(
            live_parent / "candidate.sqlite3",
            scratch_root=scratch,
        )


def test_candidate_rejects_symlinks_hardlinks_and_sqlite_sidecars(tmp_path: Path) -> None:
    snapshot, source, _payload = _verified_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    real_target = scratch / "real.sqlite3"
    real_target.write_bytes(b"")
    symlink_target = scratch / "symlink.sqlite3"
    symlink_target.symlink_to(real_target)
    with pytest.raises(CandidateTargetError, match="symlink"):
        snapshot.validate_candidate_target(symlink_target, scratch_root=scratch)

    hardlink_target = scratch / "hardlink.sqlite3"
    os.link(source, hardlink_target)
    with pytest.raises(CandidateTargetError, match="aliases"):
        snapshot.validate_candidate_target(hardlink_target, scratch_root=scratch)

    candidate = scratch / "candidate.sqlite3"
    (scratch / "candidate.sqlite3-wal").write_bytes(b"")
    with pytest.raises(CandidateTargetError, match="sidecar"):
        snapshot.validate_candidate_target(candidate, scratch_root=scratch)


def test_candidate_rejects_symlinked_scratch_or_parent(tmp_path: Path) -> None:
    snapshot, _source, _payload = _verified_fixture(tmp_path)
    real_scratch = tmp_path / "real-scratch"
    real_scratch.mkdir()
    scratch_link = tmp_path / "scratch-link"
    scratch_link.symlink_to(real_scratch, target_is_directory=True)
    with pytest.raises(CandidateTargetError, match="symlink"):
        snapshot.validate_candidate_target(
            scratch_link / "candidate.sqlite3",
            scratch_root=scratch_link,
        )

    nested_real = real_scratch / "nested-real"
    nested_real.mkdir()
    nested_link = real_scratch / "nested-link"
    nested_link.symlink_to(nested_real, target_is_directory=True)
    with pytest.raises(CandidateTargetError, match="symlink"):
        snapshot.validate_candidate_target(
            nested_link / "candidate.sqlite3",
            scratch_root=real_scratch,
        )
