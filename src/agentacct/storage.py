from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import psutil

from .env_compat import read_env_alias


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
OWNERSHIP_SCHEMA_VERSION = "agent-chronicle.process-ownership.v1"
_LIVE_STATUSES = frozenset({"running", "paused", "cancel_requested"})
_TERMINAL_STATUSES = frozenset({"completed", "succeeded", "failed", "killed", "cancelled", "lost"})


def validate_run_id(run_id: str) -> str:
    """Validate run IDs before using them in filesystem paths.

    Run IDs are path segment names, not paths. Keep this deliberately strict so
    API/MCP/CLI callers cannot escape the local runs directory with values such
    as '../outside' or nested paths.
    """
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return run_id


@dataclass
class StoredRun:
    run_id: str
    pid: int | None
    process_group_id: int | None
    owned_by_sentinel: bool
    status: str | None = None


class RunStore:
    def __init__(self, root: Path | str, *, create: bool = True) -> None:
        if root is None:
            raise ValueError("store root is required; resolve it with agentacct.store_resolution.resolve_store_dir()")
        self.root = Path(root).expanduser()
        self.runs_root = self.root / "runs"
        if create:
            self.runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # The store root itself must be owner-only too: it holds the
            # private ledger, and the rebuild suite's activation gate
            # (_require_owner_directory_0700) refuses non-0700 roots. mkdir
            # only applies the mode to directories it creates, so normalize
            # explicitly (best-effort, like every other _owner_only call).
            for private_dir in (self.root, self.runs_root):
                try:
                    private_dir.chmod(0o700)
                except OSError:
                    pass

    def run_dir(self, run_id: str) -> Path:
        run_id = validate_run_id(run_id)
        path = (self.runs_root / run_id).resolve()
        runs_root = self.runs_root.resolve()
        if path.parent != runs_root:
            raise ValueError(f"invalid run_id: {run_id!r}")
        return path

    def create_run_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id)
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    @contextmanager
    def _metadata_locked(self, run_id: str) -> Iterator[Path]:
        run_dir = self.run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"unknown run_id: {run_id}")
        lock_path = run_dir / ".metadata.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield run_dir / "metadata.json"
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_metadata_path(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError("run metadata must be a JSON object")
        return value

    @staticmethod
    def _preserve_terminal_or_cancel_state(
        current: dict[str, Any] | None,
        proposed: dict[str, Any],
    ) -> dict[str, Any]:
        if current is None:
            return proposed
        current_status = str(current.get("status") or "").strip().lower()
        proposed_status = str(proposed.get("status") or current_status).strip().lower()
        if current_status in _TERMINAL_STATUSES and proposed_status != current_status:
            return current
        if current_status == "cancel_requested" and proposed_status in {"running", "paused"}:
            return current
        return proposed

    @staticmethod
    def _write_metadata_path(path: Path, metadata: Mapping[str, Any]) -> None:
        tmp = path.parent / f".metadata.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(dict(metadata), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def write_metadata(self, run_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        with self._metadata_locked(run_id) as path:
            current = self._read_metadata_path(path)
            proposed = self._preserve_terminal_or_cancel_state(current, dict(metadata))
            if proposed is not current:
                self._write_metadata_path(path, proposed)
            return dict(proposed)

    def update_metadata(self, run_id: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        with self._metadata_locked(run_id) as path:
            current = self._read_metadata_path(path)
            if current is None:
                raise FileNotFoundError(f"unknown run_id: {run_id}")
            proposed = {**current, **dict(updates)}
            proposed = self._preserve_terminal_or_cancel_state(current, proposed)
            if proposed is not current:
                self._write_metadata_path(path, proposed)
            return dict(proposed)

    def read_metadata(self, run_id: str) -> dict[str, Any]:
        path = self.run_dir(run_id) / "metadata.json"
        if not path.exists():
            raise FileNotFoundError(f"unknown run_id: {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _required_process_proof(metadata: dict[str, Any]) -> tuple[int, int, float, str, str, str]:
        fields = {
            "pid": metadata.get("pid"),
            "process_group_id": metadata.get("process_group_id"),
            "process_birth_time": metadata.get("process_birth_time"),
            "process_executable": metadata.get("process_executable"),
            "process_cwd": metadata.get("process_cwd"),
            "ownership_nonce": metadata.get("ownership_nonce"),
        }
        if metadata.get("ownership_schema_version") != OWNERSHIP_SCHEMA_VERSION or any(
            value is None or value == "" for value in fields.values()
        ):
            raise PermissionError("run ownership proof is incomplete; refusing to control an unverifiable process")
        try:
            pid = int(fields["pid"])
            pgid = int(fields["process_group_id"])
            birth_time = float(fields["process_birth_time"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise PermissionError("run ownership proof is malformed; refusing to control an unverifiable process") from exc
        if pid <= 0 or pgid <= 0 or birth_time <= 0:
            raise PermissionError("run ownership proof is malformed; refusing to control an unverifiable process")
        return (
            pid,
            pgid,
            birth_time,
            str(fields["process_executable"]),
            str(fields["process_cwd"]),
            str(fields["ownership_nonce"]),
        )

    def verify_owned_process(self, run_id: str) -> dict[str, Any]:
        """Return metadata only when it still identifies the exact live process.

        PID/PGID values are reusable.  agentacct therefore treats the recorded
        process birth time, executable, cwd, and random environment nonce as one
        indivisible ownership proof.  Any missing/unreadable dimension fails
        closed; callers must never fall back to signalling the recorded PGID.
        """

        metadata = self.read_metadata(run_id)
        if metadata.get("owned_by_sentinel") is not True:
            raise PermissionError(f"run {run_id} is not owned by agent-chronicle; refusing to control it")
        status = str(metadata.get("status") or "").strip().lower()
        if status in _TERMINAL_STATUSES:
            raise PermissionError(f"run {run_id} already exited; refusing to control it")
        if status not in _LIVE_STATUSES:
            raise PermissionError(f"run {run_id} has no verifiable live state; refusing to control it")
        try:
            pid, pgid, birth_time, executable, cwd, nonce = self._required_process_proof(metadata)
        except PermissionError as exc:
            raise PermissionError(f"run {run_id} {exc}") from exc
        try:
            process = psutil.Process(pid)
            live_birth_time = float(process.create_time())
            live_pgid = os.getpgid(pid)
            live_executable = str(Path(process.exe()).resolve())
            live_cwd = str(Path(process.cwd()).resolve())
            live_nonce = read_env_alias("AGENTACCT_OWNERSHIP_NONCE", process.environ())
        except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            raise PermissionError(f"run {run_id} already exited; refusing to control it") from exc
        except (OSError, psutil.AccessDenied) as exc:
            raise PermissionError(f"run {run_id} ownership proof cannot be read; refusing to control it") from exc
        expected_executable = str(Path(executable).resolve())
        expected_cwd = str(Path(cwd).resolve())
        if (
            abs(live_birth_time - birth_time) > 0.05
            or live_pgid != pgid
            or live_executable != expected_executable
            or live_cwd != expected_cwd
            or live_nonce != nonce
        ):
            raise PermissionError(f"run {run_id} process identity no longer matches; refusing to control it")
        return metadata

    def assert_owned(self, run_id: str) -> dict[str, Any]:
        metadata = self.read_metadata(run_id)
        # "owned_by_sentinel" is a frozen metadata key (pre-rename, stored forever).
        if metadata.get("owned_by_sentinel") is not True:
            raise PermissionError(f"run {run_id} is not owned by agent-chronicle; refusing to control it")
        if not metadata.get("process_group_id"):
            raise PermissionError(f"run {run_id} has no recorded process group; refusing to control it")
        return self.verify_owned_process(run_id)

    def latest_run_id(self) -> str:
        if not self.runs_root.is_dir():
            # Stores opened with create=False may have no runs/ directory at
            # all; that is the same user-visible situation as an empty one.
            raise FileNotFoundError("no runs found")
        runs = [p for p in self.runs_root.iterdir() if p.is_dir()]
        if not runs:
            raise FileNotFoundError("no runs found")
        return max(runs, key=lambda p: p.stat().st_mtime).name
