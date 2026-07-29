"""Unit tests for the shared store-dir resolver (Batch 4 decision 4a).

The resolver is pure (injectable cwd/env), so these tests never depend on the
suite's own working directory or the conftest env isolation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_chronicle.store_resolution import (
    ENV_GLOBAL_STORE_DIR,
    ENV_STORE_DIR,
    StoreResolutionError,
    canonical_global_store_dir,
    claude_worktree_owner_dir,
    is_recognized_global_store,
    recognized_global_store_dirs,
    resolve_dashboard_store_dir,
    resolve_store_dir,
    worktree_owner_root,
)


def _make_project(root: Path, *, policy: bool = True, state: bool = False) -> Path:
    if policy:
        policy_path = root / ".agent-sentinel" / "policy.yaml"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text("budgets: {}\n", encoding="utf-8")
    if state:
        (root / ".agent-sentinel" / "state").mkdir(parents=True, exist_ok=True)
    return root


def _tree_snapshot(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def test_resolve_store_dir_explicit_flag_wins(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project")
    env_store = tmp_path / "env-store"
    flag_store = tmp_path / "flag-store"

    resolution = resolve_store_dir(flag_store, cwd=project, env={ENV_STORE_DIR: str(env_store)})

    assert resolution.path == flag_store
    assert resolution.source == "flag"
    assert resolution.project_root is None
    assert resolution.worktree_remapped is False


def test_resolve_store_dir_relative_flag_is_anchored_to_cwd(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project")

    resolution = resolve_store_dir(Path(".agent-sentinel/state"), cwd=project, env={})

    assert resolution.path == project / ".agent-sentinel" / "state"
    assert resolution.source == "flag"


def test_resolve_store_dir_env_beats_project_walkup(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project")
    env_store = tmp_path / "env-store"

    resolution = resolve_store_dir(None, cwd=project, env={ENV_STORE_DIR: str(env_store)})

    assert resolution.path == env_store
    assert resolution.source == "env"


def test_resolve_store_dir_env_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(StoreResolutionError) as excinfo:
        resolve_store_dir(None, cwd=tmp_path, env={ENV_STORE_DIR: "rel/path"})

    assert ENV_STORE_DIR in str(excinfo.value)
    assert "absolute" in str(excinfo.value)


def test_resolve_store_dir_walks_up_from_nested_dir(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project")
    nested = project / "pkg" / "nested"
    nested.mkdir(parents=True)

    resolution = resolve_store_dir(None, cwd=nested, env={})

    assert resolution.path == (project / ".agent-sentinel" / "state").resolve()
    assert resolution.source == "project"
    assert resolution.project_root == project.resolve()
    assert resolution.worktree_remapped is False


def test_resolve_store_dir_recognizes_state_dir_without_policy(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project", policy=False, state=True)

    resolution = resolve_store_dir(None, cwd=project, env={})

    assert resolution.path == (project / ".agent-sentinel" / "state").resolve()
    assert resolution.source == "project"


def test_resolve_store_dir_stops_at_git_boundary(tmp_path: Path) -> None:
    outer = _make_project(tmp_path / "outer")
    inner_repo = outer / "vendored" / "inner"
    inner_repo.mkdir(parents=True)
    (inner_repo / ".git").mkdir()

    with pytest.raises(StoreResolutionError):
        resolve_store_dir(None, cwd=inner_repo / "src", env={})
    # Without the inner .git boundary the same layout resolves to the outer store.
    (inner_repo / ".git").rmdir()
    resolution = resolve_store_dir(None, cwd=inner_repo, env={})
    assert resolution.project_root == outer.resolve()


def test_resolve_store_dir_worktree_remaps_to_owner(tmp_path: Path) -> None:
    owner = _make_project(tmp_path / "owner")
    (owner / ".git").mkdir()
    worktree = owner / ".claude" / "worktrees" / "feature-x"
    sub = worktree / "src" / "deep"
    sub.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")

    resolution = resolve_store_dir(None, cwd=sub, env={})

    assert resolution.path == (owner / ".agent-sentinel" / "state").resolve()
    assert resolution.project_root == owner.resolve()
    assert resolution.worktree_remapped is True


def test_resolve_store_dir_worktree_own_store_wins_over_owner_remap(tmp_path: Path) -> None:
    """Mirrors the Batch 3 hook-capture semantics: a store initialized INSIDE
    the worktree keeps winning, so hook capture and CLI agree on one ledger."""
    owner = _make_project(tmp_path / "owner")
    (owner / ".git").mkdir()
    worktree = owner / ".claude" / "worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    _make_project(worktree, policy=False, state=True)
    sub = worktree / "sub"
    sub.mkdir()

    resolution = resolve_store_dir(None, cwd=sub, env={})

    assert resolution.path == (worktree / ".agent-sentinel" / "state").resolve()
    assert resolution.project_root == worktree.resolve()
    assert resolution.worktree_remapped is False


def test_resolve_store_dir_worktree_without_any_store_fails(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    worktree = owner / ".claude" / "worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (owner / ".git").mkdir()

    with pytest.raises(StoreResolutionError):
        resolve_store_dir(None, cwd=worktree, env={})


def test_resolve_store_dir_fails_with_actionable_error(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()

    with pytest.raises(StoreResolutionError) as excinfo:
        resolve_store_dir(None, cwd=bare, env={})

    message = str(excinfo.value)
    assert "agentacct init" in message
    assert "--store-dir" in message
    assert ENV_STORE_DIR in message
    assert "~/.agent-sentinel" in message  # legacy pointer, no silent fallback


def test_resolver_is_pure_never_creates_dirs(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project")
    bare = tmp_path / "bare"
    bare.mkdir()
    before = _tree_snapshot(tmp_path)

    resolve_store_dir(None, cwd=project, env={})
    with pytest.raises(StoreResolutionError):
        resolve_store_dir(None, cwd=bare, env={})
    resolve_store_dir(tmp_path / "never-created", cwd=bare, env={})

    assert _tree_snapshot(tmp_path) == before


def test_worktree_owner_root_matches_only_exact_worktree_roots(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    worktree = owner / ".claude" / "worktrees" / "wt"

    assert worktree_owner_root(worktree) == owner
    assert worktree_owner_root(worktree / "sub") is None
    assert worktree_owner_root(owner) is None


def test_claude_worktree_owner_dir_matches_any_depth(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    deep = owner / ".claude" / "worktrees" / "wt" / "src" / "deep"
    deep.mkdir(parents=True)

    assert claude_worktree_owner_dir(deep) == owner.resolve()
    assert claude_worktree_owner_dir(owner) is None


def test_env_store_dir_expands_tilde(tmp_path: Path) -> None:
    resolution = resolve_store_dir(None, cwd=tmp_path, env={ENV_STORE_DIR: "~/sentinel-env-store"})

    assert resolution.path == Path(os.path.expanduser("~/sentinel-env-store"))
    assert resolution.source == "env"


# --- recognize-many global store (default-global / XDG) ----------------------


def _make_store(path: Path, *, data: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if data:
        (path / "events.jsonl").write_text('{"event_id": "x"}\n', encoding="utf-8")
    return path


def test_canonical_global_store_defaults_to_xdg_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert canonical_global_store_dir(env={}, home=home) == (
        home / ".local" / "state" / "agentacct" / "state"
    )


def test_canonical_global_store_honors_absolute_xdg_state_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg-state"
    assert canonical_global_store_dir(env={"XDG_STATE_HOME": str(xdg)}, home=home) == (
        xdg / "agentacct" / "state"
    )


def test_canonical_global_store_ignores_relative_xdg_state_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert canonical_global_store_dir(env={"XDG_STATE_HOME": "rel/state"}, home=home) == (
        home / ".local" / "state" / "agentacct" / "state"
    )


def test_recognized_global_store_dirs_order_and_dedup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = tmp_path / "override" / "state"
    dirs = recognized_global_store_dirs(env={ENV_GLOBAL_STORE_DIR: str(override)}, home=home)
    assert dirs == (
        override,
        home / ".local" / "state" / "agentacct" / "state",
        home / ".agent-sentinel-global" / "state",
    )


def test_recognized_global_store_dirs_skips_relative_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dirs = recognized_global_store_dirs(env={ENV_GLOBAL_STORE_DIR: "rel/state"}, home=home)
    assert dirs == (
        home / ".local" / "state" / "agentacct" / "state",
        home / ".agent-sentinel-global" / "state",
    )


def test_dashboard_new_user_uses_canonical_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    canonical = _make_store(home / ".local" / "state" / "agentacct" / "state")
    resolution = resolve_dashboard_store_dir(None, cwd=tmp_path, env={}, home=home)
    assert resolution.path == canonical
    assert resolution.source == "global"


def test_dashboard_existing_user_uses_legacy_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = _make_store(home / ".agent-sentinel-global" / "state")
    resolution = resolve_dashboard_store_dir(None, cwd=tmp_path, env={}, home=home)
    assert resolution.path == legacy
    assert resolution.source == "global"


def test_dashboard_migrating_user_prefers_populated_legacy_over_empty_canonical(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _make_store(home / ".local" / "state" / "agentacct" / "state", data=False)
    legacy = _make_store(home / ".agent-sentinel-global" / "state", data=True)
    resolution = resolve_dashboard_store_dir(None, cwd=tmp_path, env={}, home=home)
    # data-first: do not blank the dashboard just because a fresh store dir exists
    assert resolution.path == legacy


def test_dashboard_prefers_canonical_when_both_have_data(tmp_path: Path) -> None:
    home = tmp_path / "home"
    canonical = _make_store(home / ".local" / "state" / "agentacct" / "state", data=True)
    _make_store(home / ".agent-sentinel-global" / "state", data=True)
    resolution = resolve_dashboard_store_dir(None, cwd=tmp_path, env={}, home=home)
    assert resolution.path == canonical


def test_dashboard_operator_override_wins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = _make_store(tmp_path / "ops" / "state", data=True)
    _make_store(home / ".agent-sentinel-global" / "state", data=True)
    resolution = resolve_dashboard_store_dir(
        None, cwd=tmp_path, env={ENV_GLOBAL_STORE_DIR: str(override)}, home=home
    )
    assert resolution.path == override
    assert resolution.source == "global"


def test_dashboard_explicit_store_env_still_wins_over_global(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_store(home / ".local" / "state" / "agentacct" / "state", data=True)
    env_store = tmp_path / "env-store"
    resolution = resolve_dashboard_store_dir(
        None, cwd=tmp_path, env={ENV_STORE_DIR: str(env_store)}, home=home
    )
    assert resolution.path == env_store
    assert resolution.source == "env"


def test_dashboard_falls_through_to_strict_resolver_when_no_global(tmp_path: Path) -> None:
    home = tmp_path / "home"  # nothing created under home
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(StoreResolutionError):
        resolve_dashboard_store_dir(None, cwd=bare, env={}, home=home)


def test_is_recognized_global_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # legacy dot-global family: structural, location-independent
    assert is_recognized_global_store(
        tmp_path / "anywhere" / ".agent-sentinel-global" / "state", env={}, home=home
    )
    assert is_recognized_global_store(
        tmp_path / "x" / ".agent-chronicle-global" / "state", env={}, home=home
    )
    # new canonical location
    assert is_recognized_global_store(
        home / ".local" / "state" / "agentacct" / "state", env={}, home=home
    )
    # project store and arbitrary custom stores are NOT machine-wide
    assert not is_recognized_global_store(
        tmp_path / "proj" / ".agent-sentinel" / "state", env={}, home=home
    )
    assert not is_recognized_global_store(tmp_path / "random" / "state", env={}, home=home)
