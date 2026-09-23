"""Reader-activated, single-process receipt materialization with a CPU budget.

The foreground never builds or waits for a worker. Input tokens are cheap,
injected summaries of all projection sources, not wall-clock freshness timers.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .receipt_snapshot_store import (
    RECEIPT_PROJECTOR_VERSION,
    ReceiptSnapshotStore,
    SnapshotEntry,
    SnapshotGeneration,
)

SNAPSHOT_LOCK_FD_ENV = "AGENTACCT_RECEIPT_SNAPSHOT_LOCK_FD"
SNAPSHOT_CPU_FRACTION_ENV = "AGENTACCT_RECEIPT_SNAPSHOT_CPU_FRACTION"


def _write_budget(root: Path, deadline: float, failures: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".build-budget-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"allow_again_at": deadline, "failures": failures}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / "build-budget.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def worker_build_lock(store_dir: Path | str):
    """Use the inherited lock, or serialize a directly invoked worker too."""
    root = ReceiptSnapshotStore(store_dir).root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / "build.lock"
    inherited = os.environ.get(SNAPSHOT_LOCK_FD_ENV)
    descriptor = int(inherited) if inherited is not None else os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if inherited is not None:
            actual, expected = os.fstat(descriptor), path.stat()
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise RuntimeError("invalid receipt worker lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def record_worker_budget(
    store_dir: Path | str,
    *,
    cpu_seconds: float,
    failed: bool,
    cpu_fraction: float = 0.10,
    wall_clock: Callable[[], float] = time.time,
) -> None:
    """Checkpoint before child exit, even if the owning daemon has died.

    Caller holds worker_build_lock. A hard-killed child cannot run a finally
    block; clean shutdown remains the manager's responsibility. This is not a
    claim to meter an orphan that is itself SIGKILLed or loses the filesystem.
    """
    if not math.isfinite(cpu_seconds) or cpu_seconds < 0 or not math.isfinite(cpu_fraction) or not 0 < cpu_fraction <= 1:
        raise ValueError("invalid receipt worker CPU budget")
    root = ReceiptSnapshotStore(store_dir).root
    failures = 0
    if failed:
        try:
            path = root / "build-budget.json"
            if path.stat().st_size <= 4096:
                failures = max(0, min(1_000_000, int(json.loads(path.read_text())["failures"])))
        except (OSError, ValueError, TypeError, KeyError):
            pass
        failures += 1
    rest = cpu_seconds * (1.0 / cpu_fraction - 1.0)
    if failed:
        rest = max(rest, min(300.0, 5.0 * 2 ** min(failures - 1, 16)))
    _write_budget(root, wall_clock() + rest, failures)


class ReceiptSnapshotManager:
    def __init__(
        self,
        store_dir: Path | str,
        input_state: Callable[[], tuple[str, str]],
        *,
        projector_version: str = RECEIPT_PROJECTOR_VERSION,
        cpu_fraction: float = 0.10,
        settle_seconds: float = 0.4,
        max_wait_seconds: float = 2.0,
        reader_window_seconds: float = 180.0,
        check_interval_seconds: float = 0.5,
        failure_backoff_seconds: float = 5.0,
        max_failure_backoff_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        if not math.isfinite(cpu_fraction) or not 0 < cpu_fraction <= 1:
            raise ValueError("cpu_fraction must be in (0,1]")
        self.store = ReceiptSnapshotStore(store_dir, projector_version=projector_version)
        self._input_state = input_state
        self._rest_factor = 1.0 / cpu_fraction - 1.0
        self._settle = max(0.0, settle_seconds)
        self._max_wait = max(0.0, max_wait_seconds)
        self._reader_window = max(0.0, reader_window_seconds)
        self._interval = max(0.05, check_interval_seconds)
        self._failure_backoff = max(0.1, failure_backoff_seconds)
        self._max_failure_backoff = max(self._failure_backoff, max_failure_backoff_seconds)
        self._clock = clock
        self._wall_clock = wall_clock
        self._popen = popen
        self._mutex = threading.RLock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._last_reader: float | None = None
        self._pending_token: tuple[str, str] | None = None
        self._first_dirty: float | None = None
        self._last_change = 0.0
        self._rest_until = 0.0
        self._failure_count = 0
        self._error: str | None = None
        self._process: Any = None
        self._lock_fd: int | None = None
        self._started_at = 0.0
        self._started_generation: str | None = None
        self._started_budget: tuple[float, int] | None = None
        self._observed_generation: str | None = None
        self._observed_budget: tuple[float, int] | None = None

    def start(self) -> None:
        """Start the inexpensive watcher; no work runs without a reader."""
        with self._mutex:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="agentacct-receipt-snapshots", daemon=True)
            self._thread.start()

    def note_reader(self) -> None:
        # Foreground activity/status must not wait behind scheduler disk work
        # (for example clearing a large unsafe generation). Atomic attribute
        # reads/writes suffice for these advisory scheduling hints.
        if self._closed:
            return
        self._last_reader = self._clock()
        if self._thread is None:
            self.start()
        self._wake.set()

    def _read_state(self) -> tuple[str, str]:
        state = self._input_state()
        if not isinstance(state, tuple) or len(state) != 2 or not all(isinstance(v, str) and v for v in state):
            raise ValueError("invalid snapshot input state")
        return state

    def read(self, kind: str, key: str) -> SnapshotEntry | None:
        """Read a previous complete entry only across a matching safety epoch.

        A safety change during the read also refuses the payload. Logical
        invalidation is immediate; disk reclamation belongs to the worker loop.
        A missing entry in a current generation is an actual absence, never a
        reason to rebuild an unchanged store.
        """
        self.note_reader()
        try:
            before = self._read_state()
            entry = self.store.read(kind, key)
            after = self._read_state()
            if entry is None or before[1] != after[1] or entry.safety_token != after[1]:
                return None
            return entry
        except Exception:
            return None

    def status(self) -> dict[str, Any]:
        """Small foreground metadata only; never wait for a child or build."""
        generation = self.store.generation()
        try:
            input_token, safety_token = self._read_state()
        except Exception:
            return {"state": "error", "built_at": None, "generation": None,
                    "available": False, "error": "Receipt snapshot inputs are unavailable."}
        if generation is not None and generation.safety_token != safety_token:
            generation = None
        rebuilding = self._process is not None
        error = self._error
        current = generation is not None and generation.input_token == input_token
        state = "current" if current else "error" if error else "updating" if generation is not None else "pending"
        return {
            "state": state,
            "built_at": generation.built_at if generation else None,
            "generation": generation.generation_id if generation else None,
            "available": generation is not None,
            "rebuilding": rebuilding,
            "error": None if current else error,
        }

    def _forget_dirty(self) -> None:
        self._pending_token = None
        self._first_dirty = None

    def _observe_budget(self, generation: SnapshotGeneration | None) -> None:
        if generation is None or generation.generation_id == self._observed_generation:
            return
        self._observed_generation = generation.generation_id
        # Reconstruct the remaining rest on daemon restart and in other daemon
        # processes. Use monotonic time thereafter; clock rollback is conservative.
        remaining = max(0.0, generation.published_at + generation.cpu_seconds * self._rest_factor - self._wall_clock())
        self._rest_until = max(self._rest_until, self._clock() + remaining)

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            # Closing (not LOCK_UN) preserves the child's inherited lock if the
            # parent shuts down unexpectedly before the child exits.
            os.close(self._lock_fd)
            self._lock_fd = None

    def _observe_shared_budget(self) -> None:
        """Failure rest also survives restart and competing daemon processes."""
        try:
            path = self.store.root / "build-budget.json"
            if path.stat().st_size > 4096:
                return
            value = json.loads(path.read_text())
            deadline = float(value["allow_again_at"])
            failures = int(value["failures"])
            if not math.isfinite(deadline) or not 0 <= failures <= 1_000_000:
                return
            identity = (deadline, failures)
            if identity != self._observed_budget:
                self._observed_budget = identity
                self._failure_count = failures
                self._rest_until = max(self._rest_until, self._clock() + max(0.0, deadline - self._wall_clock()))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _persist_budget(self) -> None:
        # Only call while holding build.lock. No source data or payload belongs
        # here: this tiny restart checkpoint contains scheduler numbers only.
        if self._lock_fd is None:
            return
        deadline = self._wall_clock() + max(0.0, self._rest_until - self._clock())
        _write_budget(self.store.root, deadline, self._failure_count)
        self._observed_budget = (deadline, self._failure_count)

    def _failed(self, now: float, *, elapsed: float = 0.0) -> None:
        self._failure_count += 1
        backoff = min(self._max_failure_backoff, self._failure_backoff * 2 ** min(self._failure_count - 1, 16))
        self._rest_until = max(self._rest_until, now + backoff, now + elapsed * self._rest_factor)
        self._error = "Receipt snapshot rebuild failed."

    def _reap(self, now: float) -> None:
        if self._process is None:
            return
        result = self._process.poll()
        if result is None:
            return
        elapsed = max(0.0, now - self._started_at)
        self._process = None
        generation = self.store.generation()
        self._observe_shared_budget()
        worker_checkpointed = self._observed_budget != self._started_budget
        if result != 0 or generation is None or generation.generation_id == self._started_generation:
            # On failure no worker CPU receipt is trustworthy. Wall duration is
            # a conservative one-core bound, in addition to exponential backoff.
            if worker_checkpointed:
                self._error = "Receipt snapshot rebuild failed."
            else:
                self._failed(now, elapsed=elapsed)
        else:
            self._failure_count = 0
            self._error = None
            self._observe_budget(generation)
        try:
            self._persist_budget()
        except OSError:
            self._error = "Receipt snapshot scheduler checkpoint failed."
        finally:
            self._release_lock()

    def check_once(self) -> bool:
        """Apply scheduler rules once; True only when this manager spawned."""
        with self._mutex:
            if self._closed:
                return False
            now = self._clock()
            self._reap(now)
            if self._process is not None:
                return False
            if self._last_reader is None or now - self._last_reader > self._reader_window:
                self._forget_dirty()
                return False
            try:
                tokens = self._read_state()
                generation = self.store.generation()
                self._observe_shared_budget()
                self._observe_budget(generation)
                if generation is not None and generation.safety_token != tokens[1]:
                    self.store.clear(expected_generation_id=generation.generation_id)
                    generation = None
                if generation is not None and generation.input_token == tokens[0]:
                    self._error = None
                    self._forget_dirty()
                    return False
                if self._pending_token != tokens:
                    self._pending_token = tokens
                    self._first_dirty = now if self._first_dirty is None else self._first_dirty
                    self._last_change = now
                assert self._first_dirty is not None
                if now < self._rest_until or (now - self._last_change < self._settle and now - self._first_dirty < self._max_wait):
                    return False
                self.store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.store.root.chmod(0o700)
                descriptor = os.open(self.store.root / "build.lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(descriptor)
                    return False
                except BaseException:
                    os.close(descriptor)
                    raise
                self._lock_fd = descriptor
                # Recheck after acquiring the cross-process lock: another daemon
                # may have completed the exact requested generation meanwhile.
                generation = self.store.generation()
                self._observe_shared_budget()
                self._observe_budget(generation)
                if now < self._rest_until or (generation is not None and generation.input_token == tokens[0] and generation.safety_token == tokens[1]):
                    self._release_lock()
                    return False
                env = dict(os.environ)
                env[SNAPSHOT_LOCK_FD_ENV] = str(descriptor)
                env[SNAPSHOT_CPU_FRACTION_ENV] = str(1.0 / (1.0 + self._rest_factor))
                if not getattr(sys, "frozen", False):
                    # An editable install can point at another checkout while
                    # this daemon imports a worktree through sys.path. Pin the
                    # child to the same package/projector implementation.
                    source_root = str(Path(__file__).resolve().parents[1])
                    env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
                self._process = self._popen(
                    [sys.executable, "-m", "agentacct.receipt_snapshot_worker", "--store-dir", str(self.store.store_dir)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env, pass_fds=(descriptor,), close_fds=True,
                )
                self._started_at = now
                self._started_generation = generation.generation_id if generation else None
                self._started_budget = self._observed_budget
                self._error = None
                self._forget_dirty()
                return True
            except Exception:
                if now >= self._rest_until:
                    self._failed(now)
                try:
                    self._persist_budget()
                except OSError:
                    pass
                finally:
                    self._release_lock()
                return False

    def _run(self) -> None:
        while True:
            self._wake.wait(self._interval)
            self._wake.clear()
            with self._mutex:
                if self._closed:
                    return
            self.check_once()

    def close(self) -> None:
        """Stop only the child spawned by this manager, never a discovered PID."""
        with self._mutex:
            self._closed = True
            self._wake.set()
            self._reap(self._clock())
            process = self._process
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3.0)
                self._process = None
                # A restart after cancelling an expensive build must not erase
                # its consumed budget. No successful CPU receipt exists, so use
                # elapsed wall time as the conservative one-core bound.
                now = self._clock()
                self._failed(now, elapsed=max(0.0, now - self._started_at))
                try:
                    self._persist_budget()
                except OSError:
                    pass
            self._release_lock()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    stop = close
