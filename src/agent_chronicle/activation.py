"""First-run readiness and a fail-closed local runtime manager.

The manager owns only the dashboard and usage watcher processes it starts.  A
PID is never ownership proof by itself: every lifecycle action verifies process
birth time, argv, executable, cwd, store, and a random environment nonce.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import secrets
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import psutil


RUNTIME_SCHEMA_VERSION = "agent-chronicle.activation-runtime.v1"
ACTIVATION_SCHEMA_VERSION = "agent-chronicle.activation.v1"
ACTIVATION_INSTALL_SCHEMA_VERSION = "agent-chronicle.activation-install.v1"
RUNTIME_DIRNAME = "runtime"
RUNTIME_STATE_FILENAME = "state.json"
ACTIVATION_DIRNAME = "activation"
ACTIVATION_STATE_FILENAME = "state.json"

# Managed watcher/dashboard processes outlive the shell that launched them.
# Copying the full environment would retain unrelated provider credentials in
# those long-lived processes.  Keep only local runtime essentials, supported
# client-home overrides, and agentacct's non-secret feature/path settings.
RUNTIME_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "HERMES_HOME",
    "OPENCLAW_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "AGENT_CHRONICLE_PRICING_CATALOG_PATH",
    "AGENT_SENTINEL_PRICING_CATALOG_PATH",
    "AGENT_CHRONICLE_PRICING_AUTO_REFRESH",
    "AGENT_SENTINEL_PRICING_AUTO_REFRESH",
    "AGENT_CHRONICLE_EVIDENCE_V2",
    "AGENT_SENTINEL_EVIDENCE_V2",
    "AGENT_CHRONICLE_CANONICAL_READ",
    "AGENT_SENTINEL_CANONICAL_READ",
    "AGENT_CHRONICLE_CANONICAL_LIVE_WRITE",
    "AGENT_SENTINEL_CANONICAL_LIVE_WRITE",
)


class RuntimeManagerError(RuntimeError):
    """A managed-runtime action was unsafe or could not complete."""


class ActivationStateError(RuntimeError):
    """Activation configuration evidence could not be persisted safely."""


def _owner_only(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _owner_only(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _owner_only(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ActivationStateStore:
    """Persist only the install boundary; readiness remains evidence-derived.

    The file is not a mutable onboarding checklist.  It records when agentacct
    finished writing project-local client configuration so the dashboard can
    distinguish pre-existing chats from the first new, agentacct-capable chat.
    """

    def __init__(self, store_dir: Path | str) -> None:
        self.store_dir = Path(store_dir).expanduser().resolve()
        self.root = self.store_dir / ACTIVATION_DIRNAME
        self.state_path = self.root / ACTIVATION_STATE_FILENAME
        self.lock_path = self.root / ".state.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.root, 0o700)
        with self.lock_path.open("a+b") as handle:
            _owner_only(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def mark_configured(
        self,
        *,
        project_dir: Path | str,
        clients: Sequence[str],
        configured_at: float | None = None,
    ) -> dict[str, Any]:
        normalized_clients = sorted(
            {
                str(client).strip().lower()
                for client in clients
                if str(client).strip()
            }
        )
        if not normalized_clients:
            raise ActivationStateError("at least one configured client is required")
        resolved_project = str(Path(project_dir).expanduser().resolve())
        payload = {
            "schema_version": ACTIVATION_INSTALL_SCHEMA_VERSION,
            "configured_at": float(time.time() if configured_at is None else configured_at),
            "project_dir": resolved_project,
            "clients": normalized_clients,
        }
        try:
            with self._locked():
                current = self.snapshot()
                if (
                    isinstance(current, Mapping)
                    and not current.get("issue")
                    and current.get("project_dir") == resolved_project
                    and set(normalized_clients).issubset(set(current.get("clients") or ()))
                ):
                    return dict(current)
                if (
                    isinstance(current, Mapping)
                    and not current.get("issue")
                    and current.get("project_dir") == resolved_project
                ):
                    payload["clients"] = sorted(
                        set(normalized_clients) | set(current.get("clients") or ())
                    )
                _atomic_json(self.state_path, payload)
        except (OSError, TypeError, ValueError) as exc:
            raise ActivationStateError(
                f"activation configuration evidence could not be written: {type(exc).__name__}"
            ) from exc
        return payload

    def snapshot(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("activation state is not an object")
            if value.get("schema_version") != ACTIVATION_INSTALL_SCHEMA_VERSION:
                raise ValueError("activation state schema mismatch")
            configured_at = float(value.get("configured_at"))
            project_dir = str(value.get("project_dir") or "")
            clients = value.get("clients")
            if configured_at <= 0 or not project_dir or not isinstance(clients, list):
                raise ValueError("activation state fields are invalid")
            if any(not isinstance(client, str) or not client for client in clients):
                raise ValueError("activation clients are invalid")
            return {
                "schema_version": ACTIVATION_INSTALL_SCHEMA_VERSION,
                "configured_at": configured_at,
                "project_dir": project_dir,
                "clients": list(clients),
            }
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
            return {
                "schema_version": ACTIVATION_INSTALL_SCHEMA_VERSION,
                "issue": f"activation_state_corrupt:{type(exc).__name__}",
            }


@dataclass(frozen=True)
class ManagedProcess:
    role: str
    pid: int
    process_group_id: int
    create_time: float
    executable: str
    cwd: str
    argv: tuple[str, ...]
    nonce: str
    log_path: str
    started_at: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManagedProcess":
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("runtime argv is invalid")
        role = str(value.get("role") or "")
        nonce = str(value.get("nonce") or "")
        if role not in {"dashboard", "watcher"} or len(nonce) < 20:
            raise ValueError("runtime process identity is invalid")
        return cls(
            role=role,
            pid=int(value.get("pid")),
            process_group_id=int(value.get("process_group_id")),
            create_time=float(value.get("create_time")),
            executable=str(value.get("executable") or ""),
            cwd=str(value.get("cwd") or ""),
            argv=tuple(argv),
            nonce=nonce,
            log_path=str(value.get("log_path") or ""),
            started_at=float(value.get("started_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["argv"] = list(self.argv)
        return value


@dataclass(frozen=True)
class RuntimeState:
    store_dir: str
    host: str
    port: int
    executable: str
    created_at: float
    processes: tuple[ManagedProcess, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeState":
        if value.get("schema_version") != RUNTIME_SCHEMA_VERSION:
            raise ValueError("runtime schema mismatch")
        processes = value.get("processes")
        if not isinstance(processes, list):
            raise ValueError("runtime process list is invalid")
        if any(not isinstance(item, Mapping) for item in processes):
            raise ValueError("runtime process entry is invalid")
        host = str(value.get("host") or "")
        raw_port = value.get("port")
        if not isinstance(raw_port, int) or isinstance(raw_port, bool):
            raise ValueError("runtime port is invalid")
        port = raw_port
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("runtime host is not localhost")
        if not 1 <= port <= 65535:
            raise ValueError("runtime port is invalid")
        parsed_processes = tuple(ManagedProcess.from_dict(item) for item in processes)
        roles = [process.role for process in parsed_processes]
        if len(roles) != len(set(roles)):
            raise ValueError("runtime process roles are duplicated")
        return cls(
            store_dir=str(value.get("store_dir") or ""),
            host=host,
            port=port,
            executable=str(value.get("executable") or ""),
            created_at=float(value.get("created_at")),
            processes=parsed_processes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "store_dir": self.store_dir,
            "host": self.host,
            "port": self.port,
            "executable": self.executable,
            "created_at": self.created_at,
            "processes": [process.to_dict() for process in self.processes],
        }


class RuntimeManager:
    """Own the local dashboard and continuous usage watcher."""

    TERM_GRACE_SECONDS = 3.0
    KILL_GRACE_SECONDS = 2.0
    OWNERSHIP_HANDSHAKE_SECONDS = 10.0
    OWNERSHIP_POLL_INITIAL_SECONDS = 0.03
    OWNERSHIP_POLL_MAX_SECONDS = 0.25
    STARTUP_SETTLE_SECONDS = 0.5
    DASHBOARD_STARTING_GRACE_SECONDS = 5.0
    DASHBOARD_HEALTH_ATTEMPTS = 2
    DASHBOARD_HEALTH_TIMEOUT_SECONDS = 0.5
    DASHBOARD_HEALTH_RETRY_SECONDS = 0.05
    DASHBOARD_HEALTH_MAX_BODY_BYTES = 64 * 1024
    DASHBOARD_HEALTH_SERVICE = "agent-sentinel-local-api"

    def __init__(
        self,
        store_dir: Path | str,
        *,
        executable: Path | str,
        host: str = "127.0.0.1",
        port: int = 8765,
        cwd: Path | str | None = None,
    ) -> None:
        self.store_dir = Path(store_dir).expanduser().resolve()
        self.executable = str(Path(executable).expanduser().resolve())
        self.host = host
        self.port = int(port)
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else self._project_root()
        if self.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("managed dashboard host must be localhost")
        if not 1 <= self.port <= 65535:
            raise ValueError("managed dashboard port must be between 1 and 65535")
        self.runtime_root = self.store_dir / RUNTIME_DIRNAME
        self.state_path = self.runtime_root / RUNTIME_STATE_FILENAME
        self.lock_path = self.runtime_root / ".runtime.lock"

    def _project_root(self) -> Path:
        if self.store_dir.name == "state" and self.store_dir.parent.name == ".agent-sentinel":
            return self.store_dir.parent.parent
        return self.store_dir.parent

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _owner_only(self.runtime_root, 0o700)
        with self.lock_path.open("a+b") as handle:
            _owner_only(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> tuple[RuntimeState | None, str | None]:
        if not self.state_path.exists():
            return None, None
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("runtime state is not an object")
            state = RuntimeState.from_dict(raw)
            if Path(state.store_dir).resolve() != self.store_dir:
                raise ValueError("runtime state belongs to a different store")
            return state, None
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
            return None, f"runtime_state_corrupt:{type(exc).__name__}"

    def _matches(self, record: ManagedProcess) -> tuple[bool, str]:
        try:
            process = psutil.Process(record.pid)
            # A killed child can remain in the process table as an unreaped
            # zombie, especially on Linux.  It is kernel-confirmed dead and
            # cannot execute or receive another signal, so it is safe to
            # classify as stopped before reading identity fields that psutil
            # exposes only partially for zombies.
            if process.status() == psutil.STATUS_ZOMBIE:
                return False, "not_running"
            create_time = float(process.create_time())
            pgid = os.getpgid(record.pid)
            cwd = str(Path(process.cwd()).resolve())
            argv = tuple(process.cmdline())
            executable = str(Path(argv[0]).resolve()) if argv else ""
            nonce = process.environ().get("AGENT_CHRONICLE_RUNTIME_NONCE")
        except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
            return False, "not_running"
        except (OSError, psutil.AccessDenied) as exc:
            return False, f"identity_unreadable:{type(exc).__name__}"
        if (
            abs(create_time - record.create_time) > 0.05
            or pgid != record.process_group_id
            or executable != str(Path(record.executable).resolve())
            or cwd != str(Path(record.cwd).resolve())
            or argv != record.argv
            or nonce != record.nonce
        ):
            return False, "identity_mismatch"
        return True, "running"

    def _spawn(self, role: str, argv: Sequence[str]) -> ManagedProcess:
        nonce = secrets.token_urlsafe(32)
        log_path = self.runtime_root / f"{role}.log"
        env = {key: os.environ[key] for key in RUNTIME_ENV_ALLOWLIST if key in os.environ}
        env["AGENT_CHRONICLE_RUNTIME_NONCE"] = nonce
        env["AGENT_CHRONICLE_STORE_DIR"] = str(self.store_dir)
        with log_path.open("ab", buffering=0) as log_handle:
            _owner_only(log_path, 0o600)
            process = subprocess.Popen(
                list(argv),
                cwd=self.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        try:
            # Console scripts commonly pass through /usr/bin/env and then exec
            # Python.  Recording that transient image would make the first
            # status check look like PID reuse.  Require a stable final image
            # and our nonce across consecutive observations.
            observed = psutil.Process(process.pid)
            stable_fingerprint: tuple[float, str, tuple[str, ...], int] | None = None
            stable_count = 0
            deadline = time.monotonic() + self.OWNERSHIP_HANDSHAKE_SECONDS
            poll_delay = self.OWNERSHIP_POLL_INITIAL_SECONDS
            while time.monotonic() < deadline:
                try:
                    current = (
                        float(observed.create_time()),
                        str(Path(observed.cwd()).resolve()),
                        tuple(observed.cmdline()),
                        os.getpgid(process.pid),
                    )
                    observed_nonce = observed.environ().get("AGENT_CHRONICLE_RUNTIME_NONCE")
                except (OSError, psutil.AccessDenied):
                    # macOS can transiently deny KERN_PROCARGS2 while the
                    # console-script shebang is execing.  This grants no
                    # authority: keep waiting for the complete fingerprint.
                    stable_count = 0
                    poll_delay = min(self.OWNERSHIP_POLL_MAX_SECONDS, poll_delay * 2)
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(poll_delay, remaining))
                    continue
                if observed_nonce != nonce:
                    # macOS can expose a long-lived /usr/bin/env interpreter
                    # image with an empty environment while a Python console
                    # script is still execing under launch pressure.  It is
                    # neither ownership proof nor a terminal mismatch: keep
                    # the bounded wait, with backoff, until the final image
                    # exposes our nonce.
                    stable_count = 0
                    poll_delay = min(self.OWNERSHIP_POLL_MAX_SECONDS, poll_delay * 2)
                elif current == stable_fingerprint:
                    stable_count += 1
                    poll_delay = self.OWNERSHIP_POLL_INITIAL_SECONDS
                else:
                    stable_fingerprint = current
                    stable_count = 1
                    poll_delay = self.OWNERSHIP_POLL_INITIAL_SECONDS
                if stable_count >= 3:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(poll_delay, remaining))
            if stable_fingerprint is None or stable_count < 3:
                raise RuntimeManagerError("child process did not complete the agentacct ownership handshake")
            create_time, process_cwd, process_argv, process_group_id = stable_fingerprint
            executable = str(Path(process_argv[0]).resolve()) if process_argv else ""
            return ManagedProcess(
                role=role,
                pid=process.pid,
                process_group_id=process_group_id,
                create_time=create_time,
                executable=executable,
                cwd=process_cwd,
                argv=process_argv,
                nonce=nonce,
                log_path=str(log_path),
                started_at=time.time(),
            )
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            raise

    def _commands(self) -> dict[str, tuple[str, ...]]:
        store = str(self.store_dir)
        return {
            "watcher": (
                self.executable,
                "usage",
                "watch",
                "--store-dir",
                store,
                "--client",
                "all",
                "--refresh",
                "--estimate-costs",
            ),
            "dashboard": (
                self.executable,
                "serve",
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--store-dir",
                store,
            ),
        }

    def start(self, *, external_watcher_running: bool = False) -> dict[str, Any]:
        with self._locked():
            state, issue = self._read()
            live_by_role: dict[str, ManagedProcess] = {}
            if issue:
                raise RuntimeManagerError(
                    "Managed runtime state is corrupt. Run `agentacct repair` before starting it."
                )
            if state is not None:
                if state.host != self.host or state.port != self.port or state.executable != self.executable:
                    live_results = [self._matches(item) for item in state.processes]
                    if any(ok for ok, _reason in live_results):
                        raise RuntimeManagerError(
                            "A managed runtime is already live with different settings. Stop it before changing host, port, or executable."
                        )
                for item in state.processes:
                    ok, reason = self._matches(item)
                    if ok:
                        live_by_role[item.role] = item
                    elif reason not in {"not_running"}:
                        raise RuntimeManagerError(
                            f"The recorded {item.role} PID no longer matches agentacct ownership proof; refusing to replace or signal it."
                        )

            commands = self._commands()
            created: list[ManagedProcess] = []
            try:
                if not external_watcher_running and "watcher" not in live_by_role:
                    live_by_role["watcher"] = self._spawn("watcher", commands["watcher"])
                    created.append(live_by_role["watcher"])
                if "dashboard" not in live_by_role:
                    live_by_role["dashboard"] = self._spawn("dashboard", commands["dashboard"])
                    created.append(live_by_role["dashboard"])
                # A complete PID/argv/nonce fingerprint proves ownership, not
                # successful startup.  Port-bind failures and malformed child
                # commands can survive the short exec handshake and then exit
                # immediately.  Give newly-created children a bounded settling
                # window and fail before persisting a runtime lease if any one
                # disappears.  Dashboard health alone cannot finish this wait:
                # the response may belong to an unrelated process already
                # listening on the requested port.  A live but still-booting
                # dashboard remains honestly ``starting`` in the returned
                # status.
                settle_deadline = time.monotonic() + self.STARTUP_SETTLE_SECONDS
                while created:
                    failed = [
                        f"{item.role}:{reason}"
                        for item in created
                        for ok, reason in (self._matches(item),)
                        if not ok
                    ]
                    if failed:
                        raise RuntimeManagerError(
                            "child exited during startup (" + ", ".join(failed) + ")"
                        )
                    remaining = settle_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.05, remaining))
            except Exception as exc:
                for item in created:
                    ok, _reason = self._matches(item)
                    if ok:
                        try:
                            os.killpg(item.process_group_id, signal.SIGTERM)
                        except OSError:
                            pass
                raise RuntimeManagerError(
                    f"Managed runtime failed to start: {type(exc).__name__}: {exc}"
                ) from exc

            next_state = RuntimeState(
                store_dir=str(self.store_dir),
                host=self.host,
                port=self.port,
                executable=self.executable,
                created_at=state.created_at if state is not None else time.time(),
                processes=tuple(live_by_role[role] for role in ("watcher", "dashboard") if role in live_by_role),
            )
            _atomic_json(self.state_path, next_state.to_dict())
        return self.status(external_watcher_running=external_watcher_running)

    @classmethod
    def _probe_dashboard_health(cls, host: str, port: int) -> str:
        """Probe only the owned localhost endpoint, never a configured proxy.

        ``urllib.request.urlopen`` inherits macOS system proxy settings.  A
        machine whose proxy does not bypass ``127.0.0.1`` therefore sends this
        local health check to the proxy and can report ``RemoteDisconnected``
        while the dashboard is serving normally.  ``HTTPConnection`` is an
        explicit direct connection and does not follow redirects.
        """

        if (
            host not in {"127.0.0.1", "localhost"}
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            return "unreachable"
        for attempt in range(cls.DASHBOARD_HEALTH_ATTEMPTS):
            connection: http.client.HTTPConnection | None = None
            try:
                connection = http.client.HTTPConnection(
                    host,
                    port,
                    timeout=cls.DASHBOARD_HEALTH_TIMEOUT_SECONDS,
                )
                connection.request(
                    "GET",
                    "/health",
                    headers={"Accept": "application/json", "Connection": "close"},
                )
                response = connection.getresponse()
                if response.status != 200:
                    return "unhealthy"
                body = response.read(cls.DASHBOARD_HEALTH_MAX_BODY_BYTES + 1)
                if len(body) > cls.DASHBOARD_HEALTH_MAX_BODY_BYTES:
                    return "unhealthy"
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (
                    UnicodeError,
                    json.JSONDecodeError,
                    RecursionError,
                    ValueError,
                ):
                    return "unhealthy"
                if not isinstance(payload, Mapping):
                    return "unhealthy"
                return (
                    "healthy"
                    if payload.get("ok") is True
                    and payload.get("service") == cls.DASHBOARD_HEALTH_SERVICE
                    else "unhealthy"
                )
            except (OSError, http.client.HTTPException, TimeoutError, ValueError):
                if attempt + 1 >= cls.DASHBOARD_HEALTH_ATTEMPTS:
                    return "unreachable"
                time.sleep(cls.DASHBOARD_HEALTH_RETRY_SECONDS)
            finally:
                if connection is not None:
                    connection.close()
        return "unreachable"

    def _dashboard_health(self) -> str:
        return self._probe_dashboard_health(self.host, self.port)

    def status(self, *, external_watcher_running: bool = False) -> dict[str, Any]:
        state, issue = self._read()
        processes: list[dict[str, Any]] = []
        if state is not None:
            for record in state.processes:
                ok, reason = self._matches(record)
                processes.append(
                    {
                        "role": record.role,
                        "state": "running" if ok else "stale",
                        "reason": reason,
                        "pid": record.pid,
                        "log_path": record.log_path,
                    }
                )
        roles_running = {row["role"] for row in processes if row["state"] == "running"}
        dashboard_record = next(
            (
                record
                for record in (state.processes if state is not None else ())
                if record.role == "dashboard" and "dashboard" in roles_running
            ),
            None,
        )
        recorded_host = state.host if state is not None else self.host
        recorded_port = state.port if state is not None else self.port
        if "dashboard" in roles_running:
            dashboard_health = (
                self._dashboard_health()
                if recorded_host == self.host and recorded_port == self.port
                else self._probe_dashboard_health(recorded_host, recorded_port)
            )
        else:
            dashboard_health = "stopped"
        watcher_ready = "watcher" in roles_running or external_watcher_running
        dashboard_age = (
            time.time() - dashboard_record.started_at
            if dashboard_record is not None
            else None
        )
        dashboard_is_starting = (
            dashboard_age is not None
            and 0 <= dashboard_age <= self.DASHBOARD_STARTING_GRACE_SECONDS
        )
        overall = (
            "running"
            if dashboard_health == "healthy" and watcher_ready
            else "starting"
            if (
                "dashboard" in roles_running
                and dashboard_health == "unreachable"
                and dashboard_is_starting
            )
            else "degraded"
            if state is not None or issue
            else "stopped"
        )
        issues = [issue] if issue else []
        if (
            "dashboard" in roles_running
            and dashboard_health == "unreachable"
            and not dashboard_is_starting
        ):
            log_path = next(
                (row["log_path"] for row in processes if row["role"] == "dashboard"),
                str(self.runtime_root / "dashboard.log"),
            )
            issues.append(
                "dashboard_health_unverified: Dashboard process is running, but /health "
                f"could not be verified after {self.DASHBOARD_HEALTH_ATTEMPTS} direct localhost attempts. "
                f"No process was stopped or restarted. Retry status and inspect {log_path}."
            )
        elif "dashboard" in roles_running and dashboard_health == "unhealthy":
            log_path = next(
                (row["log_path"] for row in processes if row["role"] == "dashboard"),
                str(self.runtime_root / "dashboard.log"),
            )
            issues.append(
                "dashboard_health_unhealthy: The owned dashboard process is running, but "
                "/health did not return a valid healthy agentacct response. No process was "
                f"stopped or restarted. Inspect {log_path}."
            )
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "state": overall,
            "store_dir": str(self.store_dir),
            "dashboard_url": f"http://{recorded_host}:{recorded_port}/",
            "dashboard_health": dashboard_health,
            "watcher": "external" if external_watcher_running and "watcher" not in roles_running else (
                "running" if "watcher" in roles_running else "stopped"
            ),
            "processes": processes,
            "issues": issues,
        }

    def stop(self) -> dict[str, Any]:
        with self._locked():
            state, issue = self._read()
            if issue:
                raise RuntimeManagerError(
                    "Managed runtime state is corrupt, so ownership cannot be verified. Run repair; no process was signalled."
                )
            if state is None:
                return self.status()
            verified: list[ManagedProcess] = []
            for record in state.processes:
                ok, reason = self._matches(record)
                if ok:
                    verified.append(record)
                elif reason != "not_running":
                    raise RuntimeManagerError(
                        f"The recorded {record.role} process does not match agentacct ownership proof; no process was signalled."
                    )
            for record in verified:
                try:
                    os.killpg(record.process_group_id, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + self.TERM_GRACE_SECONDS
            while time.monotonic() < deadline and any(self._matches(item)[0] for item in verified):
                time.sleep(0.05)

            survivors: list[ManagedProcess] = []
            for record in verified:
                ok, reason = self._matches(record)
                if ok:
                    survivors.append(record)
                elif reason != "not_running":
                    raise RuntimeManagerError(
                        f"The recorded {record.role} process identity became unreadable while stopping. "
                        "agentacct preserved ownership state and did not escalate the signal."
                    )
            for record in survivors:
                # Re-verification above is the authority for escalation.  The
                # process ignored TERM but still matches agentacct's complete
                # ownership proof, so KILL cannot target an adopted process.
                try:
                    os.killpg(record.process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            kill_deadline = time.monotonic() + self.KILL_GRACE_SECONDS
            # Linux can expose a killed process with a partially torn-down
            # identity before reporting it as a zombie.  An ambiguous read is
            # not authority for another signal, but it also must not end the
            # bounded death wait early.  Keep observing until every survivor
            # is definitively stopped or the grace period expires; the final
            # check below still preserves state for any unresolved identity.
            while time.monotonic() < kill_deadline:
                if all(
                    not ok and reason == "not_running"
                    for item in survivors
                    for ok, reason in (self._matches(item),)
                ):
                    break
                time.sleep(0.05)

            for record in verified:
                ok, reason = self._matches(record)
                if ok:
                    raise RuntimeManagerError(
                        f"The recorded {record.role} process is still running after TERM and KILL. "
                        "agentacct preserved ownership state; inspect its log and retry stop."
                    )
                if reason != "not_running":
                    raise RuntimeManagerError(
                        f"The recorded {record.role} process could not be proven dead after signalling. "
                        "agentacct preserved ownership state."
                    )
            self.state_path.unlink(missing_ok=True)
        return self.status()

    def repair(self) -> dict[str, Any]:
        with self._locked():
            state, issue = self._read()
            if issue:
                # A corrupt record cannot authorize a signal. Rename it for
                # forensics, then clear only agentacct's unusable bookkeeping.
                if self.state_path.exists():
                    archived = self.runtime_root / (
                        f"state.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
                    )
                    self.state_path.replace(archived)
                    _owner_only(archived, 0o600)
                return {"repaired": True, "action": "archived_corrupt_state", **self.status()}
            if state is None:
                return {"repaired": False, "action": "nothing_to_repair", **self.status()}
            live: list[ManagedProcess] = []
            removed: list[str] = []
            for record in state.processes:
                ok, reason = self._matches(record)
                if ok:
                    live.append(record)
                elif reason == "not_running":
                    removed.append(record.role)
                else:
                    raise RuntimeManagerError(
                        f"The recorded {record.role} PID is live but ownership proof mismatches. agentacct left it untouched."
                    )
            if live:
                _atomic_json(
                    self.state_path,
                    RuntimeState(
                        store_dir=state.store_dir,
                        host=state.host,
                        port=state.port,
                        executable=state.executable,
                        created_at=state.created_at,
                        processes=tuple(live),
                    ).to_dict(),
                )
            else:
                self.state_path.unlink(missing_ok=True)
        return {"repaired": bool(removed), "action": "cleared_stale_processes", "removed": removed, **self.status()}


def build_activation_snapshot(
    *,
    source_rows: Sequence[Mapping[str, Any]],
    ingestion_health: Mapping[str, Any],
    task_projection: Mapping[str, Any],
    runtime_status: Mapping[str, Any],
    recording_configured: bool,
    new_session_required: bool = False,
) -> dict[str, Any]:
    """Project first-run state without converting diagnostics into user tasks."""

    found = [row for row in source_rows if str(row.get("status")) == "found"]
    tasks = task_projection.get("tasks") if isinstance(task_projection.get("tasks"), list) else []
    watcher = ingestion_health.get("watcher") if isinstance(ingestion_health.get("watcher"), Mapping) else {}
    runtime_state = str(runtime_status.get("state") or "stopped")
    first_task = "captured" if tasks else "waiting"
    if not found:
        primary = {
            "label": "Show known local sources",
            "command": "agentacct usage discover-sources",
            "reason": "No readable known client source was found.",
        }
        stage = "client_needed"
        headline = "Connect a coding-agent source"
    elif not recording_configured:
        primary = {
            "label": "Finish project setup",
            "command": "agentacct onboard",
            "reason": "Usage is readable, but work recording is not configured for this project.",
        }
        stage = "configuration_needed"
        headline = "agentacct found your agent"
    elif runtime_state not in {"running", "starting"}:
        primary = {
            "label": "Start agentacct",
            "command": "agentacct start",
            "reason": "Continuous sync and the local dashboard are stopped.",
        }
        stage = "runtime_needed"
        headline = "Start continuous capture"
    elif new_session_required:
        primary = {
            "label": "Open a new agent chat",
            "command": None,
            "reason": "Agent clients load MCP servers and hooks when a session starts.",
        }
        stage = "new_session_needed"
        headline = "agentacct is ready for your next Task"
    elif tasks:
        primary = {"label": "Open latest Task", "command": None, "reason": "A real saved Task is available."}
        stage = "active"
        headline = "Your first Task is captured"
    else:
        primary = {
            "label": "Continue in your agent",
            "command": None,
            "reason": "agentacct is waiting for the first real recognized session; demo rows do not count.",
        }
        stage = "waiting_for_task"
        headline = "Run one real Task in your agent"

    semantics_recorded = any(
        isinstance(task, Mapping) and bool(task.get("work_items")) for task in tasks
    )
    return {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "stage": stage,
        "headline": headline,
        "ready": stage == "active",
        "primary_action": primary,
        "progress": [
            {"key": "client", "label": "Agent detected", "state": "ready" if found else "needs_action"},
            {"key": "usage", "label": "Usage source", "state": "ready" if found else "unavailable"},
            {
                "key": "recording",
                "label": "Work recording",
                "state": "restart_required" if new_session_required else "ready" if recording_configured else "needs_action",
            },
            {
                "key": "sync",
                "label": "Continuous sync",
                "state": "ready" if watcher.get("state") == "running" else "starting" if runtime_state == "starting" else "needs_action",
            },
            {
                "key": "task",
                "label": "First real Task",
                "state": first_task,
                "detail": "semantic context recorded" if semantics_recorded else "usage/activity only" if tasks else None,
            },
        ],
        "detected_clients": [str(row.get("client")) for row in found if row.get("client")],
        "task_count": len(tasks),
        "runtime": dict(runtime_status),
    }


__all__ = [
    "ACTIVATION_INSTALL_SCHEMA_VERSION",
    "ACTIVATION_SCHEMA_VERSION",
    "ActivationStateError",
    "ActivationStateStore",
    "ManagedProcess",
    "RUNTIME_SCHEMA_VERSION",
    "RuntimeManager",
    "RuntimeManagerError",
    "RUNTIME_ENV_ALLOWLIST",
    "RuntimeState",
    "build_activation_snapshot",
]
