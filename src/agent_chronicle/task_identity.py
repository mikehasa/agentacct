"""Opaque, store-scoped public identities for projected Tasks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any, Mapping


TASK_PUBLIC_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
TASK_IDENTITY_DIRNAME = "task-identity"
TASK_IDENTITY_SECRET_FILENAME = "secret.key"


def _owner_only(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


class TaskIdentityCodec:
    """Derive stable opaque ids without exposing raw client session ids.

    Observed Tasks use the primary root session as the stable boundary. Explicit
    continuation links keep that primary root, so the public id survives normal
    link/rename operations. Planned Tasks may supply their already-opaque
    ``task_record_id``.
    """

    def __init__(self, store_dir: Path | str) -> None:
        self.store_dir = Path(store_dir).expanduser().resolve()
        self.identity_root = self.store_dir / TASK_IDENTITY_DIRNAME
        self.secret_path = self.identity_root / TASK_IDENTITY_SECRET_FILENAME
        self._secret = self._load_or_create_secret()

    def _load_or_create_secret(self) -> bytes:
        self.identity_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.identity_root, 0o700)
        try:
            secret = self.secret_path.read_bytes()
        except FileNotFoundError:
            secret = secrets.token_bytes(32)
            temporary = self.identity_root / f".{self.secret_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secret)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, self.secret_path)
                except FileExistsError:
                    secret = self.secret_path.read_bytes()
                _owner_only(self.secret_path, 0o600)
            finally:
                temporary.unlink(missing_ok=True)
            # The secret is the root of every public Task URL in this store.
            # fsyncing only its bytes does not make the new directory entry
            # crash-durable; losing that entry would silently rotate every URL
            # after a power loss.  Persist both the final hard link and temp
            # cleanup before returning the codec.
            directory_fd = os.open(self.identity_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if len(secret) != 32:
            raise ValueError("Task identity secret is corrupt; expected 32 bytes")
        _owner_only(self.secret_path, 0o600)
        return secret

    @staticmethod
    def _primary_material(task: Mapping[str, Any]) -> bytes:
        planned = str(task.get("task_record_id") or "")
        if TASK_PUBLIC_ID_RE.fullmatch(planned):
            return f"planned\0{planned}".encode("utf-8")
        primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
        client = str(primary.get("client") or "")
        session_id = str(primary.get("client_session_id") or "")
        if not client or not session_id:
            roots = task.get("root_keys") if isinstance(task.get("root_keys"), list) else []
            root = roots[0] if roots and isinstance(roots[0], Mapping) else {}
            client = str(root.get("client") or "")
            session_id = str(root.get("client_session_id") or "")
        if not client or not session_id:
            # The internal projection key may contain a raw id, but it is only
            # HMAC input and is never returned in the public id.
            internal = str(task.get("task_id") or "")
            if not internal:
                raise ValueError("Task has no stable identity material")
            return f"internal\0{internal}".encode("utf-8")
        return json.dumps(
            {"client": client, "client_session_id": session_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def public_id(self, task: Mapping[str, Any]) -> str:
        planned = str(task.get("task_record_id") or "")
        if TASK_PUBLIC_ID_RE.fullmatch(planned):
            return planned
        digest = hmac.new(self._secret, self._primary_material(task), hashlib.sha256).hexdigest()
        return "task_" + digest[:32]

    def decorate_projection(self, projection: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(projection)
        tasks = value.get("tasks") if isinstance(value.get("tasks"), list) else []
        decorated: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            row = dict(task)
            row["public_task_id"] = self.public_id(task)
            decorated.append(row)
        value["tasks"] = decorated
        return value

    def resolve(self, projection: Mapping[str, Any], public_id: str) -> dict[str, Any] | None:
        if not TASK_PUBLIC_ID_RE.fullmatch(public_id):
            return None
        tasks = projection.get("tasks") if isinstance(projection.get("tasks"), list) else []
        match: dict[str, Any] | None = None
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            candidate = self.public_id(task)
            if hmac.compare_digest(candidate, public_id):
                match = dict(task)
                match["public_task_id"] = candidate
        return match


__all__ = ["TASK_PUBLIC_ID_RE", "TaskIdentityCodec"]
