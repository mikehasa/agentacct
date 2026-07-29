"""Generate and install the OS launcher that keeps the managed runtime alive.

LOCKED DESIGN DECISION: the managed runtime is the sole supervisor of its own
components (the usage watcher and the dashboard). An OS launcher may ONLY
bootstrap and keep-alive the managed runtime; it must never own the
watcher/dashboard directly. So the launchd plist / systemd unit runs ONE
foreground ``agentacct start --foreground`` command that IS the managed-runtime
supervisor.

This module holds the pure generation logic (path resolution + file contents)
and the observe-only install/uninstall orchestration. Absolute paths for the
executable and the store are embedded because GUI/login-launched services do
not inherit the shell PATH or env. The install/uninstall functions only ever
write or remove the ONE managed file at its fixed managed path and never touch
any other launchd/systemd unit.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

# The managed dashboard defaults; --host/--port are embedded only when the user
# overrides them, so the generated unit stays minimal and matches `start`.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Stable reverse-DNS label / unit name for the single managed file.
LAUNCHD_LABEL = "dev.agentacct.runtime"
SYSTEMD_UNIT_NAME = "agentacct-runtime"
SYSTEMD_SERVICE_FILENAME = f"{SYSTEMD_UNIT_NAME}.service"

# Injected subprocess runner (defaults to subprocess.run). Tests pass a fake so
# no launchctl/systemctl is invoked on the host.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[bytes]"]


class AutostartError(RuntimeError):
    """A managed-autostart operation could not be completed safely."""


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(list(argv), check=False, capture_output=True)


def build_program_arguments(
    executable: str,
    store_dir: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> list[str]:
    """The exact argv the OS launcher runs: the foreground managed supervisor.

    ``--host``/``--port`` are appended only when they differ from the defaults
    so an uncustomized install produces the smallest honest command.
    """

    argv = [executable, "start", "--foreground", "--store-dir", store_dir]
    if host != DEFAULT_HOST:
        argv += ["--host", host]
    if int(port) != DEFAULT_PORT:
        argv += ["--port", str(int(port))]
    return argv


def launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / SYSTEMD_SERVICE_FILENAME


def _log_paths(store_dir: str) -> tuple[Path, Path]:
    """Launcher stdout/stderr logs, next to the store (not inside the ledger)."""

    base = Path(store_dir).parent
    return base / "autostart.out.log", base / "autostart.err.log"


def render_launchd_plist(
    executable: str,
    store_dir: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Emit valid launchd plist XML via plistlib for the managed runtime."""

    out_log, err_log = _log_paths(store_dir)
    document = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": build_program_arguments(
            executable, store_dir, host=host, port=port
        ),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
    }
    return plistlib.dumps(document, sort_keys=False).decode("utf-8")


def render_systemd_unit(
    executable: str,
    store_dir: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Emit a systemd USER unit whose ExecStart is the foreground supervisor."""

    exec_start = " ".join(
        shlex.quote(part)
        for part in build_program_arguments(executable, store_dir, host=host, port=port)
    )
    return (
        "[Unit]\n"
        "Description=agentacct managed runtime\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


@dataclass(frozen=True)
class AutostartPlan:
    """A fully-resolved plan for the one managed launcher file."""

    platform: str
    path: Path
    content: str
    # load_mode="fallback" tries alternatives until one succeeds (launchd:
    # modern bootstrap, else legacy load). load_mode="sequence" runs every
    # command in order (systemd: daemon-reload then enable --now).
    load_mode: str
    load_commands: tuple[tuple[str, ...], ...]
    unload_commands: tuple[tuple[str, ...], ...]
    check_command: tuple[str, ...]
    # Best-effort commands run just before load so re-install is idempotent:
    # launchd `bootstrap` fails on an already-loaded label, so boot the stale
    # instance out first (and reload the freshly-written unit). Failures here
    # are expected when nothing is loaded yet and are ignored.
    pre_load_commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class AutostartResult:
    """What an install/uninstall actually did (or would do under --dry-run)."""

    platform: str
    path: Path
    content: str | None
    commands: tuple[tuple[str, ...], ...]
    dry_run: bool
    wrote_file: bool
    removed_file: bool
    ran_commands: bool


def _uid_specifier(uid: int) -> str:
    return f"gui/{uid}"


def plan_install(
    *,
    platform: str,
    home: Path,
    uid: int,
    executable: str,
    store_dir: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> AutostartPlan:
    """Resolve the managed file path, its content, and loader/unloader commands.

    POSIX-only. ``platform`` is passed in (never read from ``sys`` here) so the
    generation is unit-testable on any host.
    """

    if platform == "darwin":
        path = launchd_plist_path(home)
        content = render_launchd_plist(executable, store_dir, host=host, port=port)
        target = _uid_specifier(uid)
        return AutostartPlan(
            platform="darwin",
            path=path,
            content=content,
            load_mode="fallback",
            # bootstrap is the modern loader; fall back to legacy load.
            load_commands=(
                ("launchctl", "bootstrap", target, str(path)),
                ("launchctl", "load", str(path)),
            ),
            unload_commands=(
                ("launchctl", "bootout", target, str(path)),
                ("launchctl", "unload", str(path)),
            ),
            check_command=("launchctl", "print", f"{target}/{LAUNCHD_LABEL}"),
            # Idempotent reload: boot out an already-loaded instance (best-effort)
            # so bootstrap loads the freshly-written plist instead of failing.
            pre_load_commands=(("launchctl", "bootout", target, str(path)),),
        )
    if platform.startswith("linux"):
        path = systemd_unit_path(home)
        content = render_systemd_unit(executable, store_dir, host=host, port=port)
        return AutostartPlan(
            platform="linux",
            path=path,
            content=content,
            load_mode="sequence",
            load_commands=(
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE_FILENAME),
            ),
            unload_commands=(
                ("systemctl", "--user", "disable", "--now", SYSTEMD_SERVICE_FILENAME),
                ("systemctl", "--user", "daemon-reload"),
            ),
            check_command=("systemctl", "--user", "status", SYSTEMD_SERVICE_FILENAME),
        )
    raise AutostartError(
        f"autostart supports macOS and Linux only (got platform {platform!r})"
    )


def plan_uninstall(*, platform: str, home: Path, uid: int) -> AutostartPlan:
    """Resolve the managed file path plus its unloader commands, for removal."""

    # Content/host/port are irrelevant to removal; reuse plan_install with
    # placeholder values so the path + unload commands stay in one place.
    return plan_install(
        platform=platform,
        home=home,
        uid=uid,
        executable="agentacct",
        store_dir=str(home / ".agent-sentinel" / "state"),
    )


def install_autostart(
    plan: AutostartPlan,
    *,
    dry_run: bool = False,
    runner: Runner = _default_runner,
) -> AutostartResult:
    """Write the ONE managed file and load it, unless ``dry_run``.

    Idempotent: the managed file is fully owned by agentacct, so re-running
    overwrites it in place. Nothing outside the managed path is touched.
    """

    if dry_run:
        return AutostartResult(
            platform=plan.platform,
            path=plan.path,
            content=plan.content,
            commands=plan.load_commands,
            dry_run=True,
            wrote_file=False,
            removed_file=False,
            ran_commands=False,
        )

    plan.path.parent.mkdir(parents=True, exist_ok=True)
    plan.path.write_text(plan.content, encoding="utf-8")

    # Best-effort reload prep (idempotency): boot out any already-loaded stale
    # instance so the freshly-written unit loads cleanly. Failures are expected
    # when nothing is loaded yet, so they are ignored and never counted as
    # "ran" loader commands.
    for command in plan.pre_load_commands:
        try:
            runner(command)
        except OSError:
            pass

    ran: list[tuple[str, ...]] = []
    last_error: Exception | None = None
    loaded = not plan.load_commands
    if plan.load_mode == "fallback":
        # Try alternatives until one succeeds (modern loader, then legacy).
        for command in plan.load_commands:
            try:
                completed = runner(command)
            except OSError as exc:  # loader binary missing, etc.
                last_error = exc
                continue
            ran.append(command)
            if getattr(completed, "returncode", 0) == 0:
                loaded = True
                break
    else:
        # Sequence: every command must run and succeed, in order.
        loaded = True
        for command in plan.load_commands:
            try:
                completed = runner(command)
            except OSError as exc:
                last_error = exc
                loaded = False
                break
            ran.append(command)
            if getattr(completed, "returncode", 0) != 0:
                loaded = False
                break
    if not loaded:
        detail = f": {last_error}" if last_error is not None else ""
        raise AutostartError(
            "wrote the managed unit at "
            f"{plan.path} but the loader command failed{detail}. "
            "The file is in place; load it manually to enable autostart."
        )
    return AutostartResult(
        platform=plan.platform,
        path=plan.path,
        content=plan.content,
        commands=tuple(ran),
        dry_run=False,
        wrote_file=True,
        removed_file=False,
        ran_commands=bool(ran),
    )


def uninstall_autostart(
    plan: AutostartPlan,
    *,
    dry_run: bool = False,
    runner: Runner = _default_runner,
) -> AutostartResult:
    """Unload then remove the managed file. Idempotent: a no-op if absent.

    The unloader runs before removal so the service is disabled even when the
    file was already unloaded. Missing file or already-disabled unit are not
    errors.
    """

    exists = plan.path.exists()
    if dry_run:
        return AutostartResult(
            platform=plan.platform,
            path=plan.path,
            content=None,
            commands=plan.unload_commands if exists else (),
            dry_run=True,
            wrote_file=False,
            removed_file=False,
            ran_commands=False,
        )

    ran: list[tuple[str, ...]] = []
    if exists:
        for command in plan.unload_commands:
            try:
                runner(command)
            except OSError:
                # Unloader binary missing is not fatal to removing the file;
                # the managed file is still deleted below.
                pass
            else:
                ran.append(command)
    removed = False
    try:
        plan.path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    return AutostartResult(
        platform=plan.platform,
        path=plan.path,
        content=None,
        commands=tuple(ran),
        dry_run=False,
        wrote_file=False,
        removed_file=removed,
        ran_commands=bool(ran),
    )
