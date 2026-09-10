"""Store-directory resolution for CLI commands and the product dashboard.

One precedence order everywhere (Phase 1 decision 4a):

1. explicit ``--store-dir`` flag,
2. ``AGENTACCT_STORE_DIR`` environment variable (must be absolute; the
   pre-rename ``AGENT_CHRONICLE_STORE_DIR`` / ``AGENT_SENTINEL_STORE_DIR`` names
   are accepted forever as aliases),
3. project-store walk-up from the current directory — stops at a ``.git``
   repository boundary, and remaps ``<owner>/.claude/worktrees/<name>``
   temporary Claude Code worktrees to the owning repository (a worktree's own
   store still wins when it exists, mirroring the hook-capture semantics in
   hooks.py),
4. EXPLICIT FAILURE with an actionable error.

There is deliberately no silent ``~/.agent-sentinel`` fallback: half the
commands defaulting to a home store and half to a project store silently split
the ledger. The resolver is PURE — it never creates directories or files.

The end-user managed ``agentacct serve`` surface is the one deliberate exception
to project-first selection. It chooses the machine-wide target when there is a
global signal: ledger records in a recognized store, a valid absolute global
override, or any existing recognized global directory (the latter converges on
the canonical target when all candidates are empty). This behavior lives in the
separate ``resolve_dashboard_store_dir`` helper. MCP writers, the explicit
``agentacct api serve`` integration surface, automation, and project workflows
retain the strict resolver above.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .env_compat import env_alias_names, legacy_env_name, legacy_env_names, read_env_alias
from .policy import default_policy_path

ENV_STORE_DIR = "AGENTACCT_STORE_DIR"
# Most-recent pre-rename alias, accepted silently forever: live installs (shell
# profiles, launchd plists, hook settings) still export it. The oldest
# ``AGENT_SENTINEL_STORE_DIR`` alias is accepted too (see env_alias_names).
LEGACY_ENV_STORE_DIR = legacy_env_name(ENV_STORE_DIR)
# Frozen pre-rename directory name: existing global installs and registration
# commands already point here. This is the documented machine-wide store, not
# the retired silent ``~/.agent-sentinel`` fallback. It stays a RECOGNIZED read
# location forever (dropping it would strand existing users' ledgers).
GLOBAL_STORE_DIRNAME = ".agent-sentinel-global"

# New canonical (XDG-shaped) global store, introduced with the default-global
# install. WRITE-ONE-NEW: fresh installs create ``$XDG_STATE_HOME/agentacct/state``
# (falling back to ``~/.local/state/agentacct/state``); the legacy dir names
# above stay recognized for READS. Deliberately NOT ``~/.agentacct`` — that path
# is already the CLI venv install dir, and a bare dotdir re-litters $HOME the
# tiny-footprint pivot is trying to shrink.
NEW_GLOBAL_STORE_APPNAME = "agentacct"
# Operator override for the canonical global store (absolute path). Accepts the
# pre-rename ``AGENT_SENTINEL_*`` alias forever, like every other env read.
ENV_GLOBAL_STORE_DIR = "AGENTACCT_GLOBAL_STORE_DIR"
LEGACY_ENV_GLOBAL_STORE_DIR = legacy_env_name(ENV_GLOBAL_STORE_DIR)
# Recognized-forever legacy global store dir NAMES (the ``-global`` dot family).
# Matched structurally (location-independent) so a ``--store-dir`` pointed at one
# of these anywhere still classifies as a machine-wide store.
LEGACY_GLOBAL_STORE_DIRNAMES = (".agent-sentinel-global", ".agent-chronicle-global")


@dataclass(frozen=True)
class StoreResolution:
    path: Path
    source: str  # "flag" | "env" | "project" | "global" (dashboard only)
    project_root: Path | None  # set iff source == "project"
    worktree_remapped: bool  # True when a .claude/worktrees owner remap was applied


class StoreResolutionError(RuntimeError):
    """No store could be resolved. The message is user-ready and actionable."""


def store_env_dir_value(environment: Mapping[str, str]) -> str | None:
    """The effective store-dir env value across the new/legacy alias chain.

    ``AGENTACCT_STORE_DIR`` wins, then the pre-rename ``AGENT_CHRONICLE_STORE_DIR``,
    then the oldest ``AGENT_SENTINEL_STORE_DIR`` — all accepted silently. When
    TWO of them are set to DIFFERENT paths this refuses instead of picking one:
    two names pointing at two stores is a silently split ledger (the Phase 1
    rule this module enforces). The refusal names the conflicting variables.
    Hook capture (hooks.py) and every CLI consumer share this exact rule so
    they can never resolve to different stores.
    """
    present: list[tuple[str, str]] = []
    for name in env_alias_names(ENV_STORE_DIR):
        value = (environment.get(name) or "").strip()
        if value:
            present.append((name, value))
    if len({value for _, value in present}) > 1:
        joined = ", ".join(f"{name}={value}" for name, value in present)
        raise StoreResolutionError(
            f"Conflicting store-dir environment variables ({joined}). Two store "
            "variables pointing at two stores would silently split the ledger. "
            f"Unset all but one — keep {ENV_STORE_DIR} — or make them equal."
        )
    return present[0][1] if present else None


def global_store_env_dir_value(environment: Mapping[str, str]) -> str | None:
    """Return an absolute global-store override, refusing unsafe aliases.

    A new client may read ``AGENTACCT_GLOBAL_STORE_DIR`` while an older client
    still reads one of the pre-rename names. Silently preferring the new name
    when those values differ would send the two clients to different ledgers.
    Equal values remain compatible; blank values are ignored.
    """

    present: list[tuple[str, str]] = []
    for name in env_alias_names(ENV_GLOBAL_STORE_DIR):
        value = (environment.get(name) or "").strip()
        if value:
            present.append((name, value))
    if len({value for _, value in present}) > 1:
        joined = ", ".join(f"{name}={value}" for name, value in present)
        raise StoreResolutionError(
            f"Conflicting global-store environment variables ({joined}). Two "
            "global-store variables pointing at two stores would silently split "
            f"the ledger. Unset all but one — keep {ENV_GLOBAL_STORE_DIR} — or "
            "make them equal."
        )
    if not present:
        return None
    selected = present[0][1]
    if not Path(selected).expanduser().is_absolute():
        names = ", ".join(name for name, _ in present)
        raise StoreResolutionError(
            f"Global-store environment variable {names} must use an absolute "
            f"path, not {selected!r}. Unset it or set {ENV_GLOBAL_STORE_DIR} "
            "to an absolute path."
        )
    return selected


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
                f"{ENV_STORE_DIR} (or its pre-rename aliases "
                f"{' / '.join(legacy_env_names(ENV_STORE_DIR))}) must be an absolute path "
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
    """Return the LEGACY machine-wide store path without creating it.

    This is the pre-rename ``~/.agent-sentinel-global/state`` location. It stays
    a recognized read candidate forever; new installs write
    :func:`canonical_global_store_dir` instead.
    """

    home_dir = Path.home() if home is None else Path(home)
    return home_dir.expanduser() / GLOBAL_STORE_DIRNAME / "state"


def canonical_global_store_dir(
    *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """The WRITE-ONE-NEW global store path (XDG-shaped), without creating it.

    ``$XDG_STATE_HOME/agentacct/state`` when ``XDG_STATE_HOME`` is set to an
    absolute path, else ``~/.local/state/agentacct/state``. Pure.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    home_dir = (Path.home() if home is None else Path(home)).expanduser()
    xdg_state = (environment.get("XDG_STATE_HOME") or "").strip()
    if xdg_state:
        base = Path(xdg_state).expanduser()
        if base.is_absolute():
            return base / NEW_GLOBAL_STORE_APPNAME / "state"
    return home_dir / ".local" / "state" / NEW_GLOBAL_STORE_APPNAME / "state"


def recognized_global_store_dirs(
    *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[Path, ...]:
    """Ordered global-store read candidates: override, new canonical, legacy.

    The single source of truth for "which machine-wide stores do we recognize".
    Order is preference order for reads; every entry is recognized forever so an
    existing ledger keeps opening after the new canonical name lands. Pure.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    home_dir = (Path.home() if home is None else Path(home)).expanduser()

    ordered: list[Path] = []
    override = global_store_env_dir_value(environment)
    if override:
        ordered.append(Path(override).expanduser())
    ordered.append(canonical_global_store_dir(env=environment, home=home_dir))
    ordered.append(home_dir / GLOBAL_STORE_DIRNAME / "state")

    seen: set[str] = set()
    unique: list[Path] = []
    for path in ordered:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)


def _store_has_records(path: Path) -> bool:
    """Whether a store holds records, without writing ledger content.

    Lets an existing populated legacy store win over a freshly-created (empty)
    canonical store during migration, so an upgrading user's dashboard does not
    silently go blank before they merge.
    """

    try:
        events = path / "events.jsonl"
        if events.is_file() and events.stat().st_size > 0:
            return True
        # A store cut over to the SQLite event log has no events.jsonl. SQLite
        # schema pages alone are not records: inspect the authoritative table
        # read-only so a fresh canonical DB cannot hide populated legacy data.
        sqlite_log = path / "events.sqlite3"
        if sqlite_log.is_file():
            connection: sqlite3.Connection | None = None
            try:
                uri = sqlite_log.resolve(strict=False).as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                return connection.execute(
                    "SELECT 1 FROM event_lines LIMIT 1"
                ).fetchone() is not None
            except sqlite3.Error as exc:
                raise StoreResolutionError(
                    f"Could not inspect the global event ledger at {sqlite_log}: {exc}. "
                    "Repair or move that file before selecting another global store."
                ) from exc
            finally:
                if connection is not None:
                    connection.close()
    except OSError:
        return False
    return False


def _same_store_path(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except (OSError, RuntimeError):
        return False


def _global_store_target_and_signal(
    *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[Path, bool]:
    """Return onboarding's target and whether global mode is actually present.

    Records win in recognized order. With no records, an absolute operator
    override is an explicit global signal; otherwise any existing recognized
    global directory is a signal to use the canonical fresh target. With no
    signal at all, the canonical path is returned only as a prospective target
    so callers such as project-first readers may retain their normal fallback.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    home_dir = (Path.home() if home is None else Path(home)).expanduser()
    candidates = recognized_global_store_dirs(env=environment, home=home_dir)
    for candidate in candidates:
        if candidate.is_dir() and _store_has_records(candidate):
            return candidate, True

    override = global_store_env_dir_value(environment)
    if override:
        return Path(override).expanduser(), True

    canonical = canonical_global_store_dir(env=environment, home=home_dir)
    if any(candidate.is_dir() for candidate in candidates):
        return canonical, True
    return canonical, False


def onboard_global_store_dir(
    *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[Path, bool]:
    """The global store a global install should use, plus whether it has records.

    Existing records win in recognized preference order (operator override,
    canonical XDG store, legacy store), so an upgrade never strands history.
    With no records, a valid absolute operator override is the creation target;
    otherwise a fresh machine uses the canonical XDG store.

    Returns ``(store_dir, pre_existing)`` where the compatibility-named boolean
    means the selected store already holds a non-empty ledger artifact. Pure:
    never creates anything.
    """

    target, _has_global_signal = _global_store_target_and_signal(env=env, home=home)
    return target, target.is_dir() and _store_has_records(target)


def is_recognized_global_store(
    store_dir: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> bool:
    """Whether a store path is one of agentacct's machine-wide stores.

    True for the legacy ``-global`` dot family (matched structurally, so a
    ``--store-dir`` at one of those anywhere counts) and for the new canonical /
    operator-override location (matched by path, since ``agentacct/state`` is too
    generic a shape to match structurally). Used for the "All projects" label.
    """

    path = Path(store_dir).expanduser()
    if path.name == "state" and path.parent.name in LEGACY_GLOBAL_STORE_DIRNAMES:
        return True

    # This helper is a label/classification predicate for a store path that has
    # already been selected. Conflicting GLOBAL aliases must still fail closed
    # in the resolvers that choose a store, but they are unrelated to an
    # explicit ``--store-dir`` and must not make API construction crash. When
    # the aliases are ambiguous, retain only the unambiguous canonical path;
    # neither conflicting override is safe to call "All projects".
    environment: Mapping[str, str] = os.environ if env is None else env
    home_dir = (Path.home() if home is None else Path(home)).expanduser()
    try:
        candidates = recognized_global_store_dirs(env=environment, home=home_dir)
    except StoreResolutionError:
        candidates = (canonical_global_store_dir(env=environment, home=home_dir),)
    for candidate in candidates:
        if _same_store_path(candidate, path):
            return True
    return False


def resolve_dashboard_store_dir(
    explicit: Path | str | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> StoreResolution:
    """Resolve the human-facing dashboard store.

    Explicit flags and environment variables retain the shared resolver's
    precedence and validation. With neither ordinary store override, recognized
    global stores are tried in preference order (operator override, canonical,
    legacy) and a store holding records wins. With no records, an absolute
    global override wins; any existing recognized global directory otherwise
    converges on the canonical fresh target. Only when there is no global signal
    does the normal project walk-up/failure behavior remain. Pure: never creates
    state.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    if explicit is not None:
        return resolve_store_dir(explicit, cwd=cwd, env=environment)
    if store_env_dir_value(environment):
        return resolve_store_dir(None, cwd=cwd, env=environment)

    chosen, has_global_signal = _global_store_target_and_signal(
        env=environment,
        home=home,
    )
    if has_global_signal:
        return StoreResolution(
            path=chosen,
            source="global",
            project_root=None,
            worktree_remapped=False,
        )

    return resolve_store_dir(None, cwd=cwd, env=environment)


def resolve_read_store_dir(
    explicit: Path | str | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> StoreResolution:
    """Resolve the store for a read-only display command (``tui`` / ``now`` / ``limits``).

    Project-first, then global. An explicit ``--store-dir`` or ``AGENTACCT_STORE_DIR``
    wins (the shared resolver's precedence and validation), then a project store found
    on the walk-up. ONLY when neither is present — the common case for a global-by-
    default install run from an arbitrary directory — does it fall back to the machine-
    wide store (preferring one that already holds records) so ``agentacct tui`` just
    works from anywhere instead of erroring with "No agentacct store found".
    Global selection uses the same records/override/canonical rule as onboarding,
    so an empty legacy directory cannot split the CLI from the packaged App.

    This is deliberately narrower than :func:`resolve_dashboard_store_dir`, which
    prefers the global store even when a project store exists: a project install that
    runs ``tui`` from inside its own repo still sees its project store. When there is
    no project store AND no global store either, the shared resolver's actionable error
    is preserved. Pure: never creates state; only stats candidates for existence.
    """

    environment: Mapping[str, str] = os.environ if env is None else env
    # An explicit --store-dir or an AGENTACCT_STORE_DIR override must behave EXACTLY
    # as the strict resolver — including its absolute-path and conflicting-alias
    # guards — so a MISCONFIGURED override (a relative value, or two aliases at
    # different paths) surfaces its error instead of being swallowed into a silent
    # global-store read. The fallback below is only for the "no override at all AND
    # no project store on the walk-up" case. (store_env_dir_value itself raises on
    # conflicting aliases; that propagates here, which is exactly what we want.)
    if explicit is not None or store_env_dir_value(environment):
        return resolve_store_dir(explicit, cwd=cwd, env=environment)
    try:
        return resolve_store_dir(None, cwd=cwd, env=environment)
    except StoreResolutionError:
        # No override and no project store on the walk-up: for a global install that
        # is exactly when the machine-wide store is the intended target, so fall back
        # to it rather than force a --store-dir on every read command.
        chosen, has_global_signal = _global_store_target_and_signal(
            env=environment,
            home=home,
        )
        if has_global_signal:
            return StoreResolution(
                path=chosen,
                source="global",
                project_root=None,
                worktree_remapped=False,
            )
        raise
