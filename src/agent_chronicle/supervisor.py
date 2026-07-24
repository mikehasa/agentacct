"""Safe local supervisor for agentacct-owned attempts.

Only registered argv arrays are launched.  The supervisor freezes a manifest,
starts a new process group, records a multi-dimensional process fingerprint,
and performs every later signal only after the live process still matches that
fingerprint and its random ownership nonce.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import psutil

from .control_plane import (
    ControlPlaneError,
    ControlStore,
    InvalidTransition,
    RevisionConflict,
    RunAttempt,
    contract_requires_launch_approval,
)
from .storage import OWNERSHIP_SCHEMA_VERSION, RunStore


DEFAULT_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
_ACTIVE_STATES = frozenset({"launching", "running", "cancel_requested"})
_LAUNCH_SHIM = r"""
import os
import signal
import sys

gate = int(sys.argv[1])
token = os.read(gate, 1)
os.close(gate)
if token != b"1":
    raise SystemExit(126)

cancel_requested = False

def on_term(_signum, _frame):
    global cancel_requested
    cancel_requested = True

signal.signal(signal.SIGTERM, on_term)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.execv(sys.argv[2], sys.argv[2:])

while True:
    try:
        _, status = os.waitpid(child, 0)
        break
    except InterruptedError:
        continue

# During cancellation the shim deliberately remains the stable, verifiable
# process-group leader until the owning supervisor sends the final group kill.
if cancel_requested:
    while True:
        signal.pause()
if os.WIFEXITED(status):
    raise SystemExit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status):
    raise SystemExit(128 + os.WTERMSIG(status))
raise SystemExit(1)
"""


class SupervisorError(RuntimeError):
    pass


class SupervisorAlreadyRunning(SupervisorError):
    pass


@dataclass(frozen=True)
class LaunchResult:
    attempt: RunAttempt
    run_dir: Path
    manifest_path: Path


@dataclass
class _ManagedProcess:
    process: subprocess.Popen[bytes]
    stdout_handle: Any
    stderr_handle: Any
    monitor: threading.Thread | None = None


def _nonce_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _internal_key(namespace: str, *values: object) -> str:
    material = "\0".join(str(value) for value in values).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(material).hexdigest()}"


def _safe_argv(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values:
        raise SupervisorError("launch command must be a non-empty argv array")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or any(char in value for char in ("\x00", "\r", "\n")):
            raise SupervisorError("launch command contains an invalid argv element")
        result.append(value)
    if len(result) > 256:
        raise SupervisorError("launch command contains too many argv elements")
    return tuple(result)


def _path_identity(path: Path, *, hash_content: bool) -> dict[str, Any]:
    try:
        canonical = path.resolve(strict=True)
        stat_result = canonical.stat()
    except OSError as exc:
        raise SupervisorError(f"launch path is unavailable: {path}") from exc
    identity: dict[str, Any] = {
        "canonical_path": str(canonical),
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "mode": int(stat_result.st_mode),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "content_sha256": None,
    }
    if hash_content and canonical.is_file():
        digest = hashlib.sha256()
        try:
            with canonical.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise SupervisorError(f"launch file cannot be fingerprinted: {canonical}") from exc
        identity["content_sha256"] = "sha256:" + digest.hexdigest()
    return identity


def _assert_path_identity(expected: Mapping[str, Any]) -> None:
    path = Path(str(expected.get("canonical_path") or ""))
    observed = _path_identity(path, hash_content=expected.get("content_sha256") is not None)
    if observed != dict(expected):
        raise SupervisorError(f"launch path identity changed after preflight: {path}")


class SupervisorLease:
    """One owner per control store, with heartbeat and stale takeover."""

    def __init__(self, control_store: ControlStore, *, stale_after_seconds: float = 30.0) -> None:
        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be >= 1")
        self.control_store = control_store
        self.root = control_store.control_root / "supervisor"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".state.lock"
        self.stale_after_seconds = float(stale_after_seconds)
        self.lease_id = f"supervisor_{uuid.uuid4().hex}"
        self.nonce = secrets.token_urlsafe(32)
        self.pid = os.getpid()
        self.birth_time = float(psutil.Process(self.pid).create_time())
        self.acquired = False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, value: Mapping[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        tmp.replace(self.state_path)
        try:
            self.state_path.chmod(0o600)
        except OSError:
            pass
        try:
            descriptor = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _same_live_owner(state: Mapping[str, Any]) -> bool:
        try:
            process = psutil.Process(int(state["pid"]))
            return abs(float(process.create_time()) - float(state["process_birth_time"])) <= 0.05
        except (KeyError, TypeError, ValueError, OverflowError, psutil.Error):
            return False

    def acquire(self, *, now: float | None = None) -> str:
        timestamp = time.time() if now is None else float(now)
        with self._locked():
            existing = self._read()
            if existing and existing.get("state") == "running" and existing.get("lease_id") != self.lease_id:
                heartbeat = float(existing.get("heartbeat_at") or 0.0)
                fresh = timestamp - heartbeat <= self.stale_after_seconds
                if fresh and self._same_live_owner(existing):
                    raise SupervisorAlreadyRunning("an active agentacct supervisor already owns this control store")
            self._write(
                {
                    "schema_version": "agent-chronicle.supervisor-lease.v1",
                    "state": "running",
                    "lease_id": self.lease_id,
                    "pid": self.pid,
                    "process_birth_time": self.birth_time,
                    "nonce_hash": _nonce_hash(self.nonce),
                    "started_at": timestamp,
                    "heartbeat_at": timestamp,
                }
            )
            self.acquired = True
            return self.lease_id

    def heartbeat(self, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            if not state or state.get("state") != "running" or state.get("lease_id") != self.lease_id:
                self.acquired = False
                return False
            state["heartbeat_at"] = timestamp
            self._write(state)
            return True

    @staticmethod
    def _write_gate_byte(descriptor: int) -> None:
        if os.write(descriptor, b"1") != 1:
            raise OSError("launch gate write was incomplete")

    def fence_and_write_gate(self, descriptor: int, *, now: float | None = None) -> bool:
        """Validate lease ownership and release one launch gate atomically.

        Supervisor takeover uses the same flock.  Keeping it held through the
        one-byte pipe write removes the validation/write gap in which another
        supervisor could otherwise replace the lease after fencing but before
        the target process was authorized to exec.
        """

        if not self.acquired:
            return False
        timestamp = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            exact_owner = bool(
                state
                and state.get("state") == "running"
                and state.get("lease_id") == self.lease_id
                and state.get("nonce_hash") == _nonce_hash(self.nonce)
                and state.get("pid") == self.pid
                and abs(float(state.get("process_birth_time") or 0.0) - self.birth_time) <= 0.05
            )
            if not exact_owner:
                self.acquired = False
                return False
            state["heartbeat_at"] = timestamp
            self._write(state)
            self._write_gate_byte(descriptor)
            return True

    def release(self, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            if not state or state.get("lease_id") != self.lease_id:
                self.acquired = False
                return False
            state.update({"state": "stopped", "heartbeat_at": timestamp, "stopped_at": timestamp})
            self._write(state)
            self.acquired = False
            return True


class OwnedSupervisor:
    def __init__(
        self,
        store_dir: Path | str,
        *,
        control_store: ControlStore | None = None,
        env_allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        cancel_grace_seconds: float = 1.0,
        lease_stale_after_seconds: float = 30.0,
    ) -> None:
        self.store_dir = Path(store_dir).expanduser()
        self.control = control_store or ControlStore(self.store_dir)
        self.runs = RunStore(self.store_dir)
        self.env_allowlist = tuple(dict.fromkeys(str(key) for key in env_allowlist))
        if any(not key or "=" in key or "\x00" in key for key in self.env_allowlist):
            raise ValueError("environment allowlist contains an invalid key")
        if cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds must be non-negative")
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.lease = SupervisorLease(self.control, stale_after_seconds=lease_stale_after_seconds)
        self._heartbeat_interval = max(0.25, min(5.0, lease_stale_after_seconds / 3.0))
        self._heartbeat_stop = threading.Event()
        self._lease_lost = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._managed: dict[str, _ManagedProcess] = {}
        self._recovered_monitors: dict[str, threading.Thread] = {}
        self._managed_lock = threading.Lock()
        self._completion_lock = threading.Lock()

    def start(self) -> str:
        if self._lease_lost.is_set():
            raise SupervisorError("supervisor lease was lost; create a new supervisor instance")
        if self.lease.acquired:
            self._fence_lease()
            return self.lease.lease_id
        lease_id = self.lease.acquire()
        self._heartbeat_stop.clear()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"chronicle-heartbeat-{lease_id}",
            daemon=True,
        )
        self._heartbeat_thread = heartbeat
        heartbeat.start()
        return lease_id

    def close(self) -> None:
        self._heartbeat_stop.set()
        heartbeat = self._heartbeat_thread
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=max(1.0, self._heartbeat_interval * 2))
        self._heartbeat_thread = None
        # A read-only dashboard session must not create control state merely
        # because its never-started supervisor is being closed.
        if self.lease.acquired:
            self.lease.release()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            if not self.lease.heartbeat():
                self._lease_lost.set()
                return

    def _fence_lease(self) -> None:
        if self._lease_lost.is_set() or not self.lease.acquired or not self.lease.heartbeat():
            self._lease_lost.set()
            raise SupervisorError("supervisor lease was lost")

    def _ensure_lease(self) -> None:
        if not self.lease.acquired:
            self.start()
        else:
            self._fence_lease()

    @staticmethod
    def _workspace_root(canonical_root: str) -> Path:
        try:
            root = Path(canonical_root).resolve(strict=True)
        except OSError as exc:
            raise SupervisorError("registered workspace root is unavailable") from exc
        if not root.is_dir() or str(root) != canonical_root:
            raise SupervisorError("registered workspace root identity changed")
        return root

    @staticmethod
    def _git_snapshot(root: Path) -> dict[str, Any]:
        if not (root / ".git").exists():
            return {"repository": False, "head": None, "branch": None, "dirty": None}

        def run(*args: str) -> str | None:
            try:
                result = subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            return result.stdout.strip() if result.returncode == 0 else None

        status = run("status", "--porcelain=v1")
        return {
            "repository": True,
            "head": run("rev-parse", "HEAD"),
            "branch": run("branch", "--show-current"),
            "dirty": None if status is None else bool(status),
        }

    @staticmethod
    def _resolved_command(argv: Sequence[str]) -> tuple[str, ...]:
        command = list(_safe_argv(argv))
        executable = command[0]
        resolved = Path(executable).expanduser() if os.path.sep in executable else None
        if resolved is None:
            found = shutil.which(executable)
            if found is None:
                raise SupervisorError(f"agent executable is unavailable: {executable}")
            resolved = Path(found)
        try:
            canonical = resolved.resolve(strict=True)
        except OSError as exc:
            raise SupervisorError(f"agent executable is unavailable: {executable}") from exc
        if not canonical.is_file() or not os.access(canonical, os.X_OK):
            raise SupervisorError(f"agent executable is not executable: {executable}")
        command[0] = str(canonical)
        return tuple(command)

    def preflight(self, attempt_id: str) -> tuple[RunAttempt, tuple[str, ...], Path, dict[str, Any]]:
        projection = self.control.project()
        attempt = projection.attempts.get(attempt_id)
        if attempt is None:
            raise SupervisorError("attempt does not exist")
        if attempt.execution_state != "pending":
            raise SupervisorError(f"attempt is {attempt.execution_state}, not pending")
        if attempt.control_state != "ready":
            raise SupervisorError(f"attempt control state is {attempt.control_state}, not ready")
        contract = projection.contracts.get(attempt.task_id)
        agent = projection.agents.get(attempt.agent_id)
        workspace = projection.workspaces.get(attempt.workspace_id)
        if contract is None or contract.revision != attempt.contract_revision:
            raise SupervisorError("attempt contract revision is unavailable")
        if agent is None or not agent.enabled:
            raise SupervisorError("attempt agent is disabled or missing")
        if agent.revision != attempt.agent_revision:
            raise SupervisorError("attempt agent revision is unavailable")
        if agent.adapter != "local_argv" or agent.execution_backend != "subprocess":
            raise SupervisorError("attempt agent is not a supported local subprocess adapter")
        if workspace is None or not workspace.enabled or workspace.workspace_id != contract.workspace_id:
            raise SupervisorError("attempt workspace is disabled, missing, or conflicts with its contract")
        if contract_requires_launch_approval(contract.permission_envelope):
            approval_proves_release = any(
                approval.attempt_id == attempt.attempt_id
                and approval.task_id == attempt.task_id
                and approval.requested_action == "launch"
                and approval.state == "consumed"
                and approval.consumed_at is not None
                for approval in projection.approvals.values()
            )
            if not approval_proves_release:
                raise SupervisorError("approval-required attempt has no consumed launch approval")
        missing_policies = [
            policy_id for policy_id in contract.budget_policy_ids if policy_id not in projection.budget_policies
        ]
        if missing_policies:
            raise SupervisorError("attempt contract references a missing budget policy")
        budget_policies = [projection.budget_policies[policy_id].to_dict() for policy_id in contract.budget_policy_ids]
        root = self._workspace_root(workspace.canonical_root)
        command = self._resolved_command(agent.argv_template)
        argv_file_identities: list[dict[str, Any]] = []
        for index, argument in enumerate(command):
            candidate = Path(argument).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                is_file = candidate.resolve(strict=True).is_file()
            except OSError:
                is_file = False
            if is_file:
                argv_file_identities.append({"argv_index": index, **_path_identity(candidate, hash_content=True)})
        manifest_payload = {
            "task_id": attempt.task_id,
            "attempt_id": attempt.attempt_id,
            "task_contract_revision": attempt.contract_revision,
            "objective": contract.objective,
            "workspace_id": workspace.workspace_id,
            "canonical_workspace": str(root),
            "workspace_identity": _path_identity(root, hash_content=False),
            "git": self._git_snapshot(root),
            "permission_envelope": contract.permission_envelope,
            "budget_policy_ids": list(contract.budget_policy_ids),
            "budget_policies": budget_policies,
            "success_checks": list(contract.success_checks),
            "agent_id": agent.agent_id,
            "adapter": agent.adapter,
            "execution_backend": agent.execution_backend,
            "agent_revision": attempt.agent_revision,
            "argv": list(command),
            "argv_file_identities": argv_file_identities,
            "supervisor_shim_executable": str(Path(sys.executable).resolve()),
            "environment_key_names": list(self.env_allowlist)
            + ["AGENT_CHRONICLE_RUN_ID", "AGENT_CHRONICLE_RUN_DIR", "AGENT_CHRONICLE_OWNERSHIP_NONCE"],
        }
        return attempt, command, root, manifest_payload

    @staticmethod
    def _assert_preflight_identities(manifest_payload: Mapping[str, Any]) -> None:
        workspace_identity = manifest_payload.get("workspace_identity")
        argv_identities = manifest_payload.get("argv_file_identities")
        if not isinstance(workspace_identity, Mapping) or not isinstance(argv_identities, list):
            raise SupervisorError("preflight identity manifest is incomplete")
        _assert_path_identity(workspace_identity)
        for identity in argv_identities:
            if not isinstance(identity, Mapping):
                raise SupervisorError("preflight argv identity is malformed")
            expected = {key: value for key, value in identity.items() if key != "argv_index"}
            _assert_path_identity(expected)

    @staticmethod
    def _abort_blocked_process(
        process: subprocess.Popen[bytes] | None,
        handshake_write: int,
        *,
        birth_time: float | None,
        pgid: int | None,
        cwd: Path,
        nonce: str,
    ) -> None:
        """Close the launch gate and reap, signalling only an exact bootstrap."""

        if handshake_write >= 0:
            try:
                os.close(handshake_write)
            except OSError:
                pass
        if process is None:
            return
        try:
            process.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        if birth_time is None or pgid is None:
            return
        try:
            live = psutil.Process(process.pid)
            exact_match = (
                abs(float(live.create_time()) - birth_time) <= 0.05
                and os.getpgid(process.pid) == pgid
                and str(Path(live.exe()).resolve()) == str(Path(sys.executable).resolve())
                and str(Path(live.cwd()).resolve()) == str(cwd)
                and live.environ().get("AGENT_CHRONICLE_OWNERSHIP_NONCE") == nonce
            )
        except (OSError, psutil.Error):
            return
        if not exact_match:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _record_launch_failure(self, attempt_id: str, base_key: str, reason: str) -> None:
        """Best-effort durable failure without masking the launch exception."""

        try:
            self._fence_lease()
        except SupervisorError:
            return
        for _retry in range(3):
            current = self.control.project().attempts.get(attempt_id)
            if current is None or current.execution_state in {"succeeded", "failed", "cancelled", "lost"}:
                break
            if current.execution_state not in {"launching", "running", "cancel_requested"}:
                break
            try:
                current = self.control.transition_attempt(
                    attempt_id,
                    expected_revision=current.revision,
                    execution_state="failed",
                    ended_at=time.time(),
                    reason=reason,
                    idempotency_key=_internal_key("launch-failed", base_key, attempt_id, current.revision),
                    actor_kind="supervisor",
                    actor_id=self.lease.lease_id,
                )
                break
            except RevisionConflict:
                continue
            except ControlPlaneError:
                break
        try:
            self.runs.update_metadata(
                attempt_id,
                {
                    "status": "failed",
                    "ended_at": current.ended_at if current is not None else time.time(),
                    "reason": reason,
                },
            )
        except (FileNotFoundError, OSError, ValueError):
            pass

    def launch_attempt(self, attempt_id: str, *, idempotency_key: str) -> LaunchResult:
        self._ensure_lease()
        current_projection = self.control.project()
        current = current_projection.attempts.get(attempt_id)
        if current is None:
            raise SupervisorError("attempt does not exist")
        prior_launch = current_projection.idempotency.get(idempotency_key)
        if prior_launch is not None:
            if (
                prior_launch.action != "attempt_transitioned"
                or prior_launch.target_id != attempt_id
                or prior_launch.next_state != "launching"
            ):
                raise SupervisorError("idempotency key was already used for a different control operation")
            manifest_id = current.manifest_id
            if manifest_id is None:
                raise SupervisorError("idempotent launch has no manifest")
            return LaunchResult(current, self.runs.run_dir(attempt_id), self.control.manifests_root / f"{manifest_id}.json")

        attempt, command, root, manifest_payload = self.preflight(attempt_id)
        self._fence_lease()
        manifest_id = "manifest_" + hashlib.sha256(
            f"{attempt_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        manifest_path = self.control.write_manifest(manifest_id, manifest_payload)
        self._assert_preflight_identities(manifest_payload)
        launching = self.control.transition_attempt(
            attempt_id,
            expected_revision=attempt.revision,
            execution_state="launching",
            manifest_id=manifest_id,
            reason="preflight passed; process launch started",
            idempotency_key=idempotency_key,
            actor_kind="supervisor",
            actor_id=self.lease.lease_id,
        )

        run_dir = self.runs.run_dir(attempt_id)
        stdout_handle: Any | None = None
        stderr_handle: Any | None = None
        handshake_read = -1
        handshake_write = -1
        nonce = secrets.token_urlsafe(32)
        try:
            self.runs.create_run_dir(attempt_id)
            stdout_path = run_dir / "stdout.log"
            stderr_path = run_dir / "stderr.log"
            stdout_handle = stdout_path.open("wb")
            stderr_handle = stderr_path.open("wb")
            for path in (stdout_path, stderr_path):
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
            child_env = {key: os.environ[key] for key in self.env_allowlist if key in os.environ}
            child_env.update(
                {
                    "AGENT_CHRONICLE_RUN_ID": attempt_id,
                    "AGENT_CHRONICLE_RUN_DIR": str(run_dir),
                    "AGENT_CHRONICLE_OWNERSHIP_NONCE": nonce,
                    "AGENT_SENTINEL_RUN_ID": attempt_id,
                    "AGENT_SENTINEL_RUN_DIR": str(run_dir),
                }
            )
            handshake_read, handshake_write = os.pipe()
        except Exception as exc:
            for descriptor in (handshake_read, handshake_write):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()
            self._record_launch_failure(
                attempt_id,
                idempotency_key,
                f"launch setup failed: {type(exc).__name__}",
            )
            raise SupervisorError("agentacct-owned process launch failed") from exc
        assert stdout_handle is not None and stderr_handle is not None
        process: subprocess.Popen[bytes] | None = None
        birth_time: float | None = None
        pgid: int | None = None
        try:
            self._assert_preflight_identities(manifest_payload)
            process = subprocess.Popen(
                [sys.executable, "-c", _LAUNCH_SHIM, str(handshake_read), *command],
                cwd=root,
                env=child_env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
                pass_fds=(handshake_read,),
            )
            os.close(handshake_read)
            handshake_read = -1
            live = psutil.Process(process.pid)
            birth_time = float(live.create_time())
            pgid = os.getpgid(process.pid)
            # The shim remains the stable process-group leader.  This keeps the
            # ownership proof valid for direct shebang scripts and throughout
            # cancellation even when the target leader exits before children.
            executable = str(Path(sys.executable).resolve())
            process_cwd = str(Path(live.cwd()).resolve())
            if live.environ().get("AGENT_CHRONICLE_OWNERSHIP_NONCE") != nonce:
                raise SupervisorError("process launch nonce handshake failed")
            self._assert_preflight_identities(manifest_payload)
        except Exception as exc:
            if handshake_read >= 0:
                try:
                    os.close(handshake_read)
                except OSError:
                    pass
            self._abort_blocked_process(
                process,
                handshake_write,
                birth_time=birth_time,
                pgid=pgid,
                cwd=root,
                nonce=nonce,
            )
            handshake_write = -1
            stdout_handle.close()
            stderr_handle.close()
            self._record_launch_failure(attempt_id, idempotency_key, f"launch failed: {type(exc).__name__}")
            raise SupervisorError("agentacct-owned process launch failed") from exc

        assert process is not None and birth_time is not None and pgid is not None
        started_at = time.time()
        metadata = {
            "run_id": attempt_id,
            "command": list(command),
            "pid": process.pid,
            "process_group_id": pgid,
            "owned_by_sentinel": True,
            "cwd": str(root),
            "started_at": started_at,
            "status": "running",
            "ownership_schema_version": OWNERSHIP_SCHEMA_VERSION,
            "process_birth_time": birth_time,
            "process_executable": executable,
            "process_cwd": process_cwd,
            "ownership_nonce": nonce,
            "manifest_id": manifest_id,
            "supervisor_lease_id": self.lease.lease_id,
            "env": {
                "AGENT_SENTINEL_RUN_ID": attempt_id,
                "AGENT_SENTINEL_RUN_DIR": str(run_dir),
            },
        }
        managed: _ManagedProcess | None = None
        try:
            self._fence_lease()
            self.runs.write_metadata(attempt_id, metadata)
            running = self.control.transition_attempt(
                attempt_id,
                expected_revision=launching.revision,
                execution_state="running",
                manifest_id=manifest_id,
                pid=process.pid,
                process_group_id=pgid,
                process_birth_time=birth_time,
                process_executable=executable,
                process_cwd=process_cwd,
                ownership_nonce_hash=_nonce_hash(nonce),
                started_at=started_at,
                reason="process ownership handshake passed",
                idempotency_key=_internal_key("launch-running", idempotency_key, attempt_id),
                actor_kind="supervisor",
                actor_id=self.lease.lease_id,
            )
            managed = _ManagedProcess(process=process, stdout_handle=stdout_handle, stderr_handle=stderr_handle)
            monitor = threading.Thread(
                target=self._monitor,
                args=(attempt_id, managed),
                name=f"chronicle-monitor-{attempt_id}",
                daemon=True,
            )
            managed.monitor = monitor
            with self._managed_lock:
                self._managed[attempt_id] = managed
            try:
                monitor.start()
            except Exception:
                with self._managed_lock:
                    self._managed.pop(attempt_id, None)
                raise
            self._assert_preflight_identities(manifest_payload)
            if (
                self._lease_lost.is_set()
                or not self.lease.fence_and_write_gate(handshake_write)
            ):
                self._lease_lost.set()
                raise SupervisorError("supervisor lease was lost")
        except Exception as exc:
            self._abort_blocked_process(
                process,
                handshake_write,
                birth_time=birth_time,
                pgid=pgid,
                cwd=root,
                nonce=nonce,
            )
            handshake_write = -1
            if managed is not None and managed.monitor is not None and managed.monitor.is_alive():
                managed.monitor.join(timeout=1.0)
            if not stdout_handle.closed:
                stdout_handle.close()
            if not stderr_handle.closed:
                stderr_handle.close()
            self._record_launch_failure(
                attempt_id,
                idempotency_key,
                f"launch ownership commit failed: {type(exc).__name__}",
            )
            raise SupervisorError("agentacct-owned process launch failed") from exc
        try:
            os.close(handshake_write)
        except OSError:
            pass
        handshake_write = -1
        return LaunchResult(running, run_dir, manifest_path)

    def _complete_attempt(self, attempt_id: str, exit_code: int | None) -> RunAttempt:
        # Cancellation and the background monitor may observe the same exit.
        # Serialize completion so one durable terminal transition wins and the
        # other observer returns that exact projection instead of constructing
        # a second, timestamp-different idempotent request.
        try:
            self._fence_lease()
        except SupervisorError:
            return self.control.project().attempts[attempt_id]
        with self._completion_lock:
            return self._complete_attempt_locked(attempt_id, exit_code)

    def _complete_attempt_locked(self, attempt_id: str, exit_code: int | None) -> RunAttempt:
        for _retry in range(3):
            projection = self.control.project()
            current = projection.attempts[attempt_id]
            if current.execution_state in {"succeeded", "failed", "cancelled", "lost"}:
                try:
                    self.runs.update_metadata(
                        attempt_id,
                        {
                            "status": current.execution_state,
                            "exit_code": current.exit_code,
                            "ended_at": current.ended_at,
                            "reason": current.reason,
                        },
                    )
                except (FileNotFoundError, OSError, ValueError):
                    pass
                return current
            if current.execution_state == "cancel_requested":
                next_state = "cancelled"
                reason = "agentacct-owned cancellation completed"
            else:
                next_state = "succeeded" if exit_code == 0 else "failed"
                reason = "process exited"
            try:
                result = self.control.transition_attempt(
                    attempt_id,
                    expected_revision=current.revision,
                    execution_state=next_state,
                    ended_at=time.time(),
                    exit_code=exit_code,
                    reason=reason,
                    idempotency_key=f"monitor:{attempt_id}:{current.pid}:{exit_code}:{next_state}",
                    actor_kind="supervisor",
                    actor_id=self.lease.lease_id,
                )
                try:
                    self.runs.update_metadata(
                        attempt_id,
                        {
                            "status": next_state,
                            "exit_code": exit_code,
                            "ended_at": result.ended_at,
                            "reason": reason,
                        },
                    )
                except (FileNotFoundError, OSError, ValueError):
                    pass
                return result
            except RevisionConflict:
                continue
        return self.control.project().attempts[attempt_id]

    def _monitor(self, attempt_id: str, managed: _ManagedProcess) -> None:
        try:
            exit_code = managed.process.wait()
            self._complete_attempt(attempt_id, exit_code)
        finally:
            managed.stdout_handle.close()
            managed.stderr_handle.close()
            with self._managed_lock:
                self._managed.pop(attempt_id, None)

    def wait(self, attempt_id: str, *, timeout: float = 10.0) -> RunAttempt:
        deadline = time.monotonic() + timeout
        while True:
            attempt = self.control.project().attempts.get(attempt_id)
            if attempt is None:
                raise SupervisorError("attempt does not exist")
            if attempt.execution_state in {"succeeded", "failed", "cancelled", "lost"}:
                return attempt
            if time.monotonic() >= deadline:
                raise TimeoutError(f"attempt did not finish within {timeout:g}s")
            time.sleep(0.02)

    def _verify_attempt_process(self, attempt: RunAttempt) -> dict[str, Any]:
        if not attempt.has_complete_process_proof:
            raise SupervisorError("attempt has incomplete process ownership proof")
        try:
            metadata = self.runs.verify_owned_process(attempt.attempt_id)
        except PermissionError as exc:
            raise SupervisorError(str(exc)) from exc
        nonce = str(metadata.get("ownership_nonce") or "")
        expected = (
            int(attempt.pid or 0),
            int(attempt.process_group_id or 0),
            float(attempt.process_birth_time or 0.0),
            str(Path(attempt.process_executable or "").resolve()),
            str(Path(attempt.process_cwd or "").resolve()),
            attempt.ownership_nonce_hash,
            attempt.manifest_id,
        )
        observed = (
            int(metadata.get("pid") or 0),
            int(metadata.get("process_group_id") or 0),
            float(metadata.get("process_birth_time") or 0.0),
            str(Path(str(metadata.get("process_executable") or "")).resolve()),
            str(Path(str(metadata.get("process_cwd") or "")).resolve()),
            _nonce_hash(nonce) if nonce else None,
            metadata.get("manifest_id"),
        )
        if expected != observed:
            raise SupervisorError("attempt and run metadata ownership proofs do not match")
        return metadata

    def cancel_attempt(self, attempt_id: str, *, idempotency_key: str) -> RunAttempt:
        self._ensure_lease()
        projection = self.control.project()
        current = projection.attempts.get(attempt_id)
        if current is None:
            raise SupervisorError("attempt does not exist")
        if current.execution_state in {"cancelled", "failed", "succeeded", "lost"}:
            return current
        if current.execution_state not in {"running", "cancel_requested"}:
            raise InvalidTransition(f"attempt cannot be cancelled from {current.execution_state}")
        # Prove exact identity before recording intent, then prove it again after
        # the durable cancel_requested event and immediately before every signal.
        self._verify_attempt_process(current)
        if current.execution_state == "running":
            current = self.control.transition_attempt(
                attempt_id,
                expected_revision=current.revision,
                execution_state="cancel_requested",
                reason="agentacct-owned cancellation requested",
                idempotency_key=idempotency_key,
                actor_kind="supervisor",
                actor_id=self.lease.lease_id,
            )
            self.runs.update_metadata(
                attempt_id,
                {
                    "status": "cancel_requested",
                    "reason": "agentacct-owned cancellation requested",
                },
            )
        metadata = self._verify_attempt_process(current)
        self._fence_lease()
        metadata = self._verify_attempt_process(current)
        pgid = int(metadata["process_group_id"])
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            return self._mark_lost(
                self.control.project().attempts[attempt_id],
                "cancellation lost process-group ownership before SIGTERM",
            )

        deadline = time.monotonic() + self.cancel_grace_seconds
        proof_lost = False
        while time.monotonic() < deadline:
            try:
                self._verify_attempt_process(current)
            except SupervisorError:
                proof_lost = True
                break
            time.sleep(0.02)
        if proof_lost:
            return self._mark_lost(
                self.control.project().attempts[attempt_id],
                "cancellation ownership proof disappeared before group termination was proven",
            )
        # The stable shim deliberately survives SIGTERM. Revalidate it and the
        # lease immediately before the group-wide SIGKILL that proves every
        # same-PGID descendant was addressed.
        try:
            self._fence_lease()
            metadata = self._verify_attempt_process(current)
            os.killpg(int(metadata["process_group_id"]), signal.SIGKILL)
        except (OSError, SupervisorError):
            return self._mark_lost(
                self.control.project().attempts[attempt_id],
                "cancellation could not prove the final process-group kill",
            )

        with self._managed_lock:
            managed = self._managed.get(attempt_id)
        if managed is not None:
            try:
                managed.process.wait(timeout=max(1.0, self.cancel_grace_seconds + 1.0))
            except subprocess.TimeoutExpired:
                return self._mark_lost(
                    self.control.project().attempts[attempt_id],
                    "cancellation signal was sent but the owned shim was not reaped",
                )
        return self._complete_attempt(attempt_id, managed.process.returncode if managed is not None else None)

    def _mark_lost(self, attempt: RunAttempt, reason: str) -> RunAttempt:
        if attempt.execution_state == "lost":
            return attempt
        try:
            self._fence_lease()
        except SupervisorError:
            return self.control.project().attempts[attempt.attempt_id]
        try:
            result = self.control.transition_attempt(
                attempt.attempt_id,
                expected_revision=attempt.revision,
                execution_state="lost",
                control_state="control_failure",
                ended_at=time.time(),
                reason=reason,
                idempotency_key=f"reconcile:{attempt.attempt_id}:{attempt.revision}:lost",
                actor_kind="supervisor",
                actor_id=self.lease.lease_id,
            )
        except (RevisionConflict, InvalidTransition):
            return self.control.project().attempts[attempt.attempt_id]
        try:
            self.runs.update_metadata(
                attempt.attempt_id,
                {"status": "lost", "ended_at": result.ended_at, "reason": reason},
            )
        except (FileNotFoundError, OSError, ValueError):
            pass
        return result

    def _monitor_recovered(self, attempt_id: str) -> None:
        try:
            while True:
                try:
                    self._fence_lease()
                except SupervisorError:
                    return
                attempt = self.control.project().attempts.get(attempt_id)
                if attempt is None or attempt.execution_state not in {"running", "cancel_requested"}:
                    return
                try:
                    self._verify_attempt_process(attempt)
                except SupervisorError:
                    # A restarted parent cannot recover an arbitrary child's
                    # exit code. Loss of exact live proof is therefore lost,
                    # never guessed as succeeded/failed.
                    self._mark_lost(attempt, "recovered process identity or exit result became unavailable")
                    return
                time.sleep(0.2)
        finally:
            with self._managed_lock:
                if self._recovered_monitors.get(attempt_id) is threading.current_thread():
                    self._recovered_monitors.pop(attempt_id, None)

    def reconcile(self) -> dict[str, list[str]]:
        self._ensure_lease()
        result = {"live": [], "lost": [], "legacy_lost": []}
        projection = self.control.project()
        for attempt in projection.attempts.values():
            if attempt.execution_state not in _ACTIVE_STATES:
                continue
            if attempt.execution_state == "launching" or not attempt.has_complete_process_proof:
                self._mark_lost(attempt, "restart reconciliation found no complete process ownership proof")
                result["lost"].append(attempt.attempt_id)
                continue
            try:
                self._verify_attempt_process(attempt)
            except SupervisorError:
                self._mark_lost(attempt, "restart reconciliation could not verify the owned process")
                result["lost"].append(attempt.attempt_id)
                continue
            result["live"].append(attempt.attempt_id)
            with self._managed_lock:
                managed = self._managed.get(attempt.attempt_id)
                recovered = self._recovered_monitors.get(attempt.attempt_id)
                if managed is not None or (recovered is not None and recovered.is_alive()):
                    continue
                thread = threading.Thread(
                    target=self._monitor_recovered,
                    args=(attempt.attempt_id,),
                    name=f"chronicle-recovered-{attempt.attempt_id}",
                    daemon=True,
                )
                self._recovered_monitors[attempt.attempt_id] = thread
                thread.start()

        # Historical runner metadata predates the process fingerprint.  It can
        # remain readable, but a purported live state without complete proof is
        # irrecoverable and must never authorize control of its stale PGID.
        if self.runs.runs_root.is_dir():
            controlled = set(projection.attempts)
            for run_dir in self.runs.runs_root.iterdir():
                if not run_dir.is_dir() or run_dir.name in controlled:
                    continue
                try:
                    metadata = self.runs.read_metadata(run_dir.name)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    continue
                if metadata.get("owned_by_sentinel") is not True or str(metadata.get("status")) not in {
                    "running",
                    "paused",
                    "cancel_requested",
                }:
                    continue
                if metadata.get("ownership_schema_version") == OWNERSHIP_SCHEMA_VERSION:
                    try:
                        self.runs.verify_owned_process(run_dir.name)
                        continue
                    except PermissionError:
                        pass
                self._fence_lease()
                self.runs.update_metadata(
                    run_dir.name,
                    {
                        "status": "lost",
                        "ended_at": time.time(),
                        "reason": "legacy live metadata lacks verifiable process ownership proof",
                    },
                )
                result["legacy_lost"].append(run_dir.name)
        return result


__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "LaunchResult",
    "OwnedSupervisor",
    "SupervisorAlreadyRunning",
    "SupervisorError",
    "SupervisorLease",
]
