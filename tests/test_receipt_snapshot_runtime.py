from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agentacct.receipt_snapshot_runtime import (
    ReceiptSnapshotManager, SNAPSHOT_LOCK_FD_ENV, record_worker_budget, worker_build_lock,
)


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Child:
    returncode = None
    terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    state = ["one", "safe"]
    calls = []
    children = []

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        child = Child()
        children.append(child)
        return child

    managers = []

    def make(**kwargs):
        manager = ReceiptSnapshotManager(tmp_path, lambda: tuple(state), clock=clock, wall_clock=clock, popen=spawn, **kwargs)
        manager.start = lambda: None  # exercise deterministic scheduler rules without threads
        managers.append(manager)
        return manager

    yield make, clock, state, calls, children
    for manager in managers:
        manager.close()


def finish(manager, state, child, clock, *, cpu=0.0):
    generation = manager.store.publish({("receipt", "a"): {"value": state[0]}},
                                       input_token=state[0], safety_token=state[1], built_at=clock.now, published_at=clock.now, cpu_seconds=cpu)
    child.returncode = 0
    manager.check_once()
    return generation


def test_no_work_without_readership_then_coalesces_burst(rig):
    make, clock, state, calls, children = rig
    manager = make()
    assert manager.check_once() is False and not calls
    manager.note_reader()
    assert manager.check_once() is False
    clock.now += .3
    state[0] = "two"
    assert manager.check_once() is False
    clock.now += .3
    assert manager.check_once() is False
    clock.now += .2
    assert manager.check_once() is True
    assert len(calls) == 1
    assert manager.check_once() is False
    args, kwargs = calls[0]
    assert args[0][:3] == [sys.executable, "-m", "agentacct.receipt_snapshot_worker"]
    assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
    assert int(kwargs["env"][SNAPSHOT_LOCK_FD_ENV]) in kwargs["pass_fds"]
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0].endswith("/src")
    finish(manager, state, children[0], clock)
    assert manager.status()["state"] == "current"
    for _ in range(10):
        clock.now += 10
        manager.note_reader()
        assert not manager.check_once()
    assert len(calls) == 1


def test_continuous_writes_have_maximum_debounce_delay(rig):
    make, clock, state, calls, _ = rig
    manager = make()
    manager.note_reader()
    for n in range(8):
        state[0] = str(n)
        assert not manager.check_once()
        clock.now += .25
    state[0] = "latest"
    assert manager.check_once()
    assert len(calls) == 1


def test_cpu_rest_survives_new_manager_and_no_wall_time_expiry_rebuild(rig):
    make, clock, state, calls, children = rig
    first = make(settle_seconds=0)
    first.note_reader()
    assert first.check_once()
    finish(first, state, children[0], clock, cpu=2.0)
    first.close()
    second = make(settle_seconds=0)
    second.note_reader()
    state[0] = "two"
    assert not second.check_once()
    clock.now += 17.9
    assert not second.check_once()
    clock.now += .2
    assert second.check_once()
    assert len(calls) == 2


def test_cpu_budget_uses_publication_not_old_input_capture_time(rig):
    make, clock, state, _, _ = rig
    manager = make(settle_seconds=0)
    manager.store.publish({}, input_token="old", safety_token="safe", built_at=clock.now - 60,
                          published_at=clock.now, cpu_seconds=2.0)
    manager.note_reader()
    assert not manager.check_once()
    clock.now += 17.9
    assert not manager.check_once()
    clock.now += .2
    assert manager.check_once()


def test_failure_keeps_previous_complete_snapshot_and_backs_off(rig):
    make, clock, state, calls, children = rig
    manager = make(settle_seconds=0)
    old = manager.store.publish({("receipt", "a"): {"value": "old"}}, input_token="old", safety_token="safe", built_at=clock.now - 60, published_at=clock.now - 60)
    manager.note_reader()
    assert manager.check_once()
    children[0].returncode = 1
    assert not manager.check_once()
    assert manager.status()["state"] == "error"
    assert manager.read("receipt", "a").generation_id == old.generation_id
    clock.now += 4.9
    assert not manager.check_once()
    clock.now += .2
    assert manager.check_once()
    children[1].returncode = 1
    assert not manager.check_once()
    clock.now += 9.9
    assert not manager.check_once()
    clock.now += .2
    assert manager.check_once()


def test_safety_mismatch_immediately_refuses_stale_payload(rig):
    make, clock, state, calls, _ = rig
    manager = make(settle_seconds=0)
    manager.store.publish({("receipt", "a"): {"private": "removed"}}, input_token="one", safety_token="safe", built_at=clock.now - 60, published_at=clock.now - 60)
    state[1] = "deleted"
    assert manager.read("receipt", "a") is None
    assert manager.status()["available"] is False
    assert manager.status()["state"] == "pending"
    assert manager.check_once()
    assert manager.store.read("receipt", "a") is None


def test_safety_change_during_read_refuses_payload(rig, monkeypatch):
    make, clock, state, _, _ = rig
    manager = make()
    manager.store.publish({("receipt", "a"): {}}, input_token="one", safety_token="safe", built_at=clock.now, published_at=clock.now)
    original = manager.store.read

    def read(*args):
        entry = original(*args)
        state[1] = "new"
        return entry

    monkeypatch.setattr(manager.store, "read", read)
    assert manager.read("receipt", "a") is None


def test_missing_entry_in_current_generation_does_not_trigger_rebuild(rig):
    make, clock, state, calls, _ = rig
    manager = make(settle_seconds=0)
    manager.store.publish({}, input_token="one", safety_token="safe", built_at=clock.now, published_at=clock.now)
    assert manager.read("receipt", "missing") is None
    assert not manager.check_once() and not calls
    assert manager.status()["state"] == "current"


def test_cross_process_lock_prevents_second_manager_and_close_only_owns_child(rig):
    make, _, _, calls, children = rig
    first, second = make(settle_seconds=0), make(settle_seconds=0)
    first.note_reader()
    second.note_reader()
    assert first.check_once()
    assert not second.check_once()
    second.close()
    assert children[0].terminated is False
    first.close()
    assert children[0].terminated is True
    assert len(calls) == 1


def test_idle_reader_window_stops_new_builds(rig):
    make, clock, state, calls, _ = rig
    manager = make()
    manager.note_reader()
    manager.check_once()
    clock.now += 181
    state[0] = "two"
    assert not manager.check_once() and not calls


def test_spawn_failure_releases_lock_and_uses_generic_error(rig):
    make, clock, _, calls, _ = rig
    manager = make(settle_seconds=0)
    manager._popen = lambda *a, **k: (_ for _ in ()).throw(OSError("private path or content"))
    manager.note_reader()
    assert not manager.check_once()
    assert manager.status()["error"] == "Receipt snapshot rebuild failed."
    other = make(settle_seconds=0)
    other.note_reader()
    assert not other.check_once()  # failure rest is shared across daemons
    clock.now += 5.1
    assert other.check_once()


def test_cancelled_build_rest_survives_manager_restart(rig):
    make, clock, _, calls, children = rig
    first = make(settle_seconds=0)
    first.note_reader()
    assert first.check_once()
    clock.now += 2
    first.close()
    assert children[0].terminated
    second = make(settle_seconds=0)
    second.note_reader()
    assert not second.check_once()
    clock.now += 17.9
    assert not second.check_once()
    clock.now += .2
    assert second.check_once()


def test_budget_checkpoint_failure_does_not_kill_watcher(rig, monkeypatch):
    make, clock, state, _, children = rig
    manager = make(settle_seconds=0)
    manager.note_reader()
    assert manager.check_once()
    monkeypatch.setattr(manager, "_persist_budget", lambda: (_ for _ in ()).throw(OSError("disk full")))
    children[0].returncode = 1
    assert not manager.check_once()
    assert manager.status()["state"] == "error"
    clock.now += 5.1
    assert manager.check_once()


def test_shutdown_kills_only_unresponsive_owned_child(rig):
    make, _, _, _, children = rig
    manager = make(settle_seconds=0)
    manager.note_reader()
    assert manager.check_once()
    child = children[0]
    child.terminate = lambda: None
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("worker", timeout)
        return child.returncode

    child.wait = wait
    manager.close()
    assert child.returncode == -9
    assert waits == [3.0, 3.0]


def test_inherited_flock_survives_parent_descriptor_close(tmp_path):
    """Actual kernel/process regression: do not unlock an orphaned live worker."""
    lock_path = tmp_path / "build.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE, pass_fds=(fd,))
    os.close(fd)
    probe = os.open(lock_path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child.communicate(timeout=3)
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(probe)


def test_orphan_child_failure_persists_cpu_rest_without_parent_reaping(rig):
    make, clock, state, _, _ = rig
    manager = make(settle_seconds=0)
    root = manager.store.store_dir
    manager.store.root.mkdir()
    descriptor = os.open(manager.store.root / "build.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    code = """
import sys
from agentacct.receipt_snapshot_runtime import worker_build_lock, record_worker_budget
with worker_build_lock(sys.argv[1]) as acquired:
    assert acquired
    sys.stdin.read()
    try:
        raise RuntimeError('deliberate worker failure after parent lock closed')
    finally:
        record_worker_budget(sys.argv[1], cpu_seconds=2.0, failed=True, wall_clock=lambda:1000.0)
"""
    env = dict(os.environ)
    env[SNAPSHOT_LOCK_FD_ENV] = str(descriptor)
    import agentacct.receipt_snapshot_runtime as runtime
    env["PYTHONPATH"] = str(Path(runtime.__file__).resolve().parents[1])
    process = subprocess.Popen([sys.executable, "-c", code, str(root)], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(descriptor,))
    os.close(descriptor)  # parent daemon crash: it never checkpoints or reaps via manager
    process.communicate(timeout=5)
    assert process.returncode != 0
    budget = json.loads((manager.store.root / "build-budget.json").read_text())
    assert budget == {"allow_again_at": 1018.0, "failures": 1}
    manager.note_reader()
    assert not manager.check_once()
    clock.now += 18.1
    assert manager.check_once()


def test_worker_budget_success_resets_failures_and_parent_does_not_double_count(rig):
    make, clock, _, _, children = rig
    manager = make(settle_seconds=0)
    manager.note_reader()
    assert manager.check_once()
    record_worker_budget(manager.store.store_dir, cpu_seconds=0, failed=True, wall_clock=clock)
    children[0].returncode = 1
    assert not manager.check_once()
    budget = json.loads((manager.store.root / "build-budget.json").read_text())
    assert budget["failures"] == 1 and budget["allow_again_at"] == 1005.0
    with worker_build_lock(manager.store.store_dir) as acquired:
        assert acquired
        record_worker_budget(manager.store.store_dir, cpu_seconds=3, failed=False, wall_clock=clock)
    budget = json.loads((manager.store.root / "build-budget.json").read_text())
    assert budget == {"allow_again_at": 1027.0, "failures": 0}


def test_readership_and_status_do_not_wait_for_scheduler_mutex(rig):
    make, _, _, _, _ = rig
    manager = make()
    manager._thread = threading.current_thread()  # watcher already started
    acquired = threading.Event()
    release = threading.Event()

    def hold_scheduler():
        with manager._mutex:
            acquired.set()
            release.wait(3)

    thread = threading.Thread(target=hold_scheduler)
    thread.start()
    assert acquired.wait(1)
    finished = threading.Event()

    def read_foreground():
        manager.note_reader()
        manager.status()
        finished.set()

    reader = threading.Thread(target=read_foreground)
    reader.start()
    try:
        assert finished.wait(1), "foreground blocked on scheduler mutex"
    finally:
        release.set()
        thread.join(2)
        reader.join(2)
        manager._thread = None
