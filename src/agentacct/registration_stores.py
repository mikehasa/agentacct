"""Which store the user's agent clients actually record into.

agentacct pins a store to every MCP registration by argv (``mcp serve
--store-dir <path>``), while cwd-relative read commands (``tui`` / ``now`` /
``limits``) pick their store by walking up from the current directory. Those
two rules can silently disagree: a repo with a leftover ``.agent-sentinel/state``
(from ``init`` or an old ``onboard --scope project``) makes ``tui`` read the
project store while every session on the machine records into the global one.
The dashboard then shows the session's usage with no work attached
("observed", 0 joined items) and nothing says why.

This module reads the USER-scope registrations back — best-effort and
read-only — so the CLI can name the store sessions write to whenever it is
about to read from, or create, a different one. Project-scope registrations
(``<repo>/.mcp.json``) are deliberately not consulted here: when one exists it
points at the project store by construction, and ``mcp doctor`` already checks
it against the resolved store.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .store_resolution import StoreResolution, same_store_path

# Every registration key agentacct has ever written; pre-rename names stay
# recognized forever (a legacy install still records under them).
MCP_SERVER_KEYS: tuple[str, ...] = ("agentacct", "agent-chronicle", "agent-sentinel")


@dataclass(frozen=True)
class RegisteredStore:
    """One user-scope MCP registration and the store its argv binds."""

    client: str  # "claude-code" | "codex"
    config_path: Path
    store_dir: Path


def _store_dir_from_args(args: object) -> Path | None:
    """The ``--store-dir`` value from an MCP argv list, or None.

    A relative value cannot be compared against anything from user scope (it
    depends on the client's cwd), so it is treated as unknown rather than
    guessed at.
    """
    if not isinstance(args, Sequence) or isinstance(args, (str, bytes)):
        return None
    values = [str(item) for item in args]
    if "--store-dir" not in values:
        return None
    index = values.index("--store-dir")
    if index + 1 >= len(values):
        return None
    path = Path(values[index + 1]).expanduser()
    return path if path.is_absolute() else None


def _claude_user_registrations(home: Path) -> Iterable[RegisteredStore]:
    config_path = home / ".claude.json"
    if not config_path.is_file():
        return
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        return
    for key in MCP_SERVER_KEYS:
        server = servers.get(key)
        if not isinstance(server, dict):
            continue
        store_dir = _store_dir_from_args(server.get("args"))
        if store_dir is not None:
            yield RegisteredStore(client="claude-code", config_path=config_path, store_dir=store_dir)


def _codex_user_registrations(home: Path) -> Iterable[RegisteredStore]:
    config_path = home / ".codex" / "config.toml"
    if not config_path.is_file():
        return
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return
    for key in MCP_SERVER_KEYS:
        server = servers.get(key)
        if not isinstance(server, dict):
            continue
        store_dir = _store_dir_from_args(server.get("args"))
        if store_dir is not None:
            yield RegisteredStore(client="codex", config_path=config_path, store_dir=store_dir)


def user_scope_registered_stores(*, home: Path | None = None) -> list[RegisteredStore]:
    """Every user-scope MCP registration with an absolute ``--store-dir``.

    Pure read; never raises on a missing, unreadable, or malformed config
    (an unparseable file simply contributes nothing).
    """
    home_dir = Path.home() if home is None else Path(home)
    found: list[RegisteredStore] = []
    found.extend(_claude_user_registrations(home_dir))
    found.extend(_codex_user_registrations(home_dir))
    return found


def registrations_elsewhere(store_dir: Path | str, *, home: Path | None = None) -> list[RegisteredStore]:
    """User-scope registrations whose store is NOT ``store_dir``.

    These are the clients whose sessions will never write into ``store_dir``;
    a non-empty result means reading or creating ``store_dir`` splits the
    view from the ledger.
    """
    target = Path(store_dir)
    return [row for row in user_scope_registered_stores(home=home) if not same_store_path(row.store_dir, target)]


def _distinct_store_dirs(rows: Iterable[RegisteredStore]) -> list[Path]:
    distinct: list[Path] = []
    for row in rows:
        if not any(same_store_path(row.store_dir, seen) for seen in distinct):
            distinct.append(row.store_dir)
    return distinct


def _clients_text(rows: Iterable[RegisteredStore]) -> str:
    return ", ".join(f"{row.client}: {row.config_path}" for row in rows)


def read_store_shadow_notice(
    resolution: StoreResolution, *, command: str, home: Path | None = None
) -> str | None:
    """One-line notice when a read command resolved a PROJECT store that the
    user's clients do not record into. None when there is nothing to say.

    Only a project-walk-up resolution can shadow the global ledger: an explicit
    ``--store-dir`` / env override is the user's own choice, and a global
    fallback is already the store sessions write to.
    """
    if resolution.source != "project":
        return None
    elsewhere = registrations_elsewhere(resolution.path, home=home)
    if not elsewhere:
        return None
    targets = _distinct_store_dirs(elsewhere)
    if len(targets) == 1:
        target = targets[0]
        return (
            f"Reading project store {resolution.path}. Your sessions record to {target} "
            f"({_clients_text(elsewhere)}) — run `agentacct {command} --store-dir {target}` to see them."
        )
    listed = "; ".join(f"{row.store_dir} ({row.client})" for row in elsewhere)
    return (
        f"Reading project store {resolution.path}. Your sessions record elsewhere: {listed} — "
        f"pass `--store-dir <path>` to `agentacct {command}` to see them."
    )


def project_store_create_warning(store_dir: Path | str, *, home: Path | None = None) -> list[str]:
    """Lines to print before creating a PROJECT store on a machine whose
    user-scope clients record somewhere else. Empty when there is no conflict.
    """
    elsewhere = registrations_elsewhere(store_dir, home=home)
    if not elsewhere:
        return []
    targets = _distinct_store_dirs(elsewhere)
    where = targets[0] if len(targets) == 1 else "another store"
    lines = [
        f"This machine already records to {where} ({_clients_text(elsewhere)}).",
        f"Creating {store_dir} makes cwd-relative commands run inside this repo (tui, now, limits) "
        "read THIS store instead, while sessions keep recording to the machine-wide one unless this "
        "repo registers its own MCP server (.mcp.json shadows the user-level entry).",
        "Pass `--store-dir` to the read commands to pick a view explicitly, or remove the project "
        "store later to return to the machine-wide default.",
    ]
    return lines
