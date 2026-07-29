from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentacct.task_identity import TASK_PUBLIC_ID_RE, TaskIdentityCodec


def _task(session_id: str = "raw-secret-session") -> dict[str, object]:
    return {
        "task_id": f"session:codex:{session_id}",
        "primary_root": {"client": "codex", "client_session_id": session_id},
        "root_keys": [{"client": "codex", "client_session_id": session_id}],
    }


def test_public_task_id_is_stable_opaque_and_store_scoped(tmp_path: Path) -> None:
    first = TaskIdentityCodec(tmp_path / "one")
    reloaded = TaskIdentityCodec(tmp_path / "one")
    other = TaskIdentityCodec(tmp_path / "two")

    public_id = first.public_id(_task())

    assert TASK_PUBLIC_ID_RE.fullmatch(public_id)
    assert "raw-secret-session" not in public_id
    assert reloaded.public_id(_task()) == public_id
    assert other.public_id(_task()) != public_id
    assert (tmp_path / "one" / "task-identity" / "secret.key").stat().st_mode & 0o777 == 0o600


def test_first_secret_creation_fsyncs_file_and_identity_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    fsynced_kinds: list[str] = []

    def recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsynced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    TaskIdentityCodec(tmp_path / "state")

    assert "file" in fsynced_kinds
    assert "directory" in fsynced_kinds


def test_continuation_keeps_primary_public_identity(tmp_path: Path) -> None:
    codec = TaskIdentityCodec(tmp_path / "state")
    before = _task("primary")
    after = {
        **before,
        "task_id": "ctask_internal",
        "root_keys": [
            {"client": "codex", "client_session_id": "primary"},
            {"client": "codex", "client_session_id": "continuation"},
        ],
    }

    assert codec.public_id(before) == codec.public_id(after)


def test_resolve_uses_only_current_projection(tmp_path: Path) -> None:
    codec = TaskIdentityCodec(tmp_path / "state")
    task = _task()
    projection = {"tasks": [task]}
    public_id = codec.public_id(task)

    resolved = codec.resolve(projection, public_id)

    assert resolved is not None
    assert resolved["public_task_id"] == public_id
    assert codec.resolve(projection, "task_" + "0" * 32) is None
