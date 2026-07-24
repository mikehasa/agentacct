from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "gate1_evidence_binding.py"
)
SPEC = importlib.util.spec_from_file_location(
    "gate1_evidence_binding_test_module",
    RUNNER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
binding = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = binding
SPEC.loader.exec_module(binding)


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "gate1@example.invalid")
    _git(root, "config", "user.name", "Gate 1 Test")
    (root / "src").mkdir()
    (root / "src" / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "runner.py").write_bytes(b"runner-v1\x00\xff")
    _git(root, "add", "src/alpha.py", "runner.py")
    _git(root, "commit", "--quiet", "-m", "source")
    return root, _git(root, "rev-parse", "HEAD")


def test_binding_proves_commit_bytes_and_allows_an_evidence_only_descendant(
    tmp_path: Path,
) -> None:
    root, source_commit = _repository(tmp_path)
    paths = ("runner.py", "src/alpha.py")

    first = binding.build_source_binding(
        repo_root=root,
        source_commit=source_commit,
        scope_paths=paths,
    )
    (root / "evidence.json").write_text(
        json.dumps({"source_commit": source_commit}),
        encoding="utf-8",
    )
    _git(root, "add", "evidence.json")
    _git(root, "commit", "--quiet", "-m", "evidence")
    descendant = binding.build_source_binding(
        repo_root=root,
        source_commit=source_commit,
        scope_paths=reversed(paths),
    )

    assert first == descendant
    assert first["source_commit"] == source_commit
    assert first["scope_files"] == sorted(paths)
    assert first["scope_file_count"] == 2
    assert len(first["source_tree_sha256"]) == 64
    assert first["source_commit_is_ancestor_of_head"] is True
    assert first["worktree_matches_source_commit"] is True


def test_binding_refuses_changed_scoped_bytes(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    (root / "src" / "alpha.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(binding.EvidenceBindingError, match="differ"):
        binding.build_source_binding(
            repo_root=root,
            source_commit=source_commit,
            scope_paths=("src/alpha.py", "runner.py"),
        )


def test_binding_refuses_intermediate_worktree_symlink(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    original = root / "src"
    relocated = root / "relocated-src"
    original.rename(relocated)
    original.symlink_to(relocated, target_is_directory=True)

    with pytest.raises(binding.EvidenceBindingError, match="symlink"):
        binding.build_source_binding(
            repo_root=root,
            source_commit=source_commit,
            scope_paths=("src/alpha.py", "runner.py"),
        )


@pytest.mark.parametrize(
    "paths",
    (
        (),
        ("/absolute.py",),
        ("../escape.py",),
        ("src/./alpha.py",),
        ("src\\alpha.py",),
        ("src/alpha.py", "src/alpha.py"),
    ),
)
def test_binding_paths_fail_closed(paths: tuple[str, ...]) -> None:
    with pytest.raises(binding.EvidenceBindingError):
        binding._canonical_scope_paths(paths)


def _sealed_binding(root: Path, source_commit: str) -> dict[str, object]:
    patterns = ["src/*.py", "runner.py"]
    scope = [
        line
        for line in _git(root, "ls-files", "--", *patterns).splitlines()
        if line
    ]
    built = binding.build_source_binding(
        repo_root=root,
        source_commit=source_commit,
        scope_paths=scope,
    )
    return {
        "schema_version": built["schema_version"],
        "source_commit": source_commit,
        "source_tree_sha256": built["source_tree_sha256"],
        "scope_file_count": built["scope_file_count"],
        "scope_git_ls_files_patterns": patterns,
    }


def test_verify_binding_survives_scoped_changes_after_sealing(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    sealed = _sealed_binding(root, source_commit)
    (root / "src" / "alpha.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "commit", "--quiet", "-am", "post-seal change")

    verified = binding.verify_source_binding(repo_root=root, binding=sealed)

    assert verified["source_tree_sha256"] == sealed["source_tree_sha256"]
    assert verified["scope_file_count"] == 2
    assert verified["source_commit_is_ancestor_of_head"] is True
    with pytest.raises(binding.EvidenceBindingError, match="differ"):
        binding.build_source_binding(
            repo_root=root,
            source_commit=source_commit,
            scope_paths=("src/alpha.py", "runner.py"),
        )


def test_verify_binding_ignores_scope_files_added_after_sealing(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    sealed = _sealed_binding(root, source_commit)
    (root / "src" / "beta.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "src/beta.py")
    _git(root, "commit", "--quiet", "-m", "add scope file after seal")

    verified = binding.verify_source_binding(repo_root=root, binding=sealed)

    assert verified["scope_file_count"] == sealed["scope_file_count"] == 2


def test_verify_binding_refuses_tampered_digest(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    sealed = _sealed_binding(root, source_commit)
    digest = str(sealed["source_tree_sha256"])
    sealed["source_tree_sha256"] = digest[:-1] + ("0" if digest[-1] != "0" else "1")

    with pytest.raises(binding.EvidenceBindingError, match="digest does not match"):
        binding.verify_source_binding(repo_root=root, binding=sealed)


def test_verify_binding_refuses_scope_count_drift(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    sealed = _sealed_binding(root, source_commit)
    sealed["scope_file_count"] = int(sealed["scope_file_count"]) + 1

    with pytest.raises(binding.EvidenceBindingError, match="count does not match"):
        binding.verify_source_binding(repo_root=root, binding=sealed)


def test_verify_binding_refuses_non_ancestor_commit(tmp_path: Path) -> None:
    root, source_commit = _repository(tmp_path)
    _git(root, "checkout", "--quiet", "-b", "side")
    (root / "src" / "alpha.py").write_text("VALUE = 9\n", encoding="utf-8")
    _git(root, "commit", "--quiet", "-am", "side change")
    side_commit = _git(root, "rev-parse", "HEAD")
    sealed = _sealed_binding(root, side_commit)
    _git(root, "checkout", "--quiet", "-")

    with pytest.raises(binding.EvidenceBindingError, match="failed closed"):
        binding.verify_source_binding(repo_root=root, binding=sealed)
