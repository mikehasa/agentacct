from __future__ import annotations

import http.client
import json
import os
import signal
import time
from pathlib import Path

import pytest
import psutil

import agent_chronicle.activation as activation
from agent_chronicle.activation import (
    ManagedProcess,
    RuntimeManager,
    RuntimeManagerError,
    RuntimeState,
)


FAKE_RUNTIME = r'''#!/usr/bin/env python3
import signal
import sys
import time

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.1)
'''

STUBBORN_RUNTIME = r'''#!/usr/bin/env python3
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.1)
'''

EARLY_EXIT_RUNTIME = r'''#!/usr/bin/env python3
import time

time.sleep(3.0)
'''


def _fake_executable(tmp_path: Path, source: str = FAKE_RUNTIME) -> Path:
    path = tmp_path / "agent-chronicle"
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _wait_for(manager: RuntimeManager, wanted: str, *, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    payload = manager.status(external_watcher_running=True)
    while time.monotonic() < deadline and payload["state"] != wanted:
        time.sleep(0.05)
        payload = manager.status(external_watcher_running=True)
    return payload


def test_spawn_waits_for_a_slow_env_shebang_to_reach_the_owned_final_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path)
    manager = RuntimeManager(tmp_path / "state", executable=executable, cwd=tmp_path)
    manager.runtime_root.mkdir(parents=True)
    now = [0.0]
    nonce = "n" * 32
    pid = 4242
    final_argv = ("/virtual/venv/bin/python", str(executable), "serve")
    killed: list[tuple[int, int]] = []

    class FakeChild:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.pid = pid

    class SlowEnvExecProcess:
        def __init__(self, _pid: int) -> None:
            pass

        def create_time(self) -> float:
            return 1.0

        def cwd(self) -> str:
            return str(tmp_path)

        def cmdline(self) -> list[str]:
            if now[0] < 2.5:
                return ["/usr/bin/env", "python3", str(executable), "serve"]
            return list(final_argv)

        def environ(self) -> dict[str, str]:
            if now[0] < 2.5:
                return {}
            return {"AGENT_CHRONICLE_RUNTIME_NONCE": nonce}

    monkeypatch.setattr(activation.secrets, "token_urlsafe", lambda _length: nonce)
    monkeypatch.setattr(activation.subprocess, "Popen", FakeChild)
    monkeypatch.setattr(activation.psutil, "Process", SlowEnvExecProcess)
    monkeypatch.setattr(activation.os, "getpgid", lambda _pid: pid)
    monkeypatch.setattr(activation.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(activation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(activation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    record = manager._spawn("dashboard", (str(executable), "serve"))

    assert now[0] >= 2.5
    assert now[0] < manager.OWNERSHIP_HANDSHAKE_SECONDS
    assert record.argv == final_argv
    assert record.nonce == nonce
    assert killed == []


def test_spawn_fails_closed_when_the_nonce_never_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path)
    manager = RuntimeManager(tmp_path / "state", executable=executable, cwd=tmp_path)
    manager.runtime_root.mkdir(parents=True)
    manager.OWNERSHIP_HANDSHAKE_SECONDS = 0.5
    now = [0.0]
    pid = 4343
    killed: list[tuple[int, int]] = []

    class FakeChild:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.pid = pid

    class NeverOwnedProcess:
        def __init__(self, _pid: int) -> None:
            pass

        def create_time(self) -> float:
            return 1.0

        def cwd(self) -> str:
            return str(tmp_path)

        def cmdline(self) -> list[str]:
            return ["/usr/bin/env", "python3", str(executable), "serve"]

        def environ(self) -> dict[str, str]:
            return {}

    monkeypatch.setattr(activation.subprocess, "Popen", FakeChild)
    monkeypatch.setattr(activation.psutil, "Process", NeverOwnedProcess)
    monkeypatch.setattr(activation.os, "getpgid", lambda _pid: pid)
    monkeypatch.setattr(activation.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(activation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(activation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    with pytest.raises(RuntimeManagerError, match="ownership handshake"):
        manager._spawn("dashboard", (str(executable), "serve"))

    assert now[0] == pytest.approx(manager.OWNERSHIP_HANDSHAKE_SECONDS)
    assert killed == [(pid, signal.SIGTERM)]


def test_runtime_start_is_idempotent_and_stop_is_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45101, cwd=tmp_path)
    monkeypatch.setattr(manager, "_dashboard_health", lambda: "healthy")

    first = manager.start(external_watcher_running=True)
    second = manager.start(external_watcher_running=True)
    running = _wait_for(manager, "running")
    try:
        first_pid = first["processes"][0]["pid"]
        assert second["processes"][0]["pid"] == first_pid
        assert running["dashboard_health"] == "healthy"
        state_path = store / "runtime" / "state.json"
        assert state_path.stat().st_mode & 0o777 == 0o600
    finally:
        stopped = manager.stop()
    assert stopped["state"] == "stopped"


def test_stop_refuses_a_process_identity_mismatch(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45102, cwd=tmp_path)
    manager.start(external_watcher_running=True)
    state_path = store / "runtime" / "state.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    pgid = raw["processes"][0]["process_group_id"]
    raw["processes"][0]["create_time"] += 1000
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    try:
        with pytest.raises(RuntimeManagerError, match="no process was signalled"):
            manager.stop()
    finally:
        os.killpg(pgid, signal.SIGTERM)


def test_repair_clears_only_dead_owned_state(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45103, cwd=tmp_path)
    started = manager.start(external_watcher_running=True)
    pid = started["processes"][0]["pid"]
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and manager.status(external_watcher_running=True)["processes"][0]["state"] == "running":
        time.sleep(0.05)

    repaired = manager.repair()

    assert repaired["repaired"] is True
    assert repaired["state"] == "stopped"
    assert not (store / "runtime" / "state.json").exists()


def test_stop_escalates_owned_process_and_only_then_clears_state(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path, STUBBORN_RUNTIME)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45104, cwd=tmp_path)
    manager.TERM_GRACE_SECONDS = 0.1
    manager.KILL_GRACE_SECONDS = 1.0
    manager.start(external_watcher_running=True)

    stopped = manager.stop()

    assert stopped["state"] == "stopped"
    assert not manager.state_path.exists()


def test_zombie_process_is_dead_without_reading_partial_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path)
    manager = RuntimeManager(tmp_path / "state", executable=executable, cwd=tmp_path)
    record = ManagedProcess(
        role="dashboard",
        pid=1234,
        process_group_id=1234,
        create_time=1.0,
        executable=str(executable),
        cwd=str(tmp_path),
        argv=(str(executable),),
        nonce="nonce",
        log_path=str(tmp_path / "runtime.log"),
        started_at=1.0,
    )

    class ZombieProcess:
        def status(self) -> str:
            return psutil.STATUS_ZOMBIE

        def create_time(self) -> float:
            raise AssertionError("zombie identity must not be read")

    monkeypatch.setattr(psutil, "Process", lambda _pid: ZombieProcess())

    assert manager._matches(record) == (False, "not_running")


def test_stop_waits_through_transient_post_kill_identity_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path, STUBBORN_RUNTIME)
    manager = RuntimeManager(tmp_path / "state", executable=executable, port=45110, cwd=tmp_path)
    manager.TERM_GRACE_SECONDS = 0.1
    manager.KILL_GRACE_SECONDS = 1.0
    manager.start(external_watcher_running=True)
    real_killpg = os.killpg
    real_matches = manager._matches
    kill_sent = False
    ambiguous_reads_remaining = 2

    def tracked_killpg(process_group_id: int, requested_signal: int) -> None:
        nonlocal kill_sent
        real_killpg(process_group_id, requested_signal)
        if requested_signal == signal.SIGKILL:
            kill_sent = True

    def transitioning_matches(record: ManagedProcess) -> tuple[bool, str]:
        nonlocal ambiguous_reads_remaining
        result = real_matches(record)
        if kill_sent and result == (False, "not_running") and ambiguous_reads_remaining:
            ambiguous_reads_remaining -= 1
            return False, "identity_unreadable:OSError"
        return result

    monkeypatch.setattr(os, "killpg", tracked_killpg)
    monkeypatch.setattr(manager, "_matches", transitioning_matches)

    stopped = manager.stop()

    assert stopped["state"] == "stopped"
    assert ambiguous_reads_remaining == 0
    assert not manager.state_path.exists()


def test_stop_preserves_ownership_state_when_process_cannot_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path, STUBBORN_RUNTIME)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45105, cwd=tmp_path)
    manager.TERM_GRACE_SECONDS = 0.1
    manager.KILL_GRACE_SECONDS = 0.1
    started = manager.start(external_watcher_running=True)
    pid = started["processes"][0]["pid"]
    pgid = os.getpgid(pid)
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda _pgid, _signal: None)

    try:
        with pytest.raises(RuntimeManagerError, match="preserved ownership state"):
            manager.stop()
        assert manager.state_path.exists()
        assert manager.status(external_watcher_running=True)["processes"][0]["state"] == "running"
    finally:
        real_killpg(pgid, signal.SIGKILL)


def test_start_fails_before_persisting_when_child_exits_after_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path, EARLY_EXIT_RUNTIME)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45106, cwd=tmp_path)
    manager.STARTUP_SETTLE_SECONDS = 4.0
    monkeypatch.setattr(manager, "_dashboard_health", lambda: "healthy")

    with pytest.raises(RuntimeManagerError, match="child exited during startup"):
        manager.start(external_watcher_running=True)

    assert not manager.state_path.exists()
    assert manager.status(external_watcher_running=True)["state"] == "stopped"


def test_status_uses_endpoint_recorded_by_non_default_port_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45107, cwd=tmp_path)
    monkeypatch.setattr(manager, "_dashboard_health", lambda: "healthy")
    manager.start(external_watcher_running=True)
    observer = RuntimeManager(store, executable=executable, cwd=tmp_path)
    probes: list[tuple[str, int]] = []
    monkeypatch.setattr(
        observer,
        "_probe_dashboard_health",
        lambda host, port: probes.append((host, port)) or "healthy",
    )

    try:
        status = observer.status(external_watcher_running=True)
    finally:
        manager.stop()

    assert status["dashboard_url"] == "http://127.0.0.1:45107/"
    assert probes == [("127.0.0.1", 45107)]
    assert status["state"] == "running"


def test_runtime_state_rejects_non_object_process_entries(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45108, cwd=tmp_path)
    started = manager.start(external_watcher_running=True)
    pid = started["processes"][0]["pid"]
    pgid = os.getpgid(pid)
    raw = json.loads(manager.state_path.read_text(encoding="utf-8"))
    raw["processes"].append(7)
    manager.state_path.write_text(json.dumps(raw), encoding="utf-8")

    try:
        status = manager.status(external_watcher_running=True)
        assert status["issues"] == ["runtime_state_corrupt:ValueError"]
        with pytest.raises(RuntimeManagerError, match="ownership cannot be verified"):
            manager.stop()
    finally:
        os.killpg(pgid, signal.SIGTERM)


def test_managed_children_receive_only_allowlisted_non_secret_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path)
    store = tmp_path / "state"
    manager = RuntimeManager(store, executable=executable, port=45109, cwd=tmp_path)
    monkeypatch.setattr(manager, "_dashboard_health", lambda: "healthy")
    expected_path = os.environ.get("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    secret_keys = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "AGENT_CHRONICLE_OPENAI_API_KEY",
    )
    for key in secret_keys:
        monkeypatch.setenv(key, f"must-not-reach-child-{key}")

    started = manager.start(external_watcher_running=True)
    pid = started["processes"][0]["pid"]
    try:
        child_env = psutil.Process(pid).environ()
    finally:
        manager.stop()

    assert child_env["HOME"] == str(tmp_path / "home")
    assert child_env["PATH"] == expected_path
    assert child_env["LANG"] == "C"
    assert child_env["LC_ALL"] == "C"
    assert child_env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert child_env["AGENT_CHRONICLE_STORE_DIR"] == str(store.resolve())
    assert child_env["AGENT_CHRONICLE_RUNTIME_NONCE"]
    for key in secret_keys:
        assert key not in child_env


def test_dashboard_health_probe_is_direct_and_retries_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[str, int, float]] = []
    requests: list[tuple[str, str, dict[str, str]]] = []
    closes: list[int] = []
    sleeps: list[float] = []

    class HealthyResponse:
        status = 200

        def read(self, _limit: int) -> bytes:
            if len(attempts) == 1:
                raise http.client.IncompleteRead(b"{")
            return b'{"ok":true,"service":"agent-sentinel-local-api"}'

    class DirectConnection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            attempts.append((host, port, timeout))

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            requests.append((method, path, headers))

        def getresponse(self) -> HealthyResponse:
            return HealthyResponse()

        def close(self) -> None:
            closes.append(1)

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(activation.http.client, "HTTPConnection", DirectConnection)
    monkeypatch.setattr(activation.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert RuntimeManager._probe_dashboard_health("127.0.0.1", 8878) == "healthy"
    assert attempts == [
        ("127.0.0.1", 8878, RuntimeManager.DASHBOARD_HEALTH_TIMEOUT_SECONDS),
        ("127.0.0.1", 8878, RuntimeManager.DASHBOARD_HEALTH_TIMEOUT_SECONDS),
    ]
    assert requests == [
        (
            "GET",
            "/health",
            {"Accept": "application/json", "Connection": "close"},
        ),
        (
            "GET",
            "/health",
            {"Accept": "application/json", "Connection": "close"},
        ),
    ]
    assert closes == [1, 1]
    assert sleeps == [RuntimeManager.DASHBOARD_HEALTH_RETRY_SECONDS]


def test_dashboard_health_probe_exhausts_bounded_transport_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []
    closes: list[int] = []
    sleeps: list[float] = []

    class FailingConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            attempts.append(1)

        def request(self, *_args: object, **_kwargs: object) -> None:
            raise http.client.RemoteDisconnected("no response")

        def close(self) -> None:
            closes.append(1)

    monkeypatch.setattr(activation.http.client, "HTTPConnection", FailingConnection)
    monkeypatch.setattr(activation.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert RuntimeManager._probe_dashboard_health("127.0.0.1", 8878) == "unreachable"
    assert attempts == [1, 1]
    assert closes == [1, 1]
    assert sleeps == [RuntimeManager.DASHBOARD_HEALTH_RETRY_SECONDS]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (302, b""),
        (503, b'{"ok":false,"service":"agent-sentinel-local-api"}'),
        (200, b'{"ok":true,"service":"some-other-service"}'),
        (200, b"not-json"),
        (200, (b"[" * 2000) + (b"]" * 2000)),
        (200, b"x" * (RuntimeManager.DASHBOARD_HEALTH_MAX_BODY_BYTES + 1)),
    ],
)
def test_dashboard_health_probe_rejects_unhealthy_or_unidentified_response(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    class Response:
        def __init__(self) -> None:
            self.status = status

        def read(self, _limit: int) -> bytes:
            return body

    class DirectConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(activation.http.client, "HTTPConnection", DirectConnection)

    assert RuntimeManager._probe_dashboard_health("localhost", 8878) == "unhealthy"


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("example.com", 8878),
        ("127.0.0.1", 0),
        ("localhost", 65536),
        ("localhost", True),
    ],
)
def test_dashboard_health_probe_rejects_non_local_or_invalid_endpoint_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    port: object,
) -> None:
    monkeypatch.setattr(
        activation.http.client,
        "HTTPConnection",
        lambda *_args, **_kwargs: pytest.fail("invalid endpoint must not be probed"),
    )

    assert RuntimeManager._probe_dashboard_health(host, port) == "unreachable"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("example.com", 8878),
        ("127.0.0.1", 0),
        ("localhost", 65536),
        ("localhost", True),
        ("localhost", "8878"),
        ("localhost", 8878.9),
    ],
)
def test_runtime_state_rejects_non_local_or_invalid_recorded_endpoint(
    host: str,
    port: object,
) -> None:
    with pytest.raises(ValueError, match="runtime (host|port)"):
        RuntimeState.from_dict(
            {
                "schema_version": activation.RUNTIME_SCHEMA_VERSION,
                "store_dir": "/tmp/state",
                "host": host,
                "port": port,
                "executable": "/tmp/agent-chronicle",
                "created_at": 1.0,
                "processes": [],
            }
        )


def test_runtime_state_rejects_duplicate_process_roles(tmp_path: Path) -> None:
    executable = _fake_executable(tmp_path)
    process = ManagedProcess(
        role="dashboard",
        pid=123,
        process_group_id=123,
        create_time=1.0,
        executable=str(executable),
        cwd=str(tmp_path),
        argv=(str(executable), "serve"),
        nonce="n" * 32,
        log_path=str(tmp_path / "dashboard.log"),
        started_at=1.0,
    )
    payload = {
        "schema_version": activation.RUNTIME_SCHEMA_VERSION,
        "store_dir": str(tmp_path / "state"),
        "host": "127.0.0.1",
        "port": 8878,
        "executable": str(executable),
        "created_at": 1.0,
        "processes": [process.to_dict(), process.to_dict()],
    }

    with pytest.raises(ValueError, match="roles are duplicated"):
        RuntimeState.from_dict(payload)


def test_old_owned_dashboard_with_unreachable_health_is_degraded_not_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_executable(tmp_path)
    manager = RuntimeManager(tmp_path / "state", executable=executable, cwd=tmp_path)
    dashboard = ManagedProcess(
        role="dashboard",
        pid=4321,
        process_group_id=4321,
        create_time=1.0,
        executable=str(executable),
        cwd=str(tmp_path),
        argv=(str(executable), "serve"),
        nonce="n" * 32,
        log_path=str(tmp_path / "dashboard.log"),
        started_at=100.0,
    )
    state = RuntimeState(
        store_dir=str(manager.store_dir),
        host="127.0.0.1",
        port=8765,
        executable=str(executable),
        created_at=100.0,
        processes=(dashboard,),
    )
    monkeypatch.setattr(manager, "_read", lambda: (state, None))
    monkeypatch.setattr(manager, "_matches", lambda _record: (True, "running"))
    monkeypatch.setattr(manager, "_dashboard_health", lambda: "unreachable")

    monkeypatch.setattr(activation.time, "time", lambda: 101.0)
    fresh = manager.status(external_watcher_running=True)
    assert fresh["state"] == "starting"
    assert fresh["issues"] == []

    monkeypatch.setattr(
        activation.time,
        "time",
        lambda: 100.0 + RuntimeManager.DASHBOARD_STARTING_GRACE_SECONDS + 1.0,
    )
    old = manager.status(external_watcher_running=True)
    assert old["state"] == "degraded"
    assert old["dashboard_health"] == "unreachable"
    assert old["processes"][0]["state"] == "running"
    assert old["issues"] == [
        "dashboard_health_unverified: Dashboard process is running, but /health "
        "could not be verified after 2 direct localhost attempts. No process was stopped "
        f"or restarted. Retry status and inspect {tmp_path / 'dashboard.log'}."
    ]

    monkeypatch.setattr(manager, "_dashboard_health", lambda: "unhealthy")
    unhealthy = manager.status(external_watcher_running=True)
    assert unhealthy["state"] == "degraded"
    assert unhealthy["issues"] == [
        "dashboard_health_unhealthy: The owned dashboard process is running, but /health "
        "did not return a valid healthy agentacct response. No process was stopped or "
        f"restarted. Inspect {tmp_path / 'dashboard.log'}."
    ]
