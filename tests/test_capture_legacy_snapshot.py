from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agent_chronicle.canonical.snapshot import VerifiedSnapshot


_CAPTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "capture_legacy_snapshot.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "capture_legacy_snapshot_test_module",
    _CAPTURE_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
capture = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = capture
_SPEC.loader.exec_module(capture)


def _fixture_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, bytes]:
    source = tmp_path / "synthetic-live"
    source.mkdir(mode=0o755)
    payload = (
        b'{"event_id":"evt-1","event_type":"task_started"}\n'
        b'{"event_id":"evt-2","event_type":"task_completed"}\n'
    )
    events = source / capture.EVENTS_NAME
    events.write_bytes(payload)
    events.chmod(0o600)
    lock = source / capture.LOCK_NAME
    lock.write_bytes(b"")
    lock.chmod(0o644)
    output = tmp_path / "offline-output"
    output.mkdir(mode=0o700)
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(source.resolve()))
    return source.resolve(), output.resolve(), payload


def _published_manifests(output: Path) -> list[Path]:
    return list(output.glob(f"legacy-chronicle-capture-*/{capture.MANIFEST_NAME}"))


def test_capture_creates_private_manifest_verified_exact_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, payload = _fixture_store(tmp_path, monkeypatch)
    source_before = (source / capture.EVENTS_NAME).stat()
    lock_before = (source / capture.LOCK_NAME).stat()

    result = capture.capture_snapshot(
        source_store_root=source,
        output_parent=output,
    )

    snapshot_root = Path(result["snapshot_root"])
    manifest_path = Path(result["manifest"])
    attestation_path = Path(result["attestation"])
    assert result["status"] == "verified"
    assert result["watcher_stopped"] is False
    assert result["live_content_modified"] is False
    assert snapshot_root.joinpath(capture.EVENTS_NAME).read_bytes() == payload
    assert stat.S_IMODE(snapshot_root.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot_root.joinpath(capture.EVENTS_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(attestation_path.stat().st_mode) == 0o600
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {"version", "kind", "files"}
    assert manifest["kind"] == capture.MANIFEST_KIND
    verified = VerifiedSnapshot.verify(snapshot_root, manifest_path)
    verified.verify_unchanged()
    assert verified.kind == capture.MANIFEST_KIND
    assert [item.relative_path for item in verified.files] == [capture.EVENTS_NAME]

    source_after = (source / capture.EVENTS_NAME).stat()
    lock_after = (source / capture.LOCK_NAME).stat()
    assert (source_after.st_dev, source_after.st_ino, source_after.st_size) == (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_size,
    )
    assert (source_after.st_mtime_ns, source_after.st_ctime_ns) == (
        source_before.st_mtime_ns,
        source_before.st_ctime_ns,
    )
    assert (lock_after.st_dev, lock_after.st_ino, lock_after.st_size) == (
        lock_before.st_dev,
        lock_before.st_ino,
        lock_before.st_size,
    )
    assert (source / capture.EVENTS_NAME).read_bytes() == payload


def test_capture_refuses_missing_existing_writer_lock_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    (source / capture.LOCK_NAME).unlink()

    with pytest.raises(capture.CaptureRefusal, match="must already contain"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert _published_manifests(output) == []


def test_capture_refuses_hardlinked_source_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    os.link(source / capture.EVENTS_NAME, tmp_path / "second-events-link")

    with pytest.raises(capture.CaptureRefusal, match="one unique regular file"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert _published_manifests(output) == []


def test_capture_detects_uncooperative_source_mutation_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    real_copy = capture._copy_exact

    def copy_then_mutate(*args: object, **kwargs: object) -> str:
        digest = real_copy(*args, **kwargs)
        with (source / capture.EVENTS_NAME).open("ab") as handle:
            handle.write(b'{"event_id":"uncooperative"}\n')
        return digest

    monkeypatch.setattr(capture, "_copy_exact", copy_then_mutate)

    with pytest.raises(capture.CaptureRefusal, match="exceeds its declared size"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert _published_manifests(output) == []
    retained_pattern = (
        f"legacy-chronicle-capture-*/{capture.SNAPSHOT_DIRECTORY_NAME}/"
        f"{capture.EVENTS_NAME}"
    )
    retained = list(
        output.glob(retained_pattern)
    )
    assert len(retained) == 1
    assert stat.S_IMODE(retained[0].stat().st_mode) == 0o600


def test_capture_detects_atomic_source_replace_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, payload = _fixture_store(tmp_path, monkeypatch)
    real_copy = capture._copy_exact

    def copy_then_replace(*args: object, **kwargs: object) -> str:
        digest = real_copy(*args, **kwargs)
        replacement = source / "replacement.jsonl"
        replacement.write_bytes(payload)
        replacement.chmod(0o600)
        os.replace(replacement, source / capture.EVENTS_NAME)
        return digest

    monkeypatch.setattr(capture, "_copy_exact", copy_then_replace)

    with pytest.raises(capture.CaptureRefusal, match="changed during capture"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert _published_manifests(output) == []


def test_capture_detects_writer_lock_replace_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    real_copy = capture._copy_exact

    def copy_then_replace_lock(*args: object, **kwargs: object) -> str:
        digest = real_copy(*args, **kwargs)
        lock = source / capture.LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"")
        lock.chmod(0o644)
        return digest

    monkeypatch.setattr(capture, "_copy_exact", copy_then_replace_lock)

    with pytest.raises(capture.CaptureRefusal, match="writer lock changed"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert _published_manifests(output) == []


def test_capture_times_out_on_writer_lock_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    descriptor = os.open(source / capture.LOCK_NAME, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(capture.CaptureRefusal, match="timed out"):
            capture.capture_snapshot(
                source_store_root=source,
                output_parent=output,
                lock_timeout_seconds=0.05,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert _published_manifests(output) == []


def test_lock_timer_starts_when_shared_lock_is_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter((BlockingIOError(), None))
    observed_times = iter((10.0, 10.1, 10.2))

    def synthetic_flock(*args: object, **kwargs: object) -> None:
        outcome = next(attempts)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(capture.fcntl, "flock", synthetic_flock)
    monkeypatch.setattr(capture.time, "monotonic", lambda: next(observed_times))
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)

    assert capture._acquire_shared_lock(123, timeout_seconds=1.0) == 10.2


def test_capture_refuses_live_or_source_overlapping_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, _ = _fixture_store(tmp_path, monkeypatch)
    source.chmod(0o700)

    with pytest.raises(capture.CaptureRefusal, match="disjoint"):
        capture.capture_snapshot(source_store_root=source, output_parent=source)


def test_output_parent_exchange_cannot_redirect_writes_to_live_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    moved_output = tmp_path / "offline-output-original"
    real_mkdir = capture.os.mkdir
    exchanged = False

    def exchange_before_mkdir(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal exchanged
        if not exchanged and str(path).startswith("legacy-chronicle-capture-"):
            exchanged = True
            output.rename(moved_output)
            output.symlink_to(source, target_is_directory=True)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(capture.os, "mkdir", exchange_before_mkdir)

    with pytest.raises(capture.CaptureRefusal, match="disjoint|symlink|identity"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert list(source.glob("legacy-chronicle-capture-*")) == []
    assert _published_manifests(moved_output) == []


def test_output_name_exchanged_for_source_inode_is_refused_before_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    source.chmod(0o700)
    moved_output = tmp_path / "offline-output-original"
    real_open_anchored = capture._open_anchored_directory
    exchanged = False

    def exchange_before_output_open(
        path: Path,
        *,
        label: str,
        owner_only: bool,
    ) -> tuple[int, tuple[int, int]]:
        nonlocal exchanged
        if label == "--output-parent" and not exchanged:
            exchanged = True
            output.rename(moved_output)
            source.rename(output)
        return real_open_anchored(path, label=label, owner_only=owner_only)

    monkeypatch.setattr(capture, "_open_anchored_directory", exchange_before_output_open)

    with pytest.raises(capture.CaptureRefusal, match="source store inode"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert list(output.glob("legacy-chronicle-capture-*")) == []
    assert _published_manifests(moved_output) == []


def test_manifest_is_not_published_when_staged_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)

    def fail_verification(*args: object, **kwargs: object) -> object:
        raise capture.CaptureRefusal("synthetic verification failure")

    monkeypatch.setattr(capture.VerifiedSnapshot, "verify", fail_verification)

    with pytest.raises(capture.CaptureRefusal, match="synthetic verification"):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert _published_manifests(output) == []


def test_failed_final_manifest_and_verified_stage_are_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    real_verify = capture.VerifiedSnapshot.verify
    calls = 0

    def corrupt_before_final_verify(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(args[1]).write_bytes(b"{}\n")
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(capture.VerifiedSnapshot, "verify", corrupt_before_final_verify)

    with pytest.raises(
        capture.CaptureRefusal,
        match="retained owner-only capture artifacts",
    ):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    published = _published_manifests(output)
    assert len(published) == 1
    assert published[0].read_bytes() == b"{}\n"
    staged = list(
        published[0].parent.glob(f".{capture.MANIFEST_NAME}.verified-*")
    )
    assert len(staged) == 1
    assert (
        json.loads(staged[0].read_text(encoding="utf-8"))["kind"]
        == capture.MANIFEST_KIND
    )


def test_capture_retains_replacement_after_final_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, output, _ = _fixture_store(tmp_path, monkeypatch)
    real_verify = capture.VerifiedSnapshot.verify
    calls = 0
    displaced: Path | None = None
    replacement_bytes = b'{"competitor":true}\n'

    def replace_final_then_fail(*args: object, **kwargs: object) -> object:
        nonlocal calls, displaced
        calls += 1
        if calls == 2:
            manifest = Path(args[1])
            displaced = manifest.with_name("displaced-capture-manifest.json")
            manifest.rename(displaced)
            manifest.write_bytes(replacement_bytes)
            manifest.chmod(0o600)
            raise capture.CaptureRefusal("forced replacement race")
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(capture.VerifiedSnapshot, "verify", replace_final_then_fail)

    with pytest.raises(
        capture.CaptureRefusal,
        match="retained owner-only capture artifacts",
    ):
        capture.capture_snapshot(source_store_root=source, output_parent=output)

    assert calls == 2
    published = _published_manifests(output)
    assert len(published) == 1
    assert published[0].read_bytes() == replacement_bytes
    assert displaced is not None and displaced.exists()


def test_publication_never_overwrites_existing_file(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    final = parent / "evidence.json"
    final.write_bytes(b"competitor\n")

    descriptor = os.open(parent, capture._directory_open_flags())
    try:
        with pytest.raises(capture.CaptureRefusal, match="refusing to overwrite"):
            capture._publish_new_file(parent, descriptor, final.name, b"ours\n")
    finally:
        os.close(descriptor)

    assert final.read_bytes() == b"competitor\n"


def test_new_publication_retains_replacement_after_identity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    final = parent / "evidence.json"
    displaced = parent / "displaced-evidence.json"
    trusted_bytes = b'{"trusted":true}\n'
    replacement_bytes = b'{"trusted":false}\n'
    descriptor = os.open(parent, capture._directory_open_flags())
    real_stat = os.stat
    replaced = False

    def replace_before_path_proof(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal replaced
        if path == final.name and not replaced:
            replaced = True
            final.rename(displaced)
            final.write_bytes(replacement_bytes)
            final.chmod(0o600)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(capture.os, "stat", replace_before_path_proof)

    try:
        with pytest.raises(capture.CaptureRefusal, match="identity or content changed"):
            capture._publish_new_file(parent, descriptor, final.name, trusted_bytes)
    finally:
        os.close(descriptor)

    assert final.read_bytes() == replacement_bytes
    assert displaced.read_bytes() == trusted_bytes


def test_final_manifest_publication_never_unlinks_a_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    staged_name = ".staged-manifest.json"
    staged_bytes = b"{}\n"
    descriptor = os.open(parent, capture._directory_open_flags())
    capture._publish_new_file(parent, descriptor, staged_name, staged_bytes)

    def forbid_path_unlink(*args: object, **kwargs: object) -> None:
        raise AssertionError("publication must not unlink by pathname")

    monkeypatch.setattr(capture.os, "unlink", forbid_path_unlink)

    try:
        published = capture._publish_existing_file(
            parent,
            descriptor,
            staged_name,
            capture.MANIFEST_NAME,
            expected_size=len(staged_bytes),
            expected_sha256=hashlib.sha256(staged_bytes).hexdigest(),
        )
    finally:
        os.close(descriptor)

    staged = parent / staged_name
    assert published.read_bytes() == staged_bytes
    assert staged.read_bytes() == staged_bytes
    assert (published.stat().st_dev, published.stat().st_ino) != (
        staged.stat().st_dev,
        staged.stat().st_ino,
    )
    assert published.stat().st_nlink == staged.stat().st_nlink == 1


def test_staged_name_swap_before_final_create_fails_without_path_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    staged_name = ".staged-manifest.json"
    staged_bytes = b'{"trusted":true}\n'
    replacement_bytes = b'{"trusted":false}\n'
    descriptor = os.open(parent, capture._directory_open_flags())
    capture._publish_new_file(parent, descriptor, staged_name, staged_bytes)
    real_open = os.open
    replaced = False

    def replace_staged_before_final_open(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal replaced
        if path == capture.MANIFEST_NAME and not replaced:
            replaced = True
            replacement = parent / ".replacement-manifest.json"
            replacement.write_bytes(replacement_bytes)
            replacement.chmod(0o600)
            os.replace(replacement, parent / staged_name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(capture.os, "open", replace_staged_before_final_open)

    try:
        with pytest.raises(
            capture.CaptureRefusal,
            match="owner-only artifacts retained",
        ):
            capture._publish_existing_file(
                parent,
                descriptor,
                staged_name,
                capture.MANIFEST_NAME,
                expected_size=len(staged_bytes),
                expected_sha256=hashlib.sha256(staged_bytes).hexdigest(),
            )
    finally:
        os.close(descriptor)

    assert (parent / capture.MANIFEST_NAME).read_bytes() == staged_bytes
    assert (parent / staged_name).read_bytes() == replacement_bytes
