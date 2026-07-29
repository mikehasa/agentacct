"""First-run readiness and a fail-closed local runtime manager.

This is agentacct's *first-run* phase -- onboarding a project to its first
recorded Task -- plus ``RuntimeManager``, the supervisor that keeps the
dashboard and usage watcher alive.  It is NOT the steady-state daily loop
(that lives in the work ledger, evidence store, and usage cube):

    install ► onboard ► [ FIRST-RUN GUIDE ] ► STEADY STATE ► stop

The guide is an ordered checklist: the dashboard finds the first need not yet
met and tells the user the one action to meet it.

    need                       if missing, do
    a coding-agent source      connect a client              (client_needed)
    work recording installed   run `agentacct onboard`       (configuration_needed)
    the runtime running        run `agentacct start`         (runtime_needed)
    a fresh agent chat         open a new chat -- hooks load at session start
                                                             (new_session_needed)
    a real recorded Task       do real work; the guide then steps away (active),
                               or waits for the first Task (waiting_for_task)

``build_activation_snapshot`` computes which row applies; ``ActivationStateStore``
remembers when onboarding finished, so a chat from before setup never counts as
the first Task.

Safety model: a process ID is never proof that a process is ours.  Every
lifecycle action re-checks the process's birth time, process group,
executable, working directory, full argv, and a random one-time secret (nonce)
we injected at start.  If any of those no longer match, agentacct refuses to
touch the process.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import secrets
import signal
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Generator, Mapping, Sequence

import psutil


# --- On-disk layout ---------------------------------------------------------
# The project store is ``<project>/.agent-sentinel/state`` (the
# ``.agent-sentinel`` name is frozen from a pre-rename era for data
# compatibility).  Beneath it, this module owns two separate subdirectories:
#
#   <store>/runtime/state.json    the managed-runtime "lease" -- the owned
#                                 dashboard/watcher processes
#   <store>/activation/state.json the install boundary -- when agentacct
#                                 finished configuring this project
#
# Each persisted file carries a ``schema_version`` field so a later agentacct
# can detect (and refuse or migrate) an incompatible record instead of
# misreading it.  ``schema_version`` is also stamped on the in-memory payloads
# this module returns to callers.

# Versions the persisted runtime lease file (``<store>/runtime/state.json``)
# and the ``status()`` payload.  Validated on read by RuntimeState.from_dict.
RUNTIME_SCHEMA_VERSION = "agent-chronicle.activation-runtime.v1"
# Versions the first-run readiness payload that build_activation_snapshot
# returns to the dashboard API.  NOT persisted to a file.
ACTIVATION_SCHEMA_VERSION = "agent-chronicle.activation.v1"
# Versions the persisted install-boundary file
# (``<store>/activation/state.json``).  Validated on read by
# ActivationStateStore.snapshot.
ACTIVATION_INSTALL_SCHEMA_VERSION = "agent-chronicle.activation-install.v1"

# Subdirectory and filename for the managed-runtime lease file.
RUNTIME_DIRNAME = "runtime"
RUNTIME_STATE_FILENAME = "state.json"
# Subdirectory and filename for the install-boundary file.  Note this is a
# distinct file from RUNTIME_STATE_FILENAME even though both are "state.json";
# they live in different subdirectories.
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


# --- Local-store I/O primitives -------------------------------------------------
# Generic helpers for persisting small JSON state files inside the project
# store: owner-only permissions, atomic writes, and exclusive file locking.
# They are deliberately self-contained so they can later move unchanged into a
# shared ``_store_io`` module once more stores adopt them.

# Owner-only permission modes: group and others get no access. Built from stdlib
# stat bits so the meaning is explicit and call sites read as English, not octal.
OWNER_ONLY_DIRECTORY = stat.S_IRWXU               # owner rwx -- the x bit lets the owner enter/traverse the dir
OWNER_ONLY_FILE = stat.S_IRUSR | stat.S_IWUSR     # owner rw  -- data files are never executable


def _try_chmod(path: Path, mode: int) -> None:
    """Best-effort ``chmod``: apply ``mode`` and ignore ``OSError``.

    This is a generic permission setter -- the *intent* (owner-only) lives in
    the ``OWNER_ONLY_*`` constants each caller passes.  It is allowed to fail
    silently because every state file and directory is already created
    owner-only, so this only closes the umask gap on paths that pre-existed; the
    strict create-time modes keep the data private regardless.
    """
    try:
        path.chmod(mode)
    except OSError:
        pass


def _ensure_owner_only_dir(directory: Path) -> None:
    """Create ``directory`` if needed and force it to owner-only.

    ``Path.mkdir(mode=...)`` only applies its mode when it actually creates the
    directory; if ``directory`` already exists (perhaps with looser permissions
    left by an older run or another tool), mkdir leaves its mode untouched.  A
    create-then-chmod therefore guarantees owner-only in both the fresh and the
    pre-existing cases.  The chmod is best-effort: it may fail silently (see
    ``_try_chmod``), which is safe because every file written
    underneath is itself created owner-only.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=OWNER_ONLY_DIRECTORY)
    _try_chmod(directory, OWNER_ONLY_DIRECTORY)


def _write_json_atomically(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON to ``path`` so a crash mid-write can never leave a partial file.

    Writes a fresh temp file, fsyncs the bytes and the parent directory (so the
    rename is durable on disk), then atomically moves it into place.  Readers
    therefore always see either the complete previous value or the complete
    new value -- never a half-written mixture.
    """
    _ensure_owner_only_dir(path.parent)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_ONLY_FILE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _try_chmod(path, OWNER_ONLY_FILE)
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


@contextmanager
def _flock_exclusive(lock_path: Path) -> Generator[None, None, None]:
    """Hold an exclusive flock on ``lock_path`` for the duration of the block.

    Creates the lock file and its parent dir owner-only, then blocks until an
    exclusive lock is acquired.  Closing the fd on exit releases the lock; and
    because flock is tied to the open file description, a crashed process can
    never leave it held behind.
    """
    _ensure_owner_only_dir(lock_path.parent)
    # A pure lock handle: nothing is read or written, so there is no append or
    # binary mode to reason about.  O_CLOEXEC stops the fd leaking into child
    # processes we spawn (Python's open() sets this by default; os.open does
    # not).  Mode OWNER_ONLY_FILE is applied at creation, so no separate chmod is needed.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, OWNER_ONLY_FILE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


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
    def _locked(self) -> Generator[None, None, None]:
        """Hold an exclusive file lock so two commands can't race on this state."""
        with _flock_exclusive(self.lock_path):
            yield

    def mark_configured(
        self,
        *,
        project_dir: Path | str,
        clients: Sequence[str],
        configured_at: float | None = None,
    ) -> dict[str, Any]:
        """Record that agentacct finished configuring ``clients`` for ``project_dir``.

        Safe to call repeatedly: if the same project is already recorded, the
        new clients are merged into the existing list with no duplicates.
        Requires at least one client.  Returns the resulting activation record.
        """
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
                _write_json_atomically(self.state_path, payload)
        except (OSError, TypeError, ValueError) as exc:
            raise ActivationStateError(
                f"activation configuration evidence could not be written: {type(exc).__name__}"
            ) from exc
        return payload

    def snapshot(self) -> dict[str, Any] | None:
        """Return the saved activation record, or ``None`` if none exists yet.

        If the saved file exists but is unreadable or invalid, returns a record
        carrying an ``issue`` key (``activation_state_corrupt:<Reason>``) instead
        of trusting bad data -- callers fail closed on the issue rather than
        guessing at partially-read state.
        """
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
    """The recorded identity of one process agentacct started.

    This is the "fingerprint" used later to prove a still-running PID is the
    same process we launched -- not merely the same number.  An OS-recycled PID
    pointing at an unrelated program would fail every field checked by
    ``RuntimeManager._matches``.
    """

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
        """Rebuild a ManagedProcess from saved JSON, rejecting anything malformed.

        Only ``dashboard`` and ``watcher`` roles are accepted, and the nonce
        must be long enough to be unguessable.  Raises ValueError on any
        problem so callers fail closed rather than trusting a bad record.
        """
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
    """A saved snapshot of a running managed runtime: where it serves and who is in it.

    Holds the store/host/port the dashboard is bound to, plus the tuple of
    ``ManagedProcess`` identities.  This is what gets persisted to disk so a
    later ``status`` or ``stop`` command can re-prove ownership of the
    processes instead of trusting a bare PID.
    """

    store_dir: str
    host: str
    port: int
    executable: str
    created_at: float
    processes: tuple[ManagedProcess, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeState":
        """Rebuild a RuntimeState from saved JSON with strict validation.

        Rejects the wrong schema version, a non-localhost host, an
        out-of-range port, duplicate process roles, or any malformed process
        entry.  Raises ValueError on any problem so callers fail closed.
        """
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
    """Own the local dashboard and the continuous usage watcher.

    This is agentacct's small process supervisor.  ``start`` launches the
    dashboard and watcher as child processes it can later identify; ``status``
    reports whether they are honestly running; ``stop`` and ``repair`` wind
    them down or clean up after a crash.

    It will only ever start, signal, or replace processes it can prove it
    started (see ``_spawn`` and ``_matches``).  A process whose fingerprint no
    longer matches is left untouched -- agentacct never signals a process it
    may have inherited by accident.
    """

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
    DASHBOARD_HEALTH_SERVICE = "agentacct-local-api"
    # Pre-rename value, still accepted so a running old dashboard is recognized
    # by newer code during an upgrade (and vice versa).
    DASHBOARD_HEALTH_SERVICE_LEGACY = "agent-sentinel-local-api"
    DASHBOARD_HEALTH_SERVICES = (DASHBOARD_HEALTH_SERVICE, DASHBOARD_HEALTH_SERVICE_LEGACY)

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
        """Best-effort guess of the project root from the store path.

        The store lives at ``<project>/.agent-sentinel/state`` (that directory
        name is frozen from a pre-rename era).  This walks one level up from
        ``state`` to recover the project directory.
        """
        if self.store_dir.name == "state" and self.store_dir.parent.name == ".agent-sentinel":
            return self.store_dir.parent.parent
        return self.store_dir.parent

    @contextmanager
    def _locked(self) -> Generator[None, None, None]:
        """Hold an exclusive file lock so two commands can't race on this state."""
        with _flock_exclusive(self.lock_path):
            yield

    def _read(self) -> tuple[RuntimeState | None, str | None]:
        """Load and validate the saved runtime state.

        Returns ``(state, None)`` on success, ``(None, None)`` if nothing is
        saved yet, or ``(None, issue)`` if the saved file is corrupt -- where
        ``issue`` is a stable ``runtime_state_corrupt:<Reason>`` string.  Never
        returns untrusted data; callers raise or fail closed on a non-None
        issue.
        """
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
        """Re-prove that a recorded process is still the one we started.

        Returns ``(True, "running")`` only when the live process's birth time,
        process group, executable, cwd, full argv, AND injected nonce all still
        match the record.  A bare PID match is not enough -- the OS recycles
        PIDs, so a same-number process could be an unrelated program.  Any
        mismatch or unreadable identity returns ``(False, <reason>)``.
        """
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
            # Allow ~50ms of clock jitter between the recorded and observed
            # create_time; a larger delta means a different (recycled) process.
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
        """Start one child process and capture a stable ownership fingerprint.

        Launches the command with a fresh random nonce injected into its
        environment, then waits (bounded) until the process's image has
        settled -- console scripts typically exec through ``/usr/bin/env`` and
        Python, so the first observed image is transient.  Only returns once
        the same identity has been seen across several consecutive samples, so
        the recorded fingerprint is the real long-lived one.  If the handshake
        does not complete, the child is cleaned up and an error is raised.
        """
        nonce = secrets.token_urlsafe(32)
        log_path = self.runtime_root / f"{role}.log"
        env = {key: os.environ[key] for key in RUNTIME_ENV_ALLOWLIST if key in os.environ}
        env["AGENT_CHRONICLE_RUNTIME_NONCE"] = nonce
        env["AGENT_CHRONICLE_STORE_DIR"] = str(self.store_dir)
        with log_path.open("ab", buffering=0) as log_handle:
            _try_chmod(log_path, OWNER_ONLY_FILE)
            process = subprocess.Popen(
                list(argv),
                cwd=self.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                # Detach the child into its own process group so stop() can
                # later signal the whole tree (child plus any grandchildren) by
                # group id instead of a single recyclable pid.
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
        """Build the argv for the watcher and dashboard commands we start."""
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
        """Start the watcher and dashboard if they are not already live.

        Refuses to proceed if the saved state is corrupt (run ``repair`` first)
        or if a managed runtime is already running with a different
        host/port/executable (stop it first).  Already-running owned processes
        are reused; only the missing ones are started.  After spawning, waits a
        short settling window and fails -- cleaning up anything it just started
        -- if a child exits immediately.  Persists the new runtime lease only
        after that settle check passes.
        """
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
            _write_json_atomically(self.state_path, next_state.to_dict())
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
                    and payload.get("service") in cls.DASHBOARD_HEALTH_SERVICES
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
        """Probe this manager's own dashboard endpoint (the health of our process)."""
        return self._probe_dashboard_health(self.host, self.port)

    def status(self, *, external_watcher_running: bool = False) -> dict[str, Any]:
        """Report the honest state of the managed runtime.

        Classifies the runtime as ``running`` (dashboard healthy and watcher
        ready), ``starting`` (dashboard process alive but not healthy yet and
        within the startup grace), ``degraded`` (processes recorded but not
        healthy), or ``stopped`` (nothing recorded).  Per-process rows carry
        the ownership-check result, and any health that cannot be verified is
        surfaced as an ``issue`` rather than reported as healthy.  Never starts
        or stops anything.
        """
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
        # Overall state by priority: running > starting > degraded > stopped.
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
        """Stop the owned processes, escalating SIGTERM to SIGKILL only when safe.

        Refuses to do anything if the state is corrupt (ownership cannot be
        proven -- run ``repair``).  For each recorded process it re-proves
        ownership, sends SIGTERM, waits a grace period, and only then escalates
        a still-matching survivor to SIGKILL.  A process that no longer matches
        our proof is never signalled, so an adopted or unrelated process cannot
        be killed by mistake.  Removes the state file once everything is
        stopped.
        """
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
        """Clean up runtime bookkeeping after a crash or a corrupt state file.

        Three outcomes: (1) corrupt state -- the bad file is renamed aside for
        forensics and the unusable bookkeeping is cleared (no process is
        signalled, since a corrupt record cannot authorize a signal); (2)
        nothing recorded -- nothing to repair; (3) recorded processes -- dead
        or stale ones are dropped from the record while live ones are kept.  A
        live process whose ownership proof no longer matches is left untouched
        and raises, rather than risk signalling the wrong thing.
        """
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
                    _try_chmod(archived, OWNER_ONLY_FILE)
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
                _write_json_atomically(
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
    """Project the dashboard's first-run readiness funnel for this project.

    Returns the single next action a user should take; the stage is chosen by
    strict priority -- the first condition that holds wins (see the module
    docstring for the user-facing actions and where this sits in the lifecycle).
    Stages, in priority order with their trigger condition:

      client_needed        no coding-agent source detected (no "found" row in
                           source_rows).  Nothing downstream is checked.
      configuration_needed a source is detected but recording_configured is
                           False; usage is readable, but no semantic work can
                           be recorded yet.
      runtime_needed       recording is configured but the dashboard/sync are
                           not up (runtime_status.state not in running/starting).
      new_session_needed   everything is running but new_session_required is
                           True -- the current chat predates setup, and hooks
                           attach only at session start, so a new chat is needed.
      active               task_projection has at least one task; onboarding is
                           complete and the guide steps away.  This is the only
                           stage where "ready" is True.
      waiting_for_task     everything is satisfied but no real Task is recorded
                           yet; the guide waits (demo rows don't count as tasks).

    Inputs:
      source_rows           detected coding agents (only status == "found" counts)
      ingestion_health      {"watcher": {"state": ...}} -- is background log-sync running?
      task_projection       {"tasks": [...]} -- real recorded tasks (may carry "work_items")
      runtime_status        {"state": ...} -- dashboard process up? (running/starting)
      recording_configured  are the MCP/hooks that record work installed?
      new_session_required  must the user open a NEW chat for hooks to attach?

    Diagnostics feed the returned "progress" checklist only; they never become
    user-facing "tasks".
    """

    # Normalize inputs (defensive: callers may pass partial dicts). A source
    # counts only when its status is "found"; everything else degrades safely.
    found_sources = [
        row for row in source_rows if isinstance(row, Mapping) and str(row.get("status")) == "found"
    ]
    recorded_tasks = task_projection.get("tasks") if isinstance(task_projection.get("tasks"), list) else []
    watcher_health = ingestion_health.get("watcher") if isinstance(ingestion_health.get("watcher"), Mapping) else {}
    runtime_state = str(runtime_status.get("state") or "stopped")
    first_task_state = "captured" if recorded_tasks else "waiting"

    # The funnel: first matching branch wins (strict priority). Each sets the
    # stage, its headline, and the one action button the dashboard shows.
    if not found_sources:  # -> client_needed
        primary = {
            "label": "Show known local sources",
            "command": "agentacct usage discover-sources",
            "reason": "No readable known client source was found.",
        }
        stage = "client_needed"
        headline = "Connect a coding-agent source"
    elif not recording_configured:  # -> configuration_needed
        primary = {
            "label": "Finish project setup",
            "command": "agentacct onboard",
            "reason": "Usage is readable, but work recording is not configured for this project.",
        }
        stage = "configuration_needed"
        headline = "agentacct found your agent"
    elif runtime_state not in {"running", "starting"}:  # -> runtime_needed
        primary = {
            "label": "Start agentacct",
            "command": "agentacct start",
            "reason": "Continuous sync and the local dashboard are stopped.",
        }
        stage = "runtime_needed"
        headline = "Start continuous capture"
    elif new_session_required:  # -> new_session_needed
        primary = {
            "label": "Open a new agent chat",
            "command": None,
            "reason": "Agent clients load MCP servers and hooks when a session starts.",
        }
        stage = "new_session_needed"
        headline = "agentacct is ready for your next Task"
    elif recorded_tasks:  # -> active
        primary = {"label": "Open latest Task", "command": None, "reason": "A real saved Task is available."}
        stage = "active"
        headline = "Your first Task is captured"
    else:  # -> waiting_for_task
        primary = {
            "label": "Continue in your agent",
            "command": None,
            "reason": "agentacct is waiting for the first real recognized session; demo rows do not count.",
        }
        stage = "waiting_for_task"
        headline = "Run one real Task in your agent"

    semantics_recorded = any(
        isinstance(task, Mapping) and bool(task.get("work_items")) for task in recorded_tasks
    )
    return {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "stage": stage,
        "headline": headline,
        "ready": stage == "active",
        "primary_action": primary,
        "progress": [
            {"key": "client", "label": "Agent detected", "state": "ready" if found_sources else "needs_action"},
            {"key": "usage", "label": "Usage source", "state": "ready" if found_sources else "unavailable"},
            {
                "key": "recording",
                "label": "Work recording",
                "state": "restart_required" if new_session_required else "ready" if recording_configured else "needs_action",
            },
            {
                "key": "sync",
                "label": "Continuous sync",
                "state": "ready" if watcher_health.get("state") == "running" else "starting" if runtime_state == "starting" else "needs_action",
            },
            {
                "key": "task",
                "label": "First real Task",
                "state": first_task_state,
                "detail": "semantic context recorded" if semantics_recorded else "usage/activity only" if recorded_tasks else None,
            },
        ],
        "detected_clients": [str(row.get("client")) for row in found_sources if row.get("client")],
        "task_count": len(recorded_tasks),
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
