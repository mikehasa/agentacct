"""Store-directory resolution for CLI commands and the product dashboard.

One precedence order everywhere (Phase 1 decision 4a):

1. explicit ``--store-dir`` flag,
2. ``AGENT_CHRONICLE_STORE_DIR`` environment variable (must be absolute; the
   pre-rename ``AGENT_SENTINEL_STORE_DIR`` is accepted forever as an alias),
3. project-store walk-up from the current directory — stops at a ``.git``
   repository boundary, and remaps ``<owner>/.claude/worktrees/<name>``
   temporary Claude Code worktrees to the owning repository (a worktree's own
   store still wins when it exists, mirroring the hook-capture semantics in
   hooks.py),
4. EXPLICIT FAILURE with an actionable error.

There is deliberately no silent ``~/.agent-sentinel`` fallback: half the
commands defaulting to a home store and half to a project store silently split
the ledger. The resolver is PURE — it never creates directories or files.

The end-user ``serve`` surface is the one deliberate exception to project-first
selection: when the documented machine-wide store already exists, the product
dashboard opens that all-projects store. This behavior lives in the separate
``resolve_dashboard_store_dir`` helper so MCP writers, API servers, automation,
and project workflows retain the strict resolver above.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .env_compat import legacy_env_name
from .policy import default_policy_path

ENV_STORE_DIR = "AGENT_CHRONICLE_STORE_DIR"
# Pre-rename alias, accepted silently forever: live installs (shell profiles,
# launchd plists, hook settings) still export it.
LEGACY_ENV_STORE_DIR = legacy_env_name(ENV_STORE_DIR)
# Frozen pre-rename directory name: existing global installs and registration
# commands already point here. This is the documented machine-wide store, not
# the retired silent ``~/.agent-sentinel`` fallback.
GLOBAL_STORE_DIRNAME = ".agent-sentinel-global"


@dataclass(frozen=True)
class StoreResolution:
    path: Path
    source: str  # "flag" | "env" | "project" | "global" (dashboard only)
    project_root: Path | None  # set iff source == "project"
    worktree_remapped: bool  # True when a .claude/worktrees owner remap was applied


class StoreResolutionError(RuntimeError):
    """No store could be resolved. The message is user-ready and actionable."""


def store_env_dir_value(environment: Mapping[str, str]) -> str | None:
    """The effective store-dir env value under the new/legacy alias pair.

    ``AGENT_CHRONICLE_STORE_DIR`` wins; the pre-rename
    ``AGENT_SENTINEL_STORE_DIR`` is accepted silently. When BOTH are set to
    DIFFERENT paths this refuses instead of picking one: two names pointing at
    two stores is a silently split ledger (the Phase 1 rule this module
    enforces). Hook capture (hooks.py) and every CLI consumer share this exact
    rule so they can never resolve to different stores.
    """
    new_value = (environment.get(ENV_STORE_DIR) or "").strip() or None
    legacy_value = (environment.get(LEGACY_ENV_STORE_DIR) or "").strip() or None
    if new_value and legacy_value and new_value != legacy_value:
        raise StoreResolutionError(
            f"{ENV_STORE_DIR} and {LEGACY_ENV_STORE_DIR} are both set but point at different paths "
            f"({new_value} vs {legacy_value}). Two store variables pointing at two stores would silently "
            f"split the ledger. Unset {LEGACY_ENV_STORE_DIR} (the pre-rename alias) or make both equal."
        )
    return new_value or legacy_value


def worktree_owner_root(candidate: Path) -> Path | None:
    """Map an exact ``<owner>/.claude/worktrees/<name>`` root to its owner.

    Shared with the Claude Code hook store resolution (hooks.py) so hook
    capture and CLI commands agree on which store a worktree session uses.
    """
    parent = candidate.parent
    grandparent = parent.parent
    if candidate.name and parent.name == "worktrees" and grandparent.name == ".claude":
        owner = grandparent.parent
        if owner != candidate:
            return owner
    return None


def claude_worktree_owner_dir(project_dir: Path) -> Path | None:
    """Owner repo for ANY path inside ``<owner>/.claude/worktrees/...``.

    Marker-based so it also matches subdirectories deep inside a worktree;
    used for onboarding hints, while the walk-up below uses the exact-root
    ``worktree_owner_root`` check at repository boundaries.
    """
    resolved = str(Path(project_dir).expanduser().resolve())
    marker = f"{os.sep}.claude{os.sep}worktrees{os.sep}"
    if marker not in resolved:
        return None
    owner = resolved.split(marker, 1)[0]
    return Path(owner) if owner else None


def _is_windows_shaped_path_text(path_text: str) -> bool:
    """Drive-letter (``C:\\...``/``C:/...``) or UNC (``\\\\host\\...``) shaped."""
    if path_text.startswith("\\\\"):
        return True
    return len(path_text) >= 2 and path_text[0].isascii() and path_text[0].isalpha() and path_text[1] == ":"


def claude_worktree_owner_path_text(path_text: str) -> str | None:
    """Owner repo path text for any path inside ``<owner>/.claude/worktrees/...``.

    PURE string parse of ``claude_worktree_owner_dir``'s marker rule — no
    ``.resolve()``, no filesystem access — so it is safe for HISTORICAL paths
    (stored event metadata whose directories may no longer exist) and for
    hook-time cwds. Backslashes are treated as separators ONLY for
    Windows-shaped paths (drive letter or leading ``\\\\`` UNC), so a POSIX
    directory whose NAME contains literal backslashes (legal there) can never
    mislabel as a worktree of a directory that does not exist.
    """
    normalized = path_text.replace("\\", "/") if _is_windows_shaped_path_text(path_text) else path_text
    marker = "/.claude/worktrees/"
    if marker not in normalized:
        return None
    owner = normalized.split(marker, 1)[0]
    return owner or None


def _is_project_root(candidate: Path) -> bool:
    if default_policy_path(candidate).exists():
        return True
    # ".agent-sentinel" is frozen: historical stores/logs/files carry this
    # forever (fresh init keeps writing it too; the store dir is plumbing, not
    # brand surface — renaming it would split ledgers across versions).
    return (candidate / ".agent-sentinel" / "state").is_dir()


def _no_store_message(start: Path, stopped_at: Path) -> str:
    return (
        "No agentacct store found.\n"
        f"Searched for a project store (.agent-sentinel/state or .agent-sentinel/policy.yaml) from {start} up to {stopped_at}.\n"
        "Fix one of:\n"
        "  - run `agentacct init` at the project root you intend to own the store — the search stops at the\n"
        "    nearest .git boundary, so from inside a nested repository (submodule/vendored checkout) an outer\n"
        "    project's store is deliberately not used; init the nested root, or use one of the options below\n"
        "  - pass --store-dir <path>\n"
        f"  - set {ENV_STORE_DIR}=<absolute path>\n"
        "Note: older versions silently defaulted to ~/.agent-sentinel; reach that legacy data explicitly with --store-dir ~/.agent-sentinel."
    )


def resolve_store_dir(
    explicit: Path | str | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> StoreResolution:
    """Resolve the store directory. Pure: never creates files or directories."""
    environment: Mapping[str, str] = os.environ if env is None else env
    base_cwd = Path.cwd() if cwd is None else Path(cwd)

    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = base_cwd / path
        return StoreResolution(path=path, source="flag", project_root=None, worktree_remapped=False)

    env_value = store_env_dir_value(environment)
    if env_value:
        env_path = Path(env_value).expanduser()
        if not env_path.is_absolute():
            raise StoreResolutionError(
                f"{ENV_STORE_DIR} (or its pre-rename alias {LEGACY_ENV_STORE_DIR}) must be an absolute path "
                f"(got: {env_value}). "
                "A relative value would depend on each command's working directory and silently split the ledger."
            )
        return StoreResolution(path=env_path, source="env", project_root=None, worktree_remapped=False)

    try:
        candidate = base_cwd.resolve()
    except OSError:
        candidate = base_cwd
    start = candidate
    worktree_remapped = False
    stopped_at = candidate
    while True:
        stopped_at = candidate
        if _is_project_root(candidate):
            return StoreResolution(
                # frozen: the ".agent-sentinel" store dir name predates the
                # agentacct rename and is kept forever (see _is_project_root).
                path=candidate / ".agent-sentinel" / "state",
                source="project",
                project_root=candidate,
                worktree_remapped=worktree_remapped,
            )
        if (candidate / ".git").exists():
            owner = worktree_owner_root(candidate)
            if owner is not None:
                # Temporary Claude worktree: continue the walk from the owning
                # repo root (strictly shallower, so this terminates). The
                # worktree's own store was already checked above and wins.
                candidate = owner
                worktree_remapped = True
                continue
            # Repository boundary without a store: never leak into an
            # ancestor project's store.
            break
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    raise StoreResolutionError(_no_store_message(start, stopped_at))


def default_global_store_dir(*, home: Path | None = None) -> Path:
    """Return the documented machine-wide store path without creating it."""

    home_dir = Path.home() if home is None else Path(home)
    return home_dir.expanduser() / GLOBAL_STORE_DIRNAME / "state"


def resolve_dashboard_store_dir(
    explicit: Path | str | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> StoreResolution:
    """Resolve the human-facing dashboard store.

    Explicit flags and environment variables retain the shared resolver's
    precedence and validation. With neither override, an already-installed
    machine-wide store becomes the product home; otherwise the normal project
    walk-up/failure behavior remains unchanged. Pure: never creates state.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    if explicit is not None:
        return resolve_store_dir(explicit, cwd=cwd, env=environment)
    if store_env_dir_value(environment):
        return resolve_store_dir(explicit, cwd=cwd, env=environment)

    global_store = default_global_store_dir(home=home)
    if global_store.is_dir():
        return StoreResolution(
            path=global_store,
            source="global",
            project_root=None,
            worktree_remapped=False,
        )

    return resolve_store_dir(None, cwd=cwd, env=environment)
