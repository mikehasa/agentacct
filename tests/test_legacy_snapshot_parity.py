from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agent_chronicle.client_usage import ClientUsageEvent
from agent_chronicle.canonical import safe_scratch
from agent_chronicle.usage_truth import mark_trusted_local_usage_import_event


_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "legacy_snapshot_parity.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "legacy_snapshot_parity_test_module",
    _RUNNER_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


RAW_SESSION_ID = "runner-private-session-id"
RAW_SOURCE_PATH = "/private/offline/client/source.jsonl"
RAW_PROJECT_PATH = "/private/offline/project"


def _snapshot(
    tmp_path: Path,
    *,
    kind: str | None = "legacy-chronicle",
    root_parent: Path | None = None,
) -> tuple[Path, Path]:
    event = ClientUsageEvent(
        client="codex",
        client_session_id=RAW_SESSION_ID,
        source_path=Path(RAW_SOURCE_PATH),
        title=None,
        cwd=RAW_PROJECT_PATH,
        model="gpt-runner-fixture",
        input_tokens=10,
        output_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_tokens_reported=True,
        cache_read_tokens_reported=True,
        reasoning_output_tokens=0,
        provider_name="codex",
        started_at=1_780_000_000,
        updated_at=1_780_000_001,
        turn_count=1,
        source_namespace_fingerprint="b6" * 32,
        input_tokens_reported=True,
        output_tokens_reported=True,
        reasoning_output_tokens_reported=True,
        total_tokens=10,
        total_tokens_reported=True,
    ).to_sentinel_event()
    event.update({"event_id": "runner-event", "created_at": 1_780_000_000.0})
    event = mark_trusted_local_usage_import_event(event)
    payload = (
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    parent = root_parent or tmp_path
    root = parent / "sealed-legacy"
    root.mkdir()
    (root / "events.jsonl").write_bytes(payload)
    manifest = tmp_path / f"{kind or 'kindless'}-manifest.json"
    manifest_value: dict[str, object] = {
        "version": 1,
        "files": [
            {
                "path": "events.jsonl",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    if kind is not None:
        manifest_value["kind"] = kind
    manifest.write_text(
        json.dumps(
            manifest_value,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return root.resolve(), manifest.resolve()


def _arguments(
    *,
    scratch: Path,
    root: Path,
    manifest: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        scratch_root=str(scratch.resolve()),
        snapshot_root=str(root),
        snapshot_manifest=str(manifest),
        source_file="events.jsonl",
        legacy_store_scope="custom",
    )


def test_runner_retains_private_candidate_and_redacted_manifest_verified_report(
    tmp_path: Path,
) -> None:
    root, manifest = _snapshot(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)

    report, report_path = runner.run_parity(
        _arguments(scratch=scratch, root=root, manifest=manifest)
    )

    assert report["schema_version"] == "agent-chronicle.legacy-product-parity.v2"
    assert report["status"] == "passed"
    assert report["decision"] == "go-core-truth-slice"
    assert report["cutover_decision"] == "no-go"
    assert report["acceptance"]["core_truth_slice_passed"] is True
    assert report["acceptance"]["product_scope_complete"] is False
    assert report["acceptance"]["cutover_gate_passed"] is False
    assert report["rerun"] == {
        "canonical_writes": 0,
        "canonical_sequence_delta": 0,
        "task_ids_stable": True,
        "opaque_task_ids_valid": True,
        "task_id_count": 1,
        "table_counts_stable": True,
        "projection_rebuilt": False,
        "second_import": {
            "write_dispositions": {
                "sessions": {"noop": 1},
                "usage": {"noop": 1},
            },
            "internal_parity_matches": True,
            "internal_parity_difference_keys": [],
        },
    }
    assert report["migration"]["write_dispositions"] == {
        "sessions": {"inserted": 1},
        "usage": {"inserted": 1},
    }
    assert report_path.exists()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    candidate = report_path.parent / report["runner"]["candidate_file"]
    assert candidate.exists()
    assert report["runner"]["schema_version"] == "agent-chronicle.legacy-parity-runner.v3"
    with candidate.open("rb") as candidate_handle:
        assert report["runner"]["candidate_sha256"] == hashlib.file_digest(
            candidate_handle, "sha256"
        ).hexdigest()
    assert not Path(f"{candidate}-wal").exists()
    assert not Path(f"{candidate}-shm").exists()
    assert not Path(f"{candidate}-journal").exists()
    assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    assert stat.S_IMODE(report_path.parent.stat().st_mode) == 0o700
    encoded = report_path.read_text(encoding="utf-8")
    for forbidden in (
        RAW_SESSION_ID,
        RAW_SOURCE_PATH,
        RAW_PROJECT_PATH,
        str(root),
        str(manifest),
        str(candidate),
    ):
        assert forbidden not in encoded


def test_candidate_sealing_removes_only_checkpointed_private_sidecars(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    with runner.create_anchored_run_directory(
        scratch,
        prefix="seal-test-",
        require_private_root=True,
    ) as run_directory:
        for name, content in (
            ("candidate.sqlite3-wal", b""),
            ("candidate.sqlite3-shm", b"private shared-memory index"),
        ):
            descriptor = run_directory.open_file(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        runner._remove_checkpointed_candidate_sidecars(
            run_directory,
            "candidate.sqlite3",
        )

        assert not (run_directory.path / "candidate.sqlite3-wal").exists()
        assert not (run_directory.path / "candidate.sqlite3-shm").exists()


def test_candidate_sealing_refuses_nonempty_wal(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    with runner.create_anchored_run_directory(
        scratch,
        prefix="seal-refusal-",
        require_private_root=True,
    ) as run_directory:
        descriptor = run_directory.open_file(
            "candidate.sqlite3-wal",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        try:
            os.write(descriptor, b"uncheckpointed")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        with pytest.raises(
            runner.ParityRunnerRefusal,
            match="WAL was not fully checkpointed",
        ):
            runner._remove_checkpointed_candidate_sidecars(
                run_directory,
                "candidate.sqlite3",
            )

        assert (run_directory.path / "candidate.sqlite3-wal").read_bytes() == b"uncheckpointed"


def test_runner_refuses_nonlegacy_manifest_before_creating_candidate(
    tmp_path: Path,
) -> None:
    root, manifest = _snapshot(tmp_path, kind="codex")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)

    with pytest.raises(
        runner.ParityRunnerRefusal,
        match="kind must be legacy",
    ):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert list(scratch.iterdir()) == []


def test_runner_requires_explicit_manifest_kind_before_verification_or_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _snapshot(tmp_path, kind=None)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)

    def unexpected_verify(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot verification must not run for a kindless manifest")

    monkeypatch.setattr(runner.VerifiedSnapshot, "verify", unexpected_verify)
    with pytest.raises(
        runner.ParityRunnerRefusal,
        match="explicitly declare kind",
    ):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert list(scratch.iterdir()) == []


def test_runner_rejects_custom_env_live_snapshot_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _snapshot(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(root))

    def unexpected_verify(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("configured live state must not be verified or scanned")

    monkeypatch.setattr(runner.VerifiedSnapshot, "verify", unexpected_verify)
    with pytest.raises(
        runner.ParityRunnerRefusal,
        match="snapshot-root.*disjoint.*configured live store",
    ):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert list(scratch.iterdir()) == []


def test_runner_rejects_manifest_inside_custom_live_root_before_read_or_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _snapshot(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    live_root = tmp_path / "custom-live-store"
    live_root.mkdir()
    live_manifest = manifest.rename(live_root / manifest.name)
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(live_root))

    def unexpected_manifest_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a manifest in live state must not be opened")

    def unexpected_verify(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a manifest in live state must not reach verification")

    monkeypatch.setattr(runner.SnapshotManifest, "load", unexpected_manifest_read)
    monkeypatch.setattr(runner.VerifiedSnapshot, "verify", unexpected_verify)
    with pytest.raises(
        runner.ParityRunnerRefusal,
        match="snapshot-manifest.*disjoint.*configured live store",
    ):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=live_manifest)
        )

    assert list(scratch.iterdir()) == []


def test_runner_rejects_custom_env_live_scratch_before_verification_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _snapshot(tmp_path)
    scratch = tmp_path / "custom-live-store"
    scratch.mkdir(mode=0o700)
    monkeypatch.setenv("AGENT_SENTINEL_STORE_DIR", str(scratch))

    def unexpected_verify(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("configured live scratch must not reach verification")

    monkeypatch.setattr(runner.VerifiedSnapshot, "verify", unexpected_verify)
    with pytest.raises(
        runner.ParityRunnerRefusal,
        match="scratch-root.*disjoint.*configured live store",
    ):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert list(scratch.iterdir()) == []


def test_runner_scratch_exchange_cannot_redirect_run_creation_into_live_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _snapshot(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    moved_scratch = tmp_path / "scratch-original"
    synthetic_live = tmp_path / "synthetic-live"
    synthetic_live.mkdir(mode=0o700)
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(synthetic_live))
    real_mkdir = safe_scratch.os.mkdir
    exchanged = False

    def exchange_before_run_mkdir(
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal exchanged
        if not exchanged and str(path).startswith("legacy-snapshot-parity-"):
            exchanged = True
            scratch.rename(moved_scratch)
            scratch.symlink_to(synthetic_live, target_is_directory=True)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_scratch.os, "mkdir", exchange_before_run_mkdir)

    with pytest.raises(runner.ParityRunnerRefusal):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert list(synthetic_live.iterdir()) == []
    assert exchanged is True


def test_runner_exchange_cannot_redirect_parity_report_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _snapshot(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    moved_scratch = tmp_path / "scratch-original"
    synthetic_live = tmp_path / "synthetic-live"
    synthetic_live.mkdir(mode=0o700)
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(synthetic_live))
    real_writer = safe_scratch.AnchoredRunDirectory.atomic_write_json
    exchanged = False

    def exchange_before_report_write(
        directory: safe_scratch.AnchoredRunDirectory,
        name: str,
        value: dict[str, object],
    ) -> None:
        nonlocal exchanged
        if name == "parity-report.json" and not exchanged:
            exchanged = True
            scratch.rename(moved_scratch)
            scratch.symlink_to(synthetic_live, target_is_directory=True)
        real_writer(directory, name, value)

    monkeypatch.setattr(
        safe_scratch.AnchoredRunDirectory,
        "atomic_write_json",
        exchange_before_report_write,
    )

    with pytest.raises(safe_scratch.ScratchSafetyError):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert list(synthetic_live.iterdir()) == []
    assert exchanged is True


def test_runner_refuses_scratch_ancestor_of_snapshot_before_creating_candidate(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    root, manifest = _snapshot(
        tmp_path,
        root_parent=scratch,
    )

    with pytest.raises(
        runner.ParityRunnerRefusal,
        match="must be disjoint",
    ):
        runner.run_parity(
            _arguments(scratch=scratch, root=root, manifest=manifest)
        )

    assert [path.name for path in scratch.iterdir()] == ["sealed-legacy"]
