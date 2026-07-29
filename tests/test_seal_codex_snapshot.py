from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agentacct.canonical.snapshot import VerifiedSnapshot


_SEALER_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "seal_codex_snapshot.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "seal_codex_snapshot_test_module", _SEALER_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
sealer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sealer
_SPEC.loader.exec_module(sealer)


def test_sealer_writes_canonical_manifest_and_reverifies_exact_copy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "offline-codex"
    rollout = root / "sessions" / "2026" / "rollout-fixture.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_bytes(b'{"type":"session_meta"}\n')
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "codex-manifest.json"

    result = sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert result["status"] == "sealed"
    assert result["file_count"] == 2
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    snapshot = VerifiedSnapshot.verify(root.resolve(), manifest.resolve())
    assert snapshot.kind == "codex"
    assert {item.relative_path for item in snapshot.files} == {
        "session_index.jsonl",
        "sessions/2026/rollout-fixture.jsonl",
    }


def test_sealer_refuses_live_codex_and_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    live = fake_home / ".codex"
    live.mkdir(parents=True)
    (live / "session_index.jsonl").write_bytes(b"{}\n")
    monkeypatch.setenv("HOME", str(fake_home))

    with pytest.raises(sealer.SealRefusal, match="live Codex"):
        sealer.seal_snapshot(
            str(live.resolve()),
            str((tmp_path / "live-manifest.json").resolve()),
        )

    configured_live = tmp_path / "custom-live-codex"
    configured_live.mkdir()
    (configured_live / "session_index.jsonl").write_bytes(b"{}\n")
    monkeypatch.setenv("CODEX_HOME", str(configured_live))
    with pytest.raises(sealer.SealRefusal, match="live Codex"):
        sealer.seal_snapshot(
            str(configured_live.resolve()),
            str((tmp_path / "configured-live-manifest.json").resolve()),
        )

    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / "state_5.sqlite-wal").write_bytes(b"")
    with pytest.raises(sealer.SealRefusal, match="non-source Codex file"):
        sealer.seal_snapshot(
            str(offline.resolve()),
            str((tmp_path / "offline-manifest.json").resolve()),
        )


@pytest.mark.parametrize(
    ("variable", "protected_argument"),
    (
        ("AGENT_CHRONICLE_STORE_DIR", "root"),
        ("AGENT_SENTINEL_STORE_DIR", "manifest"),
    ),
)
def test_sealer_rejects_configured_live_store_source_and_manifest_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    protected_argument: str,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    protected = root if protected_argument == "root" else manifest
    monkeypatch.setenv(variable, str(protected))

    with pytest.raises(
        sealer.SealRefusal, match="disjoint.*configured live state root"
    ):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert not manifest.exists()


def test_sealer_rejects_manifest_that_is_ancestor_of_configured_live_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(manifest / "nested-live-store"))

    with pytest.raises(
        sealer.SealRefusal, match="disjoint.*configured live state root"
    ):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert not manifest.exists()


def test_sealer_cli_refuses_existing_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    assert (
        sealer.main(
            [
                "--snapshot-root",
                str(root.resolve()),
                "--manifest",
                str(manifest.resolve()),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["status"] == "refused"


def test_sealer_does_not_overwrite_manifest_created_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    real_link = os.link

    def race_link(source: str, destination: str, **kwargs: object) -> None:
        destination_directory = kwargs.get("dst_dir_fd")
        assert isinstance(destination_directory, int)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=destination_directory,
        )
        try:
            os.write(descriptor, b"competitor\n")
        finally:
            os.close(descriptor)
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(sealer.os, "link", race_link)

    with pytest.raises(sealer.SealRefusal, match="must name a new file"):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert manifest.read_bytes() == b"competitor\n"


def test_sealer_refuses_snapshot_root_name_exchange_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b'{"source":"approved"}\n')
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "session_index.jsonl").write_bytes(b'{"source":"other"}\n')
    parked = tmp_path / "approved-inode"
    manifest = tmp_path / "manifest.json"
    real_inventory = sealer._inventory_anchored
    exchanged = False

    def exchange_then_scan(root_descriptor: int) -> object:
        nonlocal exchanged
        if not exchanged:
            exchanged = True
            root.rename(parked)
            replacement.rename(root)
        return real_inventory(root_descriptor)

    monkeypatch.setattr(sealer, "_inventory_anchored", exchange_then_scan)

    with pytest.raises(sealer.SealRefusal, match="path identity changed"):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert not manifest.exists()
    assert (parked / "session_index.jsonl").read_bytes() == b'{"source":"approved"}\n'
    assert (root / "session_index.jsonl").read_bytes() == b'{"source":"other"}\n'


def test_sealer_manifest_parent_exchange_cannot_redirect_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    replacement = tmp_path / "replacement-output"
    replacement.mkdir(mode=0o700)
    replacement.chmod(0o700)
    parked = tmp_path / "approved-output-inode"
    manifest = output / "manifest.json"
    real_stage = sealer._stage_manifest
    exchanged = False

    def exchange_then_stage(*args: object, **kwargs: object) -> object:
        nonlocal exchanged
        if not exchanged:
            exchanged = True
            output.rename(parked)
            replacement.rename(output)
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(sealer, "_stage_manifest", exchange_then_stage)

    with pytest.raises(sealer.SealRefusal, match="path identity changed"):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert not (output / "manifest.json").exists()
    assert not (parked / "manifest.json").exists()
    assert list(output.iterdir()) == []
    assert list(parked.iterdir()) == []


def test_sealer_rolls_back_exact_final_manifest_when_final_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    real_verify = sealer.VerifiedSnapshot.verify
    calls = 0

    def fail_final_verification(
        cls: type[VerifiedSnapshot],
        root_value: Path | str,
        manifest_value: Path | str,
    ) -> VerifiedSnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sealer.SnapshotSafetyError("forced final verification failure")
        return real_verify(root_value, manifest_value)

    monkeypatch.setattr(
        sealer.VerifiedSnapshot,
        "verify",
        classmethod(fail_final_verification),
    )

    with pytest.raises(
        sealer.SnapshotSafetyError,
        match="forced final verification failure",
    ):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert calls == 2
    assert not manifest.exists()
    assert list(tmp_path.glob(".manifest.json.staged-*")) == []


def test_sealer_never_removes_replacement_during_final_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    displaced = tmp_path / "displaced-sealer-manifest.json"
    real_verify = sealer.VerifiedSnapshot.verify
    calls = 0

    def replace_final_then_fail(
        cls: type[VerifiedSnapshot],
        root_value: Path | str,
        manifest_value: Path | str,
    ) -> VerifiedSnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            manifest.rename(displaced)
            manifest.write_bytes(b"competitor\n")
            raise sealer.SnapshotSafetyError("forced replacement race")
        return real_verify(root_value, manifest_value)

    monkeypatch.setattr(
        sealer.VerifiedSnapshot,
        "verify",
        classmethod(replace_final_then_fail),
    )

    with pytest.raises(
        sealer.SealRefusal,
        match="could not safely roll back invalid final manifest",
    ):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert calls == 2
    assert manifest.read_bytes() == b"competitor\n"
    assert displaced.exists()


def test_sealer_rolls_back_publication_when_directory_fsync_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "offline"
    root.mkdir()
    (root / "session_index.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "manifest.json"
    real_fsync = sealer.os.fsync
    calls = 0

    def interrupt_publication(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("forced publication interruption")
        real_fsync(descriptor)

    monkeypatch.setattr(sealer.os, "fsync", interrupt_publication)

    with pytest.raises(KeyboardInterrupt, match="forced publication interruption"):
        sealer.seal_snapshot(str(root.resolve()), str(manifest.resolve()))

    assert calls >= 3
    assert not manifest.exists()
    assert list(tmp_path.glob(".manifest.json.staged-*")) == []
