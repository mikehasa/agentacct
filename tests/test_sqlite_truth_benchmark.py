from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from agent_chronicle.canonical import safe_scratch


_HARNESS_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "sqlite_truth_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("sqlite_truth_benchmark_test_module", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = benchmark
_SPEC.loader.exec_module(benchmark)


def _write_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_action: str = "",
    hook_status: str = "passed",
    checks_ok: bool = True,
    query_budget_passed: bool = True,
    forbidden_scans: list[str] | None = None,
    import_seconds: float = 0.1,
    projection_seconds: float = 0.1,
    raise_action: str = "",
    foreign_candidate: bool = False,
    candidate_action: str = "",
) -> str:
    if forbidden_scans is None:
        forbidden_scans = []
    module_path = tmp_path / "fixture_benchmark_hook.py"
    module_path.write_text(
        f"""
import os
import sqlite3
from pathlib import Path

from agent_chronicle.canonical.sqlite import CanonicalStore


def _create_candidate(path):
    if {foreign_candidate!r}:
        connection = sqlite3.connect(path)
        try:
            connection.execute("create table imported_rows (id integer primary key) strict")
            connection.execute("insert into imported_rows values (1)")
            connection.commit()
        finally:
            connection.close()
        Path(path).chmod(0o600)
    else:
        with CanonicalStore.create(Path(path)) as store:
            assert store.quick_check()["ok"] is True

def run(request):
    source_files = request["source"].get("files", [])
    {source_action}
    {raise_action}
    _create_candidate(request["candidate_db"])
    {candidate_action}
    return {{
        "status": {hook_status!r},
        "mode": request["mode"],
        "source_kind": request["source"]["kind"],
        "source_keys": sorted(request["source"]),
        "verified_file_count": len(source_files),
        "verified_relative_paths": [item["relative_path"] for item in source_files],
        "checks": {{"ok": {checks_ok!r}}},
        "query": {{
            "query_budget_passed": {query_budget_passed!r},
            "unbounded_core_scans": {forbidden_scans!r},
        }},
        "timings": {{
            "import_seconds": {import_seconds!r},
            "projection_seconds": {projection_seconds!r},
        }},
    }}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("fixture_benchmark_hook", None)
    return "fixture_benchmark_hook:run"


def _snapshot_manifest(
    tmp_path: Path,
    snapshot_root: Path,
    *,
    declared_paths: tuple[str, ...] | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    if declared_paths is None:
        declared_paths = tuple(
            path.relative_to(snapshot_root).as_posix()
            for path in sorted(item for item in snapshot_root.rglob("*") if item.is_file())
        )
    files = []
    for relative_path in declared_paths:
        raw = (snapshot_root / relative_path).read_bytes()
        files.append(
            {
                "path": relative_path,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    payload: dict[str, object] = {
        "version": benchmark.MANIFEST_SCHEMA_VERSION,
        "kind": "codex",
        "files": files,
    }
    if extra:
        payload.update(extra)
    manifest = tmp_path / f"manifest-{len(list(tmp_path.glob('manifest-*.json')))}.json"
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return manifest


def _snapshot_argv(
    *,
    scratch: Path,
    snapshot_root: Path,
    manifest: Path,
    hook: str | None = None,
) -> list[str]:
    result = [
        "snapshot",
        "--scratch-root",
        str(scratch),
        "--snapshot-root",
        str(snapshot_root),
        "--snapshot-manifest",
        str(manifest),
    ]
    if hook is not None:
        result.extend(["--hook", hook])
    return result


def test_snapshot_mode_requires_explicit_root_and_manifest(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as missing_root:
        benchmark.build_parser().parse_args(
            [
                "snapshot",
                "--scratch-root",
                str(tmp_path.resolve()),
                "--snapshot-manifest",
                str((tmp_path / "manifest.json").resolve()),
            ]
        )
    assert missing_root.value.code == 2

    with pytest.raises(SystemExit) as missing_manifest:
        benchmark.build_parser().parse_args(
            [
                "snapshot",
                "--scratch-root",
                str(tmp_path.resolve()),
                "--snapshot-root",
                str(tmp_path.resolve()),
            ]
        )
    assert missing_manifest.value.code == 2


def test_relative_paths_are_refused_before_hook_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "state.sqlite").write_bytes(b"offline-copy")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)

    normal_exit = benchmark.main(
        [
            "normal",
            "--scratch-root",
            "relative-scratch",
            "--hook",
            "module_that_must_not_be_imported:run",
        ]
    )
    assert normal_exit == 2
    assert "absolute path" in json.loads(capsys.readouterr().err)["error"]

    snapshot_exit = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=Path("relative-snapshot"),
            manifest=manifest.resolve(),
            hook="module_that_must_not_be_imported:run",
        )
    )
    assert snapshot_exit == 2
    assert "absolute path" in json.loads(capsys.readouterr().err)["error"]
    assert list(scratch.iterdir()) == []


def test_manifest_refuses_trusted_copy_assertions_exactly_and_without_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "state.sqlite").write_bytes(b"offline-copy")
    manifest = _snapshot_manifest(
        tmp_path,
        snapshot_root,
        extra={"verified_copy": True, "live_source": False},
    )

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook="module_that_must_not_be_imported:run",
        )
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload == {
        "status": "refused",
        "error": "manifest contains unknown fields: live_source, verified_copy",
    }
    assert list(scratch.iterdir()) == []


def test_live_chronicle_snapshot_path_is_refused_before_file_reads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / ".agent-sentinel" / "state" / "snapshot"
    snapshot_root.mkdir(parents=True)
    (snapshot_root / "events.jsonl").write_bytes(b"synthetic")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
        )
    )

    assert exit_code == 2
    assert ".agent-sentinel" in json.loads(capsys.readouterr().err)["error"]
    assert list(scratch.iterdir()) == []


def test_known_live_codex_root_is_refused_before_file_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_home = tmp_path / "home"
    snapshot_root = fake_home / ".codex"
    snapshot_root.mkdir(parents=True)
    (snapshot_root / "state.sqlite").write_bytes(b"must-not-be-read")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    monkeypatch.setenv("HOME", str(fake_home))

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook="module_that_must_not_be_imported:run",
        )
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == (
        "--snapshot-root must be an offline copy, not a live Codex state path"
    )
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("variable", "protected_argument"),
    (
        ("AGENT_CHRONICLE_STORE_DIR", "root"),
        ("AGENT_SENTINEL_STORE_DIR", "manifest"),
    ),
)
def test_configured_chronicle_snapshot_paths_are_refused_before_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    protected_argument: str,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "offline-snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "state.sqlite").write_bytes(b"must-not-be-read")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    protected = snapshot_root if protected_argument == "root" else manifest
    monkeypatch.setenv(variable, str(protected))

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook="module_that_must_not_be_imported:run",
        )
    )

    assert exit_code == 2
    assert "disjoint from every configured live state root" in json.loads(
        capsys.readouterr().err
    )["error"]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    "variable",
    ("AGENT_CHRONICLE_STORE_DIR", "AGENT_SENTINEL_STORE_DIR"),
)
def test_benchmark_scratch_rejects_configured_live_root_ancestor_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv(variable, str(scratch / "nested-live-store"))

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "1",
        ]
    )

    assert exit_code == 2
    assert "disjoint from every configured live state root" in json.loads(
        capsys.readouterr().err
    )["error"]
    assert list(scratch.iterdir()) == []


def test_scratch_name_exchange_cannot_redirect_run_creation_into_live_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    moved_scratch = tmp_path / "scratch-original"
    synthetic_live = tmp_path / "synthetic-live"
    synthetic_live.mkdir()
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
        if not exchanged and str(path).startswith("sqlite-truth-benchmark-"):
            exchanged = True
            scratch.rename(moved_scratch)
            scratch.symlink_to(synthetic_live, target_is_directory=True)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_scratch.os, "mkdir", exchange_before_run_mkdir)

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "1",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err)["status"] == "refused"
    assert list(synthetic_live.iterdir()) == []
    assert exchanged is True


def test_post_creation_exchange_cannot_redirect_normal_corpus_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    moved_scratch = tmp_path / "scratch-original"
    synthetic_live = tmp_path / "synthetic-live"
    synthetic_live.mkdir()
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(synthetic_live))
    real_writer = benchmark._write_anchored_normal_corpus
    exchanged = False

    def exchange_before_corpus_write(*args: object, **kwargs: object) -> object:
        nonlocal exchanged
        exchanged = True
        scratch.rename(moved_scratch)
        scratch.symlink_to(synthetic_live, target_is_directory=True)
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(
        benchmark,
        "_write_anchored_normal_corpus",
        exchange_before_corpus_write,
    )

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "1",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err)["status"] == "refused"
    assert list(synthetic_live.iterdir()) == []
    assert exchanged is True


def test_post_creation_exchange_cannot_redirect_result_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    moved_scratch = tmp_path / "scratch-original"
    synthetic_live = tmp_path / "synthetic-live"
    synthetic_live.mkdir()
    monkeypatch.setenv("AGENT_CHRONICLE_STORE_DIR", str(synthetic_live))
    real_writer = safe_scratch.AnchoredRunDirectory.atomic_write_json
    exchanged = False

    def exchange_before_result_write(
        directory: safe_scratch.AnchoredRunDirectory,
        name: str,
        value: dict[str, object],
    ) -> None:
        nonlocal exchanged
        if name == "result.json" and not exchanged:
            exchanged = True
            scratch.rename(moved_scratch)
            scratch.symlink_to(synthetic_live, target_is_directory=True)
        real_writer(directory, name, value)

    monkeypatch.setattr(
        safe_scratch.AnchoredRunDirectory,
        "atomic_write_json",
        exchange_before_result_write,
    )

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "1",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err)["status"] == "refused"
    assert list(synthetic_live.iterdir()) == []
    assert exchanged is True


def test_snapshot_tree_must_exactly_match_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "declared.sqlite").write_bytes(b"declared")
    (snapshot_root / "unlisted.jsonl").write_bytes(b"unverified")
    manifest = _snapshot_manifest(
        tmp_path,
        snapshot_root,
        declared_paths=("declared.sqlite",),
    )

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook="module_that_must_not_be_imported:run",
        )
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert "exactly match" in payload["error"]
    assert list(scratch.iterdir()) == []


def test_hardlinked_snapshot_payload_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    source = snapshot_root / "state.sqlite"
    source.write_bytes(b"offline-copy")
    os.link(source, snapshot_root / "alias.sqlite")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
        )
    )

    assert exit_code == 2
    assert "hard-linked" in json.loads(capsys.readouterr().err)["error"]
    assert list(scratch.iterdir()) == []


def test_symlinked_snapshot_ancestor_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_parent = tmp_path / "real-parent"
    snapshot_root = real_parent / "snapshot"
    snapshot_root.mkdir(parents=True)
    (snapshot_root / "state.sqlite").write_bytes(b"offline-copy")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=linked_parent / "snapshot",
            manifest=manifest.resolve(),
        )
    )

    assert exit_code == 2
    assert "symlink components" in json.loads(capsys.readouterr().err)["error"]
    assert list(scratch.iterdir()) == []


def test_normal_mode_records_timings_rss_and_candidate_under_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hook = _write_hook(tmp_path, monkeypatch)

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "12",
            "--hook",
            hook,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["execution_status"] == "passed"
    assert payload["acceptance_status"] == "passed"
    assert payload["source"]["record_count"] == 12
    assert payload["clock"]["source"] == "time.perf_counter"
    assert payload["clock"]["seconds"] >= 0
    assert {phase["name"] for phase in payload["timings"]} >= {
        "generate_normal_corpus",
        "canonical_hook",
        "candidate_quick_check",
    }
    hook_rss = payload["resource"]["hook_process_max_rss"]
    assert hook_rss["bytes"] > 0
    assert hook_rss["measurement_scope"] == "single_fresh_hook_process"
    assert payload["candidate"]["quick_check"] == "ok"
    assert payload["candidate"]["closed_main_db_size_bytes"] == (
        payload["candidate"]["size_bytes"]
    )
    assert payload["candidate"]["size_scope"] == "closed_main_database_file"
    assert payload["hook_result"]["source_kind"] == "synthetic_normal"
    assert payload["acceptance"]["passed"] is True
    assert set(payload["acceptance"]["items"]) == {
        "hook_status",
        "hook_checks",
        "query_budget",
        "canonical_scan",
        "normal_database_size_amplification",
    }
    size_gate = payload["acceptance"]["items"][
        "normal_database_size_amplification"
    ]
    assert size_gate["applicable"] is False
    assert size_gate["passed"] is True

    result_path = Path(payload["result_path"])
    assert result_path.resolve().is_relative_to(scratch.resolve())
    assert json.loads(result_path.read_text(encoding="utf-8"))["status"] == "passed"
    assert (result_path.parent / "candidate.sqlite3").is_file()
    assert (result_path.parent / "normal-corpus.jsonl").is_file()


def test_snapshot_mode_passes_only_manifest_declared_verified_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "offline-codex-snapshot"
    snapshot_root.mkdir()
    first = snapshot_root / "state-copy.sqlite"
    second = snapshot_root / "sessions" / "rollout.jsonl"
    second.parent.mkdir()
    first.write_bytes(b"offline-copy")
    second.write_bytes(b"{}\n")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    hook = _write_hook(tmp_path, monkeypatch)

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook=hook,
        )
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["manifest"] == {
        "version": benchmark.MANIFEST_SCHEMA_VERSION,
        "kind": "codex",
        "file_count": 2,
        "known_live_path": False,
        "manifest_integrity_verified": True,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "total_size_bytes": len(b"offline-copy") + len(b"{}\n"),
    }
    assert "root" not in payload["hook_result"]["source_keys"]
    assert "verified_copy" not in payload["hook_result"]["source_keys"]
    assert "live_source" not in payload["hook_result"]["source_keys"]
    assert payload["hook_result"]["verified_relative_paths"] == [
        "sessions/rollout.jsonl",
        "state-copy.sqlite",
    ]
    phase_names = {phase["name"] for phase in payload["timings"]}
    assert {
        "verify_snapshot_manifest",
        "validate_candidate_target",
        "verify_snapshot_unchanged",
    } <= phase_names
    assert payload["acceptance"]["items"]["snapshot_import_projection"] == {
        "required": True,
        "passed": True,
        "threshold_seconds": 30.0,
        "observed_seconds": pytest.approx(0.2),
        "components": {
            "import_seconds": pytest.approx(0.1),
            "projection_seconds": pytest.approx(0.1),
        },
    }
    assert payload["acceptance"]["items"]["snapshot_max_rss"]["passed"] is True


@pytest.mark.parametrize(
    "source_action, expected_error",
    [
        (
            'Path(source_files[0]["path"]).write_bytes(b"mutated-source")',
            "mismatch",
        ),
        (
            'source = Path(source_files[0]["path"]); '
            'replacement = source.with_name("replacement.tmp"); '
            "replacement.write_bytes(source.read_bytes()); "
            "os.replace(replacement, source)",
            "identity changed after verification",
        ),
    ],
    ids=("mutation", "replacement"),
)
def test_snapshot_mutation_or_replacement_during_hook_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_action: str,
    expected_error: str,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "offline-snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "events.jsonl").write_bytes(b"original-source")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    hook = _write_hook(tmp_path, monkeypatch, source_action=source_action)

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook=hook,
        )
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "refused"
    assert expected_error in payload["error"]
    run_dirs = list(scratch.iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "result.json").exists()


def test_snapshot_mutation_then_hook_exception_is_still_a_safety_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "offline-snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "events.jsonl").write_bytes(b"original-source")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    hook = _write_hook(
        tmp_path,
        monkeypatch,
        source_action='Path(source_files[0]["path"]).write_bytes(b"mutated-source")',
        raise_action='raise ValueError("synthetic hook boom")',
    )

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook=hook,
        )
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["status"] == "refused"
    assert "mismatch" in payload["error"]


@pytest.mark.parametrize(
    "hook_options, expected_error",
    [
        (
            {"foreign_candidate": True},
            "candidate database is not a valid canonical candidate",
        ),
        (
            {
                "candidate_action": (
                    'os.link(request["candidate_db"], '
                    'Path(request["candidate_db"]).with_name("candidate-alias.sqlite3"))'
                )
            },
            "candidate database must be a unique regular non-symlink file",
        ),
        (
            {
                "candidate_action": (
                    'Path(request["candidate_db"]).chmod(0o644)'
                )
            },
            "candidate database must be owner-only (0600)",
        ),
    ],
    ids=("foreign-sqlite", "hardlink", "public-readable"),
)
def test_candidate_validation_rejects_noncanonical_or_hardlinked_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_options: dict[str, object],
    expected_error: str,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hook = _write_hook(tmp_path, monkeypatch, **hook_options)

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "6",
            "--hook",
            hook,
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["status"] == "error"
    assert payload["execution_status"] == "error"
    assert payload["error"]["message"] == expected_error


def test_candidate_validation_rejects_database_replaced_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    candidate = run_dir / "candidate.sqlite3"
    opened_inode = run_dir / "opened-inode.sqlite3"
    replacement = run_dir / "replacement.sqlite3"
    benchmark.CanonicalStore.create(candidate).close()
    benchmark.CanonicalStore.create(replacement).close()
    real_close = benchmark.CanonicalStore.close
    armed = True

    def replacing_close(store: object) -> None:
        nonlocal armed
        real_close(store)
        if armed and store.path == candidate:
            armed = False
            candidate.replace(opened_inode)
            replacement.replace(candidate)

    monkeypatch.setattr(benchmark.CanonicalStore, "close", replacing_close)

    with pytest.raises(benchmark.BenchmarkFailure, match="changed during verification"):
        benchmark._verify_candidate_database(candidate, run_dir=run_dir)


def test_hook_timeout_is_bounded_and_reaps_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hook = _write_hook(
        tmp_path,
        monkeypatch,
        source_action='__import__("time").sleep(10)',
    )
    waited_pids: list[int] = []
    real_wait4 = benchmark.os.wait4

    def recording_wait4(process_id: int, options: int):
        result = real_wait4(process_id, options)
        waited_pids.append(result[0])
        return result

    monkeypatch.setattr(benchmark.os, "wait4", recording_wait4)
    started = benchmark.time.monotonic()

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "6",
            "--hook",
            hook,
            "--hook-timeout-seconds",
            "0.05",
        ]
    )

    assert benchmark.time.monotonic() - started < 2
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["cause_type"] == "HookTimeout"
    assert payload["error"]["message"] == "benchmark hook exceeded 0.05s wall timeout"
    assert len(waited_pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(waited_pids[0], os.WNOHANG)


@pytest.mark.parametrize(
    "hook_options, failed_gate",
    [
        ({"hook_status": "failed"}, "hook_status"),
        ({"checks_ok": False}, "hook_checks"),
        ({"query_budget_passed": False}, "query_budget"),
        ({"forbidden_scans": ["SCAN facts"]}, "canonical_scan"),
    ],
    ids=("hook-status", "checks", "query-budget", "canonical-scan"),
)
def test_normal_required_gate_failure_is_nonzero_and_retains_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_options: dict[str, object],
    failed_gate: str,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hook = _write_hook(tmp_path, monkeypatch, **hook_options)

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "6",
            "--hook",
            hook,
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["execution_status"] == "passed"
    assert payload["acceptance_status"] == "failed"
    assert payload["acceptance"]["items"][failed_gate]["passed"] is False
    result_path = Path(payload["result_path"])
    retained = json.loads(result_path.read_text(encoding="utf-8"))
    assert retained["acceptance_status"] == "failed"
    assert retained["acceptance"]["items"][failed_gate]["passed"] is False


def test_hook_exception_is_structured_benchmark_failure_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hook = _write_hook(
        tmp_path,
        monkeypatch,
        raise_action='raise ValueError("synthetic hook boom")',
    )

    exit_code = benchmark.main(
        [
            "normal",
            "--scratch-root",
            str(scratch.resolve()),
            "--records",
            "6",
            "--hook",
            hook,
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    payload = json.loads(captured.err)
    assert payload["status"] == "error"
    assert payload["execution_status"] == "error"
    assert payload["acceptance_status"] == "not_evaluated"
    assert payload["error_type"] == "BenchmarkFailure"
    assert payload["error"] == {
        "type": "BenchmarkFailure",
        "cause_type": "ValueError",
        "message": "benchmark hook raised ValueError: synthetic hook boom",
    }
    result_path = Path(payload["result_path"])
    assert result_path.is_file()
    assert json.loads(result_path.read_text(encoding="utf-8"))[
        "execution_status"
    ] == "error"


def test_snapshot_import_projection_gate_is_required_and_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "offline-snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "state.sqlite").write_bytes(b"offline-copy")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    hook = _write_hook(
        tmp_path,
        monkeypatch,
        import_seconds=29.5,
        projection_seconds=0.6,
    )

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook=hook,
        )
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    gate = payload["acceptance"]["items"]["snapshot_import_projection"]
    assert gate["required"] is True
    assert gate["passed"] is False
    assert gate["threshold_seconds"] == 30.0
    assert gate["observed_seconds"] == pytest.approx(30.1)
    assert Path(payload["result_path"]).is_file()


def test_snapshot_rss_gate_uses_fresh_hook_process_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot_root = tmp_path / "offline-snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "state.sqlite").write_bytes(b"offline-copy")
    manifest = _snapshot_manifest(tmp_path, snapshot_root)
    hook = _write_hook(tmp_path, monkeypatch)
    real_runner = benchmark._run_hook_in_fresh_process

    def over_budget_runner(*args: object, **kwargs: object):
        hook_result, rss = real_runner(*args, **kwargs)
        return hook_result, {
            **rss,
            "bytes": benchmark.SNAPSHOT_MAX_RSS_BYTES + 1,
        }

    monkeypatch.setattr(benchmark, "_run_hook_in_fresh_process", over_budget_runner)

    exit_code = benchmark.main(
        _snapshot_argv(
            scratch=scratch.resolve(),
            snapshot_root=snapshot_root.resolve(),
            manifest=manifest.resolve(),
            hook=hook,
        )
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    gate = payload["acceptance"]["items"]["snapshot_max_rss"]
    assert gate == {
        "required": True,
        "passed": False,
        "threshold_bytes": 512 * 1024 * 1024,
        "observed_bytes": 512 * 1024 * 1024 + 1,
        "measurement_scope": "single_fresh_hook_process",
    }
    assert Path(payload["result_path"]).is_file()


def test_default_hook_is_available() -> None:
    assert callable(benchmark._load_hook(benchmark.DEFAULT_HOOK))


def test_normal_database_size_amplification_is_a_required_acceptance_gate() -> None:
    source_bytes = benchmark.NORMAL_SIZE_GATE_MIN_SOURCE_BYTES
    acceptance = benchmark._acceptance_gates(
        mode="normal",
        hook_result={
            "status": "passed",
            "checks": {"ok": True},
            "query": {
                "query_budget_passed": True,
                "unbounded_core_scans": [],
            },
        },
        hook_process_rss={},
        source={"size_bytes": source_bytes, "record_count": 6_000},
        candidate={
            "closed_main_db_size_bytes": (
                source_bytes * benchmark.NORMAL_SIZE_AMPLIFICATION_MAX_RATIO + 1
            )
        },
    )

    gate = acceptance["items"]["normal_database_size_amplification"]
    assert gate["required"] is True
    assert gate["applicable"] is True
    assert gate["threshold_ratio"] == 2.0
    assert gate["passed"] is False
    assert acceptance["passed"] is False
