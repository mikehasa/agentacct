from __future__ import annotations

import plistlib
import signal
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agentacct.autostart as autostart
from agentacct.autostart import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LAUNCHD_LABEL,
    SYSTEMD_SERVICE_FILENAME,
    AutostartError,
    build_program_arguments,
    install_autostart,
    plan_install,
    plan_uninstall,
    render_launchd_plist,
    render_systemd_unit,
    uninstall_autostart,
)
from agentacct.cli import _autostart_executable, _supervise_foreground, app

ABS_EXE = "/opt/venv/bin/agentacct"
ABS_STORE = "/home/dev/.agent-sentinel/state"


class RecordingRunner:
    """Injected subprocess runner that records commands and never spawns."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.returncode = returncode

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(argv))

        class _Completed:
            def __init__(self, rc: int) -> None:
                self.returncode = rc

        return _Completed(self.returncode)


def _app_owned_cli_layout(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    stable_dir = home / ".local" / "share" / "agentacct" / "cli"
    versions_root = home / ".local" / "share" / "agentacct" / "cli-versions"
    target = versions_root / "v0.10.6-a1b2c3d4e5f6-test"
    stable_launcher = stable_dir / "agentacct"
    target_marker = stable_dir / ".agentacct-app-target"
    versions_marker = versions_root / ".agentacct-app-managed"
    target_binary = target / "agentacct"

    stable_dir.mkdir(parents=True)
    target.mkdir(parents=True)
    versions_marker.write_text("agentacct-macos-app-cli-versions-v1\n", encoding="utf-8")
    versions_marker.chmod(0o600)
    target_marker.write_text(f"{target}\n", encoding="utf-8")
    target_marker.chmod(0o600)
    # Frozen App CLIs are much larger than marker files; executable validation
    # must not accidentally apply the small-text read limit.
    target_binary.write_bytes(b"#!/bin/sh\n" + (b"#" * 9_000))
    target_binary.chmod(0o755)
    stable_launcher.write_text(
        "#!/bin/sh\n"
        f"PATH='{home / '.local' / 'bin'}':\"${{PATH:-/usr/bin:/bin:/usr/sbin:/sbin}}\"\n"
        "export PATH\n"
        f"target_file='{target_marker}'\n"
        'IFS= read -r target < "$target_file" || exit 1\n'
        '[ -n "$target" ] || exit 1\n'
        'exec "$target/agentacct" "$@"\n',
        encoding="utf-8",
    )
    stable_launcher.chmod(0o755)
    return {
        "home": home,
        "stable_launcher": stable_launcher,
        "target": target,
        "target_binary": target_binary,
        "target_marker": target_marker,
        "versions_marker": versions_marker,
    }


# --- program arguments -------------------------------------------------------


def test_program_arguments_are_the_foreground_supervisor_with_absolute_paths() -> None:
    argv = build_program_arguments(ABS_EXE, ABS_STORE)
    assert argv == [ABS_EXE, "start", "--foreground", "--store-dir", ABS_STORE]
    # The launcher runs ONLY the managed-runtime supervisor, nothing else.
    assert argv[1:3] == ["start", "--foreground"]


def test_program_arguments_embed_host_and_port_only_when_non_default() -> None:
    assert "--host" not in build_program_arguments(
        ABS_EXE, ABS_STORE, host=DEFAULT_HOST, port=DEFAULT_PORT
    )
    argv = build_program_arguments(ABS_EXE, ABS_STORE, host="localhost", port=9100)
    assert argv[-4:] == ["--host", "localhost", "--port", "9100"]


# --- launchd plist -----------------------------------------------------------


def test_launchd_plist_parses_and_has_expected_keys() -> None:
    xml = render_launchd_plist(ABS_EXE, ABS_STORE)
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert parsed["Label"] == LAUNCHD_LABEL
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"] == [
        ABS_EXE,
        "start",
        "--foreground",
        "--store-dir",
        ABS_STORE,
    ]
    # Absolute exe + store embedded (login services do not inherit PATH).
    assert Path(parsed["ProgramArguments"][0]).is_absolute()
    assert ABS_STORE in parsed["ProgramArguments"]
    # Logs live next to the store, not inside the ledger.
    assert parsed["StandardOutPath"].endswith("autostart.out.log")
    assert parsed["StandardErrorPath"].endswith("autostart.err.log")


# --- systemd unit ------------------------------------------------------------


def test_systemd_unit_has_expected_execstart_restart_and_wantedby() -> None:
    unit = render_systemd_unit(ABS_EXE, ABS_STORE)
    assert f"ExecStart={ABS_EXE} start --foreground --store-dir {ABS_STORE}" in unit
    assert "Type=simple" in unit
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit
    assert "WantedBy=default.target" in unit
    assert "Description=agentacct managed runtime" in unit


def test_systemd_execstart_quotes_paths_with_spaces() -> None:
    spaced = "/home/dev/My Store/.agent-sentinel/state"
    unit = render_systemd_unit(ABS_EXE, spaced)
    assert "'/home/dev/My Store/.agent-sentinel/state'" in unit


# --- plan resolution ---------------------------------------------------------


def test_plan_install_darwin_targets_launchagents_path() -> None:
    home = Path("/home/dev")
    plan = plan_install(
        platform="darwin", home=home, uid=501, executable=ABS_EXE, store_dir=ABS_STORE
    )
    assert plan.platform == "darwin"
    assert plan.path == home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    assert plan.load_commands[0] == ("launchctl", "bootstrap", "gui/501", str(plan.path))


def test_plan_install_linux_targets_systemd_user_path() -> None:
    home = Path("/home/dev")
    plan = plan_install(
        platform="linux", home=home, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    assert plan.platform == "linux"
    assert plan.path == home / ".config" / "systemd" / "user" / SYSTEMD_SERVICE_FILENAME
    assert ("systemctl", "--user", "daemon-reload") in plan.load_commands
    assert (
        "systemctl",
        "--user",
        "enable",
        "--now",
        SYSTEMD_SERVICE_FILENAME,
    ) in plan.load_commands


def test_plan_install_rejects_unsupported_platform() -> None:
    with pytest.raises(AutostartError):
        plan_install(
            platform="sunos", home=Path("/h"), uid=0, executable=ABS_EXE, store_dir=ABS_STORE
        )


# --- install / uninstall orchestration --------------------------------------


def test_dry_run_writes_and_loads_nothing(tmp_path: Path) -> None:
    plan = plan_install(
        platform="linux", home=tmp_path, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    runner = RecordingRunner()
    result = install_autostart(plan, dry_run=True, runner=runner)
    assert result.dry_run is True
    assert result.wrote_file is False
    assert not plan.path.exists()
    assert runner.calls == []


def test_install_writes_managed_file_and_invokes_loader(tmp_path: Path) -> None:
    plan = plan_install(
        platform="linux", home=tmp_path, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    runner = RecordingRunner()
    result = install_autostart(plan, runner=runner)
    assert plan.path.exists()
    assert plan.path.read_text(encoding="utf-8") == plan.content
    assert result.wrote_file is True
    # daemon-reload + enable --now both ran.
    assert ("systemctl", "--user", "daemon-reload") in runner.calls
    assert (
        "systemctl",
        "--user",
        "enable",
        "--now",
        SYSTEMD_SERVICE_FILENAME,
    ) in runner.calls


def test_install_on_darwin_stops_at_first_successful_loader(tmp_path: Path) -> None:
    plan = plan_install(
        platform="darwin", home=tmp_path, uid=501, executable=ABS_EXE, store_dir=ABS_STORE
    )
    runner = RecordingRunner(returncode=0)
    install_autostart(plan, runner=runner)
    # Idempotent reload: boot out any stale instance first, then bootstrap
    # succeeds so the legacy `load` fallback is not attempted.
    assert runner.calls == [
        ("launchctl", "bootout", "gui/501", str(plan.path)),
        ("launchctl", "bootstrap", "gui/501", str(plan.path)),
    ]


def test_install_falls_back_to_legacy_loader_when_first_fails(tmp_path: Path) -> None:
    plan = plan_install(
        platform="darwin", home=tmp_path, uid=501, executable=ABS_EXE, store_dir=ABS_STORE
    )

    class FailFirst:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def __call__(self, argv):  # type: ignore[no-untyped-def]
            self.calls.append(tuple(argv))

            class _C:
                def __init__(self, rc: int) -> None:
                    self.returncode = rc

            return _C(1 if "bootstrap" in argv else 0)

    runner = FailFirst()
    install_autostart(plan, runner=runner)
    # The pre-load bootout runs first (best-effort), then bootstrap fails and
    # the legacy `load` fallback succeeds.
    assert runner.calls[0][:2] == ("launchctl", "bootout")
    assert runner.calls[1][:2] == ("launchctl", "bootstrap")
    assert runner.calls[2][:2] == ("launchctl", "load")


def test_install_raises_when_all_loaders_fail_but_leaves_file(tmp_path: Path) -> None:
    plan = plan_install(
        platform="linux", home=tmp_path, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    runner = RecordingRunner(returncode=1)
    with pytest.raises(AutostartError):
        install_autostart(plan, runner=runner)
    # File is still in place so the user can load it manually.
    assert plan.path.exists()


def test_install_is_idempotent_on_rerun(tmp_path: Path) -> None:
    plan = plan_install(
        platform="linux", home=tmp_path, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    install_autostart(plan, runner=RecordingRunner())
    first = plan.path.read_text(encoding="utf-8")
    # Re-running overwrites the fully-managed file in place; no error, same path.
    install_autostart(plan, runner=RecordingRunner())
    assert plan.path.exists()
    assert plan.path.read_text(encoding="utf-8") == first


def test_reinstall_on_darwin_boots_out_already_loaded_then_reloads(tmp_path: Path) -> None:
    """Regression: re-running install-autostart on macOS must not error. launchd
    `bootstrap` fails on an already-loaded label, so the pre-load `bootout`
    clears the stale instance and the freshly-written plist loads cleanly."""
    plan = plan_install(
        platform="darwin", home=tmp_path, uid=501, executable=ABS_EXE, store_dir=ABS_STORE
    )

    class StatefulLaunchd:
        def __init__(self) -> None:
            self.loaded = True  # simulate a stale already-loaded instance
            self.calls: list[tuple[str, ...]] = []

        def __call__(self, argv):  # type: ignore[no-untyped-def]
            self.calls.append(tuple(argv))

            class _C:
                def __init__(self, rc: int) -> None:
                    self.returncode = rc

            if "bootout" in argv:
                self.loaded = False
                return _C(0)
            if "bootstrap" in argv:
                return _C(5 if self.loaded else 0)  # 5 = "service already loaded"
            return _C(0)

    runner = StatefulLaunchd()
    # Must NOT raise (before the fix, bootstrap's rc=5 raised AutostartError).
    install_autostart(plan, runner=runner)
    assert ("launchctl", "bootout", "gui/501", str(plan.path)) in runner.calls
    assert ("launchctl", "bootstrap", "gui/501", str(plan.path)) in runner.calls


def test_uninstall_removes_file_and_calls_unloader(tmp_path: Path) -> None:
    plan = plan_install(
        platform="linux", home=tmp_path, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    install_autostart(plan, runner=RecordingRunner())
    assert plan.path.exists()
    uplan = plan_uninstall(platform="linux", home=tmp_path, uid=1000)
    runner = RecordingRunner()
    result = uninstall_autostart(uplan, runner=runner)
    assert result.removed_file is True
    assert not uplan.path.exists()
    assert (
        "systemctl",
        "--user",
        "disable",
        "--now",
        SYSTEMD_SERVICE_FILENAME,
    ) in runner.calls


def test_uninstall_is_noop_when_absent(tmp_path: Path) -> None:
    uplan = plan_uninstall(platform="linux", home=tmp_path, uid=1000)
    runner = RecordingRunner()
    result = uninstall_autostart(uplan, runner=runner)
    assert result.removed_file is False
    # No unloader is run when there is no managed file to unload.
    assert runner.calls == []


def test_uninstall_dry_run_changes_nothing(tmp_path: Path) -> None:
    plan = plan_install(
        platform="linux", home=tmp_path, uid=1000, executable=ABS_EXE, store_dir=ABS_STORE
    )
    install_autostart(plan, runner=RecordingRunner())
    uplan = plan_uninstall(platform="linux", home=tmp_path, uid=1000)
    runner = RecordingRunner()
    result = uninstall_autostart(uplan, dry_run=True, runner=runner)
    assert result.dry_run is True
    assert uplan.path.exists()
    assert runner.calls == []


# --- App-owned executable selection -----------------------------------------


def test_autostart_uses_stable_launcher_for_verified_app_owned_cli(tmp_path: Path) -> None:
    layout = _app_owned_cli_layout(tmp_path)

    selected = _autostart_executable(
        str(layout["target_binary"]),
        home=layout["home"],
    )

    assert selected == str(layout["stable_launcher"])


@pytest.mark.parametrize(
    "damage",
    [
        "versions_marker_contents",
        "versions_marker_permissions",
        "versions_marker_symlink",
        "target_marker_format",
        "target_marker_permissions",
        "target_marker_symlink",
        "stable_directory_permissions",
        "versions_directory_permissions",
        "target_directory_permissions",
        "target_directory_symlink",
        "stable_launcher_contents",
        "stable_launcher_permissions",
        "stable_launcher_symlink",
        "target_binary_permissions",
        "target_binary_symlink",
        "different_current_executable",
    ],
)
def test_autostart_preserves_current_executable_when_app_ownership_is_not_exact(
    tmp_path: Path,
    damage: str,
) -> None:
    layout = _app_owned_cli_layout(tmp_path)
    current = layout["target_binary"]

    if damage == "versions_marker_contents":
        layout["versions_marker"].write_text("unknown-owner\n", encoding="utf-8")
    elif damage == "versions_marker_permissions":
        layout["versions_marker"].chmod(0o644)
    elif damage == "versions_marker_symlink":
        replacement = layout["versions_marker"].with_name("versions-marker-replacement")
        layout["versions_marker"].rename(replacement)
        layout["versions_marker"].symlink_to(replacement)
    elif damage == "target_marker_format":
        layout["target_marker"].write_text(f"{layout['target']}\nextra\n", encoding="utf-8")
    elif damage == "target_marker_permissions":
        layout["target_marker"].chmod(0o644)
    elif damage == "target_marker_symlink":
        replacement = layout["target_marker"].with_name("target-marker-replacement")
        layout["target_marker"].rename(replacement)
        layout["target_marker"].symlink_to(replacement)
    elif damage == "stable_directory_permissions":
        layout["stable_launcher"].parent.chmod(0o777)
    elif damage == "versions_directory_permissions":
        layout["versions_marker"].parent.chmod(0o777)
    elif damage == "target_directory_permissions":
        layout["target"].chmod(0o777)
    elif damage == "target_directory_symlink":
        replacement = tmp_path / "target-directory-replacement"
        layout["target"].rename(replacement)
        layout["target"].symlink_to(replacement, target_is_directory=True)
        current = replacement / "agentacct"
    elif damage == "stable_launcher_contents":
        layout["stable_launcher"].write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    elif damage == "stable_launcher_permissions":
        layout["stable_launcher"].chmod(0o775)
    elif damage == "stable_launcher_symlink":
        layout["stable_launcher"].unlink()
        layout["stable_launcher"].symlink_to(layout["target_binary"])
    elif damage == "target_binary_permissions":
        layout["target_binary"].chmod(0o775)
    elif damage == "target_binary_symlink":
        replacement = layout["target_binary"].with_name("agentacct-real")
        layout["target_binary"].rename(replacement)
        layout["target_binary"].symlink_to(replacement)
    elif damage == "different_current_executable":
        current = tmp_path / "other-agentacct"
        current.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        current.chmod(0o755)
    else:  # pragma: no cover - keeps future parametrization edits explicit
        raise AssertionError(f"unknown damage case: {damage}")

    assert _autostart_executable(str(current), home=layout["home"]) == str(current)


def test_autostart_preserves_current_executable_when_files_are_not_owned_by_current_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _app_owned_cli_layout(tmp_path)
    current = str(layout["target_binary"])
    monkeypatch.setattr("agentacct.cli.os.geteuid", lambda: -1)

    assert _autostart_executable(current, home=layout["home"]) == current


# --- CLI wiring --------------------------------------------------------------


def test_cli_install_autostart_win32_exits_2_with_wsl_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentacct.cli.sys.platform", "win32")
    result = CliRunner().invoke(app, ["install-autostart"])
    assert result.exit_code == 2
    assert "WSL" in result.output


def test_cli_uninstall_autostart_win32_exits_2_with_wsl_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agentacct.cli.sys.platform", "win32")
    result = CliRunner().invoke(app, ["uninstall-autostart"])
    assert result.exit_code == 2
    assert "WSL" in result.output


def test_cli_install_autostart_dry_run_writes_and_loads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    fake_exe = tmp_path / "agentacct"
    fake_exe.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr("agentacct.cli.sys.platform", "linux")
    monkeypatch.setattr("agentacct.cli.Path.home", classmethod(lambda cls: home))
    monkeypatch.setattr("agentacct.cli.os.getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        "agentacct.cli._managed_runtime",
        lambda resolved, host="127.0.0.1", port=8765: type(
            "M", (), {"executable": str(fake_exe)}
        )(),
    )
    # Ensure the loader is never invoked under --dry-run.
    called: list[object] = []
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: called.append(1))

    result = CliRunner().invoke(
        app,
        ["install-autostart", "--store-dir", str(store), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    expected = home / ".config" / "systemd" / "user" / SYSTEMD_SERVICE_FILENAME
    assert not expected.exists()
    assert called == []
    assert "start --foreground" in " ".join(result.output.split())


def test_cli_install_autostart_uses_verified_app_stable_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _app_owned_cli_layout(tmp_path)
    store = tmp_path / "store"
    store.mkdir()

    monkeypatch.setattr("agentacct.cli.sys.platform", "linux")
    monkeypatch.setattr(
        "agentacct.cli.Path.home",
        classmethod(lambda cls: layout["home"]),
    )
    monkeypatch.setattr("agentacct.cli.os.getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        "agentacct.cli._managed_runtime",
        lambda resolved, host="127.0.0.1", port=8765: type(
            "M", (), {"executable": str(layout["target_binary"])}
        )(),
    )

    result = CliRunner().invoke(
        app,
        ["install-autostart", "--store-dir", str(store), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    unwrapped_output = "".join(result.output.splitlines())
    assert str(layout["stable_launcher"]) in unwrapped_output
    assert str(layout["target_binary"]) not in unwrapped_output


# --- foreground supervisor loop ---------------------------------------------


def test_supervise_foreground_runs_one_tick_and_tears_down_without_sleeping() -> None:
    ensures: list[int] = []
    teardowns: list[int] = []
    stop = threading.Event()

    def ensure() -> None:
        ensures.append(1)

    def teardown() -> None:
        teardowns.append(1)

    def on_tick(_n: int) -> None:
        stop.set()  # request stop after the first tick, like a signal

    ticks = _supervise_foreground(
        ensure,
        teardown,
        interval=0.0,  # no real sleep
        stop_event=stop,
        install_signal_handlers=False,
        on_tick=on_tick,
    )
    assert ticks == 1
    assert ensures == [1]
    assert teardowns == [1]


def test_supervise_foreground_signal_handler_stops_the_loop_and_restores() -> None:
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    teardowns: list[int] = []

    def on_tick(_n: int) -> None:
        # Simulate SIGTERM by invoking the installed handler directly (no real
        # signal, no real sleep).
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    ticks = _supervise_foreground(
        lambda: None,
        lambda: teardowns.append(1),
        interval=0.0,
        install_signal_handlers=True,
        on_tick=on_tick,
    )
    assert ticks == 1
    assert teardowns == [1]
    # Handlers are restored after the loop exits.
    assert signal.getsignal(signal.SIGTERM) is original_term
    assert signal.getsignal(signal.SIGINT) is original_int


def test_supervise_foreground_stops_immediately_when_event_preset() -> None:
    stop = threading.Event()
    stop.set()
    ensures: list[int] = []
    ticks = _supervise_foreground(
        lambda: ensures.append(1),
        lambda: None,
        interval=0.0,
        stop_event=stop,
        install_signal_handlers=False,
    )
    assert ticks == 0
    assert ensures == []
