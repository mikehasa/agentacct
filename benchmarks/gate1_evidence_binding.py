#!/usr/bin/env python3
"""Bind sanitized Gate 1 evidence to an exact committed source tree.

The recovery runners intentionally keep private snapshot reports restricted to
counts and booleans.  This helper creates a separate, public-safe provenance
record for the code that produced those reports.  It verifies that an explicit
commit is an ancestor of ``HEAD`` and that every declared source file still
matches the bytes stored in that commit.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence


EVIDENCE_BINDING_SCHEMA_VERSION = "agent-chronicle.gate1-source-binding.v1"
_DIGEST_DOMAIN = b"agent-chronicle-gate1-source-tree-v1\x00"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class EvidenceBindingError(ValueError):
    """The requested source binding cannot be proved exactly."""


def _canonical_scope_paths(values: Sequence[str]) -> tuple[str, ...]:
    if not values:
        raise EvidenceBindingError("at least one source path is required")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise EvidenceBindingError("source paths must be non-empty POSIX paths")
        raw_parts = value.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise EvidenceBindingError(
                "source paths must be canonical repository-relative paths"
            )
        path = PurePosixPath(value)
        if path.is_absolute():
            raise EvidenceBindingError("source paths must be canonical repository-relative paths")
        normalized.append(path.as_posix())
    if len(set(normalized)) != len(normalized):
        raise EvidenceBindingError("source paths must be unique")
    return tuple(sorted(normalized))


def _tree_digest(
    paths: Sequence[str],
    read_bytes: Callable[[str], bytes],
) -> str:
    canonical_paths = _canonical_scope_paths(paths)
    digest = hashlib.sha256(_DIGEST_DOMAIN)
    digest.update(len(canonical_paths).to_bytes(8, "big"))
    for relative_path in canonical_paths:
        path_bytes = relative_path.encode("utf-8")
        content = read_bytes(relative_path)
        if not isinstance(content, bytes):
            raise EvidenceBindingError("source reader must return bytes")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _canonical_repo_root(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise EvidenceBindingError("repository root must be an explicit absolute path")
    try:
        resolved = path.resolve(strict=True)
        observed = resolved.stat(follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        raise EvidenceBindingError("repository root cannot be inspected") from exc
    if resolved != path or not stat.S_ISDIR(observed.st_mode):
        raise EvidenceBindingError("repository root must be a canonical directory")
    return resolved


def _run_git(repo_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceBindingError("git provenance check could not run") from exc
    if completed.returncode != 0:
        raise EvidenceBindingError("git provenance check failed closed")
    return completed.stdout


def _verify_repository(repo_root: Path) -> None:
    observed = _run_git(repo_root, ("rev-parse", "--show-toplevel"))
    try:
        top_level = Path(observed.decode("utf-8", errors="strict").strip()).resolve(
            strict=True
        )
    except (UnicodeDecodeError, OSError, RuntimeError) as exc:
        raise EvidenceBindingError("git repository root is invalid") from exc
    if top_level != repo_root:
        raise EvidenceBindingError("declared repository root is not the git top level")


def _worktree_bytes(repo_root: Path, relative_path: str) -> bytes:
    parts = PurePosixPath(relative_path).parts
    target = repo_root
    try:
        for index, part in enumerate(parts):
            target /= part
            component = target.lstat()
            if stat.S_ISLNK(component.st_mode):
                raise EvidenceBindingError(
                    "declared source path may not contain symlink components"
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(component.st_mode):
                raise EvidenceBindingError(
                    "declared source parent must be a real directory"
                )
        observed = target.lstat()
        resolved = target.resolve(strict=True)
    except EvidenceBindingError:
        raise
    except (OSError, RuntimeError) as exc:
        raise EvidenceBindingError("declared source file cannot be inspected") from exc
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise EvidenceBindingError("declared source file escapes the repository") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise EvidenceBindingError("declared source path must be a regular non-symlink file")
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise EvidenceBindingError("declared source file cannot be read") from exc


def build_source_binding(
    *,
    repo_root: str | Path,
    source_commit: str,
    scope_paths: Sequence[str],
) -> dict[str, object]:
    """Return a public-safe binding or refuse any unverifiable state."""

    root = _canonical_repo_root(repo_root)
    _verify_repository(root)
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise EvidenceBindingError("source commit must be a lowercase 40-character SHA-1")
    paths = _canonical_scope_paths(scope_paths)
    _run_git(root, ("cat-file", "-e", f"{source_commit}^{{commit}}"))
    _run_git(root, ("merge-base", "--is-ancestor", source_commit, "HEAD"))

    commit_digest = _tree_digest(
        paths,
        lambda relative_path: _run_git(
            root,
            ("show", f"{source_commit}:{relative_path}"),
        ),
    )
    worktree_digest = _tree_digest(
        paths,
        lambda relative_path: _worktree_bytes(root, relative_path),
    )
    if worktree_digest != commit_digest:
        raise EvidenceBindingError(
            "declared Gate 1 source files differ from the source commit"
        )
    return {
        "schema_version": EVIDENCE_BINDING_SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_tree_sha256": commit_digest,
        "scope_file_count": len(paths),
        "scope_files": list(paths),
        "source_commit_is_ancestor_of_head": True,
        "worktree_matches_source_commit": True,
    }


def verify_source_binding(
    *,
    repo_root: str | Path,
    binding: Mapping[str, object],
) -> dict[str, object]:
    """Retrospectively verify a sealed binding against git history.

    Proves the sealed evidence still describes exactly the bytes of its
    declared source commit and that the commit remains in the history of
    ``HEAD``. It deliberately makes no claim about the current worktree:
    dated evidence stays verifiable after the product code moves on, and
    only a fresh seal (``build_source_binding``) requires a matching
    worktree.
    """

    root = _canonical_repo_root(repo_root)
    _verify_repository(root)
    if not isinstance(binding, Mapping):
        raise EvidenceBindingError("source binding must be a mapping")
    if binding.get("schema_version") != EVIDENCE_BINDING_SCHEMA_VERSION:
        raise EvidenceBindingError("unsupported source binding schema version")
    source_commit = binding.get("source_commit")
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise EvidenceBindingError("source commit must be a lowercase 40-character SHA-1")
    patterns = binding.get("scope_git_ls_files_patterns")
    if (
        not isinstance(patterns, Sequence)
        or isinstance(patterns, (str, bytes))
        or not patterns
        or not all(isinstance(pattern, str) and pattern for pattern in patterns)
    ):
        raise EvidenceBindingError("scope patterns must be non-empty strings")
    sealed_digest = binding.get("source_tree_sha256")
    if not isinstance(sealed_digest, str) or re.fullmatch(r"[0-9a-f]{64}", sealed_digest) is None:
        raise EvidenceBindingError("sealed source tree digest must be a lowercase SHA-256")
    sealed_count = binding.get("scope_file_count")
    if not isinstance(sealed_count, int) or isinstance(sealed_count, bool) or sealed_count < 1:
        raise EvidenceBindingError("sealed scope file count must be a positive integer")
    _run_git(root, ("cat-file", "-e", f"{source_commit}^{{commit}}"))
    _run_git(root, ("merge-base", "--is-ancestor", source_commit, "HEAD"))
    listed = _run_git(root, ("ls-tree", "-r", "--name-only", "-z", source_commit))
    try:
        tracked = [item.decode("utf-8") for item in listed.split(b"\x00") if item]
    except UnicodeDecodeError as exc:
        raise EvidenceBindingError("source commit lists non-UTF-8 paths") from exc
    # fnmatch without FNM_PATHNAME (``*`` may cross ``/``) reproduces the
    # ``git ls-files -- <pattern>`` matching for the wildcard-shaped patterns
    # the seals use, evaluated against the file list of the source commit
    # rather than today's worktree. Divergent shapes (e.g. bare directory
    # pathspecs) fail closed through the count and digest pins.
    scope_paths = tuple(
        sorted(
            path
            for path in tracked
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
        )
    )
    if not scope_paths:
        raise EvidenceBindingError("scope patterns matched no files at the source commit")
    if sealed_count != len(scope_paths):
        raise EvidenceBindingError("sealed scope file count does not match the source commit")
    commit_digest = _tree_digest(
        scope_paths,
        lambda relative_path: _run_git(
            root,
            ("show", f"{source_commit}:{relative_path}"),
        ),
    )
    if commit_digest != sealed_digest:
        raise EvidenceBindingError("sealed source tree digest does not match the source commit")
    return {
        "schema_version": EVIDENCE_BINDING_SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_tree_sha256": commit_digest,
        "scope_file_count": len(scope_paths),
        "source_commit_is_ancestor_of_head": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind sanitized Gate 1 evidence to committed source bytes."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--path", action="append", dest="paths", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        binding = build_source_binding(
            repo_root=arguments.repo_root,
            source_commit=arguments.source_commit,
            scope_paths=arguments.paths,
        )
    except EvidenceBindingError:
        print(
            json.dumps({"completed": False, "refused": True}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"binding": binding, "completed": True},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
