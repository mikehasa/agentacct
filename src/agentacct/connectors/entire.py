"""Read-only Entire checkpoint evidence from public Git objects.

The adapter invokes only bounded, non-mutating Git plumbing.  It reads known
public refs, exact commit trailers, and allowlisted ``metadata.json`` scalar
fields.  Prompt, transcript, message, log, and JSONL blobs are never opened.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .base import ConnectorError, ConnectorRecord, ReadOnlyConnector, stable_digest


ENTIRE_UPSTREAM_SHA = "7cd6662805fbd525f2f418ecf465a247b924af70"
ENTIRE_LICENSE = "MIT"

_REF_PATTERNS = (
    "refs/heads/entire/checkpoints/v1",
    "refs/remotes/origin/entire/checkpoints/v1",
    "refs/entire/checkpoints/",
)
_TRAILER = re.compile(r"^(Entire-(?:Checkpoint|Session|Strategy)):\s*(\S.*?)\s*$", re.IGNORECASE)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_FORBIDDEN_PATH_PARTS = frozenset(
    {
        "prompt",
        "prompts",
        "transcript",
        "transcripts",
        "message",
        "messages",
        "conversation",
        "conversations",
        "log",
        "logs",
        "content",
        "contents",
    }
)
_MAX_GIT_OUTPUT = 8 * 1024 * 1024
_MAX_METADATA_BLOB = 1024 * 1024


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if _SAFE_ID.fullmatch(text) else None


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (str, bool, int)):
        if isinstance(value, str) and (not value.strip() or len(value) > 256):
            return None
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return value
    return None


def _lookup(document: Mapping[str, Any], *names: str) -> Any:
    containers: list[Mapping[str, Any]] = [document]
    for container_name in ("checkpoint", "session", "stats", "usage", "token_usage", "tokenUsage"):
        nested = document.get(container_name)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for name in names:
            if name in container:
                return container[name]
    return None


def _has_key(document: Mapping[str, Any], *names: str) -> bool:
    containers: list[Mapping[str, Any]] = [document]
    for container_name in ("checkpoint", "session", "stats", "usage", "token_usage", "tokenUsage"):
        nested = document.get(container_name)
        if isinstance(nested, Mapping):
            containers.append(nested)
    return any(name in container for container in containers for name in names)


def _metadata_attributes(document: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], bool]:
    attributes: dict[str, Any] = {}
    subjects: dict[str, str] = {}

    checkpoint_id = _safe_identifier(_lookup(document, "checkpoint_id", "checkpointId", "id"))
    session_id = _safe_identifier(_lookup(document, "session_id", "sessionId"))
    if checkpoint_id:
        attributes["checkpoint_id"] = checkpoint_id
        subjects["artifact"] = checkpoint_id
    if session_id:
        attributes["session_id"] = session_id
        subjects["client_session"] = session_id

    scalar_fields = {
        "strategy": ("strategy",),
        "agent": ("agent", "agent_name", "agentName"),
        "model": ("model", "model_name", "modelName"),
        "created_at": ("created_at", "createdAt"),
        "updated_at": ("updated_at", "updatedAt"),
        "checkpoint_count": ("checkpoint_count", "checkpointCount"),
        "file_count": ("file_count", "fileCount", "files_changed", "filesChanged"),
        "lines_added": ("lines_added", "linesAdded", "additions"),
        "lines_removed": ("lines_removed", "linesRemoved", "deletions"),
        "total_changes": ("total_changes", "totalChanges"),
        "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
        "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "completionTokens"),
        "cache_read_tokens": ("cache_read_tokens", "cacheReadTokens"),
        "cache_write_tokens": ("cache_write_tokens", "cacheWriteTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    for canonical, names in scalar_fields.items():
        value = _safe_scalar(_lookup(document, *names))
        if value is not None:
            attributes[canonical] = value

    files_touched = _lookup(document, "files_touched", "filesTouched", "files")
    files_field_present = _has_key(document, "files_touched", "filesTouched", "files")
    if isinstance(files_touched, Sequence) and not isinstance(files_touched, (str, bytes, bytearray)):
        attributes["files_touched_count"] = len(files_touched)
    explicit_no_change = _lookup(document, "no_change", "noChange") is True
    numeric_change_fields = [
        attributes[name]
        for name in ("lines_added", "lines_removed", "total_changes")
        if name in attributes
    ]
    known_changes_are_zero = bool(numeric_change_fields) and all(
        isinstance(value, (int, float)) and value == 0 for value in numeric_change_fields
    )
    no_change = explicit_no_change or (
        files_field_present
        and attributes.get("files_touched_count") == 0
        and (known_changes_are_zero or not numeric_change_fields)
    )
    if no_change:
        attributes["no_change"] = True
        attributes["no_change_incomplete"] = True
    return attributes, subjects, no_change


class EntireGitConnector(ReadOnlyConnector):
    name = "entire"
    source_type = "git_checkpoint"
    upstream_sha = ENTIRE_UPSTREAM_SHA
    license_id = ENTIRE_LICENSE

    def __init__(self, repository: str | Path, *, max_commits: int = 100) -> None:
        self.repository = Path(repository).resolve()
        if max_commits < 1 or max_commits > 1000:
            raise ConnectorError("max_commits must be between 1 and 1000")
        self.max_commits = max_commits

    def read(self, source: Any = None) -> tuple[ConnectorRecord, ...]:
        if source is not None:
            raise ConnectorError("EntireGitConnector reads only its configured repository")
        if not self.repository.is_dir():
            raise ConnectorError(f"Git repository does not exist: {self.repository}")
        if self._git("rev-parse", "--is-inside-work-tree").strip() != "true":
            raise ConnectorError(f"not a Git worktree: {self.repository}")

        refs = self._public_refs()
        commit_refs: dict[str, set[str]] = {}
        for ref_name, _tip in refs:
            output = self._git("rev-list", f"--max-count={self.max_commits}", ref_name)
            for commit in output.splitlines():
                if re.fullmatch(r"[0-9a-f]{40,64}", commit):
                    commit_refs.setdefault(commit, set()).add(ref_name)

        records: list[ConnectorRecord] = []
        for commit in sorted(commit_refs):
            refs_for_commit = tuple(sorted(commit_refs[commit]))
            records.extend(self._read_commit(commit, refs_for_commit))
        return tuple(sorted(records, key=lambda record: record.record_id))

    def _public_refs(self) -> tuple[tuple[str, str], ...]:
        output = self._git(
            "for-each-ref",
            "--format=%(refname)%00%(objectname)",
            *_REF_PATTERNS,
        )
        refs: list[tuple[str, str]] = []
        for line in output.splitlines():
            ref_name, separator, object_name = line.partition("\x00")
            if not separator:
                continue
            if not any(ref_name == prefix or ref_name.startswith(prefix) for prefix in _REF_PATTERNS):
                continue
            if re.fullmatch(r"[0-9a-f]{40,64}", object_name):
                refs.append((ref_name, object_name))
        return tuple(sorted(set(refs)))

    def _read_commit(self, commit: str, refs: tuple[str, ...]) -> list[ConnectorRecord]:
        info = self._git(
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "-s",
            "--format=%H%x00%cI%x00%P%x00%B",
            commit,
        )
        parts = info.split("\x00", 3)
        if len(parts) != 4:
            raise ConnectorError(f"unexpected Git commit format for {commit}")
        commit_id, committed_at, parents, body = parts
        trailers = self._parse_trailers(body)
        base_attributes: dict[str, Any] = {
            "ref_names": list(refs),
            "parent_count": len(parents.split()) if parents.strip() else 0,
            "attribution_method": "git_metadata_heuristic",
            "transcript_ingested": False,
        }
        base_subjects: dict[str, str] = {"commit": commit_id}
        if trailers.get("checkpoint"):
            base_attributes["checkpoint_id"] = trailers["checkpoint"]
            base_subjects["artifact"] = trailers["checkpoint"]
        if trailers.get("session"):
            base_attributes["session_id"] = trailers["session"]
            base_subjects["client_session"] = trailers["session"]
        if trailers.get("strategy"):
            base_attributes["strategy"] = trailers["strategy"]

        records: list[ConnectorRecord] = []
        metadata_paths = self._changed_metadata_paths(commit)
        for metadata_path in metadata_paths:
            document = self._read_metadata_blob(commit, metadata_path)
            if document is None:
                continue
            metadata_attributes, metadata_subjects, no_change = _metadata_attributes(document)
            attributes = {**base_attributes, **metadata_attributes}
            subjects = {**base_subjects, **metadata_subjects}
            source_event_id = f"{commit}:{stable_digest(metadata_path)[:16]}"
            records.append(
                self._record(
                    source_event_id,
                    committed_at,
                    subjects,
                    attributes,
                    stable_digest(document),
                    no_change=no_change,
                )
            )

        if not records and trailers:
            records.append(
                self._record(
                    commit,
                    committed_at,
                    base_subjects,
                    base_attributes,
                    stable_digest({"commit": commit, "trailers": trailers}),
                    no_change=False,
                )
            )
        return records

    @staticmethod
    def _parse_trailers(body: str) -> dict[str, str]:
        output: dict[str, str] = {}
        names = {
            "entire-checkpoint": "checkpoint",
            "entire-session": "session",
            "entire-strategy": "strategy",
        }
        for line in body.splitlines():
            match = _TRAILER.fullmatch(line)
            if not match:
                continue
            value = _safe_identifier(match.group(2))
            if value:
                output[names[match.group(1).lower()]] = value
        return output

    def _changed_metadata_paths(self, commit: str) -> tuple[str, ...]:
        output = self._git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        )
        paths: list[str] = []
        for path in output.split("\x00"):
            if not path:
                continue
            pure = PurePosixPath(path)
            lowered_parts = {part.lower() for part in pure.parts}
            if pure.name.lower() != "metadata.json":
                continue
            if lowered_parts & _FORBIDDEN_PATH_PARTS:
                continue
            paths.append(path)
        return tuple(sorted(set(paths)))

    def _read_metadata_blob(self, commit: str, path: str) -> Mapping[str, Any] | None:
        object_spec = f"{commit}:{path}"
        size_text = self._git("cat-file", "-s", object_spec).strip()
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ConnectorError(f"invalid metadata blob size at {commit}") from exc
        if size < 0 or size > _MAX_METADATA_BLOB:
            return None
        text = self._git("cat-file", "blob", object_spec)
        try:
            document = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return document if isinstance(document, Mapping) else None

    def _record(
        self,
        source_event_id: str,
        occurred_at: str,
        subjects: Mapping[str, str],
        attributes: Mapping[str, Any],
        raw_digest: str,
        *,
        no_change: bool,
    ) -> ConnectorRecord:
        usage_present = any(
            key in attributes
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")
        )
        return ConnectorRecord(
            connector=self.name,
            source_type=self.source_type,
            source_instance_id=str(self.repository),
            source_event_id=source_event_id,
            event_kind="git.checkpoint.observed",
            evidence_type="observation",
            occurred_at=occurred_at,
            observed_at=occurred_at,
            measurement_basis="entire_checkpoint_observed",
            completeness="partial",
            truncation_reason=("no_change_does_not_prove_completion" if no_change else "transcript_capture_disabled"),
            subjects=subjects,
            attributes=attributes,
            usage_confidence="checkpoint_observed" if usage_present else "unknown",
            cost_confidence="unknown",
            capture_level="metadata_only",
            attribution="heuristic",
            raw_digest=raw_digest,
            upstream_sha=self.upstream_sha,
            license_id=self.license_id,
        )

    def _git(self, *arguments: str) -> str:
        environment = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
            timeout=20,
        )
        if completed.returncode != 0:
            error = completed.stderr.strip().splitlines()[-1:] or ["unknown Git error"]
            raise ConnectorError(f"read-only Git command failed: {error[0]}")
        if len(completed.stdout.encode("utf-8")) > _MAX_GIT_OUTPUT:
            raise ConnectorError("Git output exceeded connector safety bound")
        return completed.stdout
